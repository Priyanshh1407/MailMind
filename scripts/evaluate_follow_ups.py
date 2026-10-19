"""Evaluate follow-up drafts: quality checks, facts, privacy and style.

    python -m scripts.evaluate_follow_ups --record   # live: draft every benchmark thread
    python -m scripts.evaluate_follow_ups --replay   # offline: rebuild docs/FOLLOW_UP_EVALUATION.md byte for byte

Recording saves the provider's raw answers (the first draft and the revision,
if one was needed); replay runs them back through the same masking, checks
and assembly as production. Before each request the recorder asserts that no
person's name, email address or amount from the thread is in it.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import statistics
import time

from src.config import Settings
from src.follow_ups import (OUTPUT_SCHEMA, STOCK_PHRASES, SYSTEM_INSTRUCTION, draft, learn_style, prepare_thread,
                            validate_draft)

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / 'fixtures' / 'follow_up_benchmark' / 'threads.json'
RECORDING = ROOT / 'fixtures' / 'recorded_cloud' / 'follow_ups.jsonl'
REPORT = ROOT / 'docs' / 'FOLLOW_UP_EVALUATION.md'
NAMES = ('Menon', 'Iyer', 'Kapoor', 'Ramesh', 'Priya', 'Neha', 'Rahul', 'Kavya', 'Ananya', 'Arjun', 'Nair', 'Sharma')


def load():
    return json.loads(BENCHMARK.read_text(encoding='utf-8'))


def assert_private(contents, row):
    for name in NAMES:
        if re.search(rf'\b{name}\b', contents):
            raise AssertionError(f'A name reached the provider request for {row["id"]}')
    if re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', contents):
        raise AssertionError(f'An email address reached the provider request for {row["id"]}')
    if re.search(r'(?i)(?:₹|\brs\.?)\s?\d', contents):
        raise AssertionError(f'An amount reached the provider request for {row["id"]}')


def today(row):
    return datetime.fromtimestamp(row['today_ms'] / 1000).date()


def run(row, call):
    thread, _summary, masker, names, _info = prepare_thread(row['thread'], row['account'])
    style = learn_style(row['style_samples'])
    return draft(thread, style, 'standard', masker, names, call=call, today=today(row)), style


def record(rows, per_minute):
    # Write to a side file: an interrupted run never replaces a good recording.
    partial = RECORDING.with_suffix('.partial')
    from src.llm_api import StructuredUnavailable, call_structured
    from src.intelligence_contract import TokenOperation
    settings = Settings.from_environment(load_file=True)
    gap = 60 / per_minute
    RECORDING.parent.mkdir(parents=True, exist_ok=True)
    with partial.open('w', encoding='utf-8') as stream:
        for row in rows:
            answers, error = [], None
            def call(contents):
                assert_private(contents, row)
                started = time.monotonic()
                try:
                    value, source, model = call_structured(
                        contents, system=SYSTEM_INSTRUCTION, schema=OUTPUT_SCHEMA, schema_name='mailmind_follow_up',
                        settings=settings, operation=TokenOperation.FOLLOW_UP_DRAFT.value, validate=validate_draft,
                        budget_seconds=25, max_output_tokens=1024, max_chars=8000)
                    answers.append({'value': value, 'source': source, 'model': model})
                    return value
                finally:
                    time.sleep(max(0.0, gap - (time.monotonic() - started)))
            try:
                run(row, call)
            except StructuredUnavailable as failure:
                error = failure.code
            stream.write(json.dumps({'id': row['id'], 'answers': answers, 'error': error}, ensure_ascii=False,
                                    sort_keys=True) + '\n')
            stream.flush()
            print(row['id'], error or f'{len(answers)} call(s)', flush=True)
    partial.replace(RECORDING)  # complete: now it becomes the recording


def recordings():
    if not RECORDING.exists():
        return {}
    return {entry['id']: entry for entry in map(json.loads, RECORDING.read_text(encoding='utf-8').splitlines())}


def replay(rows, recorded):
    results = {}
    for row in rows:
        entry = recorded.get(row['id'])
        if not entry or not entry['answers']:
            continue
        answers = iter(entry['answers'])
        result, style = run(row, lambda contents: validate_draft(next(answers)['value']))
        text = result['text'].casefold()
        facts = all(any(option in text for option in options) for options in row['facts'])
        results[row['id']] = {
            'result': result, 'facts': facts,
            'forbidden': any(phrase.casefold() in text for phrase in row['forbidden']),
            'stock': any(phrase in text for phrase in STOCK_PHRASES),
            'invented': any('does not appear in the thread' in issue for issue in result['issues']),
            'invented_first': any('does not appear in the thread' in issue for issue in result['issues_before']),
            'leftover': bool(re.search(r'\[(?:AMOUNT|EMAIL|LINK|NUMBER)_\d+\]|\{\{', result['text'])),
            'style_ok': text.startswith(style['greeting'].casefold()) and style['signoff'].casefold() in text,
            'source': entry['answers'][-1]['source'] + ' `' + entry['answers'][-1]['model'] + '`',
        }
    return results


def pct(part, whole):
    return f'{part / whole * 100:.1f}% ({part}/{whole})' if whole else 'n/a'


def write_report(rows, results):
    done = [results[row['id']] for row in rows if row['id'] in results]
    count = len(done)
    first_clean = sum(not row['result']['issues_before'] for row in done)
    final_clean = sum(not row['result']['issues'] for row in done)
    word_counts = [row['result']['words'] for row in done]
    if not done:
        REPORT.write_text('\n'.join([
            '# Follow-up draft evaluation', '',
            '**Status: recording pending.** Both providers had used their daily quotas when this feature was built '
            '(19 October 2026), so no live drafts are recorded yet. Record them with '
            '`python -m scripts.evaluate_follow_ups --record` (about 5 minutes); this file is then regenerated from '
            'the recording and replays byte for byte.', '',
            f'What is already verified without a provider, on all {len(rows)} benchmark threads '
            '(`tests/test_follow_ups.py`): every request is free of names, email addresses and amounts; the '
            'greeting follows how the user addressed the person ("Dear Ms. Menon", "Dear Sir/Madam", "Hi Neha", or '
            'no name); and the checker catches stock AI phrases, exclamation marks, dashes, lists, wrong length, a '
            'greeting or sign-off that is not the user\'s, unverifiable references, and numbers, dates or weekdays '
            'that the thread never mentions.', '',
            'Human blind test: **pending** (`python -m scripts.follow_up_blind_test`).', '']), encoding='utf-8')
        return
    lines = ['# Follow-up draft evaluation', '',
             'Generated by `python -m scripts.evaluate_follow_ups --replay` from the CC0 threads in '
             '`fixtures/follow_up_benchmark/` and the recorded provider answers in `fixtures/recorded_cloud/follow_ups.jsonl`. '
             'Replay runs the same masking, checks and assembly as production, so re-running reproduces this file byte for byte.', '',
             f'- Threads: {len(rows)} (job applications, vendors, college and HR requests, invoices, landlords, meetings, '
             'refunds), each ending with the user\'s message and no reply. One thread contains an instruction to the AI.',
             f'- Recorded with: {", ".join(sorted({row["source"] for row in done})) or "nothing recorded yet"}.',
             '- The user\'s style (greeting, sign-off, spelling) is learned locally from sample sent mail; only those '
             'settings reach the provider.', '',
             '## Results', '', '| Measure | Result |', '| --- | ---: |',
             f'| Passed every check on the first draft | {pct(first_clean, count)} |',
             f'| Passed every check after at most one revision | {pct(final_clean, count)} |',
             f'| Mentions every gold fact of the thread | {pct(sum(row["facts"] for row in done), count)} |',
             f'| Number or date not in the thread (final draft) | {pct(sum(row["invented"] for row in done), count)} |',
             f'| Same, before the revision pass | {pct(sum(row["invented_first"] for row in done), count)} |',
             f'| Stock AI phrase in the final text | {pct(sum(row["stock"] for row in done), count)} |',
             f'| Placeholder left in the final text | {pct(sum(row["leftover"] for row in done), count)} |',
             f'| Uses the user\'s own greeting and sign-off | {pct(sum(row["style_ok"] for row in done), count)} |',
             f'| Injected instruction followed | {pct(sum(row["forbidden"] for row in done), count)} |',
             f'| Body length, median words | {int(statistics.median(word_counts)) if word_counts else 0} |', '',
             'Every request was checked before sending: no person\'s name, email address or amount from the thread '
             'reached the provider (they travel as `{{recipient_name}}`, `[EMAIL_1]`, `[AMOUNT_1]` and are restored locally).', '',
             '## Remaining problems after revision', '', '| Thread | Problem |', '| --- | --- |']
    problems = [f'| {row["id"]} | {issue} |' for row in rows if row['id'] in results for issue in results[row['id']]['result']['issues']]
    lines += problems or ['| none | |']
    lines += ['', '## Human blind test', '',
              '`python -m scripts.follow_up_blind_test` shows pairs of follow-ups, one written by a person and one by '
              'MailMind, in random order, and records which one the reader thinks is AI. Results are written to '
              '`docs/FOLLOW_UP_BLIND_TEST.md`. **Pending**: no panel has been run yet, so no human-likeness claim is made.', '',
              '## Limits', '',
              '- 18 synthetic threads by one author, judged by deterministic checks, not people.',
              '- The checks catch stock phrases, invented numbers and dates, and style mismatches; they cannot judge '
              'whether a sentence sounds natural. That is what the blind test is for.',
              '- Only names in the To and From headers, and the names the user addressed, are masked; other names in a '
              'thread are sent like they are for classification.', '']
    REPORT.write_text('\n'.join(lines), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--record', action='store_true')
    mode.add_argument('--replay', action='store_true')
    parser.add_argument('--per-minute', type=int, default=9)
    args = parser.parse_args()
    rows = load()
    if args.record:
        record(rows, args.per_minute)
    write_report(rows, replay(rows, recordings()))
    print(REPORT.read_text(encoding='utf-8'))


if __name__ == '__main__':
    main()
