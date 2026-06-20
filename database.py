import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row


FREE_RESULT_LIMIT = 5
PREMIUM_YEARS_DEFAULT = 1
_DB_INITIALIZED = False


def _load_local_env() -> None:
    env_path = Path(__file__).with_name(".env.local")
    if not env_path.exists():
        return

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


class DatabaseNotConfigured(RuntimeError):
    pass


class QuotaExceeded(RuntimeError):
    pass


@dataclass
class User:
    telegram_id: int
    username: Optional[str]
    first_name: Optional[str]
    plan: str
    premium_expires_at: Optional[datetime]
    free_results_used: int
    daily_summary_enabled: bool = False

    @property
    def is_premium(self) -> bool:
        return (
            self.plan == "premium"
            and self.premium_expires_at is not None
            and self.premium_expires_at > datetime.now(timezone.utc)
        )

    @property
    def remaining_free_results(self) -> int:
        return max(FREE_RESULT_LIMIT - self.free_results_used, 0)


@dataclass
class BidWatch:
    id: int
    telegram_id: int
    consultation_reference: str
    org_acronyme: str
    consultation_url: str
    consultation_title: Optional[str]
    status: str
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    last_checked_at: Optional[datetime]


def _database_url() -> str:
    _load_local_env()
    for name in ("DATABASE_URL", "POSTGRES_URL", "SUPABASE_DB_URL"):
        url = os.environ.get(name, "").strip()
        if url:
            return _clean_database_url(url)
    raise DatabaseNotConfigured("DATABASE_URL, POSTGRES_URL, or SUPABASE_DB_URL is not configured")


def _clean_database_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url

    allowed_query_params = {
        "application_name",
        "connect_timeout",
        "gssencmode",
        "keepalives",
        "keepalives_count",
        "keepalives_idle",
        "keepalives_interval",
        "sslcert",
        "sslcompression",
        "sslcrl",
        "sslkey",
        "sslmode",
        "sslrootcert",
        "target_session_attrs",
    }
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key in allowed_query_params
        ]
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _connect():
    return psycopg.connect(_database_url(), autocommit=True, row_factory=dict_row)


def init_db() -> None:
    global _DB_INITIALIZED
    if _DB_INITIALIZED:
        return
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                plan TEXT NOT NULL DEFAULT 'free'
                    CHECK (plan IN ('free', 'premium')),
                premium_expires_at TIMESTAMPTZ,
                free_results_used INTEGER NOT NULL DEFAULT 0
                    CHECK (free_results_used >= 0),
                daily_summary_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS procurement_results (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                consultation_url TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bid_result_watches (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                consultation_reference TEXT NOT NULL,
                org_acronyme TEXT NOT NULL DEFAULT '',
                consultation_url TEXT NOT NULL,
                consultation_title TEXT,
                status TEXT NOT NULL DEFAULT 'watching'
                    CHECK (status IN ('watching', 'notified', 'stopped')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_checked_at TIMESTAMPTZ,
                published_at TIMESTAMPTZ,
                notified_at TIMESTAMPTZ,
                last_error TEXT,
                UNIQUE (telegram_id, consultation_reference, org_acronyme)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_summary_runs (
                summary_date DATE PRIMARY KEY,
                status TEXT NOT NULL
                    CHECK (status IN ('running', 'sent', 'error')),
                recipient_count INTEGER NOT NULL DEFAULT 0,
                sent_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS daily_summary_enabled BOOLEAN NOT NULL DEFAULT FALSE
            """
        )
        conn.execute(
            """
            ALTER TABLE bid_result_watches
            ADD COLUMN IF NOT EXISTS consultation_title TEXT
            """
        )
    _DB_INITIALIZED = True


def _row_to_user(row) -> User:
    return User(
        telegram_id=row["telegram_id"],
        username=row["username"],
        first_name=row["first_name"],
        plan=row["plan"],
        premium_expires_at=row["premium_expires_at"],
        free_results_used=row["free_results_used"],
        daily_summary_enabled=bool(row.get("daily_summary_enabled", False)),
    )


def _row_to_bid_watch(row) -> BidWatch:
    return BidWatch(
        id=row["id"],
        telegram_id=row["telegram_id"],
        consultation_reference=row["consultation_reference"],
        org_acronyme=row["org_acronyme"],
        consultation_url=row["consultation_url"],
        consultation_title=row.get("consultation_title"),
        status=row["status"],
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        last_checked_at=row["last_checked_at"],
    )


def upsert_telegram_user(tg_user: dict) -> User:
    init_db()
    telegram_id = int(tg_user["id"])
    username = tg_user.get("username")
    first_name = tg_user.get("first_name")
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO users (telegram_id, username, first_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (telegram_id) DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                updated_at = NOW()
            RETURNING telegram_id, username, first_name, plan,
                premium_expires_at, free_results_used, daily_summary_enabled
            """,
            (telegram_id, username, first_name),
        ).fetchone()
    return _row_to_user(row)


def watch_bid_result(
    telegram_id: int,
    url: str,
    reference: str,
    org_acronyme: str = "",
    consultation_title: Optional[str] = None,
) -> BidWatch:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO bid_result_watches (
                telegram_id, consultation_reference, org_acronyme,
                consultation_url, consultation_title, status
            )
            VALUES (%s, %s, %s, %s, %s, 'watching')
            ON CONFLICT (telegram_id, consultation_reference, org_acronyme)
            DO UPDATE SET
                consultation_url = EXCLUDED.consultation_url,
                consultation_title = COALESCE(EXCLUDED.consultation_title, bid_result_watches.consultation_title),
                status = 'watching',
                updated_at = NOW(),
                published_at = NULL,
                notified_at = NULL,
                last_error = NULL
            RETURNING id, telegram_id, consultation_reference, org_acronyme,
                consultation_url, consultation_title, status, created_at, updated_at,
                last_checked_at
            """,
            (telegram_id, reference, org_acronyme or "", url, consultation_title),
        ).fetchone()
    return _row_to_bid_watch(row)


def list_pending_bid_watches(telegram_id: int) -> list[BidWatch]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, telegram_id, consultation_reference, org_acronyme,
                consultation_url, consultation_title, status, created_at, updated_at,
                last_checked_at
            FROM bid_result_watches
            WHERE telegram_id = %s
              AND status = 'watching'
            ORDER BY created_at DESC, id DESC
            """,
            (telegram_id,),
        ).fetchall()
    return [_row_to_bid_watch(row) for row in rows]


def stop_bid_watch(telegram_id: int, watch_id: int) -> Optional[BidWatch]:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            UPDATE bid_result_watches
            SET status = 'stopped',
                updated_at = NOW()
            WHERE id = %s
              AND telegram_id = %s
              AND status = 'watching'
            RETURNING id, telegram_id, consultation_reference, org_acronyme,
                consultation_url, consultation_title, status, created_at, updated_at,
                last_checked_at
            """,
            (watch_id, telegram_id),
        ).fetchone()
    return _row_to_bid_watch(row) if row else None


def update_bid_watch_title(watch_id: int, consultation_title: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE bid_result_watches
            SET consultation_title = %s,
                updated_at = NOW()
            WHERE id = %s
            """,
            (consultation_title, watch_id),
        )


def claim_due_bid_watches(limit: int = 10) -> list[BidWatch]:
    init_db()
    limit = max(1, min(limit, 50))
    with _connect() as conn:
        with conn.transaction():
            rows = conn.execute(
                """
                WITH due AS (
                    SELECT id
                    FROM bid_result_watches
                    WHERE status = 'watching'
                      AND (
                        last_checked_at IS NULL
                        OR last_checked_at < NOW() - INTERVAL '50 seconds'
                      )
                    ORDER BY COALESCE(last_checked_at, created_at), id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE bid_result_watches w
                SET last_checked_at = NOW(),
                    updated_at = NOW(),
                    last_error = NULL
                FROM due
                WHERE w.id = due.id
                RETURNING w.id, w.telegram_id, w.consultation_reference,
                    w.org_acronyme, w.consultation_url, w.status,
                    w.created_at, w.updated_at,
                    w.last_checked_at
                """,
                (limit,),
            ).fetchall()
    return [_row_to_bid_watch(row) for row in rows]


def mark_bid_watch_notified(watch_id: int) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE bid_result_watches
            SET status = 'notified',
                published_at = NOW(),
                notified_at = NOW(),
                updated_at = NOW(),
                last_error = NULL
            WHERE id = %s
            """,
            (watch_id,),
        )


def mark_bid_watch_error(watch_id: int, error: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE bid_result_watches
            SET last_error = %s,
                updated_at = NOW()
            WHERE id = %s
            """,
            (error[:800], watch_id),
        )


def get_user(telegram_id: int) -> Optional[User]:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT telegram_id, username, first_name, plan,
                premium_expires_at, free_results_used, daily_summary_enabled
            FROM users
            WHERE telegram_id = %s
            """,
            (telegram_id,),
        ).fetchone()
    return _row_to_user(row) if row else None


def list_telegram_user_ids() -> list[int]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT telegram_id
            FROM users
            ORDER BY created_at DESC
            """
        ).fetchall()
    return [int(row["telegram_id"]) for row in rows]


def list_daily_summary_recipients() -> list[int]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT telegram_id
            FROM users
            WHERE daily_summary_enabled = TRUE
              AND plan = 'premium'
              AND premium_expires_at IS NOT NULL
              AND premium_expires_at > NOW()
            ORDER BY created_at DESC
            """
        ).fetchall()
    return [int(row["telegram_id"]) for row in rows]


def count_users() -> int:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()
    return int(row["total"])


def can_create_procurement_result(user: User) -> bool:
    return user.is_premium or user.free_results_used < FREE_RESULT_LIMIT


def record_procurement_result(telegram_id: int, url: str) -> User:
    init_db()
    with _connect() as conn:
        with conn.transaction():
            row = conn.execute(
                """
                UPDATE users
                SET free_results_used = CASE
                        WHEN plan = 'premium'
                         AND premium_expires_at IS NOT NULL
                         AND premium_expires_at > NOW()
                        THEN free_results_used
                        ELSE free_results_used + 1
                    END,
                    updated_at = NOW()
                WHERE telegram_id = %s
                  AND (
                    (
                        plan = 'premium'
                        AND premium_expires_at IS NOT NULL
                        AND premium_expires_at > NOW()
                    )
                    OR free_results_used < %s
                  )
                RETURNING telegram_id, username, first_name, plan,
                    premium_expires_at, free_results_used, daily_summary_enabled
                """,
                (telegram_id, FREE_RESULT_LIMIT),
            ).fetchone()
            if row is None:
                raise QuotaExceeded("Free plan procurement result limit exceeded")
            conn.execute(
                """
                INSERT INTO procurement_results (telegram_id, consultation_url)
                VALUES (%s, %s)
                """,
                (telegram_id, url),
            )
    return _row_to_user(row)


def grant_premium(telegram_id: int, years: int = PREMIUM_YEARS_DEFAULT) -> User:
    init_db()
    years = max(1, years)
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO users (telegram_id, plan, premium_expires_at)
            VALUES (%s, 'premium', NOW() + (%s || ' years')::interval)
            ON CONFLICT (telegram_id) DO UPDATE SET
                plan = 'premium',
                premium_expires_at = CASE
                    WHEN users.premium_expires_at IS NOT NULL
                     AND users.premium_expires_at > NOW()
                    THEN users.premium_expires_at + (%s || ' years')::interval
                    ELSE NOW() + (%s || ' years')::interval
                END,
                updated_at = NOW()
            RETURNING telegram_id, username, first_name, plan,
                premium_expires_at, free_results_used, daily_summary_enabled
            """,
            (telegram_id, years, years, years),
        ).fetchone()
    return _row_to_user(row)


def set_free(telegram_id: int) -> User:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO users (telegram_id, plan)
            VALUES (%s, 'free')
            ON CONFLICT (telegram_id) DO UPDATE SET
                plan = 'free',
                premium_expires_at = NULL,
                updated_at = NOW()
            RETURNING telegram_id, username, first_name, plan,
                premium_expires_at, free_results_used, daily_summary_enabled
            """,
            (telegram_id,),
        ).fetchone()
    return _row_to_user(row)


def set_daily_summary_enabled(telegram_id: int, enabled: bool) -> User:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            UPDATE users
            SET daily_summary_enabled = %s,
                updated_at = NOW()
            WHERE telegram_id = %s
            RETURNING telegram_id, username, first_name, plan,
                premium_expires_at, free_results_used, daily_summary_enabled
            """,
            (enabled, telegram_id),
        ).fetchone()
    if row is None:
        raise ValueError("Telegram user not found")
    return _row_to_user(row)


def claim_daily_summary_run(summary_date) -> bool:
    init_db()
    with _connect() as conn:
        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO daily_summary_runs (summary_date, status)
                VALUES (%s, 'running')
                ON CONFLICT (summary_date) DO UPDATE SET
                    status = 'running',
                    updated_at = NOW(),
                    last_error = NULL
                WHERE daily_summary_runs.status = 'error'
                   OR (
                        daily_summary_runs.status = 'running'
                        AND daily_summary_runs.updated_at < NOW() - INTERVAL '2 hours'
                   )
                RETURNING summary_date
                """,
                (summary_date,),
            ).fetchone()
    return row is not None


def reset_daily_summary_run(summary_date) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            DELETE FROM daily_summary_runs
            WHERE summary_date = %s
            """,
            (summary_date,),
        )


def finish_daily_summary_run(
    summary_date,
    status: str,
    recipient_count: int,
    sent_count: int,
    error_count: int,
    last_error: Optional[str] = None,
) -> None:
    init_db()
    if status not in ("sent", "error"):
        raise ValueError("status must be 'sent' or 'error'")
    with _connect() as conn:
        conn.execute(
            """
            UPDATE daily_summary_runs
            SET status = %s,
                recipient_count = %s,
                sent_count = %s,
                error_count = %s,
                last_error = %s,
                updated_at = NOW()
            WHERE summary_date = %s
            """,
            (
                status,
                recipient_count,
                sent_count,
                error_count,
                last_error[:800] if last_error else None,
                summary_date,
            ),
        )
