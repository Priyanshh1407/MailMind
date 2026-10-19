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


_TYPOGRAPHIC = str.maketrans({
    '‘': "'", '’': "'", '‚': "'", '‛': "'", '′': "'",
    '“': '"', '”': '"', '„': '"', '″': '"',
    '–': '-', '—': '-', '−': '-', ' ': ' ', '…': '...',
})


def grounding_key(value):
    """Text for 'is this quote in the email?': folds case, whitespace and
    typographic punctuation (curly quotes, dashes, ellipsis). Words must match."""
    return re.sub(r'\s+', ' ', normalize_text(value).translate(_TYPOGRAPHIC).casefold())


def quote_in_source(quote, source):
    """The single grounding rule shared by explanations and actions."""
    key = grounding_key(quote).strip(' \'".')
    return bool(key) and key in grounding_key(source)


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


MAX_LINKS = 50
MAX_LINK_HREF_CHARS = 2048
MAX_LINK_TEXT_CHARS = 200
MAX_HIDDEN_TEXT_CHARS = 1000
_VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}
# Inline styles that hide text from a reader but not from a parser.
_HIDDEN_STYLE = re.compile(
    r'display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:\.0+)?(?:px|pt|em|rem|%)?\s*(?:;|$)'
    r'|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)|max-height\s*:\s*0(?:px)?\s*(?:;|$)', re.I)
_PLAIN_URL = re.compile(r'(?i)\bhttps?://[^\s<>"\'()\[\]]+')


class _HTMLText(HTMLParser):
    ignored = {'script', 'style', 'head', 'template', 'svg', 'noscript'}
    blocks = {'p', 'div', 'br', 'hr', 'li', 'tr', 'td', 'th', 'pre', 'h1', 'h2', 'h3', 'h4', 'table', 'section', 'blockquote'}

    def __init__(self, drop_hidden=False):
        super().__init__(convert_charrefs=True)
        self.drop_hidden = drop_hidden
        self.text = []
        self.hidden = []
        # Evidence for scam checks: links (visible text + target) and text a
        # reader can't see. The visible text above stays exactly as before.
        self.links = []
        self.anchor = None
        self.styled_hidden = []
        self.hidden_text = []

    def handle_starttag(self, tag, attrs):
        if tag in self.ignored:
            self.hidden.append(tag)
        if not self.hidden and tag in self.blocks:
            self.text.append('\n')
        values = {name: value or '' for name, value in attrs}
        if (tag not in _VOID_TAGS and not self.hidden
                and ('hidden' in values or _HIDDEN_STYLE.search(values.get('style', '')))):
            self.styled_hidden.append(tag)
        if tag == 'a' and not self.hidden:
            self._close_anchor()
            href = values.get('href', '').strip()
            if href:
                self.anchor = (href[:MAX_LINK_HREF_CHARS], [])

    def handle_startendtag(self, tag, attrs):
        if not self.hidden and tag in self.blocks:
            self.text.append('\n')

    def handle_endtag(self, tag):
        if tag in self.hidden:
            self.hidden = self.hidden[:len(self.hidden)-1-self.hidden[::-1].index(tag)]
        if tag in self.styled_hidden:
            self.styled_hidden = self.styled_hidden[:len(self.styled_hidden)-1-self.styled_hidden[::-1].index(tag)]
        if tag == 'a':
            self._close_anchor()
        if not self.hidden and tag in self.blocks:
            self.text.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            if self.styled_hidden:
                self.hidden_text.append(data)
                if self.drop_hidden:
                    return  # a reader cannot see it, so the AI is not shown it either
            self.text.append(data)
            if self.anchor is not None:
                self.anchor[1].append(data)

    def _close_anchor(self):
        if self.anchor is not None and len(self.links) < MAX_LINKS:
            href, parts = self.anchor
            text = ' '.join(''.join(parts).split())[:MAX_LINK_TEXT_CHARS]
            self.links.append({'text': text, 'href': href})
        self.anchor = None

    def close(self):
        super().close()
        self._close_anchor()


def html_to_text(value, *, limit=MAX_BODY_CHARS, drop_hidden=False):
    """Visible text of an HTML body. With drop_hidden, text styled to be
    invisible (display:none, zero font size, ...) is left out: a defense
    against instructions hidden from the reader but not from a model."""
    parser = _HTMLText(drop_hidden)
    parser.feed(value)
    parser.close()
    return normalize_text(''.join(parser.text), limit=limit)


def html_evidence(value):
    """Links and hidden text of an HTML body: ([{text, href}], hidden_text)."""
    parser = _HTMLText()
    parser.feed(value)
    parser.close()
    hidden = normalize_text(' '.join(parser.hidden_text), limit=MAX_HIDDEN_TEXT_CHARS)
    return parser.links, hidden


def plain_links(value):
    """Bare URLs in a plain-text body; their visible text is the URL itself."""
    links = []
    for match in _PLAIN_URL.finditer(value or ''):
        href = match.group(0).rstrip('.,;:!?')[:MAX_LINK_HREF_CHARS]
        links.append({'text': href[:MAX_LINK_TEXT_CHARS], 'href': href})
        if len(links) >= MAX_LINKS:
            break
    return links
