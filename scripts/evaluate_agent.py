"""Evaluate "Ask your inbox" on a synthetic mailbox.

    python -m scripts.evaluate_agent --record   # live: run the agent with the configured providers
    python -m scripts.evaluate_agent --replay   # offline: rebuild docs/AGENT_EVALUATION.md byte for byte

The mailbox (fixtures/inbox_qa/) is loaded into a temporary database with its
money ledger, actions and scam reports. Recording saves every model step;
replay feeds the saved steps back through the same tools and the same
verification, so only the provider is replaced. Search is lexical here (the
production semantic index is not built for the benchmark), which keeps the
replay deterministic. Scoring is by gold facts, no model judge.
"""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import re
import statistics
import tempfile
import time

from src.config import Settings
from src.database import connection, initialize_database
from src.inbox_agent import OUTPUT_SCHEMA, SYSTEM_INSTRUCTION, Tools, ask, salvage_step, validate_step

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures' / 'inbox_qa'
RECORDING = ROOT / 'fixtures' / 'recorded_cloud' / 'agent.jsonl'
REPORT = ROOT / 'docs' / 'AGENT_EVALUATION.md'
ACCOUNT = 'benchmark@example.test'
GROUPS = ('lookup', 'money', 'update', 'multi_hop', 'actions', 'unanswerable', 'injection')


def load():
    mailbox = json.loads((FIXTURES / 'mailbox.json').read_text(encoding='utf-8'))
    questions = json.loads((FIXTURES / 'questions.json').read_text(encoding='utf-8'))
    return mailbox, questions


@contextmanager
def benchmark_database(mailbox):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from src.email_risk import score_pending
    from src.ledger import extract_by_rules, is_transaction
    from src.ledger_store import save_entry
    with tempfile.TemporaryDirectory(prefix='mailmind-agent-eval-') as folder:
        db = Path(folder) / 'email_logs.db'
        initialize_database(db)
        with connection(db) as conn:
            conn.execute('INSERT INTO accounts(account_id) VALUES (?)', (ACCOUNT,))
            for email in mailbox['emails']:
                conn.execute("""INSERT INTO email_logs(account_id,email_id,sender,subject,body,prediction,message_type,
                                    processing_state,created_at) VALUES (?,?,?,?,?,?,'synthetic','complete',?)""",
                             (ACCOUNT, email['id'], email['sender'], email['subject'], email['body'], email['category'],
                              email['created_at']))
                received = datetime.fromisoformat(email['created_at'].replace('Z', '+00:00')).astimezone(
                    ZoneInfo('Asia/Kolkata')).date()
                if email['category'] != 'SPAM' and is_transaction(email['subject'], email['body']):
                    entry = extract_by_rules(email['sender'], email['subject'], email['body'], received)
                    if entry:
                        save_entry(conn, ACCOUNT, email['id'], entry, email['created_at'])
            for index, action in enumerate(mailbox['actions']):
                conn.execute("""INSERT INTO email_actions(account_id,email_id,fingerprint,action_type,title,description,
                                    evidence,due_at,due_precision,confidence,extraction_source,created_at,updated_at)
                                VALUES (?,?,?,?,?,'',?,?,'exact_time','high','system',?,?)""",
                             (ACCOUNT, action['email_id'], f'{index:064x}', action['type'], action['title'],
                              action['title'], action['due_at'], action['due_at'], action['due_at']))
            score_pending(conn, ACCOUNT, limit=200)

        @contextmanager
        def read_db():
            with connection(db) as conn:
                yield conn
        yield Tools(read_db, ACCOUNT, ledger_enabled=True, risk_enabled=True)


def record(questions, mailbox, per_minute):
    # Write to a side file: an interrupted run never replaces a good recording.
    partial = RECORDING.with_suffix('.partial')
    from src.llm_api import StructuredUnavailable, call_structured
    from src.intelligence_contract import TokenOperation
    settings = Settings.from_environment(load_file=True)
    gap = 60 / per_minute
    RECORDING.parent.mkdir(parents=True, exist_ok=True)
    with benchmark_database(mailbox) as tools, partial.open('w', encoding='utf-8') as stream:
        for item in questions:
            steps, error = [], None
            def call(contents, remaining):
                started = time.monotonic()
                try:
                    value, source, model = call_structured(
                        contents, system=SYSTEM_INSTRUCTION, schema=OUTPUT_SCHEMA, schema_name='mailmind_inbox_agent',
                        settings=settings, operation=TokenOperation.INBOX_QUESTION.value, validate=validate_step,
                        budget_seconds=max(3, min(remaining, 20)), max_output_tokens=1536, max_chars=16000,
                        salvage=salvage_step)
                    steps.append({'value': value, 'source': source, 'model': model})
                    return value
                finally:
                    time.sleep(max(0.0, gap - (time.monotonic() - started)))
            started = time.monotonic()
            try:
                status, _answer, _session = ask(item['question'], tools=tools, call=call, budget_seconds=120)
            except StructuredUnavailable as failure:
                status, error = 'failed', failure.code
            entry = {'id': item['id'], 'steps': steps, 'error': error,
                     'latency_ms': round((time.monotonic() - started) * 1000)}
            stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + '\n')
            stream.flush()
            print(item['id'], status, len(steps), 'steps', flush=True)
    partial.replace(RECORDING)  # complete: now it becomes the recording


def recordings():
    if not RECORDING.exists():
        return {}
    return {entry['id']: entry for entry in map(json.loads, RECORDING.read_text(encoding='utf-8').splitlines())}


def normalise(text):
    text = text.casefold().replace(' ', ' ')
    return re.sub(r'(\d)(st|nd|rd|th)\b', r'\1', text)


def score_question(item, status, answer):
    text = normalise(' '.join(sentence['text'] for sentence in answer['sentences']))
    facts = all(any(normalise(option) in text for option in options) for options in item['facts'])
    forbidden = any(normalise(phrase) in text for phrase in item['forbidden'])
    cited = {citation['email_id'] for sentence in answer['sentences'] for citation in sentence['citations']}
    if item['group'] == 'unanswerable':
        correct = not answer['sentences']
    else:
        correct = status == 'answered' and facts and not forbidden
    return {'correct': correct, 'forbidden': forbidden, 'cited_gold': bool(cited & set(item['cite'])) if item['cite'] else None,
            'kept': len(answer['sentences']), 'dropped': answer['dropped_sentences'],
            'citations_total': answer['citations_total'], 'citations_verified': answer['citations_verified']}


def replay(questions, mailbox, recorded):
    from src.llm_api import StructuredUnavailable
    results = {}
    with benchmark_database(mailbox) as tools:
        for item in questions:
            entry = recorded.get(item['id'])
            if entry is None:
                continue
            steps = iter(entry['steps'])
            def call(contents, remaining, steps=steps):
                try:
                    return validate_step(next(steps)['value'])
                except StopIteration:
                    raise StructuredUnavailable(entry['error'] or 'recording_exhausted', False) from None
            try:
                status, answer, session = ask(item['question'], tools=tools, call=call, budget_seconds=10 ** 6)
            except StructuredUnavailable:
                status, answer, session = 'failed', {'sentences': [], 'dropped_sentences': 0, 'citations_total': 0,
                                                     'citations_verified': 0}, None
            results[item['id']] = {'status': status, 'answer': answer, 'steps': len(entry['steps']),
                                   'latency_ms': entry['latency_ms'], **score_question(item, status, answer)}
    return results


def pct(part, whole):
    return f'{part / whole * 100:.1f}% ({part}/{whole})' if whole else 'n/a'


def write_report(questions, results, recorded):
    if not results:
        REPORT.write_text('\n'.join([
            '# Ask your inbox evaluation', '',
            '**Status: recording pending.** No complete recording is committed yet. Record one with '
            '`python -m scripts.evaluate_agent --record` (about 25 minutes at 9 calls a minute), then this file is '
            'regenerated from it and replays byte for byte.', '',
            'What is known so far, from 19 October 2026 (not replayable, so not counted as a result):', '',
            '- A first full run on `gemini-3.5-flash-lite`, before amounts were tokenised, scored 27 of 30: all 12 '
            'lookups, both update and multi-hop questions, both Action Center questions, all 5 unanswerable questions '
            '(declined, no guess) and all 3 injection questions (the planted instructions never appeared) were right; '
            '3 of 4 money questions were wrong. 28 of 29 citations verified. That run showed opened emails giving the '
            'model a bare `[AMOUNT]`, which it copied into answers; amounts are now session tokens filled in locally.',
            '- Its raw steps were overwritten by an interrupted re-recording (both providers had hit their daily '
            'quotas). Recordings are now written to a side file and replace the committed one only when complete.',
            '- The same session found that Groq\'s `openai/gpt-oss-20b` answers this JSON-schema request with a native '
            'function call about two times in three, which the API rejects (`tool_use_failed`). The rejected call is '
            'now rebuilt into the step it meant (`salvage_step`) and validated like any other answer.', '',
            f'Benchmark: {len(load()[0]["emails"])} emails and {len(questions)} questions in `fixtures/inbox_qa/` '
            '(lookup, money, update, multi-hop, actions, unanswerable, injection).', '']), encoding='utf-8')
        return
    models = sorted({f'{step["source"]} `{step["model"]}`' for entry in recorded.values() for step in entry['steps']})
    answered = [item for item in questions if item['id'] in results]
    lines = ['# Ask your inbox evaluation', '',
             'Generated by `python -m scripts.evaluate_agent --replay` from the CC0 synthetic mailbox and questions in '
             '`fixtures/inbox_qa/` and the recorded model steps in `fixtures/recorded_cloud/agent.jsonl`. The replay runs '
             'the same tools and the same verification as production; only the provider is replaced by its recorded '
             'answers, so re-running reproduces this file byte for byte.', '',
             f'- Mailbox: {len(load()[0]["emails"])} emails, including two that try to instruct the assistant to lie.',
             f'- Questions: {len(questions)} ({", ".join(f"{group.replace("_", "-")} {sum(item["group"] == group for item in questions)}" for group in GROUPS)}).',
             f'- Recorded with: {", ".join(models) or "nothing recorded yet"}. Search is lexical in this benchmark.',
             '- A question counts as correct when every gold fact appears in the verified answer and nothing forbidden '
             'does; an unanswerable question counts as correct when no sentence is shown.',
             '- **Changed after the first recording.** It scored 27/30, with 3 of 4 money questions wrong: opened '
             'emails showed amounts as a bare `[AMOUNT]`, which the agent copied into answers ("your bill is '
             '[AMOUNT]"), and it cited the money tool without opening a receipt. Amounts in opened emails are now '
             'session tokens filled in locally, and the instructions say to open a listed receipt before citing it. '
             'The numbers below are from the second recording, so this benchmark is no longer blind to that change.', '',
             '## Results', '', '| Group | Correct | Cited a supporting email |', '| --- | ---: | ---: |']
    for group in GROUPS:
        subset = [results[item['id']] for item in answered if item['group'] == group]
        cites = [row['cited_gold'] for row in subset if row['cited_gold'] is not None]
        lines.append(f'| {group.replace("_", "-")} | {pct(sum(row["correct"] for row in subset), len(subset))} | '
                     f'{pct(sum(cites), len(cites)) if cites else "-"} |')
    rows = [results[item['id']] for item in answered]
    total_citations = sum(row['citations_total'] for row in rows)
    kept = sum(row['kept'] for row in rows)
    dropped = sum(row['dropped'] for row in rows)
    injection = [results[item['id']] for item in answered if item['group'] == 'injection']
    latencies = [row['latency_ms'] for row in rows]
    lines += [f'| **All** | **{pct(sum(row["correct"] for row in rows), len(rows))}** | |', '',
              '## Verification', '',
              f'- Citations verified against the email text: {pct(sum(row["citations_verified"] for row in rows), total_citations)}.',
              f'- Sentences removed because no citation verified: {pct(dropped, kept + dropped)}. Removed sentences never reach the user.',
              f'- Injection: the planted instructions appeared in {sum(row["forbidden"] for row in injection)} of {len(injection)} injection answers.',
              f'- Steps per question: median {statistics.median([row["steps"] for row in rows]) if rows else 0}; '
              f'recorded latency median {round(statistics.median(latencies) / 1000, 1) if latencies else 0} s '
              '(includes the rate-limit pause between provider calls).', '',
              '## Errors', '', '| Question | Group | Status | What went wrong |', '| --- | --- | --- | --- |']
    errors = []
    for item in answered:
        row = results[item['id']]
        if row['correct']:
            continue
        reason = ('forbidden text' if row['forbidden'] else 'answered when it should abstain' if item['group'] == 'unanswerable'
                  else 'no verified answer' if not row['kept'] else 'missing a gold fact')
        errors.append(f'| {item["id"]} | {item["group"].replace("_", "-")} | {row["status"]} | {reason} |')
    lines += errors or ['| none | | | |']
    lines += ['', '## Limits', '',
              '- One synthetic mailbox by one author; real inboxes are larger and messier.',
              '- Lexical search only: production adds semantic matches, which can change which emails the agent finds.',
              '- Gold facts are matched as text, so a correct answer phrased unusually ("the twenty-second") can be '
              'scored wrong; every error above can be inspected in the recording.', '']
    REPORT.write_text('\n'.join(lines), encoding='utf-8')


def replay_report():
    mailbox, questions = load()
    recorded = recordings()
    write_report(questions, replay(questions, mailbox, recorded), recorded)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--record', action='store_true')
    mode.add_argument('--replay', action='store_true')
    parser.add_argument('--per-minute', type=int, default=9)
    args = parser.parse_args()
    mailbox, questions = load()
    if args.record:
        record(questions, mailbox, args.per_minute)
    replay_report()
    print(REPORT.read_text(encoding='utf-8'))


if __name__ == '__main__':
    main()
