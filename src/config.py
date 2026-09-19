"""Safe configuration names/paths; no environment files are read on import."""
from dataclasses import dataclass, field
from pathlib import Path
import os
from .prediction import Category

ROOT = Path(__file__).resolve().parents[1]
LEGACY_ACCOUNT = "legacy-unassigned"
CATEGORIES = tuple(category.value for category in Category)


@dataclass(frozen=True)
class Settings:
    data_dir: Path = ROOT / "data"
    model_path: Path = ROOT / "models" / "MailMind-Final"
    retrieval_policy_path: Path | None = None
    local_only: bool = False
    asset_manifest_path: Path | None = None
    busy_timeout_ms: int = 5000
    access_key: str | None = field(default=None, repr=False)
    frontend_origins: tuple = ("http://localhost:5173", "http://127.0.0.1:5173")
    poll_interval_seconds: int = 60
    batch_size: int = 20
    gmail_page_size: int = 10
    gmail_max_pages: int = 3
    auto_mark_read: bool = False
    provider_timeout_seconds: int = 5
    classification_budget_seconds: int = 20
    worker_lease_seconds: int = 90
    max_processing_attempts: int = 3
    gemini_models: tuple = ('gemini-3.8-flash', 'gemini-3.5-flash-lite')
    groq_model: str = 'openai/gpt-oss-20b'

    def __post_init__(self):
        for name, low, high in (('poll_interval_seconds',5,86400), ('batch_size',1,200),
                               ('gmail_page_size',1,100), ('gmail_max_pages',1,20),
                               ('provider_timeout_seconds',1,30), ('classification_budget_seconds',3,60),
                               ('worker_lease_seconds',65,300), ('max_processing_attempts',1,10)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'Invalid {name}: outside the supported range')

        if type(self.local_only) is not bool: raise ValueError('local_only must be a boolean')
        if type(self.auto_mark_read) is not bool:
            raise ValueError('auto_mark_read must be a boolean')
        import re
        model_pattern = re.compile(r'[a-z0-9][a-z0-9./_-]{0,99}')
        if not isinstance(self.gemini_models, tuple) or not 1 <= len(self.gemini_models) <= 3 or len(set(self.gemini_models)) != len(self.gemini_models) or any(not isinstance(value,str) or not model_pattern.fullmatch(value) for value in self.gemini_models):
            raise ValueError('MAILMIND_GEMINI_MODELS must contain one to three unique model IDs')
        if not isinstance(self.groq_model, str) or not model_pattern.fullmatch(self.groq_model):
            raise ValueError('Invalid MAILMIND_GROQ_MODEL')

    @property
    def legacy_token_path(self):
        return ROOT / "token.json" if self.data_dir == ROOT / "data" else self.data_dir / "legacy_token.json"

    @property
    def db_path(self):
        return self.data_dir / "email_logs.db"

    @classmethod
    def from_environment(cls, *, load_file=False):
        raw_mode=os.environ.get('MAILMIND_LOCAL_ONLY','false').lower()
        if raw_mode not in ('true','false'): raise ValueError('MAILMIND_LOCAL_ONLY must be true or false')
        if load_file and raw_mode != 'true':
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env", override=False)
        raw_mode=os.environ.get('MAILMIND_LOCAL_ONLY','false').lower()
        if raw_mode not in ('true','false'): raise ValueError('MAILMIND_LOCAL_ONLY must be true or false')
        return cls(
            local_only=raw_mode == 'true',
            asset_manifest_path=Path(os.environ['MAILMIND_ASSET_MANIFEST']).resolve() if os.environ.get('MAILMIND_ASSET_MANIFEST') else None,
            data_dir=Path(os.environ.get("MAILMIND_DATA_DIR", ROOT / "data")).resolve(),
            model_path=Path(os.environ.get("MAILMIND_MODEL_PATH", ROOT / "models" / "MailMind-Final")).resolve(),
            access_key=os.environ.get("MAILMIND_ACCESS_KEY") or None,
            retrieval_policy_path=Path(os.environ["MAILMIND_RETRIEVAL_POLICY_PATH"]).resolve() if os.environ.get("MAILMIND_RETRIEVAL_POLICY_PATH") else None,
            poll_interval_seconds=int(os.environ.get('MAILMIND_POLL_INTERVAL_SECONDS',60)),
            batch_size=int(os.environ.get('MAILMIND_BATCH_SIZE',20)),
            gmail_page_size=int(os.environ.get('MAILMIND_GMAIL_PAGE_SIZE',10)),
            gmail_max_pages=int(os.environ.get('MAILMIND_GMAIL_MAX_PAGES',3)),
            auto_mark_read=os.environ.get('MAILMIND_AUTO_MARK_READ','false').lower() == 'true',
            provider_timeout_seconds=int(os.environ.get('MAILMIND_PROVIDER_TIMEOUT_SECONDS',5)),
            classification_budget_seconds=int(os.environ.get('MAILMIND_CLASSIFICATION_BUDGET_SECONDS',20)),
            worker_lease_seconds=int(os.environ.get('MAILMIND_WORKER_LEASE_SECONDS',90)),
            max_processing_attempts=int(os.environ.get('MAILMIND_MAX_PROCESSING_ATTEMPTS',3)),
            gemini_models=tuple(value.strip() for value in os.environ.get('MAILMIND_GEMINI_MODELS','gemini-3.8-flash,gemini-3.5-flash-lite').split(',') if value.strip()),
            groq_model=os.environ.get('MAILMIND_GROQ_MODEL','openai/gpt-oss-20b').strip(),
        )
