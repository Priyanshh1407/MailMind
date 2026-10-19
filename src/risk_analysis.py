"""Explainable scam and phishing checks for one saved email.

Deterministic and local: no provider, no network. Every finding names the rule
that fired and quotes the evidence (a header verdict, a domain, a link or a
sentence), and the score is a small logistic model over those findings whose
weights are fitted on the synthetic benchmark (scripts/evaluate_risk.py) and
saved in src/risk_weights.json. Nothing is a black box: the report shows each
finding's contribution.
"""
from dataclasses import dataclass
from email.utils import parseaddr
import ipaddress
import json
import math
from pathlib import Path
import re
import unicodedata

from .email_signals import domain_of, link_domain
from .email_text import normalize_text

WEIGHTS_PATH = Path(__file__).with_name('risk_weights.json')
MAX_FINDINGS = 12
MAX_EVIDENCE_CHARS = 200
HIGH_BAND, MEDIUM_BAND = 70, 40

# Second-level suffixes where the registrable name sits one label deeper.
_MULTI_SUFFIXES = {
    'co.in', 'net.in', 'org.in', 'gov.in', 'ac.in', 'edu.in', 'res.in', 'nic.in', 'bank.in', 'firm.in', 'gen.in', 'ind.in',
    'co.uk', 'org.uk', 'ac.uk', 'gov.uk', 'com.au', 'net.au', 'org.au', 'co.nz', 'com.sg', 'com.my', 'co.jp',
    'com.br', 'com.mx', 'co.za', 'com.cn', 'com.hk', 'co.id', 'com.tr', 'com.pk', 'com.bd', 'com.np', 'co.ke',
}
FREEMAIL = {'gmail.com', 'googlemail.com', 'yahoo.com', 'yahoo.co.in', 'outlook.com', 'hotmail.com', 'live.com',
            'rediffmail.com', 'aol.com', 'icloud.com', 'proton.me', 'protonmail.com', 'zoho.com', 'gmx.com', 'yandex.com',
            'mail.com'}
SHORTENERS = {'bit.ly', 'tinyurl.com', 't.co', 'goo.gl', 'is.gd', 'cutt.ly', 'rb.gy', 'ow.ly', 'shorturl.at', 'tiny.cc',
              'rebrand.ly', 'bitly.com', 'v.gd', 'shorturl.com', 't.ly', 'lnkd.in'}
# Official domains of brands Indian users are most often impersonated as,
# primary domain first (it is the one a report says a lookalike resembles).
# A sender is a lookalike when its name resembles one of these but its
# registrable domain is none of them.
BRANDS = {
    'sbi': ('sbi.co.in', 'onlinesbi.sbi', 'sbi.bank.in', 'sbicard.com', 'onlinesbi.com'),
    'hdfc': ('hdfcbank.com', 'hdfcbank.net', 'hdfcbank.bank.in', 'hdfc.com', 'hdfclife.com', 'hdfcergo.com'),
    'icici': ('icicibank.com', 'icici.bank.in', 'icicidirect.com', 'icicilombard.com', 'iciciprulife.com'),
    'axis': ('axisbank.com', 'axis.bank.in', 'axisbank.co.in'),
    'kotak': ('kotak.com', 'kotak.bank.in', 'kotaksecurities.com'),
    'paytm': ('paytm.com', 'paytmbank.com', 'paytmmoney.com', 'paytm.in'),
    'phonepe': ('phonepe.com',),
    'gpay': ('google.com',),
    'google': ('google.com', 'gmail.com', 'youtube.com', 'googlemail.com', 'accounts.google.com'),
    'amazon': ('amazon.in', 'amazon.com', 'amazonpay.in', 'amazon.co.uk', 'amazonaws.com'),
    'flipkart': ('flipkart.com',),
    'npci': ('npci.org.in',),
    'incometax': ('incometax.gov.in', 'gov.in'),
    'uidai': ('uidai.gov.in', 'gov.in'),
    'irctc': ('irctc.co.in',),
    'indiapost': ('indiapost.gov.in', 'gov.in'),
    'bluedart': ('bluedart.com',),
    'delhivery': ('delhivery.com',),
    'linkedin': ('linkedin.com',),
    'microsoft': ('microsoft.com', 'outlook.com', 'live.com', 'office.com', 'microsoftonline.com'),
    'apple': ('apple.com', 'icloud.com'),
    'netflix': ('netflix.com',),
    'swiggy': ('swiggy.in', 'swiggy.com'),
    'zomato': ('zomato.com',),
    'airtel': ('airtel.in', 'airtel.com'),
    'jio': ('jio.com',),
    'paypal': ('paypal.com',),
    'naukri': ('naukri.com',),
    'epfo': ('epfindia.gov.in', 'gov.in'),
    'dhl': ('dhl.com', 'dhl.co.in'),
    'fedex': ('fedex.com',),
    'tcs': ('tcs.com',),
    'infosys': ('infosys.com',),
    'wipro': ('wipro.com',),
}
# Visual confusables folded before comparing names (0->o, rn->m, ...).
_CONFUSABLE = (('rn', 'm'), ('vv', 'w'), ('0', 'o'), ('1', 'l'), ('3', 'e'), ('5', 's'), ('@', 'a'), ('$', 's'),
               ('|', 'l'), ('i', 'l'))


@dataclass(frozen=True)
class Finding:
    signal: str
    evidence: str
    detail: str = ''

    def as_dict(self, weight=None):
        value = {'signal': self.signal, 'evidence': self.evidence[:MAX_EVIDENCE_CHARS]}
        if self.detail:
            value['detail'] = self.detail[:MAX_EVIDENCE_CHARS]
        if weight is not None:
            value['weight'] = round(weight, 3)
        return value


def registrable(domain):
    """The name a person registers: 'mail.sbi.co.in' -> 'sbi.co.in'."""
    labels = [label for label in (domain or '').casefold().strip('.').split('.') if label]
    if len(labels) < 2:
        return '.'.join(labels)
    if '.'.join(labels[-2:]) in _MULTI_SUFFIXES and len(labels) >= 3:
        return '.'.join(labels[-3:])
    return '.'.join(labels[-2:])


def _name(domain):
    """Registrable name without its suffix: 'sbi.co.in' -> 'sbi'."""
    value = registrable(domain)
    return value.split('.', 1)[0] if value else ''


# Cyrillic and Greek letters that look like Latin ones ("pаypal" with a
# Cyrillic "а"). Folded before comparing, so they cannot slip past.
_HOMOGLYPHS = str.maketrans({
    'а': 'a', 'е': 'e', 'о': 'o', 'р': 'p', 'с': 'c', 'у': 'y', 'х': 'x', 'і': 'i', 'ј': 'j', 'ѕ': 's',
    'ԁ': 'd', 'ӏ': 'l', 'һ': 'h', 'ԛ': 'q', 'ԝ': 'w', 'ɡ': 'g', 'ο': 'o', 'α': 'a', 'ν': 'v', 'ρ': 'p',
    'τ': 't', 'κ': 'k', 'ι': 'i', 'ε': 'e', 'μ': 'u',
})


def _skeleton(value):
    value = unicodedata.normalize('NFKD', value.casefold().translate(_HOMOGLYPHS))
    value = ''.join(c for c in value if not unicodedata.combining(c))
    for source, target in _CONFUSABLE:
        value = value.replace(source, target)
    return re.sub(r'[^a-z]', '', value)


def _distance(left, right, limit=2):
    """Levenshtein distance, stopping early once it exceeds `limit`."""
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a != b)))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def lookalike_of(domain, trusted=()):
    """The brand or trusted domain this domain imitates, or None.

    A domain is never a lookalike of itself: official brand domains and the
    user's own trusted senders pass. Otherwise it imitates a brand when its
    name contains the brand as a hyphen/dot-separated word (sbi-kyc.in), or
    when its name is one edit or one visual confusable away (hdfcbamk.com,
    paytrn.com).
    """
    domain = (domain or '').casefold()
    base = registrable(domain)
    if not base:
        return None
    trusted_bases = {registrable(item) for item in trusted if item}
    official = {registrable(item) for names in BRANDS.values() for item in names}
    if base in official or base in trusted_bases:
        return None
    name = _name(domain)
    words = [word for word in re.split(r'[-_.]', base.split('.', 1)[0]) if word]
    candidates = {name, *words}
    for brand, example in _targets(tuple(sorted(trusted_bases))):
        if brand in words and len(words) > 1:
            return example  # sbi-kyc.in, hdfcbank-alerts.com
        target = _skeleton(brand)
        for word in candidates:
            if len(brand) >= 4 and _skeleton(word) == target:
                return example  # micros0ft, linkedln, amaz0n
            # One typo away; only for longer names, where it is rarely innocent.
            if len(brand) >= 6 and len(word) >= 6 and _distance(word, brand, 1) <= 1:
                return example  # hdfcbamk, icicibamk
    return None


_TARGET_CACHE = {}


def _targets(trusted_bases):
    """(name to compare, official example) pairs: brand keys, the names of
    their official domains, and the user's own trusted domains."""
    if trusted_bases not in _TARGET_CACHE:
        targets = []
        for brand, names in BRANDS.items():
            example = names[0]
            targets.append((brand, example))
            targets += [(_name(item), example) for item in names if len(_name(item)) >= 5]
        targets += [(_name(item), item) for item in trusted_bases if len(_name(item)) >= 5]
        seen, unique = set(), []
        for item in targets:
            if item[0] and item[0] not in seen:
                seen.add(item[0])
                unique.append(item)
        _TARGET_CACHE.clear()
        _TARGET_CACHE[trusted_bases] = unique
    return _TARGET_CACHE[trusted_bases]


def _ip_host(domain):
    try:
        ipaddress.ip_address(domain.strip('[]'))
        return True
    except ValueError:
        return False


_DOMAIN_IN_TEXT = re.compile(r'(?i)\b((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})\b')
# A request to share a secret, unless the sentence warns against sharing it.
_SECRET = re.compile(
    r'(?i)\b(?:share|send|tell|provide|give|enter|reply with|forward|confirm|batao|bhejo)\b[^.\n]{0,50}?'
    r'\b(?:otp|one[- ]time (?:password|code)|upi pin|atm pin|m-?pin|cvv|password|passcode|net ?banking (?:id|password))\b'
    r'|\b(?:otp|cvv|upi pin|password)\b[^.\n]{0,30}?\b(?:share karo|batao|bhejo|bhej do|share kare)\b')
_WARNING = re.compile(r'(?i)\b(?:do not|don\'t|never|not to|na kare|mat|kabhi)\b[^.\n]{0,40}$')
# How to open a protected statement ("enter your password as: DOB"), not a request to share one.
_OPEN_INSTRUCTION = re.compile(r'(?i)\bto open\b|\bpassword (?:is|as|will be)\b|\bpassword\b[^.\n]{0,15}\bformat\b')
_LURES = {
    'upi_collect_lure': re.compile(
        r'(?i)\b(?:enter|use|type)\b[^.\n]{0,30}\bupi pin\b[^.\n]{0,40}\b(?:receive|get|credit|claim|refund)'
        r'|\bapprove\b[^.\n]{0,30}\b(?:collect|payment) request\b[^.\n]{0,40}\b(?:receive|get|refund|cashback|claim)'
        r'|\b(?:scan|use)\b[^.\n]{0,20}\bqr\b[^.\n]{0,40}\b(?:receive|get) (?:the )?(?:money|payment|amount|refund)'),
    'kyc_lure': re.compile(
        r'(?i)\b(?:kyc|pan|aadhaar)\b[^.\n]{0,60}\b(?:expir\w*|update|pending|suspend\w*|block\w*|incomplete|verify|re-?verify)\b'
        r'|\b(?:update|complete|verify)\b[^.\n]{0,30}\b(?:your )?kyc\b'),
    'account_threat': re.compile(
        r'(?i)\b(?:account|card|sim|wallet|number|services?)\b[^.\n]{0,40}\b(?:will be|has been|is|are|be)\s+'
        r'(?:temporarily\s+)?(?:blocked|suspended|deactivated|frozen|disabled|closed|terminated|band)\b'),
    'prize_lure': re.compile(
        r'(?i)\b(?:you have won|you\'ve won|you are the winner|winner of|lucky draw|lottery|jackpot|claim your (?:prize|reward|gift|cashback)|'
        r'selected for (?:a|an|the) (?:reward|prize|gift)|congratulations[^.\n]{0,40}\b(?:won|winner|selected|prize|reward))\b'),
    'fee_lure': re.compile(
        r'(?i)\b(?:pay|deposit|transfer|send)\b[^.\n]{0,40}\b(?:fee|charges?|deposit|amount)\b[^.\n]{0,50}\b'
        r'(?:release|deliver\w*|parcel|package|shipment|job|joining|offer|refund|claim|unlock|activate)\b'
        r'|\b(?:registration|processing|security|joining|customs|clearance|redelivery) (?:fee|charge|deposit)\b'),
    'gift_card_request': re.compile(
        r'(?i)\b(?:buy|purchase|get|pick up)\b[^.\n]{0,40}\bgift ?cards?\b'
        r'|\b(?:send|share|scratch)\b[^.\n]{0,20}\b(?:the )?(?:gift ?card )?(?:codes?|pins?)\b[^.\n]{0,30}\b(?:me|to me|urgently|immediately)\b'),
    'credential_request': re.compile(
        r'(?i)\b(?:verify|confirm|validate|update|re-?enter)\b[^.\n]{0,25}\b(?:your )?(?:account|identity|login|password|credentials|bank details|card details)\b'
        r'|\b(?:log ?in|sign ?in)\b[^.\n]{0,30}\b(?:to (?:verify|confirm|avoid|restore|unlock|prevent))'),
}
_URGENCY = re.compile(
    r'(?i)\b(?:within (?:the next )?\d{1,2} ?(?:hours?|hrs?|minutes?|mins?)|immediately|urgent(?:ly)?|act now|'
    r'last chance|final (?:notice|warning|reminder)|expires? (?:today|tonight)|today itself|before midnight|'
    r'turant|abhi(?: ke abhi)?)\b')


def _sentence(text, match):
    start = max(text.rfind('.', 0, match.start()), text.rfind('\n', 0, match.start())) + 1
    stops = [index for index in (text.find('.', match.end()), text.find('\n', match.end())) if index != -1]
    end = min(stops) + 1 if stops else len(text)
    return ' '.join(text[start:end].split())[:MAX_EVIDENCE_CHARS]


def content_findings(subject, body):
    """Lures and pressure in the words of the email (works without headers)."""
    text = normalize_text(f'{subject}\n{body}', limit=40000)
    findings = []
    for match in _SECRET.finditer(text):
        before = text[max(0, match.start() - 60):match.start()]
        sentence = _sentence(text, match)
        if (not _WARNING.search(before) and not _WARNING.search(text[match.start():match.start() + 25])
                and not _OPEN_INSTRUCTION.search(sentence)):
            findings.append(Finding('otp_or_pin_request', _sentence(text, match)))
            break
    for signal, pattern in _LURES.items():
        match = pattern.search(text)
        if match:
            findings.append(Finding(signal, _sentence(text, match)))
    match = _URGENCY.search(text)
    if match:
        findings.append(Finding('urgency_pressure', _sentence(text, match)))
    return findings


def header_findings(signals, sender='', trusted=()):
    """Sender authenticity and reply paths, from saved security signals."""
    findings = []
    from_address = (signals or {}).get('from_address') or parseaddr(sender or '')[1].casefold()
    from_domain = (signals or {}).get('from_domain') or domain_of(from_address)
    if signals:
        for method in ('dmarc', 'spf', 'dkim'):
            verdict = signals.get('auth_' + method)
            if verdict in ('fail', 'softfail', 'permerror'):
                findings.append(Finding(f'auth_{method}_fail', f'{method.upper()}={verdict}',
                                        f'Gmail could not verify that {from_domain or "this sender"} sent it'))
        if all(signals.get('auth_' + method) in ('none', 'unknown') for method in ('dmarc', 'spf', 'dkim')):
            findings.append(Finding('auth_missing', 'no SPF, DKIM or DMARC result',
                                    'The sender could not be authenticated at all'))
        reply = signals.get('reply_to_domain') or ''
        if reply and from_domain and registrable(reply) != registrable(from_domain):
            signal = 'reply_to_freemail' if reply in FREEMAIL else 'reply_to_mismatch'
            findings.append(Finding(signal, f'From {from_domain}, replies go to {reply}'))
    display = parseaddr(sender or '')[0].casefold()
    if from_domain in FREEMAIL and display:
        for brand in BRANDS:
            if len(brand) >= 3 and re.search(rf'\b{re.escape(brand)}\b', display) and brand not in ('google', 'gpay'):
                findings.append(Finding('brand_name_on_freemail', f'"{parseaddr(sender)[0]}" <{from_address}>'))
                break
    imitated = lookalike_of(from_domain, trusted)
    if imitated:
        findings.append(Finding('lookalike_sender', from_domain, f'Resembles {imitated}'))
    if from_domain.startswith('xn--') or '.xn--' in from_domain or not from_domain.isascii():
        findings.append(Finding('punycode_domain', from_domain, 'International characters can disguise a domain'))
    return findings


def link_findings(links, trusted=()):
    findings, seen = [], set()
    def add(signal, evidence, detail=''):
        if signal not in seen:
            seen.add(signal)
            findings.append(Finding(signal, evidence, detail))
    for link in links or ():
        href = link.get('href') or ''
        target = link.get('href_domain') or link_domain(href)
        if not target:
            continue
        text = link.get('text') or ''
        shown = [item.casefold() for item in _DOMAIN_IN_TEXT.findall(text)]
        shown = [item for item in shown if '.' in item and not item.replace('.', '').isdigit()]
        if shown and all(registrable(item) != registrable(target) for item in shown):
            add('link_text_mismatch', f'"{text}" opens {target}')
        if registrable(target) in SHORTENERS or target in SHORTENERS:
            add('shortened_link', target, 'A link shortener hides where the link goes')
        if _ip_host(target):
            add('ip_address_link', target)
        if target.startswith('xn--') or '.xn--' in target or not target.isascii():
            add('punycode_domain', target, 'International characters can disguise a domain')
        imitated = lookalike_of(target, trusted)
        if imitated:
            add('lookalike_link', target, f'Resembles {imitated}')
    return findings


def load_weights(path=WEIGHTS_PATH):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict) or not isinstance(value.get('weights'), dict):
        raise ValueError('Invalid risk weights')
    return value


_WEIGHTS = None


def weights():
    global _WEIGHTS
    if _WEIGHTS is None:
        _WEIGHTS = load_weights()
    return _WEIGHTS


def collect_findings(*, sender, subject, body, signals=None, trusted=(), analysis_signals=()):
    """Every rule that fired, once each, in a stable order."""
    findings = header_findings(signals, sender, trusted)
    findings += link_findings((signals or {}).get('links'), trusted)
    findings += content_findings(subject, body)
    if 'credential_request' in set(analysis_signals or ()) and not any(
            item.signal == 'credential_request' for item in findings):
        findings.append(Finding('credential_request', 'the classifier flagged a credential request'))
    if signals and signals.get('hidden_text'):
        findings.append(Finding('hidden_text', signals['hidden_text'], 'Text in the email that a reader cannot see'))
    unique, seen = [], set()
    for item in findings:
        if item.signal not in seen:
            seen.add(item.signal)
            unique.append(item)
    return unique


def score_findings(findings, model):
    logit = float(model.get('bias', 0.0)) + sum(float(model['weights'].get(item.signal, 0.0)) for item in findings)
    score = round(100 / (1 + math.exp(-logit)))
    return min(score, 5) if not findings else score


def assess(*, sender, subject, body, signals=None, trusted=(), analysis_signals=(), model=None):
    """The full report: score 0-100, band, and every finding with its weight."""
    model = model or weights()
    unique = collect_findings(sender=sender, subject=subject, body=body, signals=signals,
                              trusted=trusted, analysis_signals=analysis_signals)
    table = model['weights']
    score = score_findings(unique, model)
    band = 'high' if score >= HIGH_BAND else 'medium' if score >= MEDIUM_BAND else 'low'
    ranked = sorted(unique, key=lambda item: -float(table.get(item.signal, 0.0)))[:MAX_FINDINGS]
    return {
        'risk_version': model.get('version', 'risk-v1'),
        'score': int(score), 'band': band,
        'evidence_level': 'full' if signals else 'content_only',
        'findings': [item.as_dict(float(table.get(item.signal, 0.0))) for item in ranked],
    }


def trusted_domains(conn, account_id):
    """Domains this user relies on: any domain they labelled Important or
    Updates themselves, or one with at least two Important/Updates emails and
    none they or the classifier filed as Spam.

    Trade-off: a repeated scam campaign that keeps being filed as Updates stops
    counting as a lookalike from its third email; its first ones are flagged,
    and every other check still applies.
    """
    good, labelled, spam = {}, set(), set()
    for row in conn.execute(
            'SELECT sender, human_label, prediction FROM email_logs WHERE account_id=?', (account_id,)):
        domain = registrable(domain_of(parseaddr(row['sender'])[1]))
        if not domain or domain in FREEMAIL:
            continue
        category = row['human_label'] or row['prediction']
        if category == 'SPAM':
            spam.add(domain)
        elif category in ('IMPORTANT', 'UPDATES'):
            good[domain] = good.get(domain, 0) + 1
            if row['human_label'] in ('IMPORTANT', 'UPDATES'):
                labelled.add(domain)
    return labelled | {domain for domain, count in good.items() if count >= 2 and domain not in spam}
