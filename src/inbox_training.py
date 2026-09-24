"""Prepare a private, user-approved inbox dataset without exporting email text."""
from collections import Counter, defaultdict
from hashlib import sha256
import re

from .benchmark_data import digest, group_splits
from .database import connection
from .email_text import format_model_text, normalize_subject, normalize_text
from .prediction import LABEL2ID


def _identity(value):
    return sha256(value.encode("utf-8")).hexdigest()


def _template_group(sender, subject):
    """Group repeated sender/subject templates without isolating an entire sender."""
    from email.utils import parseaddr
    address = parseaddr(sender)[1].casefold()
    domain = address.rsplit("@", 1)[-1] if "@" in address else address
    template = normalize_subject(subject).casefold()
    template = re.sub(r"^(?:(?:re|fw|fwd)\s*:\s*)+", "", template)
    template = re.sub(r"https?://\S+|\b\S+@\S+\b", " <address> ", template)
    template = re.sub(r"\b(?:\d[\s._:/-]*){2,}\b", " <number> ", template)
    template = re.sub(r"\s+", " ", template).strip()
    return _identity("template:" + domain + "|" + template)


def load_user_approved_rows(db_path):
    """Prefer explicit feedback, then use predictions approved by the mailbox owner."""
    with connection(db_path) as conn:
        stored = conn.execute(
            """SELECT sender,subject,body,COALESCE(human_label,prediction) AS approved_label
               FROM email_logs
               WHERE COALESCE(human_label,prediction) IS NOT NULL
               ORDER BY account_id,email_id"""
        ).fetchall()

    grouped = defaultdict(list)
    for stored_row in stored:
        subject = normalize_subject(stored_row["subject"])
        body = normalize_text(stored_row["body"])
        label = stored_row["approved_label"]
        if label not in LABEL2ID or not (subject or body):
            continue
        sender = normalize_text(stored_row["sender"], limit=1000)
        text = format_model_text(sender, subject, body)
        grouped[text.casefold()].append({
            "sender": sender,
            "subject": subject,
            "body": body,
            "human_label": label,
        })

    rows = []
    conflicting_groups = 0
    removed = 0
    for text_key, matches in sorted(grouped.items()):
        if len({row["human_label"] for row in matches}) != 1:
            conflicting_groups += 1
            removed += len(matches)
            continue
        selected = matches[0]
        removed += len(matches) - 1
        rows.append({
            **selected,
            "id": _identity("text:" + text_key),
            "group_id": _template_group(selected["sender"], selected["subject"]),
        })

    counts = Counter(row["human_label"] for row in rows)
    summary = {
        "stored_labelled_rows": len(stored),
        "usable_unique_rows": len(rows),
        "class_counts": dict(sorted(counts.items())),
        "duplicate_or_conflicting_rows_removed": removed,
        "conflicting_text_groups_removed": conflicting_groups,
        "dataset_hash": digest([{
            "text_hash": row["id"],
            "group_hash": row["group_id"],
            "label": row["human_label"],
        } for row in rows]),
        "label_policy": "explicit_human_feedback_else_user_approved_prediction",
    }
    return rows, summary


def prepare_user_approved_splits(db_path, seed=42):
    rows, summary = load_user_approved_rows(db_path)
    splits = group_splits(rows, seed)
    summary["split_counts"] = {
        name: dict(sorted(Counter(row["human_label"] for row in entries).items()))
        for name, entries in splits.items()
    }
    summary["grouping"] = "sender_domain_subject_template_with_full_model_input_deduplication"
    summary["seed"] = seed
    return splits, summary
