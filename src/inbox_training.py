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


LABEL_POLICY = "explicit_human_feedback_else_non_local_approved_prediction"


def _label_source(stored_row):
    """Who produced this label: a person, a cloud model, or an unrecorded origin."""
    if stored_row["human_label"] is not None:
        return "human"
    source = stored_row["prediction_source"]
    if source is None:
        return "unrecorded"
    return "local" if source == "local" else "cloud"


def load_user_approved_rows(db_path):
    """Prefer explicit feedback, then use approved predictions from other models.

    email_logs.prediction keeps the first successful decision. In local-only
    mode that decision is the local model's own output, so it is excluded:
    training on it would teach the model its own mistakes.
    """
    with connection(db_path) as conn:
        stored = conn.execute(
            """SELECT sender,subject,body,human_label,
                      COALESCE(human_label,prediction) AS approved_label,
                      (SELECT p.source FROM prediction_attempts p
                       WHERE p.account_id=e.account_id AND p.email_id=e.email_id
                         AND p.category IS NOT NULL
                       ORDER BY p.attempt_id LIMIT 1) AS prediction_source
               FROM email_logs e
               WHERE COALESCE(human_label,prediction) IS NOT NULL
               ORDER BY account_id,email_id"""
        ).fetchall()

    grouped = defaultdict(list)
    excluded_local = 0
    for stored_row in stored:
        subject = normalize_subject(stored_row["subject"])
        body = normalize_text(stored_row["body"])
        label = stored_row["approved_label"]
        if label not in LABEL2ID or not (subject or body):
            continue
        label_source = _label_source(stored_row)
        if label_source == "local":
            excluded_local += 1
            continue
        sender = normalize_text(stored_row["sender"], limit=1000)
        text = format_model_text(sender, subject, body)
        grouped[text.casefold()].append({
            "sender": sender,
            "subject": subject,
            "body": body,
            "human_label": label,
            "label_source": label_source,
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
        "label_policy": LABEL_POLICY,
        "label_provenance": dict(sorted(Counter(
            row["label_source"] for row in rows).items())),
        "excluded_local_self_labels": excluded_local,
    }
    return rows, summary


def human_labelled_subset(rows, predicted):
    """Truth/prediction pairs for rows a person labelled.

    Other rows mostly carry the cloud classifier's own labels, so metrics over
    them measure agreement with that classifier, not accuracy.
    """
    pairs = [(row["human_label"], value) for row, value in zip(rows, predicted, strict=True)
             if row["label_source"] == "human"]
    return [truth for truth, _ in pairs], [value for _, value in pairs]


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
