"""Best-effort minimization, not guaranteed anonymization."""
import re


def redact(text):
    if not isinstance(text,str): return ''
    text=re.sub(r'(?i)\b(?:https?://|www\.)[^\s<>]+','[URL]',text)
    text=re.sub(r'(?<![\w])(?:\$|₹|\bRs\.?|\bINR|\bUSD)\s*\d[\d,]*(?:\.\d+)?','[AMOUNT]',text,flags=re.I)
    text=re.sub(r'(?i)(?<![\w.+-])[\w.+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+(?![\w.-])','[EMAIL]',text)
    text=re.sub(r'(?i)(?<![\w.+-])[\w.+-]+@[a-z0-9_-]+(?![\w.-])','[UPI_ID]',text)
    text=re.sub(r'\b[A-Z]{5}\d{4}[A-Z]\b','[PAN_ID]',text,flags=re.I)
    text=re.sub(r'(?<!\w)(?:\+?\d[\d ()-]{6,}\d)(?!\w)','[ACCOUNT_NUM]',text)
    return text


def external_email(sender,subject,body):
    # Display names are arbitrary identifiers; omit the sender entirely.
    return {'sender':'[SENDER]', 'subject':redact(subject),'body':redact(body)}


def provider_email_text(subject,body):
    """The exact email text a cloud provider sees.

    Provider evidence quotes this minimized text (for example '[AMOUNT]'), so
    anything that grounds provider output must check against it, not the
    original email.
    """
    from .email_text import format_email_text
    minimized=external_email('',subject,body)
    return format_email_text(minimized['subject'],minimized['body'])
