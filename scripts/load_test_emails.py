"""Manual load test: put N unread synthetic test emails into the connected inbox.

Uses Gmail messages.insert with the account's existing OAuth token, so nothing is
sent over SMTP and no second mailbox is needed. The running worker picks the
messages up through its normal history sync.

    ./venv/Scripts/python.exe -m scripts.load_test_emails you@gmail.com [count]
"""
import base64
import hashlib
import sys
import time
from email.message import EmailMessage

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from src.config import Settings
from src.email_client import SCOPES

BODIES = [
    ('Invoice #{n} is overdue', 'Your invoice is 14 days overdue. Please pay by Friday to avoid a late fee.'),
    ('Weekly product digest #{n}', 'Here are this week\'s top stories and product updates. Unsubscribe any time.'),
    ('Can we meet tomorrow? (#{n})', 'Hi, can you join a 15-minute call tomorrow at 10:00 to review the contract?'),
    ('You won a prize!!! #{n}', 'Claim your free gift card now by clicking this link and entering your details.'),
    ('Your order #{n} has shipped', 'Your package is on its way and should arrive in 3-5 business days.'),
]


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    account = sys.argv[1].strip()
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    path = Settings.from_environment().data_dir / 'oauth' / (hashlib.sha256(account.encode()).hexdigest() + '.json')
    if not path.exists():
        sys.exit(f'No saved Google token for {account}. Connect Google in the dashboard first.')
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds.valid:
        creds.refresh(Request())  # in memory only; the app keeps managing its own token file
    gmail = build('gmail', 'v1', credentials=creds, cache_discovery=False)
    run = time.strftime('%H%M%S')
    for n in range(1, count + 1):
        subject, body = BODIES[(n - 1) % len(BODIES)]
        message = EmailMessage()
        message['From'] = 'MailMind Load Test <loadtest@example.com>'
        message['To'] = account
        message['Subject'] = f'[load {run}] ' + subject.format(n=n)
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        gmail.users().messages().insert(userId='me', body={'raw': raw, 'labelIds': ['INBOX', 'UNREAD']}).execute()
        print(f'{time.strftime("%H:%M:%S")}  inserted {n:2}/{count}  {message["Subject"]}')
    print(f'done. Subjects start with "[load {run}]".')


if __name__ == '__main__':
    main()
