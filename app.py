from functools import wraps
import json
import hashlib
import hmac
import secrets
import smtplib
from email.message import EmailMessage

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
import sqlite3
import os
import sys
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from werkzeug.middleware.proxy_fix import ProxyFix


def resolve_app_base_dir():
    if getattr(sys, "frozen", False):
        appdata = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "daily-routine-tracker"
        appdata.mkdir(parents=True, exist_ok=True)
        return appdata
    return Path(__file__).resolve().parent


BASE_DIR = resolve_app_base_dir()
DB_PATH = Path(os.environ.get("ROUTINE_TRACKER_DB", BASE_DIR / "database" / "tracker.db"))


def resolve_asset_dir(name):
    candidates = []
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            candidates.append(Path(sys._MEIPASS) / name)
        candidates.append(Path(sys.executable).resolve().parent / name)
    candidates.append(Path(__file__).resolve().parent / name)

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return str(Path(__file__).resolve().parent / name)


app = Flask(__name__, template_folder=resolve_asset_dir("templates"), static_folder=resolve_asset_dir("static"))
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("routine-tracker")
runtime_environment = os.environ.get("ROUTINE_TRACKER_ENV", "development").strip().lower()
secret_key = os.environ.get("ROUTINE_TRACKER_SECRET", "").strip()
auth_mode = os.environ.get("ROUTINE_TRACKER_AUTH", "shared").strip().lower()
if auth_mode not in {"shared", "accounts"}:
    raise RuntimeError("ROUTINE_TRACKER_AUTH must be 'shared' or 'accounts'")
if runtime_environment == "production" and len(secret_key) < 32:
    raise RuntimeError("ROUTINE_TRACKER_SECRET must be at least 32 characters in production")
if not secret_key:
    secret_key = secrets.token_urlsafe(32)
    logger.warning("ROUTINE_TRACKER_SECRET is unset; using an ephemeral development secret")
app.secret_key = secret_key
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("ROUTINE_TRACKER_HTTPS") == "1",
    AUTH_MODE=auth_mode,
    LOCAL_MODE=os.environ.get("ROUTINE_TRACKER_LOCAL", "0") == "1",
    VAPID_PUBLIC_KEY=os.environ.get("VAPID_PUBLIC_KEY", ""),
)
if os.environ.get("ROUTINE_TRACKER_TRUST_PROXY") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
PRICING = {
    "monthly": {"amount": 250, "label": "$2.50/month", "price_id": os.environ.get("STRIPE_MONTHLY_PRICE_ID", "")},
    "yearly": {"amount": 3000, "label": "$30/year", "price_id": os.environ.get("STRIPE_YEARLY_PRICE_ID", "")},
}

LOGIN_ATTEMPTS = {}
LOGIN_WINDOW_SECONDS = 300
LOGIN_MAX_ATTEMPTS = 8

SCHEMA_MIGRATION_VERSION = 1
SCHEMA_MIGRATION_NAME = "current_schema_baseline"
SCHEMA_MIGRATION_CHECKSUM = hashlib.sha256(
    f"{SCHEMA_MIGRATION_VERSION}:{SCHEMA_MIGRATION_NAME}".encode("ascii")
).hexdigest()
REQUIRED_SCHEMA_COLUMNS = {
    "users": {"id", "email", "password_hash", "display_name", "timezone", "created_at"},
    "routines": {"id", "title", "description", "category", "target_minutes", "completed", "user_id", "archived", "paused", "position", "start_date", "end_date", "created_at"},
    "tasks": {"id", "routine_id", "title", "description", "position", "archived", "created_at"},
    "routine_history": {"id", "routine_id", "completion_date", "completed", "minutes_done"},
    "task_history": {"id", "task_id", "completion_date", "completed"},
    "schedules": {"id", "routine_id", "frequency", "time_of_day", "weekdays", "start_date", "end_date", "timezone", "enabled"},
    "categories": {"id", "user_id", "name", "color", "position"},
    "reminders": {"id", "user_id", "routine_id", "task_id", "title", "due_at", "timezone", "recurrence", "status", "snoozed_until", "delivery_attempts", "delivered_at", "created_at"},
    "timers": {"id", "user_id", "routine_id", "task_id", "title", "duration_seconds", "started_at", "paused_at", "paused_seconds", "status", "created_at"},
    "goals": {"id", "user_id", "name", "description", "target", "start_date", "end_date", "status", "created_at"},
    "milestones": {"id", "user_id", "name", "achieved_at"},
    "automations": {"id", "user_id", "trigger_type", "trigger_task_id", "action_type", "target_task_id", "delay_minutes", "title", "enabled", "created_at"},
    "subscriptions": {"user_id", "status", "trial_start", "trial_end", "provider_customer_id", "provider_subscription_id", "updated_at"},
    "notification_preferences": {"user_id", "enabled", "permission_state"},
    "password_reset_tokens": {"id", "user_id", "token_hash", "expires_at", "used_at", "created_at"},
    "push_subscriptions": {"id", "user_id", "endpoint", "subscription_json", "created_at"},
    "idempotency_keys": {"user_id", "operation_key", "created_at"},
}
FINAL_SCHEMA_COLUMNS = {table: set(columns) for table, columns in REQUIRED_SCHEMA_COLUMNS.items()}
FINAL_SCHEMA_COLUMNS["tasks"].update({"target_minutes", "task_date", "is_default"})

HARDENED_USERS_SQL = """
CREATE TABLE "users" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    created_at TEXT NOT NULL
)
"""
HARDENED_TABLE_SQL = {
    "users": HARDENED_USERS_SQL,
    "routines": """
        CREATE TABLE "routines" (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT,
            category TEXT, target_minutes INTEGER DEFAULT 20, completed INTEGER DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0, start_date TEXT, end_date TEXT, created_at TEXT NOT NULL,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE
        )
    """,
    "tasks": """
        CREATE TABLE "tasks" (
            id INTEGER PRIMARY KEY AUTOINCREMENT, routine_id INTEGER NOT NULL REFERENCES routines(id) ON DELETE CASCADE,
            title TEXT NOT NULL, description TEXT, target_minutes INTEGER NOT NULL DEFAULT 0,
            task_date TEXT, is_default INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        )
    """,
    "routine_history": """
        CREATE TABLE "routine_history" (
            id INTEGER PRIMARY KEY AUTOINCREMENT, routine_id INTEGER NOT NULL REFERENCES routines(id) ON DELETE CASCADE,
            completion_date TEXT NOT NULL, completed INTEGER DEFAULT 0, minutes_done INTEGER DEFAULT 0,
            UNIQUE(routine_id, completion_date)
        )
    """,
    "task_history": """
        CREATE TABLE "task_history" (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            completion_date TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
            UNIQUE(task_id, completion_date)
        )
    """,
    "schedules": """
        CREATE TABLE "schedules" (
            id INTEGER PRIMARY KEY AUTOINCREMENT, routine_id INTEGER NOT NULL UNIQUE REFERENCES routines(id) ON DELETE CASCADE,
            frequency TEXT NOT NULL DEFAULT 'daily', time_of_day TEXT, weekdays TEXT,
            start_date TEXT, end_date TEXT, timezone TEXT NOT NULL DEFAULT 'UTC', enabled INTEGER NOT NULL DEFAULT 1
        )
    """,
    "categories": """
        CREATE TABLE "categories" (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL, color TEXT NOT NULL DEFAULT '#5b5ce6', position INTEGER NOT NULL DEFAULT 0,
            UNIQUE(user_id, name)
        )
    """,
    "reminders": """
        CREATE TABLE "reminders" (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            routine_id INTEGER REFERENCES routines(id) ON DELETE SET NULL, task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
            title TEXT NOT NULL, due_at TEXT NOT NULL, timezone TEXT NOT NULL DEFAULT 'UTC',
            recurrence TEXT NOT NULL DEFAULT 'once', status TEXT NOT NULL DEFAULT 'pending', snoozed_until TEXT,
            created_at TEXT NOT NULL, delivery_attempts INTEGER NOT NULL DEFAULT 0, delivered_at TEXT
        )
    """,
    "timers": """
        CREATE TABLE "timers" (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            routine_id INTEGER REFERENCES routines(id) ON DELETE SET NULL, task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
            title TEXT NOT NULL, duration_seconds INTEGER NOT NULL, started_at TEXT, paused_at TEXT,
            paused_seconds INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'ready', created_at TEXT NOT NULL
        )
    """,
    "goals": """
        CREATE TABLE "goals" (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL, description TEXT, target INTEGER NOT NULL, start_date TEXT, end_date TEXT,
            status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
        )
    """,
    "milestones": """
        CREATE TABLE "milestones" (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL, achieved_at TEXT, UNIQUE(user_id, name)
        )
    """,
    "automations": """
        CREATE TABLE "automations" (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            trigger_type TEXT NOT NULL, trigger_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
            action_type TEXT NOT NULL, target_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
            delay_minutes INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        )
    """,
    "subscriptions": """
        CREATE TABLE "subscriptions" (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'trial', trial_start TEXT NOT NULL, trial_end TEXT NOT NULL,
            provider_customer_id TEXT, provider_subscription_id TEXT, updated_at TEXT NOT NULL
        )
    """,
    "notification_preferences": """
        CREATE TABLE "notification_preferences" (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            enabled INTEGER NOT NULL DEFAULT 1, permission_state TEXT NOT NULL DEFAULT 'default'
        )
    """,
    "password_reset_tokens": """
        CREATE TABLE "password_reset_tokens" (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL
        )
    """,
    "push_subscriptions": """
        CREATE TABLE "push_subscriptions" (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            endpoint TEXT NOT NULL UNIQUE, subscription_json TEXT NOT NULL, created_at TEXT NOT NULL
        )
    """,
    "idempotency_keys": """
        CREATE TABLE "idempotency_keys" (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            operation_key TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(user_id, operation_key)
        )
    """,
}
HARDENED_TABLE_ORDER = tuple(REQUIRED_SCHEMA_COLUMNS)
HARDENED_DROP_ORDER = tuple(reversed(HARDENED_TABLE_ORDER))
HARDENED_INDEXES = {
    "idx_routines_user_active_position": "CREATE INDEX idx_routines_user_active_position ON routines(user_id, archived, position)",
    "idx_tasks_routine_active_position": "CREATE INDEX idx_tasks_routine_active_position ON tasks(routine_id, archived, position)",
    "idx_reminders_due": "CREATE INDEX idx_reminders_due ON reminders(status, delivered_at, snoozed_until, due_at)",
    "idx_reminders_user_status": "CREATE INDEX idx_reminders_user_status ON reminders(user_id, status)",
    "idx_timers_user_status": "CREATE INDEX idx_timers_user_status ON timers(user_id, status)",
    "idx_categories_user_position": "CREATE INDEX idx_categories_user_position ON categories(user_id, position, name)",
    "idx_goals_user_created": "CREATE INDEX idx_goals_user_created ON goals(user_id, created_at)",
    "idx_milestones_user_achieved": "CREATE INDEX idx_milestones_user_achieved ON milestones(user_id, achieved_at)",
    "idx_automations_user_trigger": "CREATE INDEX idx_automations_user_trigger ON automations(user_id, trigger_task_id, enabled)",
    "idx_password_reset_user_validity": "CREATE INDEX idx_password_reset_user_validity ON password_reset_tokens(user_id, used_at, expires_at)",
    "idx_push_subscriptions_user": "CREATE INDEX idx_push_subscriptions_user ON push_subscriptions(user_id)",
    "idx_subscriptions_provider": "CREATE INDEX idx_subscriptions_provider ON subscriptions(provider_subscription_id)",
}


def validate_schema_migrations_table(conn):
    object_row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = 'schema_migrations'"
    ).fetchone()
    if not object_row:
        return
    if object_row[0] != "table":
        raise RuntimeError("schema_migrations has an incompatible schema object")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(schema_migrations)").fetchall()}
    required = {"version", "name", "applied_at", "checksum"}
    if not required.issubset(columns):
        raise RuntimeError("schema_migrations has an incompatible schema")


def validate_current_schema(conn, required_columns=REQUIRED_SCHEMA_COLUMNS):
    validate_schema_migrations_table(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    missing_tables = sorted(set(required_columns) - tables)
    if missing_tables:
        raise RuntimeError(f"database is missing required tables: {', '.join(missing_tables)}")
    for table, expected_columns in required_columns.items():
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
        missing_columns = sorted(expected_columns - columns)
        if missing_columns:
            raise RuntimeError(f"table {table} is missing required columns: {', '.join(missing_columns)}")


def ensure_schema_baseline(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            checksum TEXT NOT NULL
        )
        """
    )
    row = conn.execute(
        "SELECT version, name, checksum FROM schema_migrations WHERE version = ?",
        (SCHEMA_MIGRATION_VERSION,),
    ).fetchone()
    if row:
        if row[1] != SCHEMA_MIGRATION_NAME or row[2] != SCHEMA_MIGRATION_CHECKSUM:
            raise RuntimeError("schema migration checksum mismatch")
        return
    conn.execute(
        "INSERT INTO schema_migrations (version, name, applied_at, checksum) VALUES (?, ?, ?, ?)",
        (SCHEMA_MIGRATION_VERSION, SCHEMA_MIGRATION_NAME, utc_now_iso(), SCHEMA_MIGRATION_CHECKSUM),
    )


def validate_relationship_data(conn):
    checks = {
        "routines without users": "SELECT 1 FROM routines WHERE user_id IS NOT NULL AND user_id NOT IN (SELECT id FROM users) LIMIT 1",
        "tasks without routines": "SELECT 1 FROM tasks WHERE routine_id NOT IN (SELECT id FROM routines) LIMIT 1",
        "routine history without routines": "SELECT 1 FROM routine_history WHERE routine_id NOT IN (SELECT id FROM routines) LIMIT 1",
        "task history without tasks": "SELECT 1 FROM task_history WHERE task_id NOT IN (SELECT id FROM tasks) LIMIT 1",
        "schedules without routines": "SELECT 1 FROM schedules WHERE routine_id NOT IN (SELECT id FROM routines) LIMIT 1",
        "categories without users": "SELECT 1 FROM categories WHERE user_id NOT IN (SELECT id FROM users) LIMIT 1",
        "reminders without users": "SELECT 1 FROM reminders WHERE user_id NOT IN (SELECT id FROM users) LIMIT 1",
        "reminders with invalid routines": "SELECT 1 FROM reminders WHERE routine_id IS NOT NULL AND routine_id NOT IN (SELECT id FROM routines) LIMIT 1",
        "reminders with invalid tasks": "SELECT 1 FROM reminders WHERE task_id IS NOT NULL AND task_id NOT IN (SELECT id FROM tasks) LIMIT 1",
        "timers without users": "SELECT 1 FROM timers WHERE user_id NOT IN (SELECT id FROM users) LIMIT 1",
        "timers with invalid routines": "SELECT 1 FROM timers WHERE routine_id IS NOT NULL AND routine_id NOT IN (SELECT id FROM routines) LIMIT 1",
        "timers with invalid tasks": "SELECT 1 FROM timers WHERE task_id IS NOT NULL AND task_id NOT IN (SELECT id FROM tasks) LIMIT 1",
        "goals without users": "SELECT 1 FROM goals WHERE user_id NOT IN (SELECT id FROM users) LIMIT 1",
        "milestones without users": "SELECT 1 FROM milestones WHERE user_id NOT IN (SELECT id FROM users) LIMIT 1",
        "automations without users": "SELECT 1 FROM automations WHERE user_id NOT IN (SELECT id FROM users) LIMIT 1",
        "automations with invalid trigger tasks": "SELECT 1 FROM automations WHERE trigger_task_id IS NOT NULL AND trigger_task_id NOT IN (SELECT id FROM tasks) LIMIT 1",
        "automations with invalid target tasks": "SELECT 1 FROM automations WHERE target_task_id IS NOT NULL AND target_task_id NOT IN (SELECT id FROM tasks) LIMIT 1",
        "subscriptions without users": "SELECT 1 FROM subscriptions WHERE user_id NOT IN (SELECT id FROM users) LIMIT 1",
        "notification preferences without users": "SELECT 1 FROM notification_preferences WHERE user_id NOT IN (SELECT id FROM users) LIMIT 1",
        "password reset tokens without users": "SELECT 1 FROM password_reset_tokens WHERE user_id NOT IN (SELECT id FROM users) LIMIT 1",
        "push subscriptions without users": "SELECT 1 FROM push_subscriptions WHERE user_id NOT IN (SELECT id FROM users) LIMIT 1",
        "idempotency keys without users": "SELECT 1 FROM idempotency_keys WHERE user_id NOT IN (SELECT id FROM users) LIMIT 1",
        "reminders with cross-user routine": "SELECT 1 FROM reminders JOIN routines ON routines.id = reminders.routine_id WHERE routines.user_id IS NOT NULL AND routines.user_id != reminders.user_id LIMIT 1",
        "reminders with cross-user task": "SELECT 1 FROM reminders JOIN tasks ON tasks.id = reminders.task_id JOIN routines ON routines.id = tasks.routine_id WHERE routines.user_id IS NOT NULL AND routines.user_id != reminders.user_id LIMIT 1",
        "reminders with inconsistent task routine": "SELECT 1 FROM reminders JOIN tasks ON tasks.id = reminders.task_id WHERE reminders.routine_id IS NOT NULL AND tasks.routine_id != reminders.routine_id LIMIT 1",
        "timers with cross-user routine": "SELECT 1 FROM timers JOIN routines ON routines.id = timers.routine_id WHERE routines.user_id IS NOT NULL AND routines.user_id != timers.user_id LIMIT 1",
        "timers with cross-user task": "SELECT 1 FROM timers JOIN tasks ON tasks.id = timers.task_id JOIN routines ON routines.id = tasks.routine_id WHERE routines.user_id IS NOT NULL AND routines.user_id != timers.user_id LIMIT 1",
        "timers with inconsistent task routine": "SELECT 1 FROM timers JOIN tasks ON tasks.id = timers.task_id WHERE timers.routine_id IS NOT NULL AND tasks.routine_id != timers.routine_id LIMIT 1",
        "automations with cross-user trigger": "SELECT 1 FROM automations JOIN tasks ON tasks.id = automations.trigger_task_id JOIN routines ON routines.id = tasks.routine_id WHERE routines.user_id IS NOT NULL AND routines.user_id != automations.user_id LIMIT 1",
        "automations with cross-user target": "SELECT 1 FROM automations JOIN tasks ON tasks.id = automations.target_task_id JOIN routines ON routines.id = tasks.routine_id WHERE routines.user_id IS NOT NULL AND routines.user_id != automations.user_id LIMIT 1",
    }
    violations = [name for name, query in checks.items() if conn.execute(query).fetchone()]
    if violations:
        raise RuntimeError("foreign-key migration blocked by invalid data: " + ", ".join(violations))


def hardened_table_sql(include_google_sub=True):
    return dict(HARDENED_TABLE_SQL)


def rebuild_hardened_schema(conn, include_google_sub=True):
    validate_relationship_data(conn)
    goal_routine_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'goal_routines'"
    ).fetchone()
    goal_routine_rows = conn.execute("SELECT goal_id, routine_id FROM goal_routines").fetchall() if goal_routine_exists else []
    if goal_routine_exists:
        conn.execute("DROP TABLE goal_routines")
    definitions = hardened_table_sql(include_google_sub)
    preserved_indexes = [
        row[1]
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
        ).fetchall()
        if row[1] and row[0] not in HARDENED_INDEXES and row[0] != "idx_users_google_sub" and "google_sub" not in row[1].lower()
    ]
    table_info = {
        table: conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        for table in HARDENED_TABLE_ORDER
    }
    columns_by_table = {
        table: [row[1] for row in table_info[table] if row[1] != "google_sub"]
        for table in HARDENED_TABLE_ORDER
    }
    for table in HARDENED_TABLE_ORDER:
        backup = f"__schema_backup_{table}"
        columns = ", ".join(f'"{column}"' for column in columns_by_table[table])
        conn.execute(f'CREATE TEMP TABLE "{backup}" AS SELECT {columns} FROM "{table}"')

    for table in HARDENED_DROP_ORDER:
        conn.execute(f'DROP TABLE "{table}"')

    for table in HARDENED_TABLE_ORDER:
        conn.execute(definitions[table])
        defined_columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
        for row in table_info[table]:
            column = row[1]
            if column not in columns_by_table[table] or column in defined_columns:
                continue
            column_type = row[2] or "TEXT"
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {column_type}')
            defined_columns.add(column)
        columns = ", ".join(f'"{column}"' for column in columns_by_table[table])
        target_columns = columns
        conn.execute(
            f'INSERT INTO "{table}" ({target_columns}) SELECT {target_columns} FROM "__schema_backup_{table}"'
        )

    for index_sql in HARDENED_INDEXES.values():
        conn.execute(index_sql)
    for index_sql in preserved_indexes:
        conn.execute(index_sql)
    if goal_routine_rows:
        conn.execute(
            "CREATE TABLE goal_routines (goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE, routine_id INTEGER NOT NULL REFERENCES routines(id) ON DELETE CASCADE, PRIMARY KEY (goal_id, routine_id))"
        )
        conn.executemany("INSERT INTO goal_routines (goal_id, routine_id) VALUES (?, ?)", goal_routine_rows)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_goal_routines_routine ON goal_routines(routine_id)")

    for table in HARDENED_TABLE_ORDER:
        conn.execute(f'DROP TABLE "__schema_backup_{table}"')
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"foreign-key check failed after rebuild: {violations[0]}")


def add_task_duration_column(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "target_minutes" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN target_minutes INTEGER NOT NULL DEFAULT 0")


def add_task_date_columns(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "task_date" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN task_date TEXT")
    if "is_default" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0")


SCHEMA_MIGRATIONS = {
    2: {
        "name": "foreign_keys_and_indexes",
        "checksum": hashlib.sha256(b"2:foreign_keys_and_indexes").hexdigest(),
        "apply": lambda conn: rebuild_hardened_schema(conn, include_google_sub=True),
    },
    3: {
        "name": "remove_google_auth_schema",
        "checksum": hashlib.sha256(b"3:remove_google_auth_schema").hexdigest(),
        "apply": lambda conn: rebuild_hardened_schema(conn, include_google_sub=False),
    },
    4: {
        "name": "goal_routine_relationships",
        "checksum": hashlib.sha256(b"4:goal_routine_relationships").hexdigest(),
        "apply": lambda conn: (
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS goal_routines (
                    goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
                    routine_id INTEGER NOT NULL REFERENCES routines(id) ON DELETE CASCADE,
                    PRIMARY KEY (goal_id, routine_id)
                )
                """
            ),
            conn.execute("CREATE INDEX IF NOT EXISTS idx_goal_routines_routine ON goal_routines(routine_id)"),
        ),
    },
    5: {
        "name": "task_duration_minutes",
        "checksum": hashlib.sha256(b"5:task_duration_minutes").hexdigest(),
        "apply": add_task_duration_column,
    },
    6: {
        "name": "date_specific_tasks",
        "checksum": hashlib.sha256(b"6:date_specific_tasks").hexdigest(),
        "apply": add_task_date_columns,
    },
}


def apply_schema_migrations(conn):
    ensure_schema_baseline(conn)
    known_versions = {SCHEMA_MIGRATION_VERSION, *SCHEMA_MIGRATIONS}
    unknown_versions = [
        row[0]
        for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        if row[0] not in known_versions
    ]
    if unknown_versions:
        raise RuntimeError(f"unsupported schema migration versions: {unknown_versions}")
    for version, migration in SCHEMA_MIGRATIONS.items():
        row = conn.execute(
            "SELECT name, checksum FROM schema_migrations WHERE version = ?",
            (version,),
        ).fetchone()
        if row:
            if row[0] != migration["name"] or row[1] != migration["checksum"]:
                raise RuntimeError(f"schema migration checksum mismatch for version {version}")
            continue
        migration["apply"](conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at, checksum) VALUES (?, ?, ?, ?)",
            (version, migration["name"], utc_now_iso(), migration["checksum"]),
        )


@app.before_request
def protect_state_changes():
    if request.method not in {"POST", "PATCH", "PUT", "DELETE"} or app.config.get("TESTING") or not accounts_mode():
        return None
    if request.endpoint in {"billing_webhook", "health", "static"}:
        return None
    if current_user_id() is None and request.endpoint not in {"login", "register", "forgot_password", "reset_password"}:
        return None
    token = request.headers.get("X-CSRFToken") or request.form.get("csrf_token")
    expected = session.get("csrf_token")
    if not expected or not token or not hmac.compare_digest(token, expected):
        return jsonify({"error": "csrf validation failed"}), 400
    return None


def is_local_request():
    hostname = request.host.split(":", 1)[0].lower()
    return hostname in {"localhost", "127.0.0.1", "::1"}


@app.before_request
def establish_local_session():
    if accounts_mode():
        return None
    init_db()
    with get_db_connection() as conn:
        user = conn.execute("SELECT id, email FROM users WHERE email = ?", ("local@localhost",)).fetchone()
        if not user:
            cursor = conn.execute(
                "INSERT INTO users (email, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
                ("local@localhost", hash_password(secrets.token_urlsafe(32)), "Local user", today_iso()),
            )
            user = {"id": cursor.lastrowid, "email": "local@localhost"}
            create_trial(conn, user["id"])
    session["csrf_token"] = secrets.token_urlsafe(32)
    session["user_id"] = user["id"]
    session["user_email"] = user["email"]
    return None


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), microphone=()")
    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


def ensure_db_directory():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_db_connection():
    ensure_db_directory()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120000).hex()
    return f"{salt}${digest}"


def check_password(password, stored_password):
    try:
        salt, expected = stored_password.split("$", 1)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120000).hex()
    return hmac.compare_digest(actual, expected)


def current_user_id():
    return session.get("user_id")


def accounts_mode():
    return app.config.get("AUTH_MODE") == "accounts" and not app.config.get("LOCAL_MODE")


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if current_user_id() is None:
            if not accounts_mode():
                establish_local_session()
            else:
                if request.path.startswith("/api/") or request.is_json:
                    return jsonify({"error": "authentication required"}), 401
                return redirect(url_for("login", next=request.full_path.rstrip("?")))
        if current_user_id() is not None:
            with get_db_connection() as conn:
                if not conn.execute("SELECT 1 FROM users WHERE id = ?", (current_user_id(),)).fetchone():
                    session.clear()
                    if request.path.startswith("/api/") or request.is_json:
                        return jsonify({"error": "authentication required"}), 401
                    return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapped_view


def today_iso():
    return date.today().isoformat()


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def create_trial(conn, user_id):
    start = datetime.now(timezone.utc).date()
    end = start + timedelta(days=30)
    conn.execute(
        "INSERT OR IGNORE INTO subscriptions (user_id, status, trial_start, trial_end, updated_at) VALUES (?, 'trial', ?, ?, ?)",
        (user_id, start.isoformat(), end.isoformat(), utc_now_iso()),
    )


def safe_next_url(value):
    value = (value or "").strip()
    return value if value.startswith("/") and not value.startswith("//") else None


def get_owned_routine(conn, routine_id):
    return conn.execute(
        "SELECT * FROM routines WHERE id = ? AND user_id = ?",
        (routine_id, current_user_id()),
    ).fetchone()


def get_owned_task(conn, task_id):
    return conn.execute(
        """
        SELECT tasks.*
        FROM tasks
        JOIN routines ON routines.id = tasks.routine_id
        WHERE tasks.id = ? AND routines.user_id = ?
        """,
        (task_id, current_user_id()),
    ).fetchone()


def validate_owned_reference_ids(conn, routine_id=None, task_id=None):
    try:
        routine_id = int(routine_id) if routine_id is not None else None
        task_id = int(task_id) if task_id is not None else None
    except (TypeError, ValueError):
        return None, None, "routine_id and task_id must be numbers"

    routine = get_owned_routine(conn, routine_id) if routine_id is not None else None
    task = get_owned_task(conn, task_id) if task_id is not None else None
    if routine_id is not None and not routine:
        return None, None, "routine not found"
    if task_id is not None and not task:
        return None, None, "task not found"
    if routine and task and task["routine_id"] != routine["id"]:
        return None, None, "task does not belong to routine"
    return routine_id, task_id, None


def send_password_reset_email(email, reset_url):
    host = os.environ.get("SMTP_HOST")
    sender = os.environ.get("SMTP_SENDER")
    if not host or not sender:
        return False
    message = EmailMessage()
    message["Subject"] = "Reset your Routine Tracker password"
    message["From"] = sender
    message["To"] = email
    message.set_content(f"Reset your password within one hour:\n\n{reset_url}\n\nIf you did not request this, ignore this message.")
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=15) as smtp:
        if os.environ.get("SMTP_USE_TLS", "1") == "1":
            smtp.starttls()
        username = os.environ.get("SMTP_USERNAME")
        password = os.environ.get("SMTP_PASSWORD")
        if username and password:
            smtp.login(username, password)
        smtp.send_message(message)
    return True


def get_week_dates():
    today = date.today()
    start = today - timedelta(days=today.weekday())
    return [start + timedelta(days=i) for i in range(7)]


def init_db():
    with get_db_connection() as conn:
        latest_version = 0
        existing_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if existing_tables:
            validate_schema_migrations_table(conn)
            missing_tables = sorted(set(REQUIRED_SCHEMA_COLUMNS) - existing_tables)
            if missing_tables:
                raise RuntimeError(f"database is missing required tables: {', '.join(missing_tables)}")
            if "schema_migrations" in existing_tables:
                latest_version = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
                validate_current_schema(conn, REQUIRED_SCHEMA_COLUMNS)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                display_name TEXT,
                timezone TEXT NOT NULL DEFAULT 'UTC',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                routine_id INTEGER,
                task_id INTEGER,
                title TEXT NOT NULL,
                due_at TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'UTC',
                recurrence TEXT NOT NULL DEFAULT 'once',
                status TEXT NOT NULL DEFAULT 'pending',
                snoozed_until TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_preferences (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                permission_state TEXT NOT NULL DEFAULT 'default'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS timers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                routine_id INTEGER,
                task_id INTEGER,
                title TEXT NOT NULL,
                duration_seconds INTEGER NOT NULL,
                started_at TEXT,
                paused_at TEXT,
                paused_seconds INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ready',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'trial',
                trial_start TEXT NOT NULL,
                trial_end TEXT NOT NULL,
                provider_customer_id TEXT,
                provider_subscription_id TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                target INTEGER NOT NULL,
                start_date TEXT,
                end_date TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS milestones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                achieved_at TEXT,
                UNIQUE(user_id, name)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS automations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                trigger_type TEXT NOT NULL,
                trigger_task_id INTEGER,
                action_type TEXT NOT NULL,
                target_task_id INTEGER,
                delay_minutes INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                endpoint TEXT NOT NULL UNIQUE,
                subscription_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                user_id INTEGER NOT NULL,
                operation_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, operation_key)
            )
            """
        )
        reminder_columns = [row["name"] for row in conn.execute("PRAGMA table_info(reminders)").fetchall()]
        if "delivery_attempts" not in reminder_columns:
            conn.execute("ALTER TABLE reminders ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0")
        if "delivered_at" not in reminder_columns:
            conn.execute("ALTER TABLE reminders ADD COLUMN delivered_at TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS routines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                category TEXT,
                target_minutes INTEGER DEFAULT 20,
                completed INTEGER DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                paused INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0,
                start_date TEXT,
                end_date TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                color TEXT NOT NULL DEFAULT '#5b5ce6',
                position INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_id, name)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                routine_id INTEGER NOT NULL UNIQUE,
                frequency TEXT NOT NULL DEFAULT 'daily',
                time_of_day TEXT,
                weekdays TEXT,
                start_date TEXT,
                end_date TEXT,
                timezone TEXT NOT NULL DEFAULT 'UTC',
                enabled INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS routine_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                routine_id INTEGER NOT NULL,
                completion_date TEXT NOT NULL,
                completed INTEGER DEFAULT 0,
                minutes_done INTEGER DEFAULT 0,
                UNIQUE(routine_id, completion_date)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                routine_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                position INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                completion_date TEXT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0,
                UNIQUE(task_id, completion_date)
            )
            """
        )

        routine_columns = [row["name"] for row in conn.execute("PRAGMA table_info(routines)").fetchall()]
        if "target_minutes" not in routine_columns:
            conn.execute("ALTER TABLE routines ADD COLUMN target_minutes INTEGER DEFAULT 20")
        if "user_id" not in routine_columns:
            conn.execute("ALTER TABLE routines ADD COLUMN user_id INTEGER")
        if "archived" not in routine_columns:
            conn.execute("ALTER TABLE routines ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        if "paused" not in routine_columns:
            conn.execute("ALTER TABLE routines ADD COLUMN paused INTEGER NOT NULL DEFAULT 0")
        if "position" not in routine_columns:
            conn.execute("ALTER TABLE routines ADD COLUMN position INTEGER NOT NULL DEFAULT 0")
        if "start_date" not in routine_columns:
            conn.execute("ALTER TABLE routines ADD COLUMN start_date TEXT")
        if "end_date" not in routine_columns:
            conn.execute("ALTER TABLE routines ADD COLUMN end_date TEXT")

        existing_categories = conn.execute(
            """
            SELECT DISTINCT user_id, TRIM(category) AS name
            FROM routines
            WHERE user_id IS NOT NULL AND category IS NOT NULL AND TRIM(category) != ''
            """
        ).fetchall()
        for category in existing_categories:
            conn.execute(
                "INSERT OR IGNORE INTO categories (user_id, name) VALUES (?, ?)",
                (category["user_id"], category["name"]),
            )

        user_columns = [row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "display_name" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
        if "timezone" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC'")
        history_columns = [row["name"] for row in conn.execute("PRAGMA table_info(routine_history)").fetchall()]
        if "minutes_done" not in history_columns:
            conn.execute("ALTER TABLE routine_history ADD COLUMN minutes_done INTEGER DEFAULT 0")

        validate_current_schema(conn, REQUIRED_SCHEMA_COLUMNS)
        apply_schema_migrations(conn)
        validate_current_schema(conn, FINAL_SCHEMA_COLUMNS)
        conn.commit()


def get_period_count(conn, routine_id, days):
    start_date = (date.today() - timedelta(days=days - 1)).isoformat()
    row = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM routine_history
        WHERE routine_id = ? AND completion_date BETWEEN ? AND ? AND completed = 1
        """,
        (routine_id, start_date, today_iso()),
    ).fetchone()
    return row["total"] if row else 0


def get_streaks(conn, routine):
    schedule = conn.execute("SELECT * FROM schedules WHERE routine_id = ?", (routine["id"],)).fetchone()
    completed_dates = {
        row["completion_date"]
        for row in conn.execute(
            "SELECT completion_date FROM routine_history WHERE routine_id = ? AND completed = 1",
            (routine["id"],),
        ).fetchall()
    }
    expected = []
    for offset in range(365):
        day = date.today() - timedelta(days=offset)
        if routine_expected_on(routine, day, schedule):
            expected.append(day.isoformat())
    current = 0
    for iso_day in expected:
        if iso_day not in completed_dates:
            break
        current += 1
    best = 0
    running = 0
    for iso_day in reversed(expected):
        if iso_day in completed_dates:
            running += 1
            best = max(best, running)
        else:
            running = 0
    missed = sum(1 for iso_day in expected if iso_day < today_iso() and iso_day not in completed_dates)
    return current, best, missed


def get_progress_percent(minutes_done, target_minutes):
    minutes_done = max(0, int(minutes_done or 0))
    target_minutes = max(0, int(target_minutes or 0))

    if target_minutes == 0:
        return 100 if minutes_done > 0 else 0

    return min(100, round((minutes_done / target_minutes) * 100))


def routine_expected_on(routine, day, schedule=None):
    if routine["paused"] or routine["archived"]:
        return False
    if schedule and not schedule["enabled"]:
        return False
    start_date = (schedule["start_date"] if schedule else None) or routine["start_date"]
    end_date = (schedule["end_date"] if schedule else None) or routine["end_date"]
    if start_date and day.isoformat() < start_date:
        return False
    if end_date and day.isoformat() > end_date:
        return False
    if not schedule:
        return True
    frequency = schedule["frequency"]
    if frequency == "weekly":
        weekdays = {int(value) for value in (schedule["weekdays"] or "").split(",") if value.isdigit()}
        return day.weekday() in weekdays
    if frequency == "monthly":
        anchor = start_date or day.isoformat()
        return day.day == int(anchor[8:10])
    return True


def get_daily_task_progress(conn, routine_id, iso_date):
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN task_history.completed = 1 THEN CASE WHEN tasks.target_minutes > 0 THEN tasks.target_minutes ELSE routines.target_minutes END ELSE 0 END), 0) AS completed_minutes,
            COALESCE(SUM(CASE WHEN tasks.target_minutes > 0 THEN tasks.target_minutes ELSE routines.target_minutes END), 0) AS planned_minutes,
            COUNT(tasks.id) AS total_tasks,
            COALESCE(SUM(CASE WHEN task_history.completed = 1 THEN 1 ELSE 0 END), 0) AS completed_tasks
        FROM tasks
        JOIN routines ON routines.id = tasks.routine_id
        LEFT JOIN task_history ON task_history.task_id = tasks.id AND task_history.completion_date = ?
        WHERE tasks.routine_id = ? AND tasks.archived = 0
          AND (tasks.task_date IS NULL OR tasks.task_date = ?)
        """,
        (iso_date, routine_id, iso_date),
    ).fetchone()
    planned = int(row["planned_minutes"] or 0)
    completed = int(row["completed_minutes"] or 0)
    total_tasks = int(row["total_tasks"] or 0)
    completed_tasks = int(row["completed_tasks"] or 0)
    return {
        "planned_minutes": planned,
        "completed_minutes": completed,
        "percentage": get_progress_percent(completed, planned),
        "completed_tasks": completed_tasks,
        "total_tasks": total_tasks,
        "complete": total_tasks > 0 and completed_tasks == total_tasks,
    }


def get_week_entries(conn, routine):
    entries = []
    schedule = conn.execute(
        "SELECT frequency, time_of_day, weekdays, start_date, end_date, timezone, enabled FROM schedules WHERE routine_id = ?",
        (routine["id"],),
    ).fetchone()
    for day in get_week_dates():
        iso_date = day.isoformat()
        scheduled = routine_expected_on(routine, day, schedule)
        record = conn.execute(
            "SELECT completed, minutes_done FROM routine_history WHERE routine_id = ? AND completion_date = ?",
            (routine["id"], iso_date),
        ).fetchone()
        minutes_done = int(record["minutes_done"]) if record and record["minutes_done"] is not None else 0
        checked = bool(record and record["minutes_done"] is not None and int(record["minutes_done"]) > 0)
        target_minutes = int(routine["target_minutes"] or 0)
        task_progress = get_daily_task_progress(conn, routine["id"], iso_date)
        if task_progress["total_tasks"] > 0:
            target_minutes = task_progress["planned_minutes"]
            minutes_done = task_progress["completed_minutes"]
            checked = task_progress["complete"]
        else:
            task_progress = {
                "planned_minutes": target_minutes if scheduled else 0,
                "completed_minutes": minutes_done if scheduled else 0,
                "percentage": get_progress_percent(minutes_done if scheduled else 0, target_minutes if scheduled else 0),
                "completed_tasks": 0,
                "total_tasks": 0,
                "complete": False,
            }
        entries.append(
            {
                "day_name": day.strftime("%a"),
                "date": day,
                "iso": iso_date,
                "scheduled": scheduled,
                "checked": checked,
                "minutes_done": minutes_done,
                "target_minutes": target_minutes,
                "progress_percent": get_progress_percent(minutes_done, target_minutes),
                "planned_minutes": task_progress["planned_minutes"],
                "completed_minutes": task_progress["completed_minutes"],
                "completed_tasks": task_progress["completed_tasks"],
                "total_tasks": task_progress["total_tasks"],
                "fully_completed": task_progress["complete"],
            }
        )
    return entries


def get_weekly_progress(week_entries):
    planned = sum(entry["planned_minutes"] for entry in week_entries)
    completed = sum(entry["completed_minutes"] for entry in week_entries)
    completed_tasks = sum(entry["completed_tasks"] for entry in week_entries)
    total_tasks = sum(entry["total_tasks"] for entry in week_entries)
    return {
        "days": [
            {
                "iso": entry["iso"],
                "percentage": entry["progress_percent"],
                "planned_minutes": entry["planned_minutes"],
                "completed_minutes": entry["completed_minutes"],
                "completed_tasks": entry["completed_tasks"],
                "total_tasks": entry["total_tasks"],
                "complete": entry["fully_completed"],
            }
            for entry in week_entries
        ],
        "planned_minutes": planned,
        "completed_minutes": completed,
        "completed_tasks": completed_tasks,
        "total_tasks": total_tasks,
        "completed_days": sum(1 for entry in week_entries if entry["fully_completed"]),
        "percentage": get_progress_percent(completed, planned),
    }


def get_calendar_daily_progress(conn, routines, iso_date):
            planned = completed = completed_tasks = total_tasks = completed_days = 0
            routine_rows = []
            day = datetime.strptime(iso_date, "%Y-%m-%d").date()
            for routine in routines:
                schedule = conn.execute("SELECT * FROM schedules WHERE routine_id = ?", (routine["id"],)).fetchone()
                if not routine_expected_on(routine, day, schedule):
                    continue
                daily = get_daily_task_progress(conn, routine["id"], iso_date)
                if daily["total_tasks"] == 0:
                    record = conn.execute(
                        "SELECT minutes_done FROM routine_history WHERE routine_id = ? AND completion_date = ?",
                        (routine["id"], iso_date),
                    ).fetchone()
                    minutes = int(record["minutes_done"] or 0) if record else 0
                    target = int(routine["target_minutes"] or 0)
                    daily = {
                        "planned_minutes": target,
                        "completed_minutes": minutes,
                        "percentage": get_progress_percent(minutes, target),
                        "completed_tasks": 0,
                        "total_tasks": 0,
                        "complete": bool(record and minutes >= target and target > 0),
                    }
                task_rows = []
                if daily["total_tasks"]:
                    task_rows = [
                        {
                            "id": task["id"],
                            "title": task["title"],
                            "planned_minutes": int(task["target_minutes"] or 0),
                            "completed": bool(task["completed"]),
                        }
                        for task in conn.execute(
                            """
                            SELECT tasks.id, tasks.title, tasks.target_minutes,
                                   COALESCE(task_history.completed, 0) AS completed
                            FROM tasks
                            LEFT JOIN task_history
                              ON task_history.task_id = tasks.id
                             AND task_history.completion_date = ?
                            WHERE tasks.routine_id = ? AND tasks.archived = 0
                              AND (tasks.task_date IS NULL OR tasks.task_date = ?)
                            ORDER BY tasks.position, tasks.id
                            """,
                            (iso_date, routine["id"], iso_date),
                        ).fetchall()
                    ]
                planned += daily["planned_minutes"]
                completed += daily["completed_minutes"]
                completed_tasks += daily["completed_tasks"]
                total_tasks += daily["total_tasks"]
                completed_days += int(daily["complete"])
                routine_rows.append({"id": routine["id"], "title": routine["title"], "tasks": task_rows, **daily})
            return {
                "planned_minutes": planned,
                "completed_minutes": completed,
                "percentage": get_progress_percent(completed, planned),
                "completed_tasks": completed_tasks,
                "total_tasks": total_tasks,
                "complete": bool(routine_rows) and completed_days == len(routine_rows),
                "completed_days": completed_days,
                "routines": routine_rows,
            }


def get_monthly_progress(conn, routines, month):
    try:
        first = datetime.strptime(month, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise ValueError("month must use YYYY-MM") from exc
    next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    days = []
    cursor = first
    planned = completed = completed_tasks = total_tasks = completed_days = 0
    while cursor < next_month:
        daily = get_calendar_daily_progress(conn, routines, cursor.isoformat())
        entry = {"date": cursor.isoformat(), **daily}
        days.append(entry)
        planned += daily["planned_minutes"]
        completed += daily["completed_minutes"]
        completed_tasks += daily["completed_tasks"]
        total_tasks += daily["total_tasks"]
        completed_days += int(daily["complete"])
        cursor += timedelta(days=1)
    return {
        "month": month,
        "days": days,
        "planned_minutes": planned,
        "completed_minutes": completed,
        "completed_tasks": completed_tasks,
        "total_tasks": total_tasks,
        "completed_days": completed_days,
        "percentage": get_progress_percent(completed, planned),
    }


def save_day_entry(conn, routine, iso_day, checked, minutes_raw=None):
    target_minutes = int(routine["target_minutes"] or 0)

    try:
        minutes = int(minutes_raw) if minutes_raw not in ("", None) else 0
    except (TypeError, ValueError):
        minutes = 0

    if checked:
        if minutes == 0:
            minutes = target_minutes
        completed = 1 if minutes >= target_minutes else 0
        conn.execute(
            """
            INSERT INTO routine_history (routine_id, completion_date, completed, minutes_done)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(routine_id, completion_date)
            DO UPDATE SET completed = excluded.completed, minutes_done = excluded.minutes_done
            """,
            (routine["id"], iso_day, completed, minutes),
        )
    else:
        minutes = 0
        conn.execute(
            "DELETE FROM routine_history WHERE routine_id = ? AND completion_date = ?",
            (routine["id"], iso_day),
        )

    conn.execute(
        "UPDATE routines SET completed = ? WHERE id = ?",
        (
            1
            if conn.execute(
                "SELECT 1 FROM routine_history WHERE routine_id = ? AND completion_date = ?",
                (routine["id"], today_iso()),
            ).fetchone()
            else 0,
            routine["id"],
        ),
    )

    return {
        "iso": iso_day,
        "checked": checked and minutes > 0,
        "minutes_done": minutes,
        "progress_percent": get_progress_percent(minutes, target_minutes),
    }


def get_routine_week_summary(conn, routine, selected_date=None):
    selected_date = selected_date or today_iso()
    week_entries = get_week_entries(conn, routine)
    weekly_progress = get_weekly_progress(week_entries)
    week_count = weekly_progress["completed_days"]
    week_progress = weekly_progress["percentage"]

    today_record = conn.execute(
        "SELECT minutes_done FROM routine_history WHERE routine_id = ? AND completion_date = ?",
        (routine["id"], selected_date),
    ).fetchone()
    today_minutes = int(today_record["minutes_done"]) if today_record and today_record["minutes_done"] is not None else 0
    task_progress = get_daily_task_progress(conn, routine["id"], selected_date)
    has_tasks = task_progress["total_tasks"] > 0
    task_minutes = task_progress["completed_minutes"]
    task_goal_minutes = task_progress["planned_minutes"]
    progress_minutes = task_minutes if has_tasks else today_minutes
    total_goal_minutes = task_goal_minutes if has_tasks else int(routine["target_minutes"] or 0)
    schedule = conn.execute("SELECT * FROM schedules WHERE routine_id = ?", (routine["id"],)).fetchone()
    selected_day = date.fromisoformat(selected_date)
    today_done = progress_minutes > 0 and get_progress_percent(progress_minutes, total_goal_minutes) >= 100

    return {
        "week_count": week_count,
        "week_progress": week_progress,
        "today_done": bool(today_done),
        "today_minutes": progress_minutes,
        "task_minutes": task_minutes,
        "task_goal_minutes": task_goal_minutes,
        "today_goal_minutes": total_goal_minutes,
        "daily_progress": task_progress,
        "weekly_progress": weekly_progress,
    }


def get_global_stats(conn, category=None):
    _, stats, _ = get_dashboard_data(conn, category)
    return stats


def get_dashboard_data(conn, category=None, selected_date=None):
    selected_date = selected_date or today_iso()
    query = "SELECT * FROM routines WHERE user_id = ? AND archived = 0"
    params = [current_user_id()]
    if category and category != "all":
        query += " AND category = ?"
        params.append(category)
    query += " ORDER BY position ASC, created_at DESC, id DESC"

    routines = conn.execute(query, params).fetchall()
    routine_ids = [routine["id"] for routine in routines]
    tasks_by_routine = {routine_id: [] for routine_id in routine_ids}
    if routine_ids:
        placeholders = ",".join("?" for _ in routine_ids)
        for routine in routines:
            task_exists = conn.execute(
                """
                SELECT 1 FROM tasks
                WHERE routine_id = ? AND archived = 0
                  AND (task_date IS NULL OR task_date = ?)
                LIMIT 1
                """,
                (routine["id"], selected_date),
            ).fetchone()
            if not task_exists:
                conn.execute(
                    """
                    INSERT INTO tasks (routine_id, title, description, target_minutes, task_date, is_default, position, created_at)
                    VALUES (?, ?, '', ?, ?, 1, 0, ?)
                    """,
                    (routine["id"], routine["title"], int(routine["target_minutes"] or 0), selected_date, today_iso()),
                )
        task_rows = conn.execute(
            f"""
            SELECT tasks.id, tasks.routine_id, tasks.title, tasks.description,
                   tasks.target_minutes, tasks.position, task_history.completed
            FROM tasks
            LEFT JOIN task_history
              ON task_history.task_id = tasks.id AND task_history.completion_date = ?
            WHERE tasks.routine_id IN ({placeholders}) AND tasks.archived = 0
              AND (tasks.task_date IS NULL OR tasks.task_date = ?)
            ORDER BY tasks.routine_id, tasks.position, tasks.id
            """,
            [selected_date, *routine_ids, selected_date],
        ).fetchall()
        for task in task_rows:
            tasks_by_routine[task["routine_id"]].append(
                {
                    "id": task["id"],
                    "title": task["title"],
                    "description": task["description"] or "",
                    "target_minutes": int(task["target_minutes"] or 0),
                    "completed": bool(task["completed"]),
                }
            )

    routine_cards = []
    total_routines = len(routines)
    today_completed = 0
    week_total = 0
    month_total = 0
    year_total = 0
    total_missed = 0
    current_streak = 0
    best_streak = 0
    total_week_minutes_done = 0
    total_week_minutes_goal = 0
    week_expected = 0

    for routine in routines:
        week_entries = get_week_entries(conn, routine)
        weekly_progress = get_weekly_progress(week_entries)
        week_count = weekly_progress["completed_days"]
        week_total += week_count
        week_expected += sum(1 for entry in week_entries if entry["scheduled"])
        total_week_minutes_done += weekly_progress["completed_minutes"]
        total_week_minutes_goal += weekly_progress["planned_minutes"]

        today_record = conn.execute(
            "SELECT minutes_done, completed FROM routine_history WHERE routine_id = ? AND completion_date = ?",
            (routine["id"], today_iso()),
        ).fetchone()
        today_minutes = int(today_record["minutes_done"]) if today_record and today_record["minutes_done"] is not None else 0
        daily_progress = get_daily_task_progress(conn, routine["id"], selected_date)
        has_tasks = daily_progress["total_tasks"] > 0
        task_minutes = daily_progress["completed_minutes"]
        task_goal_minutes = daily_progress["planned_minutes"]
        progress_minutes = task_minutes if has_tasks else today_minutes
        total_goal_minutes = task_goal_minutes if has_tasks else int(routine["target_minutes"] or 0)
        schedule = conn.execute("SELECT * FROM schedules WHERE routine_id = ?", (routine["id"],)).fetchone()
        today_done = routine_expected_on(routine, date.today(), schedule) and progress_minutes > 0 and get_progress_percent(progress_minutes, total_goal_minutes) >= 100
        if today_done:
            today_completed += 1

        month_count = get_period_count(conn, routine["id"], 30)
        year_count = get_period_count(conn, routine["id"], 365)
        month_total += month_count
        year_total += year_count
        routine_current, routine_best, routine_missed = get_streaks(conn, routine)
        current_streak = max(current_streak, routine_current)
        best_streak = max(best_streak, routine_best)
        total_missed += routine_missed

        week_progress = weekly_progress["percentage"]

        routine_cards.append(
            {
                "id": routine["id"],
                "title": routine["title"],
                "description": routine["description"],
                "category": routine["category"] or "General",
                "target_minutes": int(routine["target_minutes"] or 0),
                "today_minutes": progress_minutes,
                "task_minutes": task_minutes,
                "task_goal_minutes": task_goal_minutes,
                "today_goal_minutes": total_goal_minutes,
                "today_percent": get_progress_percent(progress_minutes, total_goal_minutes),
                "today_done": bool(today_done),
                "week_entries": week_entries,
                "week_count": week_count,
                "week_progress": week_progress,
                "weekly_progress": weekly_progress,
                "month_count": month_count,
                "year_count": year_count,
                "month_percent": min(100, round((month_count / 30) * 100)),
                "tasks": tasks_by_routine.get(routine["id"], []),
                "paused": bool(routine["paused"]),
                "schedule": dict(schedule) if schedule else None,
                "selected_date": selected_date,
            }
        )

    week_goal = max(1, total_routines * 7)
    month_goal = max(1, total_routines * 30)
    year_goal = max(1, total_routines * 365)

    stats = {
        "total_routines": total_routines,
        "today_completed": today_completed,
        "week_total": week_total,
        "week_expected": week_expected,
        "month_total": month_total,
        "year_total": year_total,
        "week_percent": (
            min(100, round((total_week_minutes_done / total_week_minutes_goal) * 100))
            if total_week_minutes_goal > 0
            else 0
        ),
        "month_percent": min(100, round((month_total / month_goal) * 100)),
        "year_percent": min(100, round((year_total / year_goal) * 100)),
        "current_streak": current_streak,
        "best_streak": best_streak,
        "total_missed": total_missed,
    }

    categories = [
        row["name"]
        for row in conn.execute(
            "SELECT name FROM categories WHERE user_id = ? ORDER BY position, name",
            (current_user_id(),),
        ).fetchall()
    ]

    return routine_cards, stats, categories


@app.context_processor
def inject_user():
    return {"current_user": session.get("user_email"), "csrf_token": csrf_token()}


def login_throttle_key(email):
    return f"{request.remote_addr or 'unknown'}:{email.lower()}"


def login_is_throttled(key):
    now = time.monotonic()
    attempts = [timestamp for timestamp in LOGIN_ATTEMPTS.get(key, []) if now - timestamp < LOGIN_WINDOW_SECONDS]
    LOGIN_ATTEMPTS[key] = attempts
    return len(attempts) >= LOGIN_MAX_ATTEMPTS


def record_login_failure(key):
    LOGIN_ATTEMPTS.setdefault(key, []).append(time.monotonic())


@app.route("/login", methods=["GET", "POST"])
def login():
    if not accounts_mode():
        return redirect(url_for("index"))
    if current_user_id() is not None:
        return redirect(url_for("index"))
    next_url = safe_next_url(request.args.get("next") or request.form.get("next"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        throttle_key = login_throttle_key(email)
        if login_is_throttled(throttle_key):
            return render_template("login.html", error="Too many login attempts. Please try again later.", next_url=next_url), 429
        with get_db_connection() as conn:
            user = conn.execute("SELECT id, email, password_hash FROM users WHERE email = ?", (email,)).fetchone()
        if not user or not check_password(password, user["password_hash"]):
            record_login_failure(throttle_key)
            return render_template("login.html", error="Invalid email or password.", next_url=next_url), 401
        LOGIN_ATTEMPTS.pop(throttle_key, None)
        session.clear()
        session["user_id"] = user["id"]
        session["user_email"] = user["email"]
        session["csrf_token"] = secrets.token_urlsafe(32)
        return redirect(next_url or url_for("index"))
    return render_template("login.html", next_url=next_url)


@app.route("/register", methods=["GET", "POST"])
def register():
    if not accounts_mode():
        return redirect(url_for("index"))
    if current_user_id() is not None:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        display_name = request.form.get("display_name", "").strip()[:100]
        password = request.form.get("password", "")
        if "@" not in email or len(email) > 254:
            return render_template("register.html", error="Enter a valid email address.", email=email, display_name=display_name), 400
        if len(password) < 8:
            return render_template("register.html", error="Password must be at least 8 characters.", email=email, display_name=display_name), 400
        with get_db_connection() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO users (email, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
                    (email, hash_password(password), display_name or None, today_iso()),
                )
            except sqlite3.IntegrityError:
                return render_template("register.html", error="An account with that email already exists.", email=email, display_name=display_name), 409
            create_trial(conn, cursor.lastrowid)
            user_id = cursor.lastrowid
        session.clear()
        session["user_id"] = user_id
        session["user_email"] = email
        session["csrf_token"] = secrets.token_urlsafe(32)
        return redirect(url_for("index"))
    return render_template("register.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("index") if not accounts_mode() else url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if not accounts_mode():
        return redirect(url_for("index"))
    message = "If an account matches that email, reset instructions will be sent."
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        with get_db_connection() as conn:
            user = conn.execute("SELECT id, email FROM users WHERE email = ?", (email,)).fetchone()
            if user:
                raw_token = secrets.token_urlsafe(32)
                token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
                expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
                conn.execute(
                    "INSERT INTO password_reset_tokens (user_id, token_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
                    (user["id"], token_hash, expires_at, utc_now_iso()),
                )
                reset_url = url_for("reset_password", token=raw_token, _external=True)
                try:
                    if not send_password_reset_email(user["email"], reset_url):
                        logger.warning("Password reset email is not configured")
                except (OSError, smtplib.SMTPException, ValueError):
                    logger.exception("Password reset email delivery failed")
    return render_template("forgot_password.html", message=message)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if not accounts_mode():
        return redirect(url_for("index"))
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if request.method == "POST":
        password = request.form.get("password", "")
        if len(password) < 8:
            return render_template("reset_password.html", error="Password must be at least 8 characters.", token=token), 400
        with get_db_connection() as conn:
            reset = conn.execute(
                "SELECT id, user_id FROM password_reset_tokens WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
                (token_hash, utc_now_iso()),
            ).fetchone()
            if not reset:
                return render_template("reset_password.html", error="This reset link is invalid or expired.", token=token), 400
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), reset["user_id"]))
            conn.execute("UPDATE password_reset_tokens SET used_at = ? WHERE id = ?", (utc_now_iso(), reset["id"]))
        session.clear()
        return redirect(url_for("login"))
    with get_db_connection() as conn:
        valid = conn.execute(
            "SELECT 1 FROM password_reset_tokens WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
            (token_hash, utc_now_iso()),
        ).fetchone()
    if not valid:
        return render_template("reset_password.html", error="This reset link is invalid or expired.", token=token), 400
    return render_template("reset_password.html", token=token)


def current_subscription(conn):
    row = conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (current_user_id(),)).fetchone()
    if not row:
        return {"status": "free", "pro": False}
    status = row["status"]
    if status == "trial" and row["trial_end"] < date.today().isoformat():
        conn.execute("UPDATE subscriptions SET status = 'free', updated_at = ? WHERE user_id = ?", (utc_now_iso(), current_user_id()))
        status = "free"
    return {"status": status, "pro": status in {"trial", "active"}, "trial_end": row["trial_end"]}


@app.route("/api/subscription")
@login_required
def subscription_api():
    with get_db_connection() as conn:
        return jsonify({"subscription": current_subscription(conn), "plans": PRICING})


@app.route("/api/billing/checkout", methods=["POST"])
@login_required
def billing_checkout_api():
    plan = str((request.get_json(silent=True) or {}).get("plan", "")).lower()
    price = PRICING.get(plan)
    secret = os.environ.get("STRIPE_SECRET_KEY")
    if not price or not secret or not price["price_id"]:
        return jsonify({"error": "Stripe billing is not configured"}), 503
    try:
        import stripe
        stripe.api_key = secret
        checkout = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price["price_id"], "quantity": 1}],
            success_url=os.environ.get("STRIPE_SUCCESS_URL", url_for("stats_page", _external=True)),
            cancel_url=os.environ.get("STRIPE_CANCEL_URL", url_for("stats_page", _external=True)),
            client_reference_id=str(current_user_id()),
            metadata={"user_id": str(current_user_id()), "plan": plan},
        )
    except (ImportError, Exception) as error:
        app.logger.warning("Stripe checkout unavailable: %s", error)
        return jsonify({"error": "Stripe checkout is unavailable"}), 503
    return jsonify({"url": checkout.url})


@app.route("/api/billing/webhook", methods=["POST"])
def billing_webhook():
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    signature = request.headers.get("Stripe-Signature")
    if not secret or not signature:
        return jsonify({"error": "Stripe webhook is not configured"}), 503
    try:
        import stripe
        event = stripe.Webhook.construct_event(request.data, signature, secret)
    except (ImportError, Exception):
        return jsonify({"error": "invalid webhook"}), 400
    event_type = event.get("type")
    data = event.get("data", {}).get("object", {})
    user_id = (data.get("metadata") or {}).get("user_id") or data.get("client_reference_id")
    status_map = {"active": "active", "trialing": "trial", "past_due": "past_due", "canceled": "canceled", "unpaid": "expired"}
    if user_id and event_type in {"checkout.session.completed", "customer.subscription.updated", "customer.subscription.deleted"}:
        status = "active" if event_type == "checkout.session.completed" else status_map.get(data.get("status"), "free")
        with get_db_connection() as conn:
            conn.execute("UPDATE subscriptions SET status = ?, provider_customer_id = COALESCE(?, provider_customer_id), provider_subscription_id = COALESCE(?, provider_subscription_id), updated_at = ? WHERE user_id = ?", (status, data.get("customer"), data.get("subscription") or data.get("id"), utc_now_iso(), int(user_id)))
    return jsonify({"received": True})


@app.route("/api/billing/cancel", methods=["POST"])
@login_required
def billing_cancel_api():
    secret = os.environ.get("STRIPE_SECRET_KEY")
    if not secret:
        return jsonify({"error": "Stripe billing is not configured"}), 503
    with get_db_connection() as conn:
        subscription = conn.execute("SELECT provider_subscription_id FROM subscriptions WHERE user_id = ?", (current_user_id(),)).fetchone()
    if not subscription or not subscription["provider_subscription_id"]:
        return jsonify({"error": "no active provider subscription"}), 409
    try:
        import stripe
        stripe.api_key = secret
        stripe.Subscription.modify(subscription["provider_subscription_id"], cancel_at_period_end=True)
    except (ImportError, Exception) as error:
        app.logger.warning("Stripe cancellation unavailable: %s", error)
        return jsonify({"error": "Stripe cancellation is unavailable"}), 503
    return jsonify({"status": "cancellation_scheduled"})


@app.route("/api/account", methods=["GET", "PATCH", "DELETE"])
@login_required
def account_api():
    with get_db_connection() as conn:
        if request.method == "GET":
            user = conn.execute("SELECT id, email, display_name, timezone, created_at FROM users WHERE id = ?", (current_user_id(),)).fetchone()
            return jsonify({"account": dict(user), "subscription": current_subscription(conn)})
        if request.method == "PATCH":
            payload = request.get_json(silent=True) or {}
            display_name = str(payload.get("display_name", "")).strip()[:100]
            timezone_name = str(payload.get("timezone", "UTC")).strip()[:80] or "UTC"
            conn.execute("UPDATE users SET display_name = ?, timezone = ? WHERE id = ?", (display_name, timezone_name, current_user_id()))
            return jsonify({"status": "updated"})
        confirmation = str((request.get_json(silent=True) or {}).get("confirmation", ""))
        if confirmation != "DELETE":
            return jsonify({"error": "confirmation must be DELETE"}), 400
        user_id = current_user_id()
        try:
            with get_db_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM task_history WHERE task_id IN (SELECT id FROM tasks WHERE routine_id IN (SELECT id FROM routines WHERE user_id = ?))", (user_id,))
                conn.execute("DELETE FROM routine_history WHERE routine_id IN (SELECT id FROM routines WHERE user_id = ?)", (user_id,))
                conn.execute("DELETE FROM automations WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM reminders WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM timers WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM push_subscriptions WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM password_reset_tokens WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM idempotency_keys WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM tasks WHERE routine_id IN (SELECT id FROM routines WHERE user_id = ?)", (user_id,))
                conn.execute("DELETE FROM schedules WHERE routine_id IN (SELECT id FROM routines WHERE user_id = ?)", (user_id,))
                conn.execute("DELETE FROM routines WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM categories WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM goal_routines WHERE goal_id IN (SELECT id FROM goals WHERE user_id = ?)", (user_id,))
                conn.execute("DELETE FROM goals WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM milestones WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM notification_preferences WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        except sqlite3.Error:
            logger.exception("Account deletion failed for user %s", user_id)
            return jsonify({"error": "account deletion failed"}), 500
    session.clear()
    return jsonify({"status": "deleted"})


@app.route("/api/account/password", methods=["POST"])
@login_required
def change_password_api():
    payload = request.get_json(silent=True) or {}
    with get_db_connection() as conn:
        user = conn.execute("SELECT password_hash FROM users WHERE id = ?", (current_user_id(),)).fetchone()
        if not user or not check_password(str(payload.get("current_password", "")), user["password_hash"]):
            return jsonify({"error": "current password is incorrect"}), 400
        new_password = str(payload.get("new_password", ""))
        if len(new_password) < 8:
            return jsonify({"error": "new password must be at least 8 characters"}), 400
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_password), current_user_id()))
    return jsonify({"status": "updated"})


@app.route("/api/export.json")
@login_required
def export_account_api():
    with get_db_connection() as conn:
        user = dict(conn.execute("SELECT id, email, display_name, timezone, created_at FROM users WHERE id = ?", (current_user_id(),)).fetchone())
        routines = [dict(row) for row in conn.execute("SELECT * FROM routines WHERE user_id = ?", (current_user_id(),)).fetchall()]
        routine_ids = [row["id"] for row in routines]
        tasks = []
        if routine_ids:
            placeholders = ",".join("?" for _ in routine_ids)
            tasks = [dict(row) for row in conn.execute(f"SELECT * FROM tasks WHERE routine_id IN ({placeholders})", routine_ids).fetchall()]
        return jsonify({"user": user, "categories": [dict(row) for row in conn.execute("SELECT * FROM categories WHERE user_id = ?", (current_user_id(),))], "routines": routines, "tasks": tasks, "history": [dict(row) for row in conn.execute(f"SELECT * FROM routine_history WHERE routine_id IN ({','.join('?' for _ in routine_ids)})", routine_ids).fetchall()] if routine_ids else [], "goals": [dict(row) for row in conn.execute("SELECT * FROM goals WHERE user_id = ?", (current_user_id(),))]})


@app.route("/api/history")
@login_required
def history_api():
    selected_date = request.args.get("date", today_iso())
    with get_db_connection() as conn:
        routines = conn.execute("SELECT * FROM routines WHERE user_id = ? AND archived = 0 ORDER BY position, id", (current_user_id(),)).fetchall()
        daily = get_calendar_daily_progress(conn, routines, selected_date)
        return jsonify({"date": selected_date, **daily})


@app.route("/api/monthly")
@login_required
def monthly_api():
    month = request.args.get("month", date.today().strftime("%Y-%m"))
    try:
        datetime.strptime(month, "%Y-%m")
    except ValueError:
        return jsonify({"error": "month must use YYYY-MM"}), 400
    with get_db_connection() as conn:
        routines = conn.execute(
            "SELECT * FROM routines WHERE user_id = ? AND archived = 0 ORDER BY position, id",
            (current_user_id(),),
        ).fetchall()
        return jsonify(get_monthly_progress(conn, routines, month))


@app.route("/api/analytics")
@login_required
def analytics_api():
    month = request.args.get("month")
    try:
        days = min(3660, max(7, int(request.args.get("days", 30))))
    except ValueError:
        days = 30
    if month:
        try:
            start = datetime.strptime(month, "%Y-%m").date().replace(day=1)
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            days = (end - start).days + 1
        except ValueError:
            return jsonify({"error": "month must use YYYY-MM"}), 400
    else:
        if days == 7:
            start = date.today() - timedelta(days=date.today().weekday())
        else:
            end = date.today()
            start = end - timedelta(days=days - 1)
    with get_db_connection() as conn:
        if request.args.get("premium") == "1" and not current_subscription(conn)["pro"]:
            return jsonify({"error": "premium analytics requires Pro"}), 402
        routines = conn.execute("SELECT * FROM routines WHERE user_id = ? AND archived = 0", (current_user_id(),)).fetchall()
        series = []
        for offset in range(days):
            day = start + timedelta(days=offset)
            scheduled_count = completed_count = 0
            for routine in routines:
                schedule = conn.execute("SELECT * FROM schedules WHERE routine_id = ?", (routine["id"],)).fetchone()
                if not routine_expected_on(routine, day, schedule):
                    continue
                scheduled_count += 1
                record = conn.execute("SELECT completed FROM routine_history WHERE routine_id = ? AND completion_date = ?", (routine["id"], day.isoformat())).fetchone()
                completed_count += int(bool(record and record["completed"]))
            expected = int(scheduled_count > 0)
            completed = int(expected and completed_count == scheduled_count)
            series.append({"date": day.isoformat(), "expected": expected, "completed": completed, "missed": max(0, expected - completed), "percent": round((completed / expected) * 100) if expected else None})
    return jsonify({"days": days, "series": series})


@app.route("/calendar")
@login_required
def calendar_page():
    month = request.args.get("month", date.today().strftime("%Y-%m"))
    selected_date = request.args.get("date", date.today().isoformat())
    try:
        selected = datetime.strptime(month, "%Y-%m").date().replace(day=1)
    except ValueError:
        selected = date.today().replace(day=1)
    return render_template("calendar.html", month=selected.strftime("%Y-%m"), selected_date=selected_date)


@app.route("/api/calendar")
@login_required
def calendar_api():
    month = request.args.get("month", date.today().strftime("%Y-%m"))
    try:
        first = datetime.strptime(month, "%Y-%m").date().replace(day=1)
    except ValueError:
        return jsonify({"error": "month must use YYYY-MM"}), 400
    next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    with get_db_connection() as conn:
        routines = conn.execute("SELECT * FROM routines WHERE user_id = ? AND archived = 0", (current_user_id(),)).fetchall()
        days = []
        cursor = first
        while cursor < next_month:
            daily = get_calendar_daily_progress(conn, routines, cursor.isoformat())
            days.append({"date": cursor.isoformat(), **daily})
            cursor += timedelta(days=1)
    return jsonify({"month": month, "days": days})


def serialize_goal(conn, goal):
    links = conn.execute(
        """
        SELECT routines.id, routines.title
        FROM goal_routines
        JOIN routines ON routines.id = goal_routines.routine_id
        WHERE goal_routines.goal_id = ? AND routines.user_id = ?
        ORDER BY routines.position, routines.id
        """,
        (goal["id"], current_user_id()),
    ).fetchall()
    start = goal["start_date"] or (date.today() - timedelta(days=29)).isoformat()
    end = goal["end_date"] or today_iso()
    total_days = max(1, (datetime.strptime(end, "%Y-%m-%d").date() - datetime.strptime(start, "%Y-%m-%d").date()).days + 1)
    routine_ids = [row["id"] for row in links]
    completed = 0
    if routine_ids:
        placeholders = ",".join("?" for _ in routine_ids)
        completed = conn.execute(
            f"""
            SELECT COUNT(*) AS total FROM routine_history
            WHERE routine_id IN ({placeholders}) AND completion_date BETWEEN ? AND ? AND completed = 1
            """,
            (*routine_ids, start, end),
        ).fetchone()["total"]
    progress = round(min(100, (completed / (len(routine_ids) * total_days)) * 100)) if routine_ids else 0
    result = dict(goal)
    result.update({"routine_ids": routine_ids, "routines": [dict(row) for row in links], "progress": progress})
    return result


@app.route("/api/goals", methods=["GET", "POST"])
@login_required
def goals_api():
    with get_db_connection() as conn:
        if request.method == "GET":
            rows = conn.execute("SELECT * FROM goals WHERE user_id = ? ORDER BY created_at DESC", (current_user_id(),)).fetchall()
            return jsonify({"goals": [serialize_goal(conn, row) for row in rows]})
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name", "")).strip()
        try:
            target = max(1, int(payload.get("target", 1)))
        except (TypeError, ValueError):
            return jsonify({"error": "target must be a number"}), 400
        if not name or len(name) > 160:
            return jsonify({"error": "goal name must be 1-160 characters"}), 400
        routine_ids = payload.get("routine_ids", [])
        if not isinstance(routine_ids, list):
            return jsonify({"error": "routine_ids must be a list"}), 400
        try:
            routine_ids = sorted({int(value) for value in routine_ids})
        except (TypeError, ValueError):
            return jsonify({"error": "routine_ids must contain numbers"}), 400
        if routine_ids:
            placeholders = ",".join("?" for _ in routine_ids)
            owned = conn.execute(
                f"SELECT id FROM routines WHERE user_id = ? AND id IN ({placeholders})",
                (current_user_id(), *routine_ids),
            ).fetchall()
            if len(owned) != len(routine_ids):
                return jsonify({"error": "one or more routines do not belong to this user"}), 403
        cursor = conn.execute(
            "INSERT INTO goals (user_id, name, description, target, start_date, end_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (current_user_id(), name, str(payload.get("description", "")).strip(), target, payload.get("start_date"), payload.get("end_date"), today_iso()),
        )
        conn.executemany(
            "INSERT INTO goal_routines (goal_id, routine_id) VALUES (?, ?)",
            [(cursor.lastrowid, routine_id) for routine_id in routine_ids],
        )
        goal = conn.execute("SELECT * FROM goals WHERE id = ? AND user_id = ?", (cursor.lastrowid, current_user_id())).fetchone()
        return jsonify({"goal": serialize_goal(conn, goal)}), 201


@app.route("/api/goals/<int:goal_id>", methods=["PATCH", "DELETE"])
@login_required
def goal_api(goal_id):
    with get_db_connection() as conn:
        goal = conn.execute("SELECT * FROM goals WHERE id = ? AND user_id = ?", (goal_id, current_user_id())).fetchone()
        if not goal:
            return jsonify({"error": "goal not found"}), 404
        if request.method == "DELETE":
            conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
            return jsonify({"status": "deleted"})
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name", goal["name"])).strip()
        if not name or len(name) > 160:
            return jsonify({"error": "goal name must be 1-160 characters"}), 400
        try:
            target = max(1, int(payload.get("target", goal["target"])))
        except (TypeError, ValueError):
            return jsonify({"error": "target must be a number"}), 400
        routine_ids = payload.get("routine_ids", [])
        try:
            routine_ids = sorted({int(value) for value in routine_ids})
        except (TypeError, ValueError):
            return jsonify({"error": "routine_ids must contain numbers"}), 400
        if routine_ids:
            placeholders = ",".join("?" for _ in routine_ids)
            owned = conn.execute(
                f"SELECT id FROM routines WHERE user_id = ? AND id IN ({placeholders})",
                (current_user_id(), *routine_ids),
            ).fetchall()
            if len(owned) != len(routine_ids):
                return jsonify({"error": "one or more routines do not belong to this user"}), 403
        conn.execute(
            "UPDATE goals SET name = ?, description = ?, target = ?, start_date = ?, end_date = ?, status = ? WHERE id = ?",
            (name, str(payload.get("description", goal["description"] or "")).strip(), target,
             payload.get("start_date", goal["start_date"]), payload.get("end_date", goal["end_date"]),
             str(payload.get("status", goal["status"])), goal_id),
        )
        conn.execute("DELETE FROM goal_routines WHERE goal_id = ?", (goal_id,))
        conn.executemany("INSERT INTO goal_routines (goal_id, routine_id) VALUES (?, ?)", [(goal_id, value) for value in routine_ids])
        updated = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        return jsonify({"goal": serialize_goal(conn, updated)})


@app.route("/api/milestones")
@login_required
def milestones_api():
    with get_db_connection() as conn:
        completed_tasks = conn.execute(
            "SELECT COUNT(*) AS total FROM task_history JOIN tasks ON tasks.id = task_history.task_id JOIN routines ON routines.id = tasks.routine_id WHERE routines.user_id = ? AND task_history.completed = 1",
            (current_user_id(),),
        ).fetchone()["total"]
        completed_routines = conn.execute(
            "SELECT COUNT(*) AS total FROM routine_history JOIN routines ON routines.id = routine_history.routine_id WHERE routines.user_id = ? AND routine_history.completed = 1",
            (current_user_id(),),
        ).fetchone()["total"]
        milestones = []
        candidates = [("First completed routine", completed_routines >= 1), ("100 completed tasks", completed_tasks >= 100)]
        for name, achieved in candidates:
            if achieved:
                conn.execute("INSERT OR IGNORE INTO milestones (user_id, name, achieved_at) VALUES (?, ?, ?)", (current_user_id(), name, utc_now_iso()))
        rows = conn.execute("SELECT name, achieved_at FROM milestones WHERE user_id = ? ORDER BY achieved_at", (current_user_id(),)).fetchall()
    return jsonify({"milestones": [dict(row) for row in rows]})


@app.route("/")
@login_required
def index():
    category = request.args.get("category", "all")
    selected_date = request.args.get("date", today_iso())
    try:
        date.fromisoformat(selected_date)
    except ValueError:
        selected_date = today_iso()
    conn = get_db_connection()
    routine_cards, stats, categories = get_dashboard_data(conn, category, selected_date)
    category_records = [
        dict(row)
        for row in conn.execute(
            "SELECT id, name FROM categories WHERE user_id = ? ORDER BY position, name",
            (current_user_id(),),
        ).fetchall()
    ]
    conn.close()
    return render_template(
        "index.html",
        routines=routine_cards,
        stats=stats,
        categories=categories,
        category_records=category_records,
        selected_category=category,
        current_date=selected_date,
        selected_date=selected_date,
    )


@app.route("/stats")
@login_required
def stats_page():
    category = request.args.get("category", "all")
    conn = get_db_connection()
    routine_cards, stats, categories = get_dashboard_data(conn, category)
    subscription = current_subscription(conn)
    conn.close()
    return render_template("stats.html", routines=routine_cards, stats=stats, categories=categories, selected_category=category, subscription=subscription)


@app.route("/history")
@login_required
def history_page():
    return redirect(url_for("calendar_page", date=request.args.get("date", today_iso())))


@app.route("/goals")
@login_required
def goals_page():
    with get_db_connection() as conn:
        goals = conn.execute("SELECT * FROM goals WHERE user_id = ? ORDER BY created_at DESC", (current_user_id(),)).fetchall()
        routines = conn.execute(
            "SELECT id, title FROM routines WHERE user_id = ? AND archived = 0 ORDER BY position, id",
            (current_user_id(),),
        ).fetchall()
    return render_template("goals.html", goals=goals, routines=routines)


@app.route("/add", methods=["POST"])
@login_required
def add_routine():
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    category = request.form.get("category", "General").strip() or "General"
    target_minutes = request.form.get("target_minutes", "20")

    if not title:
        flash("Routine title is required.")
        return redirect(url_for("index"))

    try:
        target_minutes = max(0, int(target_minutes))
    except ValueError:
        target_minutes = 20

    conn = get_db_connection()
    next_position = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS next_position FROM routines WHERE user_id = ? AND archived = 0",
        (current_user_id(),),
    ).fetchone()["next_position"]
    conn.execute(
        """
        INSERT INTO routines (title, description, category, target_minutes, position, created_at, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (title, description, category, target_minutes, next_position, today_iso(), current_user_id()),
    )
    conn.execute("INSERT OR IGNORE INTO categories (user_id, name) VALUES (?, ?)", (current_user_id(), category))
    conn.commit()
    conn.close()
    flash("Routine added successfully.")
    return redirect(url_for("index"))


def parse_iso_datetime(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


@app.route("/api/reminders", methods=["GET", "POST"])
@login_required
def reminders_api():
    if request.method == "GET":
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, title, due_at, timezone, recurrence, status, snoozed_until, routine_id, task_id
                FROM reminders
                WHERE user_id = ? AND status = 'pending'
                ORDER BY COALESCE(snoozed_until, due_at), id
                LIMIT 50
                """,
                (current_user_id(),),
            ).fetchall()
        return jsonify({"reminders": [dict(row) for row in rows]})

    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title", "")).strip()
    due_at = parse_iso_datetime(payload.get("due_at"))
    if not title or len(title) > 180:
        return jsonify({"error": "title must be 1-180 characters"}), 400
    if not due_at:
        return jsonify({"error": "due_at must be an ISO datetime"}), 400
    recurrence = str(payload.get("recurrence", "once")).strip().lower()
    if recurrence not in {"once", "daily", "weekly"}:
        return jsonify({"error": "unsupported recurrence"}), 400
    with get_db_connection() as conn:
        routine_id, task_id, reference_error = validate_owned_reference_ids(
            conn, payload.get("routine_id"), payload.get("task_id")
        )
        if reference_error:
            status = 400 if "must be" in reference_error else 404
            return jsonify({"error": reference_error}), status
        cursor = conn.execute(
            """
            INSERT INTO reminders (user_id, routine_id, task_id, title, due_at, timezone, recurrence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                current_user_id(), routine_id, task_id, title,
                due_at, str(payload.get("timezone", "UTC")), recurrence, utc_now_iso(),
            ),
        )
    return jsonify({"reminder": {"id": cursor.lastrowid, "title": title, "due_at": due_at, "status": "pending"}}), 201


@app.route("/api/reminders/<int:reminder_id>/<action>", methods=["POST"])
@login_required
def reminder_action_api(reminder_id, action):
    if action not in {"complete", "dismiss", "snooze"}:
        return jsonify({"error": "unsupported reminder action"}), 400
    with get_db_connection() as conn:
        reminder = conn.execute(
            "SELECT id FROM reminders WHERE id = ? AND user_id = ? AND status = 'pending'",
            (reminder_id, current_user_id()),
        ).fetchone()
        if not reminder:
            return jsonify({"error": "reminder not found"}), 404
        if action == "snooze":
            payload = request.get_json(silent=True) or {}
            try:
                minutes = max(1, min(1440, int(payload.get("minutes", 10))))
            except (TypeError, ValueError):
                return jsonify({"error": "minutes must be a number"}), 400
            snoozed_until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).replace(microsecond=0).isoformat()
            conn.execute("UPDATE reminders SET snoozed_until = ? WHERE id = ? AND user_id = ?", (snoozed_until, reminder_id, current_user_id()))
        else:
            conn.execute("UPDATE reminders SET status = ? WHERE id = ? AND user_id = ?", (action, reminder_id, current_user_id()))
    return jsonify({"status": action})


def timer_payload(timer):
    remaining = int(timer["duration_seconds"])
    if timer["status"] == "running" and timer["started_at"]:
        started = datetime.fromisoformat(timer["started_at"])
        elapsed = (datetime.now(timezone.utc) - started).total_seconds() - int(timer["paused_seconds"] or 0)
        remaining = max(0, int(timer["duration_seconds"] - elapsed))
    elapsed_seconds = max(0, int(timer["duration_seconds"]) - remaining)
    if remaining == 0 and timer["status"] == "running":
        return {**dict(timer), "status": "complete", "remaining_seconds": 0, "elapsed_seconds": int(timer["duration_seconds"])}
    return {**dict(timer), "remaining_seconds": remaining, "elapsed_seconds": elapsed_seconds}

def timer_elapsed_seconds(timer):
    if not timer["started_at"]:
        return 0
    end = timer["paused_at"] if timer["status"] == "paused" and timer["paused_at"] else utc_now_iso()
    elapsed = datetime.fromisoformat(end) - datetime.fromisoformat(timer["started_at"])
    return max(0, int(elapsed.total_seconds()) - int(timer["paused_seconds"] or 0))


def finish_timer(conn, timer, status):
    elapsed_seconds = min(timer["duration_seconds"], timer_elapsed_seconds(timer))
    tracked_minutes = round(elapsed_seconds / 60) if elapsed_seconds else 0
    goal_completed = False
    if status == "complete" and timer["routine_id"] and tracked_minutes:
        routine = conn.execute("SELECT target_minutes FROM routines WHERE id = ? AND user_id = ?", (timer["routine_id"], current_user_id())).fetchone()
        if routine:
            existing = conn.execute(
                "SELECT minutes_done FROM routine_history WHERE routine_id = ? AND completion_date = ?",
                (timer["routine_id"], today_iso()),
            ).fetchone()
            total_minutes = (int(existing["minutes_done"] or 0) if existing else 0) + tracked_minutes
            task_goal = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN target_minutes > 0 THEN target_minutes ELSE ? END), 0) AS total FROM tasks WHERE routine_id = ? AND archived = 0",
                (int(routine["target_minutes"] or 0), timer["routine_id"]),
            ).fetchone()["total"]
            total_goal = int(routine["target_minutes"] or 0) + int(task_goal or 0)
            goal_completed = total_minutes >= total_goal
            conn.execute(
                "INSERT INTO routine_history (routine_id, completion_date, completed, minutes_done) VALUES (?, ?, ?, ?) ON CONFLICT(routine_id, completion_date) DO UPDATE SET completed = excluded.completed, minutes_done = excluded.minutes_done",
                (timer["routine_id"], today_iso(), int(goal_completed), total_minutes),
            )
            conn.execute("UPDATE routines SET completed = ? WHERE id = ? AND user_id = ?", (int(goal_completed), timer["routine_id"], current_user_id()))
    conn.execute("UPDATE timers SET status = ? WHERE id = ?", (status, timer["id"]))
    return {"tracked_minutes": tracked_minutes, "tracked_seconds": elapsed_seconds, "goal_completed": goal_completed}


@app.route("/api/timers", methods=["GET", "POST"])
@login_required
def timers_api():
    with get_db_connection() as conn:
        if request.method == "POST":
            payload = request.get_json(silent=True) or {}
            title = str(payload.get("title", "Timer")).strip() or "Timer"
            try:
                duration = max(1, min(86400, int(payload.get("duration_seconds", 300))))
            except (TypeError, ValueError):
                return jsonify({"error": "duration_seconds must be a number"}), 400
            routine_id, task_id, reference_error = validate_owned_reference_ids(
                conn, payload.get("routine_id"), payload.get("task_id")
            )
            if reference_error:
                status = 400 if "must be" in reference_error else 404
                return jsonify({"error": reference_error}), status
            cursor = conn.execute(
                "INSERT INTO timers (user_id, routine_id, task_id, title, duration_seconds, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (current_user_id(), routine_id, task_id, title, duration, utc_now_iso()),
            )
            timer = conn.execute("SELECT * FROM timers WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return jsonify({"timer": timer_payload(timer)}), 201
        rows = conn.execute(
            "SELECT * FROM timers WHERE user_id = ? AND status IN ('ready', 'running', 'paused') ORDER BY id DESC LIMIT 20",
            (current_user_id(),),
        ).fetchall()
        timers = []
        completed = []
        for row in rows:
            payload = timer_payload(row)
            if payload["status"] == "complete":
                result = finish_timer(conn, row, "complete")
                completed.append({**payload, **result})
            else:
                timers.append(payload)
    return jsonify({"timers": timers, "completed": completed})


@app.route("/api/timers/<int:timer_id>/<action>", methods=["POST"])
@login_required
def timer_action_api(timer_id, action):
    if action not in {"start", "pause", "resume", "cancel", "complete"}:
        return jsonify({"error": "unsupported timer action"}), 400
    with get_db_connection() as conn:
        timer = conn.execute("SELECT * FROM timers WHERE id = ? AND user_id = ?", (timer_id, current_user_id())).fetchone()
        if not timer:
            return jsonify({"error": "timer not found"}), 404
        now = utc_now_iso()
        result = {"tracked_minutes": 0, "tracked_seconds": 0, "goal_completed": False}
        if action == "start" and timer["status"] == "ready":
            conn.execute("UPDATE timers SET status = 'running', started_at = ? WHERE id = ?", (now, timer_id))
        elif action == "pause" and timer["status"] == "running":
            conn.execute("UPDATE timers SET status = 'paused', paused_at = ? WHERE id = ?", (now, timer_id))
        elif action == "resume" and timer["status"] == "paused":
            paused_at = datetime.fromisoformat(timer["paused_at"])
            extra_pause = max(0, int((datetime.now(timezone.utc) - paused_at).total_seconds()))
            conn.execute("UPDATE timers SET status = 'running', paused_at = NULL, paused_seconds = paused_seconds + ? WHERE id = ?", (extra_pause, timer_id))
        elif action in {"cancel", "complete"}:
            result = finish_timer(conn, timer, action)
        else:
            result = {"tracked_minutes": 0, "tracked_seconds": 0, "goal_completed": False}
        updated = conn.execute("SELECT * FROM timers WHERE id = ?", (timer_id,)).fetchone()
    return jsonify({"timer": {**timer_payload(updated), **result}})


@app.route("/api/notification-preferences", methods=["GET", "POST"])
@login_required
def notification_preferences_api():
    with get_db_connection() as conn:
        if request.method == "POST":
            payload = request.get_json(silent=True) or {}
            enabled = int(bool(payload.get("enabled", True)))
            permission_state = str(payload.get("permission_state", "default"))
            conn.execute(
                """
                INSERT INTO notification_preferences (user_id, enabled, permission_state) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET enabled = excluded.enabled, permission_state = excluded.permission_state
                """,
                (current_user_id(), enabled, permission_state),
            )
        row = conn.execute("SELECT enabled, permission_state FROM notification_preferences WHERE user_id = ?", (current_user_id(),)).fetchone()
    return jsonify(dict(row) if row else {"enabled": True, "permission_state": "default"})


@app.route("/api/push-subscriptions", methods=["POST", "DELETE"])
@login_required
def push_subscriptions_api():
    payload = request.get_json(silent=True) or {}
    endpoint = str(payload.get("endpoint", "")).strip()
    if not endpoint or len(endpoint) > 2000:
        return jsonify({"error": "valid endpoint is required"}), 400
    with get_db_connection() as conn:
        if request.method == "DELETE":
            conn.execute("DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?", (current_user_id(), endpoint))
        else:
            conn.execute(
                "INSERT INTO push_subscriptions (user_id, endpoint, subscription_json, created_at) VALUES (?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET user_id = excluded.user_id, subscription_json = excluded.subscription_json",
                (current_user_id(), endpoint, json.dumps(payload), utc_now_iso()),
            )
    return jsonify({"status": "removed" if request.method == "DELETE" else "registered"})


@app.route("/api/routines/<int:routine_id>", methods=["PATCH"])
@login_required
def update_routine_api(routine_id):
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title", "")).strip()
    if not title:
        return jsonify({"error": "routine title is required"}), 400
    try:
        target_minutes = max(0, int(payload.get("target_minutes", 20)))
    except (TypeError, ValueError):
        return jsonify({"error": "target_minutes must be a number"}), 400

    with get_db_connection() as conn:
        routine = get_owned_routine(conn, routine_id)
        if not routine or routine["archived"]:
            return jsonify({"error": "routine not found"}), 404
        conn.execute(
            """
            UPDATE routines
            SET title = ?, description = ?, category = ?, target_minutes = ?, start_date = ?, end_date = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                title,
                str(payload.get("description", "")).strip(),
                str(payload.get("category", "General")).strip() or "General",
                target_minutes,
                payload.get("start_date") or None,
                payload.get("end_date") or None,
                routine_id,
                current_user_id(),
            ),
        )
        conn.execute("INSERT OR IGNORE INTO categories (user_id, name) VALUES (?, ?)", (current_user_id(), str(payload.get("category", "General")).strip() or "General"))
    return jsonify({"status": "ok"})


@app.route("/api/categories", methods=["POST"])
@login_required
def create_category_api():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    if not name or len(name) > 60:
        return jsonify({"error": "category name must be 1-60 characters"}), 400
    with get_db_connection() as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO categories (user_id, name, color) VALUES (?, ?, ?)",
                (current_user_id(), name, str(payload.get("color", "#5b5ce6"))),
            )
        except sqlite3.IntegrityError:
            return jsonify({"error": "category already exists"}), 409
    return jsonify({"category": {"id": cursor.lastrowid, "name": name}}), 201


@app.route("/api/categories/<int:category_id>", methods=["PATCH", "DELETE"])
@login_required
def manage_category_api(category_id):
    with get_db_connection() as conn:
        category = conn.execute(
            "SELECT id, name FROM categories WHERE id = ? AND user_id = ?",
            (category_id, current_user_id()),
        ).fetchone()
        if not category:
            return jsonify({"error": "category not found"}), 404
        if request.method == "DELETE":
            if category["name"] == "General":
                return jsonify({"error": "the General category cannot be deleted"}), 400
            replacement = str((request.get_json(silent=True) or {}).get("replacement", "General")).strip() or "General"
            conn.execute("UPDATE routines SET category = ? WHERE user_id = ? AND category = ?", (replacement, current_user_id(), category["name"]))
            conn.execute("INSERT OR IGNORE INTO categories (user_id, name) VALUES (?, ?)", (current_user_id(), replacement))
            conn.execute("DELETE FROM categories WHERE id = ? AND user_id = ?", (category_id, current_user_id()))
            return jsonify({"status": "deleted"})
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name", "")).strip()
        if not name or len(name) > 60:
            return jsonify({"error": "category name must be 1-60 characters"}), 400
        try:
            conn.execute("UPDATE categories SET name = ?, color = COALESCE(?, color) WHERE id = ? AND user_id = ?", (name, payload.get("color"), category_id, current_user_id()))
            conn.execute("UPDATE routines SET category = ? WHERE user_id = ? AND category = ?", (name, current_user_id(), category["name"]))
        except sqlite3.IntegrityError:
            return jsonify({"error": "category already exists"}), 409
    return jsonify({"status": "updated"})


@app.route("/api/routines/<int:routine_id>/schedule", methods=["POST"])
@login_required
def save_schedule_api(routine_id):
    payload = request.get_json(silent=True) or {}
    frequency = str(payload.get("frequency", "daily")).strip().lower()
    if frequency not in {"daily", "weekly", "monthly"}:
        return jsonify({"error": "unsupported schedule frequency"}), 400
    weekdays = payload.get("weekdays", [])
    if not isinstance(weekdays, list) or any(str(day) not in {"0", "1", "2", "3", "4", "5", "6"} for day in weekdays):
        return jsonify({"error": "weekdays must contain values from 0 to 6"}), 400
    with get_db_connection() as conn:
        if not get_owned_routine(conn, routine_id):
            return jsonify({"error": "routine not found"}), 404
        conn.execute(
            """
            INSERT INTO schedules (routine_id, frequency, time_of_day, weekdays, start_date, end_date, timezone, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(routine_id) DO UPDATE SET frequency = excluded.frequency,
                time_of_day = excluded.time_of_day, weekdays = excluded.weekdays,
                start_date = excluded.start_date, end_date = excluded.end_date,
                timezone = excluded.timezone, enabled = excluded.enabled
            """,
            (
                routine_id,
                frequency,
                payload.get("time_of_day") or None,
                ",".join(sorted(set(str(day) for day in weekdays))),
                payload.get("start_date") or None,
                payload.get("end_date") or None,
                str(payload.get("timezone", "UTC")),
                int(bool(payload.get("enabled", True))),
            ),
        )
    return jsonify({"status": "saved"})


@app.route("/api/routines/<int:routine_id>/archive", methods=["POST"])
@login_required
def archive_routine_api(routine_id):
    with get_db_connection() as conn:
        routine = get_owned_routine(conn, routine_id)
        if not routine:
            return jsonify({"error": "routine not found"}), 404
        conn.execute("UPDATE routines SET archived = 1 WHERE id = ? AND user_id = ?", (routine_id, current_user_id()))
    return jsonify({"status": "archived"})


@app.route("/api/routines/<int:routine_id>/pause", methods=["POST"])
@login_required
def pause_routine_api(routine_id):
    with get_db_connection() as conn:
        routine = get_owned_routine(conn, routine_id)
        if not routine or routine["archived"]:
            return jsonify({"error": "routine not found"}), 404
        paused = not bool(routine["paused"])
        conn.execute("UPDATE routines SET paused = ? WHERE id = ? AND user_id = ?", (int(paused), routine_id, current_user_id()))
    return jsonify({"paused": paused})


@app.route("/api/routines/<int:routine_id>/duplicate", methods=["POST"])
@login_required
def duplicate_routine_api(routine_id):
    with get_db_connection() as conn:
        routine = get_owned_routine(conn, routine_id)
        if not routine or routine["archived"]:
            return jsonify({"error": "routine not found"}), 404
        next_position = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next_position FROM routines WHERE user_id = ? AND archived = 0",
            (current_user_id(),),
        ).fetchone()["next_position"]
        cursor = conn.execute(
            """
            INSERT INTO routines (title, description, category, target_minutes, position, start_date, end_date, created_at, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"{routine['title']} copy",
                routine["description"],
                routine["category"],
                routine["target_minutes"],
                next_position,
                routine["start_date"],
                routine["end_date"],
                today_iso(),
                current_user_id(),
            ),
        )
        new_routine_id = cursor.lastrowid
        tasks = conn.execute(
            "SELECT title, description, position FROM tasks WHERE routine_id = ? AND archived = 0 ORDER BY position, id",
            (routine_id,),
        ).fetchall()
        conn.executemany(
            "INSERT INTO tasks (routine_id, title, description, position, created_at) VALUES (?, ?, ?, ?, ?)",
            [(new_routine_id, task["title"], task["description"], task["position"], today_iso()) for task in tasks],
        )
    return jsonify({"status": "duplicated", "routine_id": new_routine_id}), 201


@app.route("/api/routines/<int:routine_id>/tasks", methods=["POST"])
@login_required
def add_task_api(routine_id):
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title", "")).strip()
    if not title:
        return jsonify({"error": "task title is required"}), 400
    if len(title) > 160:
        return jsonify({"error": "task title is too long"}), 400
    try:
        target_minutes = max(0, min(1440, int(payload.get("target_minutes", 0))))
    except (TypeError, ValueError):
        return jsonify({"error": "target_minutes must be a number"}), 400

    with get_db_connection() as conn:
        routine = conn.execute(
            "SELECT id FROM routines WHERE id = ? AND user_id = ?",
            (routine_id, current_user_id()),
        ).fetchone()
        if not routine:
            return jsonify({"error": "routine not found"}), 404
        task_date = str(payload.get("task_date") or today_iso())
        try:
            date.fromisoformat(task_date)
        except ValueError:
            return jsonify({"error": "task_date must be a valid ISO date"}), 400
        conn.execute(
            "UPDATE tasks SET archived = 1 WHERE routine_id = ? AND task_date = ? AND is_default = 1",
            (routine_id, task_date),
        )
        position = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next_position FROM tasks WHERE routine_id = ? AND task_date = ?",
            (routine_id, task_date),
        ).fetchone()["next_position"]
        cursor = conn.execute(
            """
            INSERT INTO tasks (routine_id, title, description, target_minutes, task_date, is_default, position, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (routine_id, title, str(payload.get("description", "")).strip(), target_minutes, task_date, position, today_iso()),
        )
        task_id = cursor.lastrowid

    return jsonify({"task": {"id": task_id, "title": title, "target_minutes": target_minutes, "completed": False, "task_date": task_date}}), 201


@app.route("/api/tasks/<int:task_id>/toggle", methods=["POST"])
@login_required
def toggle_task_api(task_id):
    payload = request.get_json(silent=True) or {}
    completed = bool(payload.get("completed"))
    completion_date = str(payload.get("date") or today_iso())
    try:
        date.fromisoformat(completion_date)
    except ValueError:
        return jsonify({"error": "date must be a valid ISO date"}), 400
    with get_db_connection() as conn:
        idempotency_key = request.headers.get("X-Idempotency-Key")
        if idempotency_key:
            inserted = conn.execute("INSERT OR IGNORE INTO idempotency_keys (user_id, operation_key, created_at) VALUES (?, ?, ?)", (current_user_id(), idempotency_key, utc_now_iso())).rowcount
            if inserted == 0:
                return jsonify({"task": {"id": task_id, "completed": completed}, "duplicate": True})
        task = conn.execute(
            """
            SELECT tasks.id, tasks.title, tasks.routine_id
            FROM tasks
            JOIN routines ON routines.id = tasks.routine_id
            WHERE tasks.id = ? AND routines.user_id = ? AND tasks.archived = 0
            """,
            (task_id, current_user_id()),
        ).fetchone()
        if not task:
            return jsonify({"error": "task not found"}), 404
        conn.execute(
            """
            INSERT INTO task_history (task_id, completion_date, completed)
            VALUES (?, ?, ?)
            ON CONFLICT(task_id, completion_date)
            DO UPDATE SET completed = excluded.completed
            """,
            (task_id, completion_date, int(completed)),
        )
        task_counts = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN task_history.completed = 1 THEN 1 ELSE 0 END) AS completed
            FROM tasks
            LEFT JOIN task_history
              ON task_history.task_id = tasks.id AND task_history.completion_date = ?
            WHERE tasks.routine_id = ? AND tasks.archived = 0
              AND (tasks.task_date IS NULL OR tasks.task_date = ?)
            """,
            (completion_date, task["routine_id"], completion_date),
        ).fetchone()
        all_tasks_completed = bool(task_counts["total"] and task_counts["completed"] == task_counts["total"])
        if all_tasks_completed:
            routine = conn.execute("SELECT * FROM routines WHERE id = ?", (task["routine_id"],)).fetchone()
            conn.execute(
                """
                INSERT INTO routine_history (routine_id, completion_date, completed, minutes_done)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(routine_id, completion_date)
                DO UPDATE SET completed = 1, minutes_done = MAX(routine_history.minutes_done, excluded.minutes_done)
                """,
                (task["routine_id"], completion_date, int(routine["target_minutes"] or 0)),
            )
        else:
            conn.execute(
                "UPDATE routine_history SET completed = 0, minutes_done = 0 WHERE routine_id = ? AND completion_date = ?",
                (task["routine_id"], completion_date),
            )
        if completed:
            automations = conn.execute(
                "SELECT * FROM automations WHERE user_id = ? AND trigger_type = 'task_completed' AND trigger_task_id = ? AND action_type = 'create_reminder' AND enabled = 1",
                (current_user_id(), task_id),
            ).fetchall()
            for automation in automations:
                due_at = datetime.now(timezone.utc) + timedelta(minutes=automation["delay_minutes"])
                conn.execute(
                    "INSERT INTO reminders (user_id, task_id, title, due_at, timezone, created_at) VALUES (?, ?, ?, ?, 'UTC', ?)",
                    (current_user_id(), automation["target_task_id"], automation["title"], due_at.replace(microsecond=0).isoformat(), utc_now_iso()),
                )

        routine = conn.execute("SELECT * FROM routines WHERE id = ?", (task["routine_id"],)).fetchone()
        routine_summary = get_routine_week_summary(conn, routine, completion_date)
        stats = get_global_stats(conn)
        daily_progress = routine_summary["daily_progress"]
    return jsonify({
        "task": {"id": task["id"], "title": task["title"], "completed": completed},
        "routine": routine_summary,
        "day": {
            "iso": completion_date,
            "checked": daily_progress["complete"],
            "minutes_done": daily_progress["completed_minutes"],
            "progress_percent": daily_progress["percentage"],
            "completed_tasks": daily_progress["completed_tasks"],
            "total_tasks": daily_progress["total_tasks"],
        },
        "stats": stats,
    })


@app.route("/api/automation-tasks", methods=["GET"])
@login_required
def automation_tasks_api():
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT tasks.id, tasks.title, routines.title AS routine_title
            FROM tasks
            JOIN routines ON routines.id = tasks.routine_id
            WHERE routines.user_id = ? AND tasks.archived = 0
            ORDER BY routines.title, tasks.position, tasks.id
            """,
            (current_user_id(),),
        ).fetchall()
    return jsonify({"tasks": [dict(row) for row in rows]})


@app.route("/api/automations", methods=["GET", "POST"])
@login_required
def automations_api():
    with get_db_connection() as conn:
        if request.method == "GET":
            rows = conn.execute(
                "SELECT * FROM automations WHERE user_id = ? "
                "AND NOT (trigger_type = 'task_completed' AND trigger_task_id IS NULL) "
                "ORDER BY id DESC",
                (current_user_id(),),
            ).fetchall()
            return jsonify({"automations": [dict(row) for row in rows]})
        payload = request.get_json(silent=True) or {}
        trigger_type = str(payload.get("trigger_type", "")).strip()
        action_type = str(payload.get("action_type", "")).strip()
        if trigger_type not in {"task_completed", "scheduled_time", "task_missed"} or action_type not in {"create_reminder", "start_timer"}:
            return jsonify({"error": "unsupported trigger or action"}), 400
        try:
            delay = max(0, min(10080, int(payload.get("delay_minutes", 0))))
        except (TypeError, ValueError):
            return jsonify({"error": "delay_minutes must be a number"}), 400
        title = str(payload.get("title", "Automation reminder")).strip()[:180]
        trigger_task_id = payload.get("trigger_task_id")
        target_task_id = payload.get("target_task_id")
        try:
            trigger_task_id = int(trigger_task_id) if trigger_task_id is not None else None
            target_task_id = int(target_task_id) if target_task_id is not None else None
        except (TypeError, ValueError):
            return jsonify({"error": "task IDs must be numbers"}), 400
        if trigger_type == "task_completed" and trigger_task_id is None:
            return jsonify({"error": "a trigger task is required"}), 400
        if trigger_task_id is not None and not get_owned_task(conn, trigger_task_id):
            return jsonify({"error": "trigger task not found"}), 404
        if target_task_id is not None and not get_owned_task(conn, target_task_id):
            return jsonify({"error": "target task not found"}), 404
        cursor = conn.execute(
            "INSERT INTO automations (user_id, trigger_type, trigger_task_id, action_type, target_task_id, delay_minutes, title, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (current_user_id(), trigger_type, trigger_task_id, action_type, target_task_id, delay, title or "Automation reminder", utc_now_iso()),
        )
    return jsonify({"automation": {"id": cursor.lastrowid, "trigger_type": trigger_type, "action_type": action_type}}), 201


@app.route("/api/automations/<int:automation_id>", methods=["PATCH", "DELETE"])
@login_required
def manage_automation_api(automation_id):
    with get_db_connection() as conn:
        automation = conn.execute("SELECT id FROM automations WHERE id = ? AND user_id = ?", (automation_id, current_user_id())).fetchone()
        if not automation:
            return jsonify({"error": "automation not found"}), 404
        if request.method == "DELETE":
            conn.execute("DELETE FROM automations WHERE id = ? AND user_id = ?", (automation_id, current_user_id()))
            return jsonify({"status": "deleted"})
        payload = request.get_json(silent=True) or {}
        if "enabled" in payload:
            conn.execute("UPDATE automations SET enabled = ? WHERE id = ? AND user_id = ?", (int(bool(payload["enabled"])), automation_id, current_user_id()))
        if "title" in payload:
            conn.execute("UPDATE automations SET title = ? WHERE id = ? AND user_id = ?", (str(payload["title"]).strip()[:180], automation_id, current_user_id()))
        if "delay_minutes" in payload:
            try:
                delay = max(0, min(10080, int(payload["delay_minutes"])))
            except (TypeError, ValueError):
                return jsonify({"error": "delay_minutes must be a number"}), 400
            conn.execute("UPDATE automations SET delay_minutes = ? WHERE id = ? AND user_id = ?", (delay, automation_id, current_user_id()))
    return jsonify({"status": "updated"})


@app.route("/api/tasks/<int:task_id>/move", methods=["POST"])
@login_required
def move_task_api(task_id):
    direction = (request.get_json(silent=True) or {}).get("direction")
    if direction not in {"up", "down"}:
        return jsonify({"error": "direction must be up or down"}), 400

    with get_db_connection() as conn:
        task = conn.execute(
            """
            SELECT tasks.id, tasks.routine_id, tasks.position
            FROM tasks
            JOIN routines ON routines.id = tasks.routine_id
            WHERE tasks.id = ? AND routines.user_id = ? AND tasks.archived = 0
            """,
            (task_id, current_user_id()),
        ).fetchone()
        if not task:
            return jsonify({"error": "task not found"}), 404
        operator = "<" if direction == "up" else ">"
        order = "DESC" if direction == "up" else "ASC"
        neighbor = conn.execute(
            f"""
            SELECT id, position FROM tasks
            WHERE routine_id = ? AND archived = 0 AND position {operator} ?
            ORDER BY position {order}, id {order} LIMIT 1
            """,
            (task["routine_id"], task["position"]),
        ).fetchone()
        if neighbor:
            conn.execute("UPDATE tasks SET position = ? WHERE id = ?", (neighbor["position"], task_id))
            conn.execute("UPDATE tasks SET position = ? WHERE id = ?", (task["position"], neighbor["id"]))

    return jsonify({"status": "ok"})


@app.route("/log_week/<int:routine_id>", methods=["POST"])
@login_required
def log_week(routine_id):
    conn = get_db_connection()
    routine = conn.execute("SELECT * FROM routines WHERE id = ? AND user_id = ?", (routine_id, current_user_id())).fetchone()
    if not routine:
        conn.close()
        return redirect(url_for("index"))

    for day in get_week_dates():
        iso_day = day.isoformat()
        checkbox_name = f"day_{iso_day}"
        minutes_name = f"minutes_{iso_day}"
        checked = request.form.get(checkbox_name) == "on"
        minutes_raw = request.form.get(minutes_name, "")
        save_day_entry(conn, routine, iso_day, checked, minutes_raw)

    conn.commit()
    conn.close()
    flash("Weekly progress saved.")
    return redirect(url_for("index"))


@app.route("/api/log_day/<int:routine_id>", methods=["GET", "POST"])
@login_required
def log_day_api(routine_id):
    conn = get_db_connection()
    routine = conn.execute("SELECT * FROM routines WHERE id = ? AND user_id = ?", (routine_id, current_user_id())).fetchone()
    if not routine:
        conn.close()
        return jsonify({"error": "routine not found"}), 404

    if request.method == "GET":
        iso_day = request.args.get("date", today_iso())
        try:
            date.fromisoformat(iso_day)
        except (TypeError, ValueError):
            conn.close()
            return jsonify({"error": "date must be a valid ISO date"}), 400
        record = conn.execute(
            "SELECT completed, minutes_done FROM routine_history WHERE routine_id = ? AND completion_date = ?",
            (routine_id, iso_day),
        ).fetchone()
        daily_progress = get_daily_task_progress(conn, routine_id, iso_day)
        conn.close()
        if daily_progress["total_tasks"] > 0:
            return jsonify({
                "day": {
                    "iso": iso_day,
                    "checked": daily_progress["complete"],
                    "minutes_done": daily_progress["completed_minutes"],
                    "progress_percent": daily_progress["percentage"],
                }
            })
        return jsonify({
            "day": {
                "iso": iso_day,
                "checked": bool(record["completed"]) if record else False,
                "minutes_done": int(record["minutes_done"] or 0) if record else 0,
                "progress_percent": min(100, round(((record["minutes_done"] or 0) / max(1, int(routine["target_minutes"] or 0))) * 100)) if record else 0,
            }
        })

    payload = request.get_json(silent=True) or {}
    iso_day = payload.get("date")
    checked = bool(payload.get("checked"))
    minutes_raw = payload.get("minutes")

    if not iso_day:
        return jsonify({"error": "date is required"}), 400

    try:
        date.fromisoformat(iso_day)
    except (TypeError, ValueError):
        return jsonify({"error": "date must be a valid ISO date"}), 400

    idempotency_key = request.headers.get("X-Idempotency-Key")
    if idempotency_key:
        inserted = conn.execute("INSERT OR IGNORE INTO idempotency_keys (user_id, operation_key, created_at) VALUES (?, ?, ?)", (current_user_id(), idempotency_key, utc_now_iso())).rowcount
        if inserted == 0:
            conn.close()
            return jsonify({"duplicate": True, "status": "already applied"})
    day_result = save_day_entry(conn, routine, iso_day, checked, minutes_raw)
    if iso_day == today_iso():
        conn.execute(
            """
            INSERT INTO task_history (task_id, completion_date, completed)
            SELECT id, ?, ?
            FROM tasks
            WHERE routine_id = ? AND archived = 0
            ON CONFLICT(task_id, completion_date)
            DO UPDATE SET completed = excluded.completed
            """,
            (iso_day, int(checked), routine_id),
        )
    routine_summary = get_routine_week_summary(conn, routine)
    daily_progress = get_daily_task_progress(conn, routine_id, iso_day)
    if daily_progress["total_tasks"] > 0:
        day_result = {
            "iso": iso_day,
            "checked": daily_progress["complete"],
            "minutes_done": daily_progress["completed_minutes"],
            "progress_percent": daily_progress["percentage"],
            "completed_tasks": daily_progress["completed_tasks"],
            "total_tasks": daily_progress["total_tasks"],
        }
    stats = get_global_stats(conn)
    conn.commit()
    conn.close()

    return jsonify(
        {
            "day": day_result,
            "routine": routine_summary,
            "stats": stats,
        }
    )


@app.route("/delete/<int:routine_id>", methods=["POST"])
@login_required
def delete_routine(routine_id):
    conn = get_db_connection()
    routine = conn.execute("SELECT id FROM routines WHERE id = ? AND user_id = ?", (routine_id, current_user_id())).fetchone()
    if routine:
        conn.execute("UPDATE routines SET archived = 1 WHERE id = ? AND user_id = ?", (routine_id, current_user_id()))
    conn.commit()
    conn.close()
    flash("Routine archived. Your history is preserved.")
    return redirect(url_for("index"))


@app.route("/health")
def health():
    try:
        with get_db_connection() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ok", "database": "ok"}, 200
    except sqlite3.Error:
        logger.exception("Health check database failure")
        return {"status": "error", "database": "unavailable"}, 503


if __name__ == "__main__":
    init_db()
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", host="0.0.0.0", port=5000)
