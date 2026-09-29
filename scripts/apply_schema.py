"""Apply all pending migrations in db/migrations/ to the database at DATABASE_URL.

Tracks applied migrations in a `schema_migrations` table so this is safe to
run repeatedly — only files not yet recorded get applied. Each migration
still runs in its own transaction and rolls back cleanly on error; a failed
migration is not recorded as applied.

This replaces the earlier single-file version (hardcoded to 001_init.sql),
which explicitly flagged this exact limitation in its own docstring before a
002_*.sql actually existed.

Usage:
    python scripts/apply_schema.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"


def _ensure_tracking_table(conn) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    filename TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )


def _already_applied(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def _bootstrap_pre_tracking_migration(conn) -> None:
    """One-time transitional shim: 001_init.sql was applied (Step 2) before
    this schema_migrations table existed. Without this, a fresh run would try
    to re-execute 001 and fail on "relation already exists." If the tracking
    table is empty but the tables 001 creates are already present, backfill a
    row for it instead of re-running it."""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM schema_migrations")
        (tracked_count,) = cur.fetchone()
        if tracked_count > 0:
            return  # tracking already has data; nothing to bootstrap

        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'obligations')"
        )
        (obligations_exists,) = cur.fetchone()
        if not obligations_exists:
            return  # genuinely fresh database — let 001 run normally below

        with conn:
            cur.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)", ("001_init.sql",)
            )
        print("Bootstrapped: 001_init.sql was already applied pre-tracking — backfilled, not re-run.")


def main() -> None:
    load_dotenv(REPO_ROOT / ".env")

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print(
            "ERROR: DATABASE_URL is not set. Copy .env.example to .env and fill "
            "it in with your Supabase pooler connection string (or a local "
            "Docker Postgres URL) before running this script.",
            file=sys.stderr,
        )
        sys.exit(1)

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migration_files:
        print(f"ERROR: no migration files found in {MIGRATIONS_DIR}", file=sys.stderr)
        sys.exit(1)

    try:
        conn = psycopg2.connect(database_url)
    except psycopg2.OperationalError as exc:
        print(f"ERROR: could not connect to database: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        _ensure_tracking_table(conn)
        _bootstrap_pre_tracking_migration(conn)
        applied = _already_applied(conn)

        pending = [f for f in migration_files if f.name not in applied]
        if not pending:
            print("Nothing to apply — all migrations already recorded in schema_migrations.")
            return

        for migration_file in pending:
            sql = migration_file.read_text(encoding="utf-8")
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(sql)
                        cur.execute(
                            "INSERT INTO schema_migrations (filename) VALUES (%s)",
                            (migration_file.name,),
                        )
                print(f"Applied {migration_file.name} successfully.")
            except psycopg2.Error as exc:
                # psycopg2 rolls back automatically on exception inside `with conn:`.
                print(
                    f"ERROR: {migration_file.name} failed, transaction rolled back: {exc}",
                    file=sys.stderr,
                )
                print(
                    f"Stopping — {len(pending) - pending.index(migration_file) - 1} "
                    "later migration(s) not attempted.",
                    file=sys.stderr,
                )
                sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
