"""Synthetic local scaling measurements. Never loads private assets or real providers."""
import argparse
from contextlib import ExitStack
import json
import math
import platform
from pathlib import Path
import socket
import tempfile
import threading
import time
from unittest.mock import Mock, patch

import psutil
from fastapi.testclient import TestClient
from api.app import create_app
from src import main
from src.config import Settings
from src.database import connection
from src.email_client import FetchBatch
from src.notifier import Delivery
from src.prediction import Prediction
from tests.test_phase2_security import FakeCollection


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def scenario(messages=100, delay_ms=0, reader=True):
    if type(messages) is not int or not 1 <= messages <= 2000:
        raise ValueError('messages must be 1..2000')
    if not 0 <= delay_ms <= 100:
        raise ValueError('delay_ms must be 0..100')
    process = psutil.Process()
    baseline = process.memory_info().rss
    peak = [baseline]
    stopped = threading.Event()
    latencies = {'emails': [], 'telemetry': []}
    errors = []
    cycles = []
    provider_times = []
    with tempfile.TemporaryDirectory(prefix='mailmind-phase10-') as directory:
        settings = Settings(data_dir=Path(directory), auto_mark_read=True)
        collection = FakeCollection()
        class Model:
            model_loaded = False
            model_version = 'synthetic-scale-stub'
            training_scope = None
            def predict(self, subject, body, **kwargs):
                return Prediction(category=subject.split()[0], outcome='CLASSIFIED', source='synthetic-stub')
        model = Model()
        app = create_app(settings=settings, model_factory=lambda **kwargs: model,
                         vector_factory=lambda path: collection,
                         oauth_factory=Mock(return_value=('scale@example.test', '{}')))
        rows = [{'id': f'scale-{index:06d}', 'sender': 'synthetic@example.test',
                 'subject': ['IMPORTANT', 'UPDATES', 'SPAM'][index % 3] + ' synthetic example',
                 'body': 'Original synthetic text. No private inbox data. ' * 20}
                for index in range(messages)]
        def classify(sender, subject, body, **kwargs):
            started = time.perf_counter()
            time.sleep(delay_ms / 1000)
            provider_times.append(time.perf_counter() - started)
            return model.predict(subject, body)
        def sample(client):
            try:
                next_read = 0.0
                while not stopped.is_set():
                    peak[0] = max(peak[0], process.memory_info().rss)
                    if reader and time.monotonic() >= next_read:
                        for name, path in [('emails', '/emails?limit=50'), ('telemetry', '/telemetry')]:
                            started = time.perf_counter()
                            response = client.get(path)
                            if response.status_code != 200:
                                raise AssertionError('Dashboard read failed: ' + str(response.status_code))
                            latencies[name].append((time.perf_counter() - started) * 1000)
                        next_read = time.monotonic() + 5
                    stopped.wait(.05)
            except Exception as error:
                errors.append(type(error).__name__)
        with ExitStack() as stack:
            stack.enter_context(patch.object(socket, 'create_connection', side_effect=AssertionError('No external network')))
            stack.enter_context(patch.object(socket, 'getaddrinfo', side_effect=AssertionError('No external DNS')))
            client = stack.enter_context(TestClient(app, base_url='http://localhost'))
            client.headers['Origin'] = 'http://localhost:5173'
            session = client.post('/session')
            client.headers['X-CSRF-Token'] = session.json()['csrf_token']
            authenticated = client.post('/authenticate')
            assert authenticated.status_code == 202
            app.state.auth_futures[authenticated.json()['job_id']].result(timeout=5)
            manager = app.state.accounts
            fetch = Mock()
            alerts = Mock(return_value=Delivery('sent', message_id='synthetic-alert'))
            marker = Mock(return_value=True)
            for name, replacement in [('refresh_gmail', Mock(return_value=object())),
                                      ('get_unread_emails', fetch), ('classify_email', classify),
                                      ('send_telegram_alert', alerts), ('mark_as_read', marker)]:
                stack.enter_context(patch.object(main, name, replacement))
            def cycle(batch):
                fetch.return_value = FetchBatch(emails=batch)
                return main._run_agent(settings=settings, manager=manager, model=model, collection=collection)
            # Adopt the synthetic backlog through the real guarded ingestion path.
            # A stopped cycle saves bodies but deliberately claims no new tasks.
            stop_ingestion = threading.Event(); stop_ingestion.set()
            fetch.return_value = FetchBatch(emails=rows)
            main._run_agent(settings=settings, manager=manager, model=model, collection=collection, stop_event=stop_ingestion)
            sampler = threading.Thread(target=sample, args=(client,), daemon=True)
            cpu_before = process.cpu_times()
            started = time.perf_counter()
            sampler.start()
            remaining = messages
            try:
                while remaining:
                    cycle_start = time.perf_counter()
                    result = cycle([])
                    with connection(settings.db_path) as conn:
                        remaining = conn.execute("SELECT COUNT(*) FROM processing_tasks WHERE status!='complete'").fetchone()[0]
                    cycles.append({'seconds': time.perf_counter() - cycle_start,
                                   'completed': result['processed_count'], 'remaining': remaining})
                    if result['processed_count'] == 0:
                        raise AssertionError('Backlog did not progress')
            finally:
                seconds = time.perf_counter() - started
                stopped.set(); sampler.join(timeout=10)
            if sampler.is_alive() or errors:
                raise AssertionError('Concurrent dashboard sampling failed: ' + str(errors))
            cpu_after = process.cpu_times()
            before = (len(provider_times), alerts.call_count, marker.call_count)
            replay = cycle(rows[:settings.batch_size])
            assert replay['processed_count'] == 0
            assert before == (len(provider_times), alerts.call_count, marker.call_count)
            with connection(settings.db_path) as conn:
                saved = conn.execute('SELECT COUNT(*) FROM email_logs').fetchone()[0]
                completed = conn.execute("SELECT COUNT(*) FROM processing_tasks WHERE status='complete'").fetchone()[0]
                predictions = conn.execute('SELECT COUNT(*) FROM prediction_attempts').fetchone()[0]
            assert saved == completed == predictions == messages
            assert marker.call_count == messages
            assert alerts.call_count == (messages + 2) // 3
            storage = sum(p.stat().st_size for p in Path(directory).glob('email_logs.db*'))
            result = {'messages': messages, 'fake_classification_delay_ms': delay_ms,
                      'dashboard_poll_interval_seconds': 5, 'dashboard_page_limit': 50,
                      'concurrent_dashboard_reader': reader, 'batch_size': settings.batch_size,
                      'cycles': cycles, 'drain_seconds_without_poll_wait': seconds,
                      'synthetic_messages_per_second': messages / seconds,
                      'scheduled_drain_estimate_seconds': seconds + (len(cycles) - 1) * settings.poll_interval_seconds,
                      'schedule_estimate_note': f'Existing backlog, first cycle immediate; adds {settings.poll_interval_seconds}s between cycles. Excludes ingestion, cold assets, retries and live providers.',
                      'classification_adapter_seconds': sum(provider_times),
                      'cpu_seconds': cpu_after.user + cpu_after.system - cpu_before.user - cpu_before.system,
                      'rss_baseline_bytes': baseline, 'rss_peak_sampled_bytes': peak[0],
                      'rss_growth_sampled_bytes': max(0, peak[0] - baseline),
                      'sqlite_files_bytes': storage,
                      'api': {name: {'samples': len(values), 'p50_ms': percentile(values, .5),
                                     'p95_ms': percentile(values, .95), 'max_ms': max(values) if values else None}
                              for name, values in latencies.items()},
                      'saved': saved, 'completed': completed, 'prediction_attempts': predictions,
                      'duplicate_replay': 'no duplicate rows, classifications, notifications or marks',
                      'all_passed': True}
    return result


def measure(report):
    scenarios = []
    for size, delay, reader in [(100, 0, False), (100, 0, True), (500, 0, True), (100, 10, True)]:
        print(f'Measuring {size} messages, fake delay {delay}ms, dashboard reader {reader}', flush=True)
        scenarios.append(scenario(size, delay, reader))
    result = {'scope': 'Single process, real API/SQLite/worker, synthetic providers/model/vector collection; no real inference, provider quota or production capacity claim',
              'platform': platform.system(), 'python': platform.python_version(),
              'logical_cpus': psutil.cpu_count(), 'memory_total_bytes': psutil.virtual_memory().total,
              'rss_note': 'Entire process including API/testing imports; sampled about every 50ms plus reader latency, not a hard memory bound. Scenarios run sequentially; allocator/cache warmth differs.',
              'ingestion_note': 'All synthetic bodies are pre-adopted using a stopped real worker cycle to isolate saved-backlog processing. This intentionally exceeds a live Gmail page; Gmail download throughput is not measured.',
              'run_note': 'Run sequentially after the backend suite; other host activity is uncontrolled. Small API sample counts must not be treated as an SLA.',
              'latency_note': 'In-process TestClient; includes routing/serialization/SQLite, excludes HTTP transport/browser rendering. API subset only: emails at limit50 and telemetry. UI uses limit20 plus session/status. Poll cadence matches UI, request mix does not. No frontend load test.',
              'scenarios': scenarios, 'all_passed': all(s['all_passed'] for s in scenarios)}
    path = Path(report); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', default='docs/evaluation/phase10/local_scale.json')
    args = parser.parse_args()
    measure(args.report)
