"""Evaluate money extraction: local rules vs the cloud-assisted production path.

    python -m scripts.evaluate_ledger --record   # live: one provider call per detected receipt
    python -m scripts.evaluate_ledger --replay   # offline: rebuild docs/LEDGER_EVALUATION.md byte for byte

The provider sees exactly what production sends: the email with every amount
replaced by a numbered placeholder. Before each request this script asserts
that no amount from the email appears in it. Recordings keep only the
provider's answer (placeholders, kinds, merchant names), so they contain no
amounts either. Splits: "dev" cases shaped the local rules; "holdout" cases
were written after the rules were frozen and never used to change them.
"""
import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import re
import time

from src.config import Settings
from src.intelligence_contract import TokenOperation
from src.ledger import (OUTPUT_SCHEMA, SYSTEM_INSTRUCTION, detect_subscriptions, extract_by_rules, find_amounts,
                        is_transaction, merchant_key, provider_contents, resolve_cloud_answer)

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / 'fixtures' / 'receipts_benchmark' / 'emails.json'
RECORDING = ROOT / 'fixtures' / 'recorded_cloud' / 'ledger.jsonl'
REPORT = ROOT / 'docs' / 'LEDGER_EVALUATION.md'
TODAY = date(2026, 10, 1)


def load():
    return json.loads(BENCHMARK.read_text(encoding='utf-8'))


def assert_no_amounts(contents, row):
    """The privacy contract: no amount from the email reaches the provider."""
    # Compare each amount with each separate number in the request (digits of
    # neighbouring numbers, like a date and an order ID, must not be joined).
    # The received date and the placeholder numbers are not email numbers.
    text = re.sub(r'\[AMOUNT_\d+\]', ' ', json.loads(contents)['email']['text'])
    # Standalone numbers only: digits inside a code or ID ("TREAT150") are
    # not amounts and stay as written (see the report's limits).
    numbers = {re.sub(r'\D', '', token) for token in re.findall(r'(?<![A-Za-z0-9])\d[\d,.]*(?![A-Za-z0-9])', text)}
    for amount in find_amounts(f'{row["subject"]}\n{row["body"]}'):
        digits = re.sub(r'\D', '', amount.text)
        whole = re.sub(r'\D', '', amount.text.split('.')[0])
        if amount.text in contents or digits in numbers or whole in numbers:
            raise AssertionError(f'Amount leaked into the provider request for {row["id"]}')


def record(rows, per_minute):
    settings = Settings.from_environment(load_file=True)
    from src.llm_api import StructuredUnavailable, call_structured
    RECORDING.parent.mkdir(parents=True, exist_ok=True)
    gap = 60 / per_minute
    with RECORDING.open('w', encoding='utf-8') as stream:
        for row in rows:
            if not is_transaction(row['subject'], row['body']):
                continue
            contents, _masked, _amounts = provider_contents(row['sender'], row['subject'], row['body'],
                                                            date.fromisoformat(row['received']))
            assert_no_amounts(contents, row)
            started = time.monotonic()
            entry = {'id': row['id']}
            try:
                value, source, model = call_structured(
                    contents, system=SYSTEM_INSTRUCTION, schema=OUTPUT_SCHEMA, schema_name='mailmind_ledger',
                    settings=settings, operation=TokenOperation.LEDGER_EXTRACTION.value, validate=lambda value: value,
                    max_output_tokens=512, record_key='ledger-eval', max_chars=4000)
                entry.update({'source': source, 'model': model, 'answer': value})
            except StructuredUnavailable as error:
                entry.update({'error': error.code})
            entry['latency_ms'] = round((time.monotonic() - started) * 1000)
            stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + '\n')
            stream.flush()
            print(row['id'], entry.get('source') or entry.get('error'), flush=True)
            time.sleep(max(0.0, gap - (time.monotonic() - started)))


def recordings():
    if not RECORDING.exists():
        return {}
    return {entry['id']: entry for entry in map(json.loads, RECORDING.read_text(encoding='utf-8').splitlines())}


def merchant_matches(found, gold):
    a, b = merchant_key(found), merchant_key(gold)
    return bool(a) and (a == b or a in b or b in a)


def run(rows, mode, recorded):
    """Production behaviour per mode: 'rules' (local-only) or 'hybrid'
    (cloud answer when recorded and valid, else the rules)."""
    results = {}
    for row in rows:
        received = date.fromisoformat(row['received'])
        if not is_transaction(row['subject'], row['body']):
            results[row['id']] = None
            continue
        entry = None
        if mode == 'hybrid' and 'answer' in recorded.get(row['id'], {}):
            _contents, masked, amounts = provider_contents(row['sender'], row['subject'], row['body'], received)
            try:
                entry = resolve_cloud_answer(recorded[row['id']]['answer'], sender=row['sender'], masked=masked,
                                             amounts=amounts, received=received, source=recorded[row['id']]['source'])
            except ValueError:
                entry = None
            if entry is not None and entry.get('kind') == 'none':
                results[row['id']] = None
                continue
        results[row['id']] = entry or extract_by_rules(row['sender'], row['subject'], row['body'], received)
    return results


def score(rows, results):
    tp = sum(1 for row in rows if row['gold']['kind'] != 'none' and results[row['id']])
    fp = sum(1 for row in rows if row['gold']['kind'] == 'none' and results[row['id']])
    fn = sum(1 for row in rows if row['gold']['kind'] != 'none' and not results[row['id']])
    found = [(row, results[row['id']]) for row in rows if row['gold']['kind'] != 'none' and results[row['id']]]
    amount = sum(1 for row, entry in found if (entry['amount_minor'], entry['currency']) ==
                 (row['gold']['amount_minor'], row['gold']['currency']))
    kind = sum(1 for row, entry in found if entry['kind'] == row['gold']['kind'])
    merchant = sum(1 for row, entry in found if merchant_matches(entry['merchant'], row['gold']['merchant']))
    transactions = tp + fn
    return {'tp': tp, 'fp': fp, 'fn': fn, 'transactions': transactions, 'amount': amount, 'kind': kind,
            'merchant': merchant, 'negatives': sum(1 for row in rows if row['gold']['kind'] == 'none')}


def subscription_score(rows, results):
    series = [row for row in rows if row.get('series')]
    gold = {row['series']['merchant'] for row in series if row['series']['recurring']}
    entries = [{**results[row['id']], 'occurred_at': row['received'] + 'T09:00:00.000000Z', 'email_id': row['id']}
               for row in series if results[row['id']]]
    found = {item['merchant'] for item in detect_subscriptions(entries, TODAY)}
    # Map detected names back to gold names loosely ("Netflix" vs "Netflix").
    hits = {name for name in gold if any(merchant_matches(item, name) for item in found)}
    false = {item for item in found if not any(merchant_matches(item, name) for name in gold)}
    precision = len(hits) / (len(hits) + len(false)) if hits or false else 1.0
    recall = len(hits) / len(gold) if gold else 1.0
    return {'gold': len(gold), 'hits': len(hits), 'false': sorted(false), 'precision': precision, 'recall': recall}


def pct(part, whole):
    return f'{part / whole * 100:.1f}% ({part}/{whole})' if whole else 'n/a'


def write_report(rows, recorded):
    lines = ['# Money extraction evaluation', '',
             'Generated by `python -m scripts.evaluate_ledger --replay` from the CC0 synthetic benchmark in '
             '`fixtures/receipts_benchmark/` and the recorded provider answers in `fixtures/recorded_cloud/ledger.jsonl`. '
             'Re-running it reproduces this file byte for byte.', '',
             '**Privacy contract.** Every amount is replaced by a numbered placeholder (`[AMOUNT_1]`, ...) before a '
             'provider call; the recorder asserts that no amount from the email appears in the request, and the '
             'provider only answers which placeholder is the total. The real number is filled in locally.', '',
             '- **dev**: cases the local rules were developed against (so the rules are expected to do well).',
             '- **holdout**: cases written after the rules were frozen, in other phrasings, never used to change them.', '',
             '- **Rules**: local-only mode, no provider.',
             '- **Hybrid**: what production does when a provider is configured: the local detector first, then the '
             'provider picks the placeholder; the rules decide if the provider fails or answers invalidly.', '',
             '**Merchant** means whoever was paid or refunded when the email names them (the shop in a UPI or card '
             'alert), otherwise the sender. The first recorded run showed the labels had used the sender for nine '
             'UPI and bank alerts (six dev, three holdout) where the provider correctly named the payee; the labels '
             'were corrected to this definition, never to match an answer. The local rules use the sender, so they '
             'now miss those cases.', '']
    models = Counter(f'{entry["source"]} `{entry["model"]}`' for entry in recorded.values() if 'answer' in entry)
    errors = Counter(entry['error'] for entry in recorded.values() if 'error' in entry)
    lines += [f'Recorded answers: {sum(models.values())} ({", ".join(f"{name}: {count}" for name, count in sorted(models.items())) or "none"})'
              + (f'; failed calls: {", ".join(f"{code} x{count}" for code, count in sorted(errors.items()))}' if errors else '') + '.', '']
    for split in ('dev', 'holdout'):
        subset = [row for row in rows if row['split'] == split]
        lines += [f'## {split.capitalize()} split ({len(subset)} emails, '
                  f'{sum(row["gold"]["kind"] != "none" for row in subset)} transactions)', '',
                  '| Mode | Detected | False alarms | Amount exact | Kind | Merchant |',
                  '| --- | ---: | ---: | ---: | ---: | ---: |']
        for mode in ('rules', 'hybrid'):
            m = score(subset, run(subset, mode, recorded))
            lines.append(f'| {mode.capitalize()} | {pct(m["tp"], m["transactions"])} | {pct(m["fp"], m["negatives"])} | '
                         f'{pct(m["amount"], m["transactions"])} | {pct(m["kind"], m["transactions"])} | '
                         f'{pct(m["merchant"], m["transactions"])} |')
        lines.append('')
    dev = [row for row in rows if row['split'] == 'dev']
    lines += ['## Subscription detection (dev series)', '',
              'Monthly, quarterly and yearly charge series plus irregular repeat purchases from one shop that must '
              'not count as a subscription.', '', '| Mode | Precision | Recall | Wrongly detected |', '| --- | ---: | ---: | --- |']
    for mode in ('rules', 'hybrid'):
        m = subscription_score(dev, run(dev, mode, recorded))
        lines.append(f'| {mode.capitalize()} | {m["precision"] * 100:.1f}% | {m["recall"] * 100:.1f}% ({m["hits"]}/{m["gold"]}) | '
                     f'{", ".join(m["false"]) or "none"} |')
    holdout = [row for row in rows if row['split'] == 'holdout']
    misses = []
    for row in holdout:
        for mode in ('rules', 'hybrid'):
            entry = run([row], mode, recorded)[row['id']]
            gold = row['gold']
            if gold['kind'] == 'none' and entry:
                misses.append(f'| {row["id"]} | {mode} | false alarm ({entry["kind"]}) |')
            elif gold['kind'] != 'none' and (not entry or (entry['amount_minor'], entry['currency']) != (gold['amount_minor'], gold['currency'])):
                misses.append(f'| {row["id"]} | {mode} | {"missed" if not entry else "wrong total"} |')
    lines += ['', '## Holdout errors', '', 'Every holdout email a mode got wrong (amounts are not shown).', '',
              '| Case | Mode | Error |', '| --- | --- | --- |', *(misses or ['| none | | |']), '',
              '## Limits', '',
              '- Synthetic receipts by one author; real merchants format receipts in many more ways.',
              '- The detector is a keyword gate: a receipt with no transaction words, or an advert that uses '
              'them ("pay only ₹99"), can be misjudged before any provider sees it.',
              '- One entry per email: an email listing several separate payments records only its total.',
              '- Only amounts are hidden. Digits inside other text stay as written, so a coupon code like `TREAT150` '
              'can still hint at a ₹150 discount; order numbers, dates and IDs are sent like they are for '
              'classification (minimised as described in the privacy policy).', '']
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
    write_report(rows, recordings())
    print(REPORT.read_text(encoding='utf-8'))


if __name__ == '__main__':
    main()
