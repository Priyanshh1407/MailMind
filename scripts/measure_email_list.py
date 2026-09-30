"""Measure GET /emails latency on synthetic saved mail. No private data or providers.

Usage: python -m scripts.measure_email_list [--emails 5000] [--repeats 40]
"""
import argparse
import json
import math
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient

from api.app import create_app
from src.config import Settings
from src.database import connection, initialize_database, utc_timestamp
from src.db_utils import log_email_to_db
from src.prediction import Prediction

ACCOUNT = 'bench@example.test'
ORIGIN = 'http://localhost:5173'
CATEGORIES = ('IMPORTANT', 'UPDATES', 'SPAM')


class EmptyCollection:
    def delete(self, **kwargs):
        return None


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def seed(db_path, count):
    """Every row gets the side tables the list endpoint joins per row."""
    stamp = utc_timestamp()
    with connection(db_path) as conn:
        for index in range(count):
            email_id = f'bench-{index:06d}'
            category = CATEGORIES[index % 3]
            log_email_to_db(
                email_id, f'Sender {index % 97} <s{index % 97}@example.test>',
                f'Synthetic subject {index}', f'Synthetic body {index} ' * 20,
                Prediction(category=category, outcome='CLASSIFIED', source='gemini',
                           model_version='bench-cloud'),
                Prediction(category=category, outcome='CLASSIFIED', source='local'),
                account_id=ACCOUNT, db_conn=conn)
            conn.execute(
                """INSERT INTO processing_tasks(account_id,email_id,status,stage,created_at,updated_at)
                   VALUES (?,?,'complete','complete',?,?)""", (ACCOUNT, email_id, stamp, stamp))
            if category == 'IMPORTANT':
                conn.execute(
                    """INSERT INTO notification_outbox(account_id,email_id,status,created_at,updated_at)
                       VALUES (?,?,'sent',?,?)""", (ACCOUNT, email_id, stamp, stamp))
            if index % 10 == 0:
                conn.execute(
                    'INSERT INTO feedback_history(account_id,email_id,label,created_at) VALUES (?,?,?,?)',
                    (ACCOUNT, email_id, category, stamp))


def measure(emails, repeats):
    with tempfile.TemporaryDirectory(prefix='mailmind-bench-') as directory:
        settings = Settings(data_dir=Path(directory))
        initialize_database(settings.db_path)
        seed(settings.db_path, emails)
        model = Mock(model_loaded=False, load_reason='missing_checkpoint')
        app = create_app(settings=settings, model_factory=Mock(return_value=model),
                         vector_factory=Mock(return_value=EmptyCollection()))
        results = {}
        with TestClient(app, base_url='http://localhost') as client:
            client.headers['Origin'] = ORIGIN
            client.headers['X-CSRF-Token'] = client.post('/session').json()['csrf_token']
            context, _ = app.state.accounts.session(client.cookies.get('mailmind_session'))
            app.state.accounts.finish_auth(app.state.accounts.begin_auth(context), (ACCOUNT, '{}'))
            cases = {f'limit_{limit}': f'/emails?limit={limit}' for limit in (20, 50, 200)}
            # Two characters use lexical search only; longer queries also try
            # semantic search, which falls back to lexical with no index here.
            cases['search_2_chars'] = '/emails?limit=20&search=ub'
            cases['search_body_term'] = '/emails?limit=20&search=body%204999'
            for name, path in cases.items():
                assert client.get(path).status_code == 200
                samples = []
                for _ in range(repeats):
                    started = time.perf_counter()
                    response = client.get(path)
                    samples.append((time.perf_counter() - started) * 1000)
                    assert response.status_code == 200
                results[name] = {
                    'p50_ms': round(percentile(samples, .5), 1),
                    'p95_ms': round(percentile(samples, .95), 1),
                    'rows': len(response.json()['emails']),
                }
        return {'saved_emails': emails, 'repeats': repeats, 'results': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--emails', type=int, default=5000)
    parser.add_argument('--repeats', type=int, default=40)
    arguments = parser.parse_args()
    print(json.dumps(measure(arguments.emails, arguments.repeats), indent=2))
