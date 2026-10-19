"""Fit and evaluate the scam/phishing risk score.

    python -m scripts.evaluate_risk --fit      # fit weights on the train split -> src/risk_weights.json
    python -m scripts.evaluate_risk --report   # score the test split -> docs/RISK_EVALUATION.md
    python -m scripts.evaluate_risk --inbox    # private: band counts on your own saved mail (prints only)

Fully local and deterministic: the same benchmark and code always produce the
same weights and report. The model is a logistic regression over the binary
findings of src/risk_analysis.py with weights constrained to be >= 0, so adding
a red flag can never lower a score (an attacker cannot buy a lower score by
adding legitimate-looking features).
"""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import sqlite3

from src.mime_parser import parse_gmail_message
from src.risk_analysis import (HIGH_BAND, MEDIUM_BAND, WEIGHTS_PATH, collect_findings, score_findings,
                               trusted_domains)

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / 'fixtures' / 'phishing_benchmark' / 'emails.json'
REPORT = ROOT / 'docs' / 'RISK_EVALUATION.md'
VERSION = 'risk-v1'
SIGNALS = (
    'auth_dmarc_fail', 'auth_spf_fail', 'auth_dkim_fail', 'auth_missing',
    'reply_to_freemail', 'reply_to_mismatch', 'brand_name_on_freemail',
    'lookalike_sender', 'lookalike_link', 'punycode_domain',
    'link_text_mismatch', 'shortened_link', 'ip_address_link',
    'otp_or_pin_request', 'upi_collect_lure', 'kyc_lure', 'account_threat', 'prize_lure', 'fee_lure',
    'gift_card_request', 'credential_request', 'urgency_pressure', 'hidden_text',
)
GROUPS = {
    'Sender authentication (SPF/DKIM/DMARC)': ('auth_dmarc_fail', 'auth_spf_fail', 'auth_dkim_fail', 'auth_missing'),
    'Reply paths and display names': ('reply_to_freemail', 'reply_to_mismatch', 'brand_name_on_freemail'),
    'Lookalike domains': ('lookalike_sender', 'lookalike_link', 'punycode_domain'),
    'Link checks': ('link_text_mismatch', 'shortened_link', 'ip_address_link'),
    'Lures (OTP, UPI, KYC, prizes, fees, credentials)': (
        'otp_or_pin_request', 'upi_collect_lure', 'kyc_lure', 'account_threat', 'prize_lure', 'fee_lure',
        'gift_card_request', 'credential_request'),
    'Urgency': ('urgency_pressure',),
    'Hidden text': ('hidden_text',),
}
L2 = 0.05
STEPS = 4000
RATE = 0.5


def load_cases():
    rows = json.loads(BENCHMARK.read_text(encoding='utf-8'))
    cases = []
    for row in rows:
        parsed = parse_gmail_message(row['message'], row['id'])
        findings = collect_findings(sender=parsed['sender'], subject=parsed['subject'], body=parsed['body'],
                                    signals=parsed['signals'])
        cases.append({**row, 'findings': findings, 'signals_fired': {item.signal for item in findings},
                      'y': 1 if row['label'] == 'phishing' else 0})
    return cases


def fit(cases, excluded=()):
    """Projected gradient descent: L2-regularised logistic loss, weights >= 0."""
    names = [name for name in SIGNALS if name not in excluded]
    weights, bias = dict.fromkeys(names, 0.0), 0.0
    for _ in range(STEPS):
        grad, grad_bias = dict.fromkeys(names, 0.0), 0.0
        for case in cases:
            z = bias + sum(weights[name] for name in names if name in case['signals_fired'])
            error = 1 / (1 + math.exp(-z)) - case['y']
            grad_bias += error
            for name in names:
                if name in case['signals_fired']:
                    grad[name] += error
        count = len(cases)
        bias -= RATE * grad_bias / count
        for name in names:
            weights[name] = max(0.0, weights[name] - RATE * (grad[name] / count + L2 * weights[name]))
    return {'version': VERSION, 'bias': round(bias, 4), 'weights': {name: round(value, 4) for name, value in weights.items()}}


def score(case, model):
    return score_findings([item for item in case['findings'] if item.signal in model['weights']], model)


def metrics(cases, model, threshold):
    tp = sum(1 for case in cases if case['y'] and score(case, model) >= threshold)
    fp = sum(1 for case in cases if not case['y'] and score(case, model) >= threshold)
    positives = sum(case['y'] for case in cases)
    negatives = len(cases) - positives
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / positives if positives else 0.0
    return {'tp': tp, 'fp': fp, 'positives': positives, 'negatives': negatives, 'precision': precision,
            'recall': recall, 'f1': 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
            'fpr': fp / negatives if negatives else 0.0}


def pct(value):
    return f'{value * 100:.1f}%'


def write_report(cases, model):
    train = [case for case in cases if case['split'] == 'train']
    test = [case for case in cases if case['split'] == 'test']
    lines = ['# Scam and phishing risk evaluation', '',
             'Generated by `python -m scripts.evaluate_risk --report` from the CC0 synthetic benchmark in '
             '`fixtures/phishing_benchmark/` (built by `scripts/build_phishing_benchmark.py`). The run is local and '
             'deterministic: re-running it reproduces this file byte for byte.', '',
             f'- Cases: {len(cases)} ({len(train)} train, {len(test)} test), from '
             f'{len({case["template_id"] for case in cases})} templates in {len({case["family"] for case in cases})} families. '
             'Each template appears as an HTML email and as a plain-text email.',
             '- The split is **by template**: no test email shares wording with a training email.',
             f'- Score: logistic regression over the findings, weights >= 0, L2 {L2}. '
             f'Bands: high >= {HIGH_BAND}, medium >= {MEDIUM_BAND}.', '',
             '## Test results', '',
             '| Flag when | Precision | Recall | F1 | False-positive rate |',
             '| --- | ---: | ---: | ---: | ---: |']
    for label, threshold in (('High (>= 70)', HIGH_BAND), ('Medium or high (>= 40)', MEDIUM_BAND)):
        m = metrics(test, model, threshold)
        lines.append(f'| {label} | {pct(m["precision"])} ({m["tp"]}/{m["tp"] + m["fp"]}) | {pct(m["recall"])} '
                     f'({m["tp"]}/{m["positives"]}) | {m["f1"]:.3f} | {pct(m["fpr"])} ({m["fp"]}/{m["negatives"]}) |')
    lines += ['', '## Per family (test split, medium or high)', '',
              '| Family | Label | Cases | Flagged |', '| --- | --- | ---: | ---: |']
    families = sorted({(case['family'], case['label']) for case in test}, key=lambda item: (item[1], item[0]))
    for family, label in families:
        group = [case for case in test if case['family'] == family]
        flagged = sum(1 for case in group if score(case, model) >= MEDIUM_BAND)
        lines.append(f'| {family} | {label} | {len(group)} | {flagged} |')
    lines += ['', '## What each check contributes (ablation)', '',
              'Each row refits the model without one group of checks and scores the same test split. '
              'The checks overlap on purpose (defense in depth), so at the medium threshold most single groups can '
              'be removed without losing a case; the high-threshold recall shows how much each group adds to confidence.', '',
              '| Without | Recall (medium+) | False-positive rate (medium+) | Recall (high) |',
              '| --- | ---: | ---: | ---: |']
    def row(name, candidate):
        medium, high = metrics(test, candidate, MEDIUM_BAND), metrics(test, candidate, HIGH_BAND)
        lines.append(f'| {name} | {pct(medium["recall"])} | {pct(medium["fpr"])} | {pct(high["recall"])} |')
    row('(all checks)', model)
    for group, names in GROUPS.items():
        row(group, fit(train, excluded=names))
    auth_only = fit(train, excluded=[name for name in SIGNALS if not name.startswith('auth_')])
    m = metrics(test, auth_only, MEDIUM_BAND)
    lines += ['', f'Authentication alone (SPF/DKIM/DMARC only) catches {pct(m["recall"])} of test phishing: '
              'attackers who register their own lookalike domain (`hdfcbank-alerts.com`) or send from Gmail pass '
              'every check. That is why the score combines authentication with lookalike, link and content checks.', '',
              '## Fitted weights', '', 'Bias ' + f'{model["bias"]}' + '. A weight of 0 means the check never '
              'helped on the training split; it still appears in reports as evidence.', '',
              '| Finding | Weight | Fired in train (phishing / legit) |', '| --- | ---: | ---: |']
    fired = Counter((name, case['y']) for case in train for name in case['signals_fired'])
    for name in sorted(SIGNALS, key=lambda item: -model['weights'].get(item, 0)):
        lines.append(f'| `{name}` | {model["weights"].get(name, 0):.3f} | {fired[(name, 1)]} / {fired[(name, 0)]} |')
    lines += ['', '## Limits', '',
              '- The benchmark and the rules were written by the same person, so these numbers are optimistic. '
              'They show the checks work as designed on unseen templates, not that they generalise to every scam.',
              f'- {len(test)} test cases give wide uncertainty: one extra error moves a rate by 2 to 4 points.',
              '- Lookalike matching trades recall for false alarms on innocent names one letter from a brand '
              '(`tomato.in` resembles `zomato.com`) or containing a short brand word (`axis-design.com`). Domains '
              'you label Important or Updates, or that sent you at least two such emails and no spam, are trusted '
              'and never count as lookalikes; a repeated scam campaign filed as Updates therefore stops being flagged '
              'as a lookalike from its third email, while every other check still applies.',
              '- Small businesses without DMARC that ask for payment by a deadline and reply from Gmail look like '
              'scams to every rule here (the `small_business` false positives above). The report shows the '
              'reasons so you can judge; MailMind never hides or deletes mail because of this score.',
              '- The more meaningful number is the false-positive rate on your own mail. Run '
              '`python -m scripts.evaluate_risk --inbox`: it prints band counts for your saved mail and never writes '
              'them anywhere.', '']
    REPORT.write_text('\n'.join(lines), encoding='utf-8')


def inbox(db_path):
    """Band counts on the user's own saved mail, by category. Read-only, prints only."""
    from src.email_signals import get_email_signals
    from src.risk_analysis import assess
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    counts = Counter()
    # A database the app has not migrated yet has no signals: content checks only.
    has_signals = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='email_signals'").fetchone() is not None
    for account in [row[0] for row in conn.execute("SELECT DISTINCT account_id FROM email_logs WHERE message_type='gmail'")]:
        trusted = trusted_domains(conn, account)
        for row in conn.execute("SELECT * FROM email_logs WHERE account_id=? AND message_type='gmail'", (account,)):
            signals = get_email_signals(conn, account, row['email_id']) if has_signals else None
            report = assess(sender=row['sender'], subject=row['subject'], body=row['body'], signals=signals,
                            trusted=trusted)
            category = row['human_label'] or row['prediction'] or 'REVIEW'
            counts[(category, report['band'], report['evidence_level'])] += 1
    for (category, band, level), count in sorted(counts.items()):
        print(f'{category:10} {band:7} {level:13} {count}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--fit', action='store_true')
    mode.add_argument('--report', action='store_true')
    mode.add_argument('--inbox', action='store_true')
    parser.add_argument('--db', default=str(ROOT / 'data' / 'email_logs.db'))
    args = parser.parse_args()
    if args.inbox:
        inbox(args.db)
        return
    cases = load_cases()
    if args.fit:
        model = fit([case for case in cases if case['split'] == 'train'])
        WEIGHTS_PATH.write_text(json.dumps(model, indent=1) + '\n', encoding='utf-8')
        print(json.dumps(model, indent=1))
    else:
        model = json.loads(WEIGHTS_PATH.read_text(encoding='utf-8'))
        write_report(cases, model)
        print(REPORT.read_text(encoding='utf-8'))


if __name__ == '__main__':
    main()
