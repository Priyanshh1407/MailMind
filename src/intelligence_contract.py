"""Frozen product vocabulary for MailMind's integrated intelligence features.

This module is deliberately dependency-free. Later database, provider, worker,
API, and frontend phases must share these wire values instead of defining
feature-specific variants.
"""
from enum import Enum


class ActionType(str, Enum):
    REPLY_REQUIRED = "reply_required"
    APPROVAL_REQUIRED = "approval_required"
    PAYMENT_REQUIRED = "payment_required"
    DOCUMENT_REQUIRED = "document_required"
    MEETING = "meeting"
    REVIEW_REQUIRED = "review_required"
    FOLLOW_UP_REQUIRED = "follow_up_required"
    GENERAL_TASK = "general_task"


class ActionStatus(str, Enum):
    OPEN = "open"
    COMPLETED = "completed"
    DISMISSED = "dismissed"
    SNOOZED = "snoozed"


class DuePrecision(str, Enum):
    EXACT_TIME = "exact_time"
    DATE_ONLY = "date_only"
    RELATIVE = "relative"
    UNKNOWN = "unknown"


class ConfidenceBand(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AnalysisSource(str, Enum):
    GEMINI = "gemini"
    GROQ = "groq"
    LOCAL_HEURISTIC = "local_heuristic"
    SYSTEM = "system"


class ExplanationSignal(str, Enum):
    DIRECT_REQUEST = "direct_request"
    DEADLINE = "deadline"
    APPROVAL_REQUEST = "approval_request"
    PAYMENT_REQUEST = "payment_request"
    DOCUMENT_REQUEST = "document_request"
    MEETING_REQUEST = "meeting_request"
    FOLLOW_UP_REQUEST = "follow_up_request"
    REVIEW_REQUEST = "review_request"
    TRANSACTIONAL_UPDATE = "transactional_update"
    DELIVERY_UPDATE = "delivery_update"
    NO_RESPONSE_REQUESTED = "no_response_requested"
    PROMOTIONAL_CONTENT = "promotional_content"
    UNSOLICITED_CLAIM = "unsolicited_claim"
    CREDENTIAL_REQUEST = "credential_request"
    FEEDBACK_PRECEDENT = "feedback_precedent"
    LOCAL_MODEL_SIGNAL = "local_model_signal"


class ReminderChannel(str, Enum):
    DASHBOARD = "dashboard"
    TELEGRAM = "telegram"


class ReminderStatus(str, Enum):
    SCHEDULED = "scheduled"
    CLAIMED = "claimed"
    DELIVERED = "delivered"
    DISMISSED = "dismissed"
    RETRY = "retry"
    DEAD = "dead"


class TokenProvider(str, Enum):
    GEMINI = "gemini"
    GROQ = "groq"
    LOCAL = "local"
    EMBEDDING = "embedding"


class TokenOperation(str, Enum):
    CLASSIFICATION_ANALYSIS = "classification_analysis"
    LOCAL_SHADOW = "local_shadow"
    DOCUMENT_EMBEDDING = "document_embedding"
    QUERY_EMBEDDING = "query_embedding"
    MANUAL_PREDICTION = "manual_prediction"
    ACTION_REANALYSIS = "action_reanalysis"


class TokenCountMethod(str, Enum):
    PROVIDER_REPORTED = "provider_reported"
    TOKENIZER_COUNTED = "tokenizer_counted"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class TokenOutcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


MAX_ACTIONS_PER_EMAIL = 5
MAX_ACTION_TITLE_CHARS = 160
MAX_ACTION_DESCRIPTION_CHARS = 500
MAX_ACTION_EVIDENCE_CHARS = 320
MAX_EXPLANATION_SUMMARY_CHARS = 240
MAX_EXPLANATION_SIGNALS = 3
MAX_SIGNAL_EVIDENCE_CHARS = 240
MAX_AUTOMATIC_DEADLINE_DAYS = 366
EXACT_TIME_REMINDER_LEAD_MINUTES = 30
DATE_ONLY_REMINDER_LOCAL_HOUR = 9

DEFAULT_TIMEZONE = "Asia/Kolkata"
WEEK_START = "monday"

CLOUD_BILLED_TOKEN_PROVIDERS = frozenset(
    (TokenProvider.GEMINI.value, TokenProvider.GROQ.value)
)
LOCAL_PROCESSED_TOKEN_PROVIDERS = frozenset(
    (TokenProvider.LOCAL.value, TokenProvider.EMBEDDING.value)
)

# Only explicit user actions may reopen terminal actions. Snooze expiry is the
# sole automatic transition and returns the action to open.
ACTION_STATUS_TRANSITIONS = {
    ActionStatus.OPEN.value: frozenset((
        ActionStatus.COMPLETED.value,
        ActionStatus.DISMISSED.value,
        ActionStatus.SNOOZED.value,
    )),
    ActionStatus.SNOOZED.value: frozenset((
        ActionStatus.OPEN.value,
        ActionStatus.COMPLETED.value,
        ActionStatus.DISMISSED.value,
    )),
    ActionStatus.COMPLETED.value: frozenset((ActionStatus.OPEN.value,)),
    ActionStatus.DISMISSED.value: frozenset((ActionStatus.OPEN.value,)),
}
