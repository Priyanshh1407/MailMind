"""Run with python -m src.setup_db; initialization never runs on import."""
from .config import Settings
from .database import initialize_database
from .logging_utils import log_event

DB_PATH = Settings().db_path


def create_database(db_path=None):
    initialize_database(db_path or Settings.from_environment().db_path)


if __name__ == "__main__":
    try:
        create_database(Settings.from_environment(load_file=True).db_path)
    except Exception as error:
        log_event("database_initialization_failed", error=error)
        raise SystemExit("Database initialization failed; check the migration policy.") from None
