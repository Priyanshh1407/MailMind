"""Your corrections win for similar mail.

A correction is matched when a new email is close to one you corrected:
- from the same sender and within cosine distance SAME_SENDER_DISTANCE, or
- a near-copy from anyone, within NEAR_COPY_DISTANCE.

Thresholds come from the owner's own inbox (October 2026, MiniLM embeddings):
- same-sender look-alikes: median distance 0.28; different senders (300-pair
  sample): never closer than 0.32, well above the near-copy limit of 0.20;
- leave-one-out over 83 emails the owner labelled: at 0.40 the rule covered 55
  and matched the owner's own label on 52 (95%); at 0.45 accuracy fell to 90%.
  The remaining misses were near-identical emails the owner labelled differently.
"""
from email.utils import parseaddr

from .prediction import Category

SAME_SENDER_DISTANCE = 0.40
NEAR_COPY_DISTANCE = 0.20
# How far retrieval looks for corrections; also the widest rule above.
MAX_CORRECTION_DISTANCE = SAME_SENDER_DISTANCE


def sender_address(sender):
    """'Shop <Deals@Shop.test>' -> 'deals@shop.test'; '' when there is none."""
    if not isinstance(sender, str):
        return ''
    return parseaddr(sender)[1].strip().lower()


def match_correction(neighbors, sender, sender_of):
    """The nearest verified correction that should decide this email, or None.

    `neighbors` are corrections already checked against your current labels;
    `sender_of(email_id)` returns the corrected email's sender from SQLite.
    """
    address = sender_address(sender)
    candidates = []
    for item in neighbors or ():
        distance = item.get('distance') if isinstance(item, dict) else None
        if (type(distance) not in (int, float) or not 0 <= distance <= MAX_CORRECTION_DISTANCE
                or item.get('label') not in tuple(category.value for category in Category)):
            continue
        candidates.append(item)
    for item in sorted(candidates, key=lambda row: row['distance']):
        if item['distance'] <= NEAR_COPY_DISTANCE:
            return item
        if address and sender_address(sender_of(item.get('email_id'))) == address:
            return item
    return None
