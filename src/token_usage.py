"""Privacy-safe, account-scoped token event recording and aggregation."""
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .database import connection, utc_timestamp
from .email_text import normalize_text
from .intelligence_contract import (
    CLOUD_BILLED_TOKEN_PROVIDERS,
    DEFAULT_TIMEZONE,
    LOCAL_PROCESSED_TOKEN_PROVIDERS,
    TokenCountMethod,
    TokenOperation,
    TokenOutcome,
    TokenProvider,
)


MAX_TOKEN_COUNT = 1_000_000_000
MAX_USAGE_EVENTS_PER_READ = 500
EMBEDDING_MODEL_VERSION = "chroma-default-embedding-v1"
_METADATA_KEYS = {"document_count", "cached_tokens", "thinking_tokens"}


class TokenRequestConflict(Exception):
    pass


@dataclass(frozen=True)
class TokenMeasurement:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    count_method: str = TokenCountMethod.UNAVAILABLE.value
    metadata: dict | None = None


def failure_outcome(error):
    """Map an interrupted model call to the token-event outcome it represents."""
    from .account_state import WorkCancelled
    if isinstance(error, WorkCancelled):
        return TokenOutcome.CANCELLED.value
    if isinstance(error, TimeoutError):
        return TokenOutcome.TIMEOUT.value
    return TokenOutcome.FAILED.value


def unavailable_measurement():
    return TokenMeasurement()


def _usage_field(value, *names):
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if not isinstance(value, dict) and hasattr(value, name):
            return getattr(value, name)
    return None


def _reported_measurement(usage, *, input_names, output_names,
                          total_names, cached_names=(), thinking_names=()):
    if usage is None:
        return unavailable_measurement()
    raw_input = _usage_field(usage, *input_names)
    raw_output = _usage_field(usage, *output_names)
    raw_total = _usage_field(usage, *total_names)
    raw_cached = _usage_field(usage, *cached_names) if cached_names else None
    raw_thinking = _usage_field(usage, *thinking_names) if thinking_names else None
    values = (raw_input, raw_output, raw_total, raw_cached, raw_thinking)
    if any(value is not None and (
        type(value) is not int or not 0 <= value <= MAX_TOKEN_COUNT
    ) for value in values):
        return unavailable_measurement()
    if raw_input is None and raw_output is None and raw_total is None:
        return unavailable_measurement()
    if raw_total is None and raw_input is not None and raw_output is not None:
        raw_total = raw_input + raw_output
    known = [value for value in (raw_input, raw_output) if value is not None]
    if raw_total is not None and known and raw_total < sum(known):
        return unavailable_measurement()
    metadata = {}
    if raw_cached is not None:
        metadata["cached_tokens"] = raw_cached
    if raw_thinking is not None:
        metadata["thinking_tokens"] = raw_thinking
    return TokenMeasurement(
        input_tokens=raw_input,
        output_tokens=raw_output,
        total_tokens=raw_total,
        count_method=TokenCountMethod.PROVIDER_REPORTED.value,
        metadata=metadata,
    )


def gemini_usage_measurement(response):
    usage = _usage_field(response, "usage_metadata", "usageMetadata")
    return _reported_measurement(
        usage,
        input_names=("prompt_token_count", "promptTokenCount"),
        output_names=("candidates_token_count", "candidatesTokenCount"),
        total_names=("total_token_count", "totalTokenCount"),
        cached_names=("cached_content_token_count", "cachedContentTokenCount"),
        thinking_names=("thoughts_token_count", "thoughtsTokenCount"),
    )


def groq_usage_measurement(payload):
    usage = _usage_field(payload, "usage")
    measurement = _reported_measurement(
        usage,
        input_names=("prompt_tokens",),
        output_names=("completion_tokens",),
        total_names=("total_tokens",),
    )
    if measurement.count_method == TokenCountMethod.UNAVAILABLE.value:
        return measurement
    metadata = dict(measurement.metadata or {})
    prompt_details = _usage_field(usage, "prompt_tokens_details")
    completion_details = _usage_field(usage, "completion_tokens_details")
    cached = _usage_field(prompt_details, "cached_tokens")
    thinking = _usage_field(completion_details, "reasoning_tokens")
    for key, value in (("cached_tokens", cached), ("thinking_tokens", thinking)):
        if value is not None:
            if type(value) is not int or not 0 <= value <= MAX_TOKEN_COUNT:
                return unavailable_measurement()
            metadata[key] = value
    return TokenMeasurement(
        input_tokens=measurement.input_tokens,
        output_tokens=measurement.output_tokens,
        total_tokens=measurement.total_tokens,
        count_method=measurement.count_method,
        metadata=metadata,
    )


def tokenizer_usage_measurement(input_tokens):
    count = _count(input_tokens, "input_tokens")
    if count is None:
        return unavailable_measurement()
    return TokenMeasurement(
        input_tokens=count,
        output_tokens=0,
        total_tokens=count,
        count_method=TokenCountMethod.TOKENIZER_COUNTED.value,
        metadata={},
    )


def estimate_text_tokens(values):
    if isinstance(values, str):
        documents = [values]
    elif isinstance(values, (list, tuple)) and values and all(
        isinstance(value, str) for value in values
    ):
        documents = list(values)
    else:
        raise ValueError("Text token estimation requires non-empty text")
    count = sum(max(1, (len(value.encode("utf-8")) + 3) // 4) for value in documents)
    if count > MAX_TOKEN_COUNT:
        raise ValueError("Estimated token count exceeds the supported limit")
    return TokenMeasurement(
        input_tokens=count,
        output_tokens=0,
        total_tokens=count,
        count_method=TokenCountMethod.ESTIMATED.value,
        metadata={"document_count": len(documents)},
    )


def usage_request_prefix(namespace, *parts):
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("Usage namespace is required")
    payload = json.dumps(
        [namespace, *[str(part) for part in parts]],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "usage:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TokenRecorder:
    """Best-effort recorder that never changes inference behavior."""

    def __init__(self, account_id, request_prefix, *, email_id=None,
                 job_id=None, db_path=None, enabled=True):
        self.account_id = account_id
        self.request_prefix = request_prefix
        self.email_id = email_id
        self.job_id = job_id
        self.db_path = db_path
        self.enabled = bool(enabled and account_id and request_prefix)

    def record(self, attempt, *, provider, model_version, operation, outcome,
               measurement=None):
        if not self.enabled:
            return None
        measurement = measurement or unavailable_measurement()
        request_id = usage_request_prefix(
            self.request_prefix, attempt, provider, model_version, operation
        )
        try:
            return record_token_usage(
                account_id=self.account_id,
                email_id=self.email_id,
                job_id=self.job_id,
                request_id=request_id,
                provider=provider,
                model_version=model_version,
                operation=operation,
                input_tokens=measurement.input_tokens,
                output_tokens=measurement.output_tokens,
                total_tokens=measurement.total_tokens,
                count_method=measurement.count_method,
                outcome=outcome,
                metadata=measurement.metadata,
                db_path=self.db_path,
            )
        except Exception:
            return None


def _database(db_conn, db_path):
    return nullcontext(db_conn) if db_conn is not None else connection(db_path)


def _required_text(value, name, limit):
    text = normalize_text(value, limit=limit + 1)
    if not text or len(text) > limit:
        raise ValueError(f"Invalid {name}")
    return text


def _count(value, name):
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_TOKEN_COUNT:
        raise ValueError(f"Invalid {name}")
    return value


def _metadata(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or not set(value).issubset(_METADATA_KEYS):
        raise ValueError("Token metadata contains unsupported fields")
    result = {}
    for key, item in value.items():
        if type(item) is not int or not 0 <= item <= MAX_TOKEN_COUNT:
            raise ValueError("Token metadata values must be bounded integers")
        if key == "document_count" and item < 1:
            raise ValueError("document_count must be positive")
        result[key] = item
    return result


def _validate_provider_operation(provider, operation):
    if provider == TokenProvider.EMBEDDING.value:
        if operation not in (
            TokenOperation.DOCUMENT_EMBEDDING.value,
            TokenOperation.QUERY_EMBEDDING.value,
        ):
            raise ValueError("Embedding provider requires an embedding operation")
    elif operation in (
        TokenOperation.DOCUMENT_EMBEDDING.value,
        TokenOperation.QUERY_EMBEDDING.value,
    ):
        raise ValueError("Embedding operations require the embedding provider")
    if provider in (TokenProvider.GEMINI.value, TokenProvider.GROQ.value) and operation == TokenOperation.LOCAL_SHADOW.value:
        raise ValueError("Cloud providers cannot record local shadow usage")


def record_token_usage(*, account_id, request_id, provider, model_version,
                       operation, count_method, outcome, input_tokens=None,
                       output_tokens=None, total_tokens=None, email_id=None,
                       job_id=None, metadata=None, created_at=None,
                       db_path=None, db_conn=None):
    account_id = _required_text(account_id, "account_id", 320)
    request_id = _required_text(request_id, "request_id", 160)
    model_version = _required_text(model_version, "model_version", 160)
    provider = TokenProvider(provider).value
    operation = TokenOperation(operation).value
    count_method = TokenCountMethod(count_method).value
    outcome = TokenOutcome(outcome).value
    _validate_provider_operation(provider, operation)
    input_tokens = _count(input_tokens, "input_tokens")
    output_tokens = _count(output_tokens, "output_tokens")
    total_tokens = _count(total_tokens, "total_tokens")
    counts = (input_tokens, output_tokens, total_tokens)
    if count_method == TokenCountMethod.UNAVAILABLE.value:
        if any(value is not None for value in counts):
            raise ValueError("Unavailable token usage cannot contain counts")
    elif all(value is None for value in counts):
        raise ValueError("Known token usage requires at least one count")
    if total_tokens is not None:
        known_parts = [value for value in (input_tokens, output_tokens) if value is not None]
        if known_parts and total_tokens < sum(known_parts):
            raise ValueError("total_tokens cannot be lower than its known components")
    metadata = _metadata(metadata)
    if email_id is not None:
        email_id = _required_text(email_id, "email_id", 256)
    if job_id is not None and (type(job_id) is not int or job_id < 1):
        raise ValueError("Invalid job_id")
    stamp = utc_timestamp(created_at)

    with _database(db_conn, db_path) as conn:
        if not conn.execute(
            "SELECT 1 FROM accounts WHERE account_id=?", (account_id,)
        ).fetchone():
            raise LookupError("Unknown account")
        if email_id is not None and not conn.execute(
            "SELECT 1 FROM email_logs WHERE account_id=? AND email_id=?",
            (account_id, email_id),
        ).fetchone():
            raise LookupError("Email does not belong to this account")
        if job_id is not None and not conn.execute(
            "SELECT 1 FROM worker_jobs WHERE account_id=? AND job_id=?",
            (account_id, job_id),
        ).fetchone():
            raise LookupError("Job does not belong to this account")
        inserted = conn.execute("""INSERT INTO token_usage_events(
            account_id,email_id,job_id,request_id,provider,model_version,
            operation,input_tokens,output_tokens,total_tokens,count_method,
            outcome,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(request_id) DO NOTHING""", (
                account_id, email_id, job_id, request_id, provider, model_version,
                operation, input_tokens, output_tokens, total_tokens, count_method,
                outcome, json.dumps(metadata, separators=(",", ":")), stamp,
            )).rowcount
        row = conn.execute(
            "SELECT * FROM token_usage_events WHERE account_id=? AND request_id=?",
            (account_id, request_id),
        ).fetchone()
        if row is None:
            raise TokenRequestConflict("Token request ID is already owned")
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        result["inserted"] = bool(inserted)
        return result


def list_token_usage(account_id, *, limit=100, offset=0, db_path=None,
                     db_conn=None):
    if type(limit) is not int or not 1 <= limit <= MAX_USAGE_EVENTS_PER_READ:
        raise ValueError("Invalid token event limit")
    if type(offset) is not int or not 0 <= offset <= 1_000_000:
        raise ValueError("Invalid token event offset")
    with _database(db_conn, db_path) as conn:
        rows = conn.execute("""SELECT * FROM token_usage_events
            WHERE account_id=? ORDER BY created_at DESC,usage_id DESC
            LIMIT ? OFFSET ?""", (account_id, limit, offset)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            result.append(item)
        return result


def usage_window(window, *, timezone_name=DEFAULT_TIMEZONE, now=None):
    if window not in ("day", "week", "month"):
        raise ValueError("Window must be day, week, or month")
    if not isinstance(timezone_name, str) or not 1 <= len(timezone_name) <= 80:
        raise ValueError("Invalid IANA timezone")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
        raise ValueError("Invalid IANA timezone") from error
    current = now or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError("Current time must include a timezone")
    local = current.astimezone(zone)
    if window == "day":
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif window == "week":
        start = (local - timedelta(days=local.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end = start + timedelta(days=7)
    else:
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(
            year=start.year + (1 if start.month == 12 else 0),
            month=1 if start.month == 12 else start.month + 1,
        )
    return utc_timestamp(start), utc_timestamp(end)


def _breakdown(conn, account_id, start, end, column):
    allowed = {"provider", "operation", "count_method", "outcome"}
    if column not in allowed:
        raise ValueError("Invalid token breakdown")
    rows = conn.execute(f"""SELECT {column} AS key,COUNT(*) AS event_count,
        COALESCE(SUM(input_tokens),0) AS input_tokens,
        COALESCE(SUM(output_tokens),0) AS output_tokens,
        COALESCE(SUM(total_tokens),0) AS total_tokens,
        SUM(total_tokens IS NULL) AS unknown_events
        FROM token_usage_events WHERE account_id=? AND created_at>=? AND created_at<?
        GROUP BY {column} ORDER BY {column}""", (account_id, start, end)).fetchall()
    return [dict(row) for row in rows]


def _daily_trend(conn, account_id, start, end, timezone_name):
    zone = ZoneInfo(timezone_name)
    first = datetime.fromisoformat(start.replace('Z', '+00:00')).astimezone(zone)
    finish = datetime.fromisoformat(end.replace('Z', '+00:00')).astimezone(zone)
    rows = []
    cursor = first
    while cursor < finish:
        next_cursor = min(cursor + timedelta(days=1), finish)
        day_start = utc_timestamp(cursor)
        day_end = utc_timestamp(next_cursor)
        totals = dict(conn.execute(
            '''SELECT COUNT(*) AS event_count,
            COALESCE(SUM(input_tokens),0) AS input_tokens,
            COALESCE(SUM(output_tokens),0) AS output_tokens,
            COALESCE(SUM(total_tokens),0) AS total_tokens,
            COALESCE(SUM(total_tokens IS NULL),0) AS unknown_events,
            COALESCE(SUM(CASE WHEN provider IN ('gemini','groq')
                THEN total_tokens ELSE 0 END),0) AS provider_billed_tokens,
            COALESCE(SUM(CASE WHEN provider IN ('local','embedding')
                THEN total_tokens ELSE 0 END),0) AS local_processed_tokens
            FROM token_usage_events
            WHERE account_id=? AND created_at>=? AND created_at<?''',
            (account_id, day_start, day_end),
        ).fetchone())
        rows.append({
            'date': cursor.date().isoformat(),
            'start_at': day_start,
            'end_at': day_end,
            **totals,
        })
        cursor = next_cursor
    return rows


def aggregate_token_usage(account_id, *, window="day",
                          timezone_name=DEFAULT_TIMEZONE, now=None,
                          db_path=None, db_conn=None):
    start, end = usage_window(window, timezone_name=timezone_name, now=now)
    with _database(db_conn, db_path) as conn:
        total = dict(conn.execute("""SELECT COUNT(*) AS event_count,
            COALESCE(SUM(input_tokens),0) AS input_tokens,
            COALESCE(SUM(output_tokens),0) AS output_tokens,
            COALESCE(SUM(total_tokens),0) AS total_tokens,
            SUM(total_tokens IS NULL) AS unknown_events
            FROM token_usage_events WHERE account_id=? AND created_at>=? AND created_at<?""",
            (account_id, start, end)).fetchone())
        providers = _breakdown(conn, account_id, start, end, "provider")
        provider_totals = {row["key"]: row["total_tokens"] for row in providers}
        total["unknown_events"] = total["unknown_events"] or 0
        return {
            "window": window,
            "timezone": timezone_name,
            "start_at": start,
            "end_at": end,
            "totals": total,
            "provider_billed_tokens": sum(
                provider_totals.get(key, 0) for key in CLOUD_BILLED_TOKEN_PROVIDERS
            ),
            "local_processed_tokens": sum(
                provider_totals.get(key, 0) for key in LOCAL_PROCESSED_TOKEN_PROVIDERS
            ),
            "providers": providers,
            "operations": _breakdown(conn, account_id, start, end, "operation"),
            "count_methods": _breakdown(conn, account_id, start, end, "count_method"),
            "outcomes": _breakdown(conn, account_id, start, end, "outcome"),
            'daily': _daily_trend(
                conn, account_id, start, end, timezone_name),
        }
