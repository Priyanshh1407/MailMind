"""Blind test: can a reader tell MailMind's follow-ups from a person's?

    python -m scripts.follow_up_blind_test

Shows each benchmark thread with two follow-ups in random order: one written
by a person (fixtures/follow_up_benchmark/human.json) and MailMind's recorded
draft. The reader picks the one they think is AI. Results are appended to
docs/FOLLOW_UP_BLIND_TEST.md with the reader's chosen label (no other
personal data). Detection near 50% means readers could not tell them apart.
A handful of readers is an informal panel, and the report says so.
"""
import json
from pathlib import Path
import random

from scripts.evaluate_follow_ups import load, recordings, replay

ROOT = Path(__file__).resolve().parents[1]
HUMAN = ROOT / 'fixtures' / 'follow_up_benchmark' / 'human.json'
RESULTS = ROOT / 'docs' / 'FOLLOW_UP_BLIND_TEST.md'


def main():
    if not HUMAN.exists():
        raise SystemExit(f'Write one human follow-up per thread in {HUMAN} ({{"thread id": "text"}}) first.')
    human = json.loads(HUMAN.read_text(encoding='utf-8'))
    rows = load()
    drafts = replay(rows, recordings())
    reader = input('Reader label (for example "friend-1"): ').strip() or 'anonymous'
    rng = random.Random()
    correct = total = 0
    for row in rows:
        if row['id'] not in human or row['id'] not in drafts:
            continue
        pair = [('human', human[row['id']]), ('ai', drafts[row['id']]['result']['text'])]
        rng.shuffle(pair)
        print('\n' + '=' * 70 + f'\nThread: {row["subject"]}\nLast message:\n{row["thread"][-1]["body"]}\n')
        for label, (_, text) in zip('AB', pair):
            print(f'--- {label} ---\n{text}\n')
        choice = ''
        while choice not in ('A', 'B'):
            choice = input('Which one did an AI write? [A/B]: ').strip().upper()
        total += 1
        correct += pair['AB'.index(choice)][0] == 'ai'
    if not total:
        raise SystemExit('No thread has both a human and a MailMind follow-up.')
    header = '' if RESULTS.exists() else ('# Follow-up blind test\n\nEach row is one reader. "Spotted" is how often they '
                                          'picked the AI draft; 50% means they could not tell. Informal panel.\n\n'
                                          '| Reader | Pairs | Spotted the AI |\n| --- | ---: | ---: |\n')
    with RESULTS.open('a', encoding='utf-8') as stream:
        stream.write(header + f'| {reader} | {total} | {correct / total * 100:.0f}% ({correct}/{total}) |\n')
    print(f'\nYou spotted the AI in {correct} of {total} pairs. Saved to {RESULTS}.')


if __name__ == '__main__':
    main()
