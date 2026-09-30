"""Seed a throwaway MailMind sandbox for API testing: no Gmail, cloud or Telegram.

Run it AFTER the sandbox API has started (API startup disconnects accounts):
  MAILMIND_DATA_DIR=<dir> python -m scripts.sandbox_seed --data-dir <dir>

It connects a fake account and loads synthetic CC0 benchmark emails with
predictions, explanations, actions and token events. It refuses the real
data directory.
"""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.account_state import AccountManager
from src.action_center import create_action
from src.config import ROOT, Settings
from src.database import connection, initialize_database
from src.db_utils import log_email_to_db
from src.email_analysis import save_analysis_result
from src.prediction import AnalysisSignal, EmailAnalysis, Prediction
from src.token_usage import record_token_usage

ACCOUNT = 'sandbox@example.test'
BENCHMARK = ROOT / 'fixtures' / 'priority_benchmark' / 'emails.json'
SIGNALS = {'IMPORTANT': 'direct_request', 'UPDATES': 'transactional_update',
           'SPAM': 'unsolicited_claim'}


def seed(data_dir, emails):
    data_dir = Path(data_dir).resolve()
    if data_dir == (ROOT / 'data').resolve():
        raise SystemExit('Refusing to seed the real data directory.')
    settings = Settings(data_dir=data_dir)
    initialize_database(settings.db_path)
    manager = AccountManager(settings)
    token, _csrf = manager.open_session()
    context, _ = manager.session(token)
    manager.finish_auth(manager.begin_auth(context), (ACCOUNT, '{}'))

    rows = json.loads(BENCHMARK.read_text(encoding='utf-8'))
    # Interleave categories so any page shows all three lanes.
    by_label = {label: [r for r in rows if r['human_label'] == label]
                for label in ('IMPORTANT', 'UPDATES', 'SPAM')}
    mixed = [row for group in zip(*by_label.values()) for row in group][:emails]
    now = datetime.now(timezone.utc)
    with connection(settings.db_path) as conn:
        for index, row in enumerate(mixed):
            email_id = 'sandbox-' + row['id']
            label = row['human_label']
            review = index in (4, 9)  # two emails end up in Needs Review
            decision = (Prediction(outcome='ERROR', source='gemini', model_version='sandbox-cloud',
                                   reason='provider_timeout') if review
                        else Prediction(category=label, outcome='CLASSIFIED', source='gemini',
                                        model_version='sandbox-cloud'))
            log_email_to_db(email_id, 'Sandbox Sender <sender@example.test>', row['subject'],
                            row['body'], decision,
                            Prediction(category=label, outcome='CLASSIFIED', source='local'),
                            account_id=ACCOUNT, db_conn=conn)
            if review:
                continue
            save_analysis_result(ACCOUNT, email_id, EmailAnalysis(
                predicted_category=label,
                explanation_summary=f'Synthetic sandbox reason for a {label.lower()} email.',
                signals=(AnalysisSignal(SIGNALS[label], None),), source='gemini',
                model_version='sandbox-cloud'), db_conn=conn)
            if label == 'IMPORTANT' and index < 15:
                create_action(ACCOUNT, email_id, action_type='reply_required',
                              title=row['subject'][:100], description='Sandbox follow-up.',
                              evidence=row['body'][:80], due_at=(now + timedelta(days=2)).isoformat(),
                              due_precision='exact_time', confidence='high',
                              extraction_source='gemini', db_conn=conn)
        for index, (provider, operation) in enumerate(
                [('gemini', 'classification_analysis')] * 3 + [('local', 'local_shadow')] * 3):
            record_token_usage(account_id=ACCOUNT, request_id=f'sandbox-{index}', provider=provider,
                               model_version='sandbox', operation=operation,
                               count_method='provider_reported' if provider == 'gemini' else 'tokenizer_counted',
                               outcome='success', input_tokens=120, output_tokens=40 if provider == 'gemini' else 0,
                               db_conn=conn)
    print(json.dumps({'account': ACCOUNT, 'emails': len(mixed), 'data_dir': str(data_dir)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--emails', type=int, default=40)
    arguments = parser.parse_args()
    seed(arguments.data_dir, arguments.emails)
