"""One bounded text representation for mail, training, retrieval, and inference."""
from html.parser import HTMLParser
import re
import unicodedata

MAX_BODY_CHARS = 32000
MAX_SUBJECT_CHARS = 2000
MAX_SENDER_CHARS = 1000
PREVIEW_CHARS = 200
MODEL_MAX_TOKENS = 512


def normalize_text(value, *, limit=MAX_BODY_CHARS):
    if not isinstance(value, str):
        value = ''
    value = unicodedata.normalize('NFC', value.replace('\r\n', '\n').replace('\r', '\n'))
    value = ''.join(c for c in value if c in '\n\t' or unicodedata.category(c) not in ('Cc', 'Cf'))
    value = '\n'.join(re.sub(r'[^\S\n]+', ' ', line).strip() for line in value.split('\n'))
    value = re.sub(r'\n{3,}', '\n\n', value).strip()
    return value[:limit].rstrip()


def normalize_subject(value):
    return ' '.join(normalize_text(value, limit=MAX_SUBJECT_CHARS).split())


def format_email_text(subject, body):
    return f'Subject: {normalize_subject(subject)} | Body: {normalize_text(body)}'


def format_model_text(sender, subject, body):
    """Local-model input including bounded sender evidence."""
    from email.utils import parseaddr
    sender = normalize_text(sender, limit=MAX_SENDER_CHARS)
    address = parseaddr(sender)[1].casefold()
    domain = address.rsplit('@', 1)[-1] if '@' in address else ''
    return (f'From: {sender} | Sender domain: {domain} | '
            f'Subject: {normalize_subject(subject)} | Body: {normalize_text(body)}')


def preview_text(body, limit=PREVIEW_CHARS):
    return body[:limit] + ('...' if len(body) > limit else '')


def prepare_training_text(frame):
    """Accept a dataframe without importing pandas or model libraries here."""
    frame['subject'] = frame['subject'].fillna('')
    frame['body'] = frame['body'].fillna('')
    frame['combined_text'] = [format_email_text(s, b) for s, b in zip(frame['subject'], frame['body'])]
    return frame


class _HTMLText(HTMLParser):
    ignored = {'script', 'style', 'head', 'template', 'svg', 'noscript'}
    blocks = {'p', 'div', 'br', 'hr', 'li', 'tr', 'td', 'th', 'pre', 'h1', 'h2', 'h3', 'h4', 'table', 'section', 'blockquote'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in self.ignored:
            self.hidden.append(tag)
        if not self.hidden and tag in self.blocks:
            self.text.append('\n')

    def handle_startendtag(self, tag, attrs):
        if not self.hidden and tag in self.blocks:
            self.text.append('\n')

    def handle_endtag(self, tag):
        if tag in self.hidden:
            self.hidden = self.hidden[:len(self.hidden)-1-self.hidden[::-1].index(tag)]
        if not self.hidden and tag in self.blocks:
            self.text.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def html_to_text(value, *, limit=MAX_BODY_CHARS):
    parser = _HTMLText()
    parser.feed(value)
    parser.close()
    return normalize_text(''.join(parser.text), limit=limit)
