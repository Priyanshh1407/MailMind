"""Bounded MIME reader. No attachment downloads, HTML execution, or network."""
import base64
from dataclasses import dataclass, field
from email import message_from_string
from email.header import decode_header
from email.message import Message
from email.policy import default
from .email_text import MAX_BODY_CHARS, MAX_SENDER_CHARS, normalize_text, normalize_subject, html_to_text, preview_text

MAX_DECODED_BYTES = 262144
MAX_PARTS = 200
MAX_DEPTH = 20
MAX_RAW_CHARS = 2_000_000
MAX_INLINE_FETCHES = 2


class MessageParseError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass
class ParsedBody:
    text: str
    kind: str
    warnings: list = field(default_factory=list)
    truncated: bool = False


def _headers(part):
    headers = part.get('headers') or []
    if not isinstance(headers, list):
        raise MessageParseError('malformed_headers')
    result = Message()
    for header in headers[:100]:
        if isinstance(header, dict) and isinstance(header.get('name'), str) and isinstance(header.get('value'), str):
            result[header['name'][:100]] = header['value'][:8192]
    return result


def _decode_bytes(data, charset, warnings):
    charset = charset or 'utf-8'
    try:
        return data.decode(charset, errors='strict')
    except LookupError:
        warnings.append('unknown_charset')
        return data.decode('utf-8', errors='replace')
    except (UnicodeError, TypeError):
        warnings.append('decode_replacement')
        try:
            return data.decode(charset, errors='replace')
        except (LookupError, UnicodeError, TypeError):
            return data.decode('utf-8', errors='replace')


def decode_mail_header(value):
    warnings = []
    try:
        pieces = decode_header(value or '')
        return ''.join(_decode_bytes(p, c, warnings) if isinstance(p, bytes) else p for p, c in pieces)
    except (ValueError, UnicodeError):
        return value or ''


def _is_attachment(part, headers):
    if part.get('filename'):
        return True
    # Duplicate/malformed headers must not hide an attachment indicator.
    for value in headers.get_all('Content-Type', []):
        check = Message()
        check['Content-Type'] = value
        if check.get_param('name'):
            return True
    for value in headers.get_all('Content-Disposition', []):
        check = Message()
        check['Content-Disposition'] = value
        if check.get_content_disposition() == 'attachment' or check.get_filename():
            return True
    return False


def parse_body(payload, *, max_body_chars=MAX_BODY_CHARS, inline_loader=None):
    warnings, visited = [], 0
    mime_limited = False
    inline_fetches = 0
    def visit(part, depth):
        nonlocal visited, mime_limited, inline_fetches
        visited += 1
        if visited > MAX_PARTS or depth > MAX_DEPTH:
            warnings.append('mime_limit')
            mime_limited = True
            return '', 'none', False
        if not isinstance(part, dict):
            warnings.append('malformed_part')
            return '', 'none', False
        try:
            headers = _headers(part)
            mime = str(part.get('mimeType') or headers.get_content_type()).lower()
            if _is_attachment(part, headers):
                return '', 'none', False
            # A forwarded email file is not the outer message's body.
            if mime == 'message/rfc822':
                return '', 'none', False
            children = part.get('parts') or []
            if mime.startswith('multipart/') or children:
                if not isinstance(children, list):
                    raise MessageParseError('malformed_parts')
                if len(children) > MAX_PARTS:
                    children = children[:MAX_PARTS]
                    warnings.append('mime_limit')
                    mime_limited = True
                if mime == 'multipart/related' and headers.get_param('start'):
                    root = headers.get_param('start').strip('<>')
                    def priority(child):
                        try:
                            return 0 if isinstance(child,dict) and str(_headers(child).get('Content-ID','')).strip('<>') == root else 1
                        except MessageParseError:
                            return 1
                    children = sorted(children, key=priority)
                bodies = []
                for child in children:
                    if visited >= MAX_PARTS:
                        warnings.append('mime_limit')
                        mime_limited = True
                        break
                    text, kind, clipped = visit(child, depth+1)
                    if text:
                        bodies.append((text, kind, clipped))
                if mime == 'multipart/alternative':
                    return next((b for b in bodies if b[1] == 'plain'), bodies[0] if bodies else ('', 'none', False))
                if mime == 'multipart/related':
                    return bodies[0] if bodies else ('', 'none', False)
                # Mixed sections may have distinct content. Alternatives above
                # already chose one representation instead of duplicating it.
                return '\n\n'.join(b[0] for b in bodies), 'plain' if any(b[1] == 'plain' for b in bodies) else ('html' if bodies else 'none'), any(b[2] for b in bodies)
            if mime not in ('text/plain', 'text/html'):
                return '', 'none', False
            body = part.get('body') or {}
            if not isinstance(body,dict):
                raise MessageParseError('malformed_body')
            data = body.get('data')
            if not data and body.get('attachmentId') and inline_loader is not None:
                if inline_fetches >= MAX_INLINE_FETCHES:
                    raise MessageParseError('inline_fetch_limit')
                inline_fetches += 1
                data = inline_loader(body['attachmentId'])
            if data is None:
                warnings.append('missing_body_data')
                return '', 'none', False
            if not isinstance(data, str):
                raise MessageParseError('malformed_base64')
            encoded_limit = ((MAX_DECODED_BYTES+2)//3)*4
            clipped = False
            if len(data) > encoded_limit:
                data = data[:encoded_limit]
                warnings.append('body_byte_limit')
                clipped = True
            try:
                decoded = base64.b64decode(data + '='*((-len(data)) % 4), altchars=b'-_', validate=True)
            except (ValueError, base64.binascii.Error):
                raise MessageParseError('malformed_base64') from None
            if len(decoded) > MAX_DECODED_BYTES:
                clipped = True
                warnings.append('body_byte_limit')
            text = _decode_bytes(decoded[:MAX_DECODED_BYTES], headers.get_content_charset(), warnings)
            if mime == 'text/html':
                text = html_to_text(text, limit=MAX_DECODED_BYTES)
            text = normalize_text(text, limit=MAX_DECODED_BYTES)
            # Keep a bounded prefix in each subtree; the final message budget
            # applies again after joining its inline sections.
            if len(text) > max_body_chars:
                clipped = True
                warnings.append('body_character_limit')
            return normalize_text(text, limit=max_body_chars), 'plain' if mime == 'text/plain' else 'html', clipped
        except MessageParseError as error:
            warnings.append(error.code)
            return '', 'none', False
    text, kind, truncated = visit(payload, 0)
    truncated = truncated or mime_limited
    if len(text) > max_body_chars:
        truncated = True
        warnings.append('body_character_limit')
    text = normalize_text(text, limit=max_body_chars)
    if not text:
        raise MessageParseError(warnings[0] if warnings else 'empty_body')
    return ParsedBody(text, kind, sorted(set(warnings)), truncated)


def parse_gmail_message(message, message_id, *, inline_loader=None):
    if not isinstance(message, dict) or not isinstance(message.get('payload'), dict):
        raise MessageParseError('missing_payload')
    payload = message['payload']
    headers = _headers(payload)
    body = parse_body(payload, inline_loader=inline_loader)
    return {'id': message_id,
            'sender': normalize_text(decode_mail_header(headers.get('From', 'Unknown Sender')), limit=MAX_SENDER_CHARS),
            'subject': normalize_subject(decode_mail_header(headers.get('Subject', 'No Subject'))),
            'body': body.text, 'body_snippet': preview_text(body.text), 'body_kind': body.kind,
            'body_truncated': body.truncated, 'parse_warnings': body.warnings}


def parse_raw_email(raw):
    """Training uses the same body selection and normalization as Gmail."""
    if not isinstance(raw, str) or len(raw) > MAX_RAW_CHARS:
        raise MessageParseError('raw_message_limit')
    message = message_from_string(raw, policy=default)
    count = 0
    limited = False
    def convert(part, depth=0):
        nonlocal count, limited
        count += 1
        if count > MAX_PARTS or depth > MAX_DEPTH:
            limited = True
            return {'mimeType':'application/octet-stream'}
        result = {'mimeType':part.get_content_type(), 'filename':part.get_filename() or '',
                  'headers':[{'name':n,'value':str(v)} for n,v in part.items()]}
        if part.is_multipart():
            result['parts'] = []
            for child in part.get_payload():
                if count >= MAX_PARTS:
                    limited = True
                    break
                result['parts'].append(convert(child,depth+1))
        else:
            data = part.get_payload(decode=True) or b''
            # Transfer decoding is done by the stdlib. Retain a bounded body.
            result['body'] = {'data':base64.urlsafe_b64encode(data[:MAX_DECODED_BYTES+3]).decode('ascii')}
        return result
    parsed = parse_gmail_message({'payload':convert(message)}, 'training-row')
    if limited:
        parsed['body_truncated'] = True
        parsed['parse_warnings'] = sorted(set(parsed['parse_warnings'] + ['mime_limit']))
    return parsed
