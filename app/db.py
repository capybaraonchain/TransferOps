from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import get_settings

settings = get_settings()

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, future=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


SQLITE_MIGRATIONS: dict[str, list[str]] = {
    "release_candidates": [
        "ALTER TABLE release_candidates ADD COLUMN source_confidence FLOAT DEFAULT 0.5",
        "ALTER TABLE release_candidates ADD COLUMN exclude_from_learning BOOLEAN DEFAULT 0",
        "ALTER TABLE release_candidates ADD COLUMN wanted BOOLEAN DEFAULT 0",
        "ALTER TABLE release_candidates ADD COLUMN wanted_reason TEXT",
        "ALTER TABLE release_candidates ADD COLUMN dedupe_key TEXT",
        "ALTER TABLE release_candidates ADD COLUMN release_year INTEGER",
    ],
    "torrents": [
        "ALTER TABLE torrents ADD COLUMN managed BOOLEAN DEFAULT 1",
        "ALTER TABLE torrents ADD COLUMN exclude_from_learning BOOLEAN DEFAULT 0",
        "ALTER TABLE torrents ADD COLUMN last_seen_at DATETIME",
        "ALTER TABLE torrents ADD COLUMN last_learning_at DATETIME",
        "ALTER TABLE torrents ADD COLUMN executor_state TEXT DEFAULT 'confirmed'",
        "ALTER TABLE torrents ADD COLUMN executor_deadline_at DATETIME",
        "ALTER TABLE torrents ADD COLUMN executor_confirmed_at DATETIME",
    ],
    "system_snapshots": [
        "ALTER TABLE system_snapshots ADD COLUMN protocol_usage_bytes INTEGER DEFAULT 0",
        "ALTER TABLE system_snapshots ADD COLUMN protocol_projected_usage_bytes INTEGER DEFAULT 0",
        "ALTER TABLE system_snapshots ADD COLUMN manual_usage_bytes INTEGER DEFAULT 0",
        "ALTER TABLE system_snapshots ADD COLUMN manual_projected_usage_bytes INTEGER DEFAULT 0",
    ],
    "runtime_settings": [
        "ALTER TABLE runtime_settings ADD COLUMN updated_at DATETIME",
    ],
    "manual_requests": [
        "ALTER TABLE manual_requests ADD COLUMN exclude_from_learning BOOLEAN DEFAULT 1",
    ],
}


def migrate_sqlite_schema() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as connection:
        for table_name, statements in SQLITE_MIGRATIONS.items():
            columns = {
                row[1]
                for row in connection.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
            }
            for statement in statements:
                column_name = statement.split(" ADD COLUMN ", 1)[1].split()[0]
                if column_name not in columns:
                    connection.execute(text(statement))
        duplicate_rows = connection.execute(
            text(
                """
                SELECT dedupe_key
                FROM release_candidates
                WHERE dedupe_key IS NOT NULL
                GROUP BY dedupe_key
                HAVING COUNT(*) > 1
                """
            )
        ).fetchall()
        for (dedupe_key,) in duplicate_rows:
            rows = connection.execute(
                text(
                    """
                    SELECT id
                    FROM release_candidates
                    WHERE dedupe_key = :dedupe_key
                    ORDER BY id ASC
                    """
                ),
                {"dedupe_key": dedupe_key},
            ).fetchall()
            for (row_id,) in rows[1:]:
                connection.execute(
                    text(
                        """
                        UPDATE release_candidates
                        SET dedupe_key = :replacement
                        WHERE id = :row_id
                        """
                    ),
                    {"replacement": f"{dedupe_key}:legacy:{row_id}", "row_id": row_id},
                )
        connection.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_release_candidates_dedupe_key
                ON release_candidates(dedupe_key)
                WHERE dedupe_key IS NOT NULL
                """
            )
        )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
