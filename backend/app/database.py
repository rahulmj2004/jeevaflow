"""
Medical database engine.

The audit table is protected by SQLite triggers that reject UPDATE
and DELETE, so the application cannot rewrite history through the
normal data path. (A database-level attacker can still bypass
triggers; the hash chain and its HMAC make that detectable.)
"""

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import ConfigurationError, settings


DATABASE_URL = settings.database_url

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 15},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(connection, _):
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA secure_delete=ON")
    cursor.close()

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()


def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()


AUDIT_TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS audit_events_no_update
    BEFORE UPDATE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
    BEFORE DELETE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit_events is append-only');
    END
    """,
)


def _reject_legacy_schema():
    inspector = inspect(engine)

    if "patients" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("patients")}

        if "name" in columns or "phone" in columns:
            raise ConfigurationError(
                "This database uses the pre-security schema, which stores "
                "identity and medical data together in plaintext. Move "
                "backend/jeevaflow.db aside (it contains synthetic demo "
                "data only) and restart to create the secured schema."
            )


def init_db():
    from . import models  # noqa: F401  (register tables)
    from .identity import init_identity_db
    from .security import audit  # noqa: F401  (register the audit anchor table)

    _reject_legacy_schema()

    Base.metadata.create_all(bind=engine)

    with engine.begin() as connection:
        for statement in AUDIT_TRIGGERS:
            connection.execute(text(statement))

    init_identity_db()
