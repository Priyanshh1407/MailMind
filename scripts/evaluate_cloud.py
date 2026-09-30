"""UPG-01: evaluate the classification routes on the CC0 synthetic benchmark.

Record once, replay forever:
  --plan     print the routes, model IDs and request counts; makes no calls.
  --record   send each benchmark email to each route (live API calls; uses keys
             from the environment/.env) and store the RAW answers, latency and
             token counts in fixtures/recorded_cloud/<route>.jsonl.
  --replay   score the recordings offline through the production parser
             (llm_api.parse_provider_analysis) and write docs/EVALUATION.md.

Requests are byte-identical to production: the same payload builder, system
instruction, schema and request config. Only synthetic benchmark mail is sent.
"""
import argparse
import json
import random
import statistics
import threading
import time
from pathlib import Path

from src import llm_api
from src.config import Settings
from src.evaluation import classification_metrics
from src.token_usage import gemini_usage_measurement, groq_usage_measurement

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / 'fixtures' / 'priority_benchmark' / 'emails.json'
RECORDINGS = ROOT / 'fixtures' / 'recorded_cloud'
REPORT = ROOT / 'docs' / 'EVALUATION.md'
# Relative deadlines are resolved against a fixed "received" time, never now.
RECEIVED_AT = '2026-09-28T09:00:00+00:00'
CHAIN = ('gemini_primary', 'gemini_fallback', 'groq')
BOOTSTRAP_SAMPLES = 2000


def load_benchmark():
    return json.loads(BENCHMARK.read_text(encoding='utf-8'))


def routes(settings):
    models = list(settings.gemini_models)
    found = {'gemini_primary': ('gemini', models[0])}
    if len(models) > 1:
        found['gemini_fallback'] = ('gemini', models[1])
    found['groq'] = ('groq', settings.groq_model)
    return found


def request_text(row):
    contents, _ = llm_api.build_classification_payload(
        '[benchmark]', row['subject'], row['body'], [], source_timestamp=RECEIVED_AT)
    email = json.loads(contents)['email']
    return contents, email['sender'] + '\n' + email['text']


# ---------------------------------------------------------------- recording

class ProviderHTTPError(Exception):
    """An HTTP failure with the provider's short error code (never content)."""
    def __init__(self, status_code, detail=None):
        super().__init__(f'HTTP {status_code} {detail or ""}'.strip())
        self.status_code = status_code
        self.detail = detail if isinstance(detail, str) and len(detail) <= 64 else None


# Server-side trouble says nothing about model quality, so recording retries
# it; everything else (4xx, bad answers) is recorded as the route's result.
TRANSIENT = {'provider_quota', 'provider_transient', 'provider_timeout',
             'provider_connect_timeout', 'provider_connection'}
BREAKER_ERRORS = 5


class Throttle:
    def __init__(self, per_minute):
        self.interval = 60.0 / per_minute
        self.next_at = 0.0

    def wait(self):
        delay = self.next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.next_at = max(self.next_at, time.monotonic()) + self.interval


def call_route(provider, model, contents, *, client, groq_key):
    """One production-identical request; returns (text, input_tokens, output_tokens)."""
    if provider == 'gemini':
        response = client.models.generate_content(
            model=model, contents=contents, config=llm_api.gemini_request_config(model))
        usage = gemini_usage_measurement(response)
        return response.text, usage.input_tokens, usage.output_tokens
    import requests
    response = requests.post(llm_api.GROQ_URL, headers={'Authorization': f'Bearer {groq_key}'},
                             timeout=(15, 30), json=llm_api.groq_request_body(model, contents))
    if response.status_code >= 400:
        try:
            detail = response.json()['error'].get('code')
        except (ValueError, KeyError, TypeError, AttributeError):
            detail = None
        raise ProviderHTTPError(response.status_code, detail)
    payload = response.json()
    usage = groq_usage_measurement(payload)
    return payload['choices'][0]['message']['content'], usage.input_tokens, usage.output_tokens


def record_route(name, provider, model, rows, *, per_minute, client, groq_key, lock, progress):
    """Record every row for one route; resumable (skips rows already recorded)."""
    path = RECORDINGS / f'{name}.jsonl'
    done = set()
    if path.exists():
        done = {json.loads(line)['id'] for line in path.read_text(encoding='utf-8').splitlines()
                if line.strip()}
    throttle = Throttle(per_minute)
    consecutive_errors = 0
    with path.open('a', encoding='utf-8') as output:
        for row in rows:
            if row['id'] in done:
                continue
            if consecutive_errors >= BREAKER_ERRORS:
                # A route that keeps failing (e.g. an exhausted daily quota)
                # would only burn requests; stop it and resume later.
                print(f'{name}: stopped after {BREAKER_ERRORS} consecutive errors; '
                      'rerun --record later to resume', flush=True)
                return
            contents, _ = request_text(row)
            record = {'id': row['id'], 'route': name, 'provider': provider, 'model': model}
            for attempt in range(4):
                throttle.wait()
                started = time.perf_counter()
                try:
                    text, input_tokens, output_tokens = call_route(
                        provider, model, contents, client=client, groq_key=groq_key)
                    record.update(status='ok', text=text, input_tokens=input_tokens,
                                  output_tokens=output_tokens, error_code=None,
                                  provider_detail=None)
                    break
                except Exception as error:
                    failure = llm_api.provider_failure(error)
                    record.update(status='error', error_code=failure.code, text=None,
                                  input_tokens=None, output_tokens=None,
                                  provider_detail=getattr(error, 'detail', None))
                    if failure.code not in TRANSIENT:
                        break
                    # Evaluation only: back off on transient trouble instead of scoring it.
                    time.sleep(min(60, 10 * 2 ** attempt))
                finally:
                    record['latency_ms'] = round((time.perf_counter() - started) * 1000, 1)
            record['attempts'] = attempt + 1
            if record['status'] == 'error' and record['error_code'] in TRANSIENT:
                # Transient trouble that survived every retry is not the route's
                # answer; leave the row unrecorded so a later run retries it.
                consecutive_errors += 1
                continue
            consecutive_errors = 0 if record['status'] == 'ok' else consecutive_errors + 1
            output.write(json.dumps(record, ensure_ascii=False) + '\n')
            output.flush()
            with lock:
                progress[name] = progress.get(name, 0) + 1
                print(f"{name}: {progress[name]} recorded ({record['status']})", flush=True)


def record(settings, rows, selected, per_minute):
    if settings.local_only:
        raise SystemExit('Live recording is disabled in local-only mode.')
    import os
    RECORDINGS.mkdir(parents=True, exist_ok=True)
    available = routes(settings)
    client = llm_api.create_cloud_client()
    groq_key = os.getenv('GROQ_API_KEY')
    lock, progress, threads = threading.Lock(), {}, []
    for name in selected:
        provider, model = available[name]
        if provider == 'groq' and not groq_key:
            print('groq: GROQ_API_KEY is not configured; skipped')
            continue
        thread = threading.Thread(target=record_route, args=(name, provider, model, rows), kwargs={
            'per_minute': per_minute[provider], 'client': client, 'groq_key': groq_key,
            'lock': lock, 'progress': progress})
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()


# ------------------------------------------------------------------ scoring

def parse(entry, row):
    """Score one recorded answer exactly as production would parse it."""
    if entry is None or entry['status'] != 'ok':
        return None, None
    _, evidence_source = request_text(row)
    try:
        category, analysis = llm_api.parse_provider_analysis(
            entry['text'], source='gemini' if entry['provider'] == 'gemini' else 'groq',
            model_version=entry['model'], source_text=evidence_source)
    except (ValueError, TypeError, KeyError):
        return None, None
    return category, analysis


def load_recordings(name):
    path = RECORDINGS / f'{name}.jsonl'
    if not path.exists():
        return None
    entries = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.strip():
            entry = json.loads(line)
            entries[entry['id']] = entry
    return entries


def raw_action_count(entry):
    try:
        return len(json.loads(entry['text']).get('actions', []))
    except (TypeError, ValueError, AttributeError):
        return 0


def macro_f1_interval(rows, predicted):
    """95% bootstrap interval, resampling whole scenario groups."""
    by_group = {}
    for row, guess in zip(rows, predicted):
        by_group.setdefault(row['group_id'], []).append((row['human_label'], guess))
    groups = list(by_group.values())
    generator = random.Random(42)
    scores = []
    for _ in range(BOOTSTRAP_SAMPLES):
        sample = [pair for _ in groups for pair in generator.choice(groups)]
        scores.append(classification_metrics([t for t, _ in sample], [g for _, g in sample])['macro_f1'])
    scores.sort()
    return scores[int(.025 * len(scores))], scores[int(.975 * len(scores)) - 1]


def score(rows, predictions, entries=None, timeout_ms=None):
    truth = [row['human_label'] for row in rows]
    metrics = classification_metrics(truth, predictions)
    low, high = macro_f1_interval(rows, predictions)
    result = {
        'emails': len(rows),
        'coverage': metrics['coverage'],
        'accuracy': metrics['accuracy_including_abstention'],
        'macro_f1': metrics['macro_f1'],
        'macro_f1_ci95': (low, high),
        'important_recall': metrics['per_class']['IMPORTANT']['recall'],
        'urgent_misses': metrics['urgent_false_negatives'],
        'confusion': metrics['confusion_matrix'],
    }
    if entries is not None:
        recorded = [entries.get(row['id']) for row in rows]
        ok = [entry for entry in recorded if entry and entry['status'] == 'ok']
        latencies = [entry['latency_ms'] for entry in ok]
        result.update(
            errors=sum(1 for entry in recorded if entry is None or entry['status'] != 'ok'),
            invalid_output=sum(1 for entry, guess in zip(recorded, predictions)
                               if entry and entry['status'] == 'ok' and guess is None),
            latency_p50_ms=statistics.median(latencies) if latencies else None,
            over_production_timeout=(sum(1 for latency in latencies if latency > timeout_ms)
                                     if timeout_ms else None),
            latency_p95_ms=(sorted(latencies)[max(0, int(.95 * len(latencies)) - 1)]
                            if latencies else None),
            input_tokens_per_email=(statistics.mean(entry['input_tokens'] for entry in ok
                                                    if entry['input_tokens'] is not None)
                                    if any(entry['input_tokens'] is not None for entry in ok)
                                    else None),
            output_tokens_per_email=(statistics.mean(entry['output_tokens'] for entry in ok
                                                     if entry['output_tokens'] is not None)
                                     if any(entry['output_tokens'] is not None for entry in ok)
                                     else None),
        )
    return result


def replay(settings, rows):
    results, models, action_stats = {}, {}, {}
    per_route = {}
    for name, (_provider, model) in routes(settings).items():
        entries = load_recordings(name)
        if not entries:
            continue
        models[name] = next(iter(entries.values()))['model']
        parsed = [parse(entries.get(row['id']), row) for row in rows]
        per_route[name] = (entries, parsed)
        results[name] = score(rows, [category for category, _ in parsed], entries,
                              timeout_ms=settings.provider_timeout_seconds * 1000)
        proposed = sum(raw_action_count(entry) for entry in entries.values()
                       if entry['status'] == 'ok')
        kept = sum(len(analysis.actions) for _, analysis in parsed if analysis is not None)
        action_stats[name] = {'proposed': proposed, 'passed_validation': kept}
    chain = [name for name in CHAIN if name in per_route]
    if len(chain) > 1:
        decided, route_used = [], []
        for index in range(len(rows)):
            category = None
            for name in chain:
                category = per_route[name][1][index][0]
                if category is not None:
                    route_used.append(name)
                    break
            else:
                route_used.append(None)
            decided.append(category)
        results['failover_chain'] = score(rows, decided)
        results['failover_chain']['answered_by'] = {
            name: route_used.count(name) for name in chain}
    local = RECORDINGS / 'local_shadow.jsonl'
    if local.exists():
        entries = {json.loads(line)['id']: json.loads(line)
                   for line in local.read_text(encoding='utf-8').splitlines() if line.strip()}
        models['local_shadow'] = next(iter(entries.values()))['model']
        results['local_shadow'] = score(rows, [entries[row['id']]['category'] for row in rows])
    return results, models, action_stats


def record_local(settings, rows):
    """Run the local shadow checkpoint once on the benchmark (no network)."""
    from src.local_llm import MailMindModel, predict_with_token_count
    model = MailMindModel(settings.shadow_model_path, settings=settings)
    if not model.model_loaded:
        raise SystemExit(f'Local model unavailable: {model.load_reason}')
    RECORDINGS.mkdir(parents=True, exist_ok=True)
    with (RECORDINGS / 'local_shadow.jsonl').open('w', encoding='utf-8') as output:
        for row in rows:
            prediction, _tokens = predict_with_token_count(model, row['subject'], row['body'])
            output.write(json.dumps({'id': row['id'], 'model': model.model_version,
                                     'category': prediction.category,
                                     'outcome': prediction.outcome}) + '\n')
    print(f'local_shadow: recorded {len(rows)} predictions with {model.model_version}')


# ------------------------------------------------------------------- report

def percent(value):
    return '—' if value is None else f'{value * 100:.1f}%'


def number(value, digits=0):
    return '—' if value is None else f'{value:.{digits}f}'


def write_report(results, models, action_stats, rows):
    labels = {'gemini_primary': 'Gemini primary', 'gemini_fallback': 'Gemini fallback',
              'groq': 'Groq', 'failover_chain': 'Production failover chain',
              'local_shadow': 'Local shadow (DistilBERT)'}
    lines = [
        '# Classifier evaluation',
        '',
        'Generated by `python -m scripts.evaluate_cloud --replay` from recorded raw provider',
        'answers in `fixtures/recorded_cloud/`. Replaying scores the recordings through the',
        'production parser; it makes no network calls and reproduces this file exactly.',
        '',
        f'**Dataset:** the CC0 synthetic priority benchmark: {len(rows)} author-labelled emails',
        '(60 per category) in 60 scenario groups of three variants. It is small, English-only and',
        'synthetic, and all labels come from one author. Treat these numbers as a controlled comparison',
        'between routes, not as real-inbox accuracy.',
        '',
        '**Method:** one production-identical request per email and route (same payload builder,',
        'system instruction, JSON schema and config; relative dates resolved against a fixed',
        'received time). An answer that fails production validation counts as "no answer".',
        'The 95% interval for macro-F1 comes from a bootstrap that resamples whole scenario groups',
        f'({BOOTSTRAP_SAMPLES} samples). The failover chain replays the recorded answers in production',
        'order (Gemini primary → Gemini fallback → Groq); the first valid answer wins.',
        '',
        '| Route | Model | Answered | Accuracy | Macro-F1 (95% CI) | IMPORTANT recall | Urgent misses |',
        '| --- | --- | ---: | ---: | --- | ---: | ---: |',
    ]
    for name in ('gemini_primary', 'gemini_fallback', 'groq', 'failover_chain', 'local_shadow'):
        if name not in results:
            continue
        result = results[name]
        low, high = result['macro_f1_ci95']
        model = models.get(name, 'recorded answers of the routes above')
        lines.append(
            f"| {labels[name]} | `{model}` | {percent(result['coverage'])} | "
            f"{percent(result['accuracy'])} | {result['macro_f1']:.3f} ({low:.3f}–{high:.3f}) | "
            f"{percent(result['important_recall'])} | {result['urgent_misses']} |")
    lines += ['', '## Operational cost per route', '',
              '| Route | Errors | Invalid answers | Latency p50 | Latency p95 | '
              'Slower than production timeout | Input tokens / email | Output tokens / email |',
              '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for name in ('gemini_primary', 'gemini_fallback', 'groq'):
        if name not in results:
            continue
        result = results[name]
        lines.append(
            f"| {labels[name]} | {result['errors']} | {result['invalid_output']} | "
            f"{number(result['latency_p50_ms'])} ms | {number(result['latency_p95_ms'])} ms | "
            f"{result['over_production_timeout']} | "
            f"{number(result['input_tokens_per_email'])} | {number(result['output_tokens_per_email'])} |")
    lines += ['', 'Latency is measured per successful request and excludes rate-limit backoff.',
              '"Slower than production timeout" counts answers that took longer than the',
              "worker's per-provider timeout; in production those requests would time out and",
              'fall through to the next route.',
              'Token counts are the providers\' reported usage. No price table is applied; cost',
              'depends on the account tier.', '']
    if action_stats:
        lines += ['## Action extraction', '',
                  '| Route | Actions proposed | Passed validation (schema, safety, grounding) |',
                  '| --- | ---: | ---: |']
        for name, stats in action_stats.items():
            lines.append(f"| {labels[name]} | {stats['proposed']} | {stats['passed_validation']} |")
        lines += ['', 'There is no hand-labelled action ground truth, so this measures how many',
                  'proposed actions survive validation, not whether they are correct.', '']
    if 'failover_chain' in results:
        answered = results['failover_chain']['answered_by']
        lines += ['## Failover chain', '',
                  'Answers taken from each route: ' + ', '.join(
                      f'{labels[name]} {count}' for name, count in answered.items()) + '.', '']
    saturated = [labels[name] for name in ('gemini_primary', 'gemini_fallback', 'groq')
                 if name in results and results[name]['accuracy'] == 1.0]
    if saturated:
        lines += ['## Interpreting a perfect score', '',
                  f"{', '.join(saturated)} answered every benchmark email correctly. That is a",
                  'ceiling effect: the scenarios are deliberately unambiguous, so this benchmark',
                  'cannot rank strong models, and a perfect score is not evidence of real-inbox',
                  'accuracy. Separating strong routes needs a harder, independently labelled set.',
                  'The labels are never sent to the provider (the request carries only subject and',
                  'body; `tests/test_cloud_evaluation.py` checks the payload).', '']
    missing = [name for name in CHAIN if name not in results]
    notes = RECORDINGS / 'NOTES.md'
    if missing or notes.exists():
        lines += ['## Routes not evaluated', '']
        lines += [f'- {labels[name]}: no complete recording.' for name in missing]
        if notes.exists():
            lines += ['', notes.read_text(encoding='utf-8').strip()]
        lines.append('')
    lines += ['## Confusion matrices', '']
    for name, result in results.items():
        matrix = result['confusion']
        lines += [f'**{labels[name]}** (rows = truth)', '',
                  '| | ' + ' | '.join(matrix['columns']) + ' |',
                  '| --- |' + ' ---: |' * len(matrix['columns'])]
        for label, values in zip(matrix['rows'], matrix['values']):
            lines.append(f'| {label} | ' + ' | '.join(str(value) for value in values) + ' |')
        lines.append('')
    if 'local_shadow' in results:
        lines += ['## Local shadow model caveat', '',
                  'The local checkpoint was trained on a private mailbox whose labels are mostly',
                  "Gemini's own decisions (see the training report's label provenance), so it is",
                  'effectively a distillation of the cloud classifier. On this benchmark it is',
                  'out-of-distribution; it runs as a shadow and never decides in normal mode.', '']
    REPORT.write_text('\n'.join(lines), encoding='utf-8')
    print(f'wrote {REPORT}')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--record', action='store_true')
    mode.add_argument('--record-local', action='store_true')
    mode.add_argument('--replay', action='store_true')
    parser.add_argument('--routes', default=','.join(CHAIN))
    parser.add_argument('--gemini-models', default=None,
                        help='comma-separated Gemini primary,fallback (overrides the environment)')
    parser.add_argument('--limit', type=int, default=None, help='first N benchmark emails only')
    parser.add_argument('--gemini-rpm', type=int, default=10)
    parser.add_argument('--groq-rpm', type=int, default=25)
    arguments = parser.parse_args()
    settings = Settings.from_environment(load_file=True)
    if arguments.gemini_models:
        from dataclasses import replace
        settings = replace(settings, gemini_models=tuple(
            value.strip() for value in arguments.gemini_models.split(',') if value.strip()))
    rows = load_benchmark()[:arguments.limit] if arguments.limit else load_benchmark()
    available = routes(settings)
    selected = [name for name in arguments.routes.split(',') if name in available]
    if arguments.plan:
        for name in selected:
            provider, model = available[name]
            print(f'{name:16} {provider:6} {model:32} {len(rows)} requests')
        print(f'total: {len(rows) * len(selected)} requests (plus retries of transient server errors only)')
    elif arguments.record:
        record(settings, rows, selected,
               {'gemini': arguments.gemini_rpm, 'groq': arguments.groq_rpm})
    elif arguments.record_local:
        record_local(settings, rows)
    else:
        results, models, action_stats = replay(settings, rows)
        if not results:
            raise SystemExit('No recordings found; run --record first.')
        write_report(results, models, action_stats, rows)
        print(json.dumps({name: {key: value for key, value in result.items() if key != 'confusion'}
                          for name, result in results.items()}, indent=2, default=list))


if __name__ == '__main__':
    main()
