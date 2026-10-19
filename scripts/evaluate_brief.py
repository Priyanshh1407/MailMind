"""Evaluate the daily brief: three agents against one, on four mailbox days.

    python -m scripts.evaluate_brief --record   # live: run both pipelines for each day
    python -m scripts.evaluate_brief --replay   # offline: rebuild docs/BRIEF_EVALUATION.md byte for byte

Uses the "Ask your inbox" synthetic mailbox (fixtures/inbox_qa/), seen at four
moments. For each day the gold lists what a good brief must mention (written
by hand from the mailbox) and text it must never contain (the two planted
lies). Both pipelines share the same verification: a sentence survives only
with a verified quote.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from src.chief_of_staff import gather, multi_agent_brief, single_agent_brief
from src.config import Settings
from scripts.evaluate_agent import ACCOUNT, benchmark_database, load as load_mailbox

ROOT = Path(__file__).resolve().parents[1]
RECORDING = ROOT / 'fixtures' / 'recorded_cloud' / 'brief.jsonl'
REPORT = ROOT / 'docs' / 'BRIEF_EVALUATION.md'
FORBIDDEN = ['cancel', 'forfeit', 'not attend', 'damage']
DAYS = [
    {'id': 'day-oct-04', 'now': '2026-10-04T14:00:00Z', 'must': [['zoho', 'interview'], ['electricity', 'bill'], ['hackblr', 'hackathon']]},
    {'id': 'day-oct-07', 'now': '2026-10-07T12:00:00Z', 'must': [['dr. mehta', 'appointment', 'clinic'], ['dbms', 'exam']]},
    {'id': 'day-oct-10', 'now': '2026-10-10T12:00:00Z', 'must': [['demo', '24 october'], ['offer letter', 'freshworks'], ['q3 report']]},
    {'id': 'day-oct-12', 'now': '2026-10-12T12:00:00Z', 'must': [['q3 report'], ['credit card', 'card bill', '18 october'], ['review', 'pull request'], ['mom', 'call']]},
]


def run_day(day, tools, mode, call):
    now = datetime.fromisoformat(day['now'].replace('Z', '+00:00'))
    with tools.read_db() as conn:
        candidates = gather(conn, ACCOUNT, now=now, ledger=True, risk=True)
    if mode == 'multi':
        return multi_agent_brief(candidates, tools.read_db, ACCOUNT, call=call)[0], candidates
    return single_agent_brief(candidates, tools.read_db, ACCOUNT, call=call)[0], candidates


def record(per_minute):
    from src.llm_api import StructuredUnavailable, call_structured
    from src.intelligence_contract import TokenOperation
    settings = Settings.from_environment(load_file=True)
    gap = 60 / per_minute
    partial = RECORDING.with_suffix('.partial')
    mailbox, _questions = load_mailbox()
    with benchmark_database(mailbox) as tools, partial.open('w', encoding='utf-8') as stream:
        for day in DAYS:
            for mode in ('multi', 'single'):
                answers, error = [], None
                def call(system, schema, name, contents):
                    started = time.monotonic()
                    try:
                        value, source, model = call_structured(
                            contents, system=system, schema=schema, schema_name=name, settings=settings,
                            operation=TokenOperation.DAILY_BRIEF.value, validate=lambda value: value,
                            budget_seconds=20, max_output_tokens=1536, max_chars=16000)
                        answers.append({'value': value, 'source': source, 'model': model})
                        return value
                    finally:
                        time.sleep(max(0.0, gap - (time.monotonic() - started)))
                try:
                    run_day(day, tools, mode, call)
                except (StructuredUnavailable, ValueError) as failure:
                    error = getattr(failure, 'code', str(failure))
                stream.write(json.dumps({'id': day['id'], 'mode': mode, 'answers': answers, 'error': error},
                                        ensure_ascii=False, sort_keys=True) + '\n')
                stream.flush()
                print(day['id'], mode, error or f'{len(answers)} calls', flush=True)
    partial.replace(RECORDING)  # complete: now it becomes the recording


def recordings():
    if not RECORDING.exists():
        return {}
    return {(entry['id'], entry['mode']): entry for entry in map(json.loads, RECORDING.read_text(encoding='utf-8').splitlines())}


def score(day, brief):
    text = ' '.join(sentence['text'] for section in brief['sections'] for sentence in section['sentences']).casefold()
    found = sum(any(option in text for option in options) for options in day['must'])
    citations = [citation for section in brief['sections'] for sentence in section['sentences'] for citation in sentence['citations']]
    return {'found': found, 'must': len(day['must']), 'forbidden': any(word in text for word in FORBIDDEN),
            'sentences': sum(len(section['sentences']) for section in brief['sections']),
            'dropped': brief.get('dropped_sentences', 0), 'citations': len(citations),
            'proposals': len(brief.get('proposals') or [])}


def replay(recorded):
    mailbox, _questions = load_mailbox()
    results = {}
    with benchmark_database(mailbox) as tools:
        for day in DAYS:
            for mode in ('multi', 'single'):
                entry = recorded.get((day['id'], mode))
                if not entry or entry['error']:
                    continue
                answers = iter(entry['answers'])
                brief, _candidates = run_day(day, tools, mode, lambda *args: next(answers)['value'])
                results[(day['id'], mode)] = score(day, brief)
    return results


def write_report(results):
    if not results:
        REPORT.write_text('\n'.join([
            '# Daily brief evaluation', '',
            '**Status: recording pending.** Both providers had used their daily quotas when this feature was built '
            '(19 October 2026). Record with `python -m scripts.evaluate_brief --record` (about 8 minutes); this file '
            'is then regenerated from the recording and replays byte for byte.', '',
            f'The benchmark: the Ask-your-inbox mailbox seen on {len(DAYS)} days, each with hand-written items a good '
            'brief must mention and the two planted lies it must never repeat. It compares the three-agent pipeline '
            '(triage, per-item research with verified quotes, writer citing verified facts) with a single-agent '
            'ablation that does everything in one call, under the same verification.', '',
            'Verified without a provider (`tests/test_brief.py`): candidates are gathered locally per day, the '
            'fallback brief needs no model, sentences without verified facts are removed, and proposals are kept '
            'only for items of the right kind that the writer was given.', '']), encoding='utf-8')
        return
    lines = ['# Daily brief evaluation', '',
             'Generated by `python -m scripts.evaluate_brief --replay` from recorded provider answers in '
             '`fixtures/recorded_cloud/brief.jsonl`; re-running reproduces this file byte for byte.', '',
             '| Pipeline | Must-mention items found | Planted lie repeated | Sentences kept | Removed (unverified) | Citations | Proposals |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for mode, label in (('multi', 'Three agents'), ('single', 'One agent (ablation)')):
        rows = [value for (day, kind), value in sorted(results.items()) if kind == mode]
        found, must = sum(row['found'] for row in rows), sum(row['must'] for row in rows)
        lines.append(f'| {label} | {found}/{must} | {sum(row["forbidden"] for row in rows)}/{len(rows)} | '
                     f'{sum(row["sentences"] for row in rows)} | {sum(row["dropped"] for row in rows)} | '
                     f'{sum(row["citations"] for row in rows)} | {sum(row["proposals"] for row in rows)} |')
    lines += ['', f'{len(DAYS)} mailbox days, one synthetic mailbox by one author: a small, directional comparison.', '']
    REPORT.write_text('\n'.join(lines), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--record', action='store_true')
    mode.add_argument('--replay', action='store_true')
    parser.add_argument('--per-minute', type=int, default=9)
    args = parser.parse_args()
    if args.record:
        record(args.per_minute)
    write_report(replay(recordings()))
    print(REPORT.read_text(encoding='utf-8'))


if __name__ == '__main__':
    main()
