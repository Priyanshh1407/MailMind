"""Safe configuration names/paths; no environment files are read on import."""
from dataclasses import dataclass
from pathlib import Path
import os
from .prediction import Category
from .intelligence_contract import DEFAULT_TIMEZONE, WEEK_START

ROOT = Path(__file__).resolve().parents[1]
LEGACY_ACCOUNT = "legacy-unassigned"
CATEGORIES = tuple(category.value for category in Category)


def _environment_bool(name, default):
    value = os.environ.get(name, "true" if default else "false").strip().lower()
    if value not in ("true", "false"):
        raise ValueError(f"{name} must be true or false")
    return value == "true"


@dataclass(frozen=True)
class Settings:
    data_dir: Path = ROOT / "data"
    # Local-only decides with the same three-category checkpoint normal mode
    # runs as its shadow. The legacy binary checkpoint cannot label UPDATES.
    model_path: Path = ROOT / "models" / "inbox-approved-v2"
    shadow_model_path: Path = ROOT / "models" / "inbox-approved-v2"
    retrieval_policy_path: Path | None = None
    local_only: bool = False
    asset_manifest_path: Path | None = None
    busy_timeout_ms: int = 5000
    frontend_origins: tuple = ("http://localhost:5173", "http://127.0.0.1:5173")
    poll_interval_seconds: int = 5
    batch_size: int = 20
    gmail_page_size: int = 10
    gmail_max_pages: int = 3
    max_pending_tasks: int = 100
    resume_pending_tasks: int = 50
    auto_mark_read: bool = False
    provider_timeout_seconds: int = 5
    classification_budget_seconds: int = 20
    worker_lease_seconds: int = 90
    max_processing_attempts: int = 3
    gemini_models: tuple = ('gemini-3.8-flash', 'gemini-3.5-flash-lite')
    groq_model: str = 'openai/gpt-oss-20b'
    default_timezone: str = DEFAULT_TIMEZONE
    week_start: str = WEEK_START
    action_extraction_enabled: bool = False
    action_reminders_enabled: bool = False
    telegram_action_reminders_enabled: bool = False
    token_collection_enabled: bool = True
    token_analytics_visible: bool = False
    explanations_visible: bool = False
    # The local model as a comparison-only shadow in normal mode. Off by default:
    # it never decides, and costs a PyTorch process plus CPU per email.
    shadow_model_enabled: bool = False

    def __post_init__(self):
        for name, low, high in (('poll_interval_seconds',5,86400), ('batch_size',1,200),
                               ('gmail_page_size',1,100), ('gmail_max_pages',1,20),
                               ('max_pending_tasks',1,1000),
                               ('resume_pending_tasks',0,999),
                               ('provider_timeout_seconds',1,30), ('classification_budget_seconds',3,60),
                               ('worker_lease_seconds',65,300), ('max_processing_attempts',1,10)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'Invalid {name}: outside the supported range')

        if type(self.local_only) is not bool: raise ValueError('local_only must be a boolean')
        if self.resume_pending_tasks >= self.max_pending_tasks:
            raise ValueError('resume_pending_tasks must be lower than max_pending_tasks')
        if type(self.auto_mark_read) is not bool:
            raise ValueError('auto_mark_read must be a boolean')
        for name in ('action_extraction_enabled', 'action_reminders_enabled',
                     'telegram_action_reminders_enabled',
                     'token_collection_enabled', 'token_analytics_visible',
                     'explanations_visible'):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f'{name} must be a boolean')
        if self.action_reminders_enabled and not self.action_extraction_enabled:
            raise ValueError('Action reminders require action extraction: set MAILMIND_ACTION_EXTRACTION_ENABLED=true, or MAILMIND_ACTION_REMINDERS_ENABLED=false')
        if (self.telegram_action_reminders_enabled
                and not self.action_reminders_enabled):
            raise ValueError('Telegram action reminders require reminders: set MAILMIND_ACTION_REMINDERS_ENABLED=true, or MAILMIND_TELEGRAM_ACTION_REMINDERS_ENABLED=false')
        if self.telegram_action_reminders_enabled and self.local_only:
            raise ValueError('Telegram action reminders are unavailable in local-only mode: set MAILMIND_TELEGRAM_ACTION_REMINDERS_ENABLED=false for local-only runs, or MAILMIND_LOCAL_ONLY=false')
        if self.token_analytics_visible and not self.token_collection_enabled:
            raise ValueError('Token analytics visibility requires token collection: set MAILMIND_TOKEN_COLLECTION_ENABLED=true, or MAILMIND_TOKEN_ANALYTICS_VISIBLE=false')
        if self.default_timezone != DEFAULT_TIMEZONE:
            raise ValueError(f'Unsupported default timezone: {self.default_timezone}')
        if self.week_start != WEEK_START:
            raise ValueError(f'Unsupported week boundary: {self.week_start}')
        import re
        model_pattern = re.compile(r'[a-z0-9][a-z0-9./_-]{0,99}')
        if not isinstance(self.gemini_models, tuple) or not 1 <= len(self.gemini_models) <= 3 or len(set(self.gemini_models)) != len(self.gemini_models) or any(not isinstance(value,str) or not model_pattern.fullmatch(value) for value in self.gemini_models):
            raise ValueError('MAILMIND_GEMINI_MODELS must contain one to three unique model IDs')
        if not isinstance(self.groq_model, str) or not model_pattern.fullmatch(self.groq_model):
            raise ValueError('Invalid MAILMIND_GROQ_MODEL')

    @property
    def shadow_active(self):
        """Run the local model as a shadow beside the cloud (never in local-only mode, where it decides)."""
        return self.shadow_model_enabled and not self.local_only

    @property
    def legacy_token_path(self):
        return ROOT / "token.json" if self.data_dir == ROOT / "data" else self.data_dir / "legacy_token.json"

    @property
    def db_path(self):
        return self.data_dir / "email_logs.db"

    @classmethod
    def from_environment(cls, *, load_file=False):
        local_only = _environment_bool('MAILMIND_LOCAL_ONLY', False)
        if load_file and not local_only:
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env", override=False)
        local_only = _environment_bool('MAILMIND_LOCAL_ONLY', False)
        return cls(
            local_only=local_only,
            asset_manifest_path=Path(os.environ['MAILMIND_ASSET_MANIFEST']).resolve() if os.environ.get('MAILMIND_ASSET_MANIFEST') else None,
            data_dir=Path(os.environ.get("MAILMIND_DATA_DIR", ROOT / "data")).resolve(),
            model_path=Path(os.environ.get("MAILMIND_MODEL_PATH", ROOT / "models" / "inbox-approved-v2")).resolve(),
            shadow_model_path=Path(os.environ.get("MAILMIND_SHADOW_MODEL_PATH", ROOT / "models" / "inbox-approved-v2")).resolve(),
            retrieval_policy_path=Path(os.environ["MAILMIND_RETRIEVAL_POLICY_PATH"]).resolve() if os.environ.get("MAILMIND_RETRIEVAL_POLICY_PATH") else None,
            poll_interval_seconds=int(os.environ.get('MAILMIND_POLL_INTERVAL_SECONDS',5)),
            batch_size=int(os.environ.get('MAILMIND_BATCH_SIZE',20)),
            gmail_page_size=int(os.environ.get('MAILMIND_GMAIL_PAGE_SIZE',10)),
            gmail_max_pages=int(os.environ.get('MAILMIND_GMAIL_MAX_PAGES',3)),
            max_pending_tasks=int(os.environ.get('MAILMIND_MAX_PENDING_TASKS',100)),
            resume_pending_tasks=int(os.environ.get('MAILMIND_RESUME_PENDING_TASKS',50)),
            auto_mark_read=_environment_bool('MAILMIND_AUTO_MARK_READ', False),
            provider_timeout_seconds=int(os.environ.get('MAILMIND_PROVIDER_TIMEOUT_SECONDS',5)),
            classification_budget_seconds=int(os.environ.get('MAILMIND_CLASSIFICATION_BUDGET_SECONDS',20)),
            worker_lease_seconds=int(os.environ.get('MAILMIND_WORKER_LEASE_SECONDS',90)),
            max_processing_attempts=int(os.environ.get('MAILMIND_MAX_PROCESSING_ATTEMPTS',3)),
            gemini_models=tuple(value.strip() for value in os.environ.get('MAILMIND_GEMINI_MODELS','gemini-3.8-flash,gemini-3.5-flash-lite').split(',') if value.strip()),
            groq_model=os.environ.get('MAILMIND_GROQ_MODEL','openai/gpt-oss-20b').strip(),
            default_timezone=os.environ.get('MAILMIND_DEFAULT_TIMEZONE',DEFAULT_TIMEZONE).strip(),
            week_start=os.environ.get('MAILMIND_WEEK_START',WEEK_START).strip().lower(),
            action_extraction_enabled=_environment_bool('MAILMIND_ACTION_EXTRACTION_ENABLED',False),
            action_reminders_enabled=_environment_bool('MAILMIND_ACTION_REMINDERS_ENABLED',False),
            telegram_action_reminders_enabled=_environment_bool('MAILMIND_TELEGRAM_ACTION_REMINDERS_ENABLED',False),
            token_collection_enabled=_environment_bool('MAILMIND_TOKEN_COLLECTION_ENABLED',True),
            token_analytics_visible=_environment_bool('MAILMIND_TOKEN_ANALYTICS_VISIBLE',False),
            explanations_visible=_environment_bool('MAILMIND_EXPLANATIONS_VISIBLE',False),
            shadow_model_enabled=_environment_bool('MAILMIND_SHADOW_MODEL_ENABLED',False),
        )
