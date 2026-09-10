import sqlite3
import secrets
from uuid import uuid4

import pytest
import app as app_module
from app import app, get_monthly_progress, get_weekly_progress, hash_password, init_db, DB_PATH


@pytest.fixture
def client(monkeypatch, tmp_path):
    database_path = tmp_path / "test.sqlite"
    monkeypatch.setattr(app_module, "DB_PATH", database_path)
    app.config["TESTING"] = True
    app.config["AUTH_MODE"] = "accounts"
    app.config["LOCAL_MODE"] = False
    init_db()
    with sqlite3.connect(database_path) as conn:
        user_id = conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (f"test-{uuid4().hex}@example.com", hash_password("password123"), "2026-09-10"),
        ).lastrowid
        app_module.create_trial(conn, user_id)
        conn.commit()
    with app.test_client() as client:
        with client.session_transaction() as session_data:
            session_data["user_id"] = user_id
            session_data["user_email"] = f"test-{user_id}@example.com"
            session_data["csrf_token"] = secrets.token_urlsafe(32)
        yield client


def latest_routine_id():
    with sqlite3.connect(app_module.DB_PATH) as conn:
        return conn.execute("SELECT id FROM routines ORDER BY id DESC LIMIT 1").fetchone()[0]


def migration_db(monkeypatch, tmp_path, name):
    database_path = tmp_path / name
    monkeypatch.setattr(app_module, "DB_PATH", database_path)
    app.config["TESTING"] = True
    return database_path


def test_fresh_database_gets_one_schema_baseline(monkeypatch, tmp_path):
    database_path = migration_db(monkeypatch, tmp_path, "fresh.sqlite")
    init_db()
    with app_module.get_db_connection() as app_connection:
        assert app_connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with sqlite3.connect(database_path) as conn:
        row = conn.execute(
            "SELECT version, name, checksum FROM schema_migrations"
        ).fetchone()
        assert row == (
            app_module.SCHEMA_MIGRATION_VERSION,
            app_module.SCHEMA_MIGRATION_NAME,
            app_module.SCHEMA_MIGRATION_CHECKSUM,
        )
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 1").fetchone()[0] == 1


def test_hardened_foreign_keys_cover_owned_relationships(monkeypatch, tmp_path):
    database_path = migration_db(monkeypatch, tmp_path, "foreign_keys.sqlite")
    init_db()
    expected = {
        "routines": "users",
        "tasks": "routines",
        "routine_history": "routines",
        "task_history": "tasks",
        "schedules": "routines",
        "categories": "users",
        "reminders": "users",
        "timers": "users",
        "goals": "users",
        "milestones": "users",
        "automations": "users",
        "subscriptions": "users",
        "notification_preferences": "users",
        "password_reset_tokens": "users",
        "push_subscriptions": "users",
        "idempotency_keys": "users",
    }
    with sqlite3.connect(database_path) as conn:
        for table, parent in expected.items():
            parents = {row[2] for row in conn.execute(f"PRAGMA foreign_key_list({table})")}
            assert parent in parents
        automation_parents = {row[2] for row in conn.execute("PRAGMA foreign_key_list(automations)")}
        assert "tasks" in automation_parents


def test_existing_current_schema_baseline_preserves_data_and_is_idempotent(monkeypatch, tmp_path):
    database_path = migration_db(monkeypatch, tmp_path, "current.sqlite")
    init_db()
    with sqlite3.connect(database_path) as conn:
        user_id = conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            ("migration@example.com", hash_password("password123"), "2026-09-10"),
        ).lastrowid
        routine_id = conn.execute(
            "INSERT INTO routines (title, user_id, created_at) VALUES (?, ?, ?)",
            ("Keep this routine", user_id, "2026-09-10"),
        ).lastrowid
        conn.execute("DROP TABLE schema_migrations")
        conn.commit()

    init_db()
    init_db()
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 1").fetchone()[0] == 1
        assert conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()[0] == "migration@example.com"
        assert conn.execute("SELECT title FROM routines WHERE id = ?", (routine_id,)).fetchone()[0] == "Keep this routine"
        assert "google_sub" not in {row[1] for row in conn.execute("PRAGMA table_info(users)")}


def test_legacy_missing_column_is_upgraded_before_baseline(monkeypatch, tmp_path):
    database_path = migration_db(monkeypatch, tmp_path, "legacy.sqlite")
    init_db()
    with sqlite3.connect(database_path) as conn:
        conn.execute("ALTER TABLE routines RENAME COLUMN position TO legacy_position")
        conn.execute("DROP TABLE schema_migrations")
        conn.commit()

    init_db()
    with sqlite3.connect(database_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(routines)")}
        assert "position" in columns
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 1").fetchone()[0] == 1
        assert "legacy_position" in columns


def test_invalid_schema_is_rejected_without_baseline(monkeypatch, tmp_path):
    database_path = migration_db(monkeypatch, tmp_path, "invalid.sqlite")
    init_db()
    with sqlite3.connect(database_path) as conn:
        conn.execute("ALTER TABLE users RENAME COLUMN email TO invalid_email")
        conn.execute("DROP TABLE schema_migrations")
        conn.commit()

    with pytest.raises(RuntimeError, match="missing required columns: email"):
        init_db()
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()[0] == 0


def test_migration_checksum_mismatch_is_rejected(monkeypatch, tmp_path):
    database_path = migration_db(monkeypatch, tmp_path, "checksum.sqlite")
    init_db()
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
            ("incorrect", app_module.SCHEMA_MIGRATION_VERSION),
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        init_db()
    with sqlite3.connect(database_path) as conn:
        row = conn.execute(
            "SELECT name, checksum FROM schema_migrations WHERE version = ?",
            (app_module.SCHEMA_MIGRATION_VERSION,),
        ).fetchone()
        assert row == (app_module.SCHEMA_MIGRATION_NAME, "incorrect")


def test_migration_failure_rolls_back_baseline_transaction(monkeypatch, tmp_path):
    database_path = migration_db(monkeypatch, tmp_path, "rollback.sqlite")
    init_db()
    with sqlite3.connect(database_path) as conn:
        user_id = conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            ("rollback@example.com", hash_password("password123"), "2026-09-10"),
        ).lastrowid
        conn.execute("DELETE FROM schema_migrations WHERE version >= 3")
        conn.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")
        conn.execute("CREATE UNIQUE INDEX idx_users_google_sub_test ON users(google_sub)")
        conn.execute(
            """
            CREATE TRIGGER fail_schema_baseline
            BEFORE INSERT ON schema_migrations
            BEGIN
                SELECT RAISE(ABORT, 'forced migration failure');
            END
            """
        )
        conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced migration failure"):
        init_db()
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 3").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM users WHERE id = ?", (user_id,)).fetchone()[0] == 1


def test_sqlite_connection_uses_wal_and_busy_timeout(monkeypatch, tmp_path):
    database_path = migration_db(monkeypatch, tmp_path, "connection.sqlite")
    with app_module.get_db_connection() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


def test_local_auth_remains_and_google_routes_are_removed(client):
    assert client.get("/auth/google").status_code == 404
    client.post("/logout")
    with sqlite3.connect(app_module.DB_PATH) as conn:
        email = conn.execute("SELECT email FROM users LIMIT 1").fetchone()[0]
    response = client.post(
        "/login",
        data={"email": email, "password": "wrong-password"},
    )
    assert response.status_code == 401


def test_single_user_can_use_related_references(client):
    client.post("/add", data={"title": "Private", "category": "Work", "target_minutes": 20})
    routine_id = latest_routine_id()
    task_id = client.post(f"/api/routines/{routine_id}/tasks", json={"title": "Private task"}).get_json()["task"]["id"]
    assert client.post("/api/reminders", json={"title": "Valid", "due_at": "2026-09-10T08:00:00+00:00", "routine_id": routine_id, "task_id": task_id}).status_code == 201
    assert client.post("/api/timers", json={"title": "Valid", "duration_seconds": 60, "routine_id": routine_id, "task_id": task_id}).status_code == 201
    assert client.post("/api/automations", json={"trigger_type": "task_completed", "trigger_task_id": task_id, "action_type": "create_reminder", "title": "Valid"}).status_code == 201


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_local_mode_reuses_persistent_local_account(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "local.sqlite")
    monkeypatch.setitem(app.config, "LOCAL_MODE", True)
    app.config["TESTING"] = True
    init_db()
    with app.test_client() as client:
        first = client.get("/", base_url="http://127.0.0.1:5000")
        assert first.status_code == 200
        with sqlite3.connect(app_module.DB_PATH) as conn:
            first_user = conn.execute("SELECT id FROM users WHERE email = 'local@localhost'").fetchone()
            conn.execute("INSERT INTO routines (title, created_at, user_id) VALUES (?, ?, ?)", ("Local persistence", "2026-08-24", first_user[0]))
            conn.commit()

    with app.test_client() as client:
        second = client.get("/", base_url="http://127.0.0.1:5000")
        assert second.status_code == 200
        assert b"Local persistence" in second.data

    monkeypatch.setitem(app.config, "LOCAL_MODE", False)


def test_normal_startup_disables_local_mode_and_enforces_csrf():
    assert app.config["LOCAL_MODE"] is False
    app.config["TESTING"] = False
    with app.test_client() as client:
        login_page = client.get("/login")
        assert login_page.status_code == 200
        response = client.post(
            "/add",
            data={"title": "Blocked", "category": "Security", "target_minutes": 20},
        )
    app.config["TESTING"] = True
    assert response.status_code == 302


def test_normal_mode_rejects_unauthenticated_browser_and_api_requests(client):
    with client.session_transaction() as session_data:
        session_data.clear()
    assert client.get("/").status_code == 302
    response = client.get("/api/history")
    assert response.status_code == 401
    assert response.get_json() == {"error": "authentication required"}


def test_login_valid_invalid_and_csrf_behavior(client):
    with sqlite3.connect(app_module.DB_PATH) as conn:
        email = conn.execute("SELECT email FROM users LIMIT 1").fetchone()[0]
    with client.session_transaction() as session_data:
        session_data.clear()
    app.config["TESTING"] = False
    try:
        assert client.post("/login", data={"email": email, "password": "password123"}).status_code == 400
        client.get("/login")
        with client.session_transaction() as session_data:
            token = session_data["csrf_token"]
        assert client.post("/login", data={"csrf_token": token, "email": email, "password": "wrong"}).status_code == 401
        client.get("/login")
        with client.session_transaction() as session_data:
            token = session_data["csrf_token"]
        assert client.post("/login", data={"csrf_token": token, "email": email, "password": "password123"}).status_code == 302
    finally:
        app.config["TESTING"] = True


def test_registration_rejects_duplicates_and_logout_requires_csrf(client):
    email = f"registered-{uuid4().hex}@example.com"
    client.post("/logout")
    assert client.post("/register", data={"email": email, "password": "password123"}).status_code == 302
    client.post("/logout")
    assert client.post("/register", data={"email": email, "password": "password123"}).status_code == 409
    assert client.post("/login", data={"email": email, "password": "password123"}).status_code == 302
    app.config["TESTING"] = False
    try:
        assert client.post("/logout").status_code == 400
        with client.session_transaction() as session_data:
            token = session_data["csrf_token"]
        assert client.post("/logout", data={"csrf_token": token}).status_code == 302
        assert client.get("/api/account").status_code == 401
    finally:
        app.config["TESTING"] = True


def test_index_loads(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Routine Tracker" in response.data
    assert b"All routines" in response.data


def test_add_routine(client):
    response = client.post(
        "/add",
        data={
            "title": "Read",
            "description": "Read for 20 minutes",
            "category": "Learning",
            "target_minutes": 20,
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Routine added successfully" in response.data


def test_weekly_calendar_renders(client):
    client.post(
        "/add",
        data={
            "title": "Read",
            "description": "Read for 20 minutes",
            "category": "Learning",
            "target_minutes": 20,
        },
        follow_redirects=True,
    )
    response = client.get("/")
    assert response.status_code == 200
    assert b"Mon" in response.data
    assert b"Archive routine" in response.data
    assert b"Log minutes" in response.data


def test_category_filter_and_stats_page(client):
    client.post(
        "/add",
        data={
            "title": "Read",
            "description": "Read for 20 minutes",
            "category": "Learning",
            "target_minutes": 20,
        },
        follow_redirects=True,
    )
    client.post(
        "/add",
        data={
            "title": "Workout",
            "description": "Train for 30 minutes",
            "category": "Health",
            "target_minutes": 30,
        },
        follow_redirects=True,
    )

    filtered = client.get("/?category=Learning")
    assert filtered.status_code == 200
    assert b"Learning" in filtered.data
    assert b"Health" in filtered.data
    assert b"Read" in filtered.data
    assert b"Workout" not in filtered.data

    stats_page = client.get("/stats")
    assert stats_page.status_code == 200
    assert b"Weekly progress" in stats_page.data
    assert b"Completion" in stats_page.data


def test_partial_minutes_count_toward_progress(client):
    client.post(
        "/add",
        data={
            "title": "Read",
            "description": "Read for 25 minutes",
            "category": "Learning",
            "target_minutes": 25,
        },
        follow_redirects=True,
    )

    routine_id = latest_routine_id()
    today = __import__("datetime").date.today().isoformat()
    response = client.post(
        f"/api/log_day/{routine_id}",
        json={
            "date": today,
            "checked": True,
            "minutes": 10,
        },
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["day"]["minutes_done"] == 10
    assert payload["day"]["progress_percent"] == 40

    page = client.get("/?category=Learning")
    assert page.status_code == 200
    assert b"40%" in page.data


def test_log_day_api_updates_stats_without_full_reload(client):
    client.post(
        "/add",
        data={
            "title": "Read",
            "description": "Read for 20 minutes",
            "category": "Learning",
            "target_minutes": 20,
        },
        follow_redirects=True,
    )

    today = __import__("datetime").date.today().isoformat()
    response = client.post(
        f"/api/log_day/{latest_routine_id()}",
        json={
            "date": today,
            "checked": True,
            "minutes": 20,
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["routine"]["today_done"] is True
    assert payload["stats"]["today_completed"] == 1


def test_tasks_can_be_added_toggled_and_rendered(client):
    client.post(
        "/add",
        data={"title": "Morning routine", "category": "Health", "target_minutes": 20},
    )
    routine_id = latest_routine_id()

    added = client.post(
        f"/api/routines/{routine_id}/tasks",
        json={"title": "Drink water"},
    )
    assert added.status_code == 201
    task_id = added.get_json()["task"]["id"]

    toggled = client.post(f"/api/tasks/{task_id}/toggle", json={"completed": True})
    assert toggled.status_code == 200
    assert toggled.get_json()["task"]["completed"] is True

    page = client.get("/")
    assert b"Drink water" in page.data
    assert b"1 / 1" in page.data


def test_weekly_progress_is_weighted_and_has_seven_daily_values():
    entries = [
        {
            "iso": f"2026-09-{day:02d}",
            "progress_percent": percentage,
            "planned_minutes": planned,
            "completed_minutes": completed,
            "completed_tasks": completed_tasks,
            "total_tasks": total_tasks,
            "fully_completed": fully_completed,
        }
        for day, percentage, planned, completed, completed_tasks, total_tasks, fully_completed in [
            (7, 100, 10, 10, 1, 1, True),
            (8, 10, 100, 10, 1, 2, False),
            (9, 50, 20, 10, 1, 2, False),
            (10, 100, 30, 30, 2, 2, True),
            (11, 75, 40, 30, 3, 4, False),
            (12, 40, 50, 20, 1, 3, False),
            (13, 90, 10, 9, 2, 3, False),
        ]
    ]

    weekly = get_weekly_progress(entries)

    assert len(weekly["days"]) == 7
    assert weekly["planned_minutes"] == 260
    assert weekly["completed_minutes"] == 119
    assert weekly["completed_tasks"] == 11
    assert weekly["total_tasks"] == 17
    assert weekly["completed_days"] == 2
    assert weekly["percentage"] == 46


def test_task_toggle_returns_daily_and_weekly_progress_without_reload(client):
    client.post("/add", data={"title": "Morning", "category": "Health", "target_minutes": 20})
    routine_id = latest_routine_id()
    first = client.post(
        f"/api/routines/{routine_id}/tasks",
        json={"title": "Workout", "target_minutes": 20},
    ).get_json()["task"]
    second = client.post(
        f"/api/routines/{routine_id}/tasks",
        json={"title": "Stretch", "target_minutes": 30},
    ).get_json()["task"]

    partial = client.post(f"/api/tasks/{first['id']}/toggle", json={"completed": True}).get_json()
    assert partial["routine"]["daily_progress"]["completed_minutes"] == 20
    assert partial["routine"]["daily_progress"]["planned_minutes"] == 50
    assert partial["routine"]["daily_progress"]["percentage"] == 40
    assert partial["routine"]["weekly_progress"]["completed_minutes"] == 20
    assert partial["day"]["checked"] is False

    complete = client.post(f"/api/tasks/{second['id']}/toggle", json={"completed": True}).get_json()
    assert complete["routine"]["weekly_progress"]["completed_minutes"] == 50
    assert complete["routine"]["weekly_progress"]["percentage"] == 14
    assert complete["routine"]["daily_progress"]["percentage"] == 100
    assert complete["day"]["checked"] is True

    undone = client.post(f"/api/tasks/{first['id']}/toggle", json={"completed": False}).get_json()
    assert undone["routine"]["weekly_progress"]["completed_minutes"] == 30
    assert undone["routine"]["weekly_progress"]["percentage"] == 9
    assert undone["routine"]["daily_progress"]["percentage"] == 60
    assert undone["day"]["checked"] is False


def test_monthly_aggregation_is_weighted_and_live(client):
    client.post("/add", data={"title": "Monthly", "category": "Focus", "target_minutes": 10})
    routine_id = latest_routine_id()
    first = client.post(
        f"/api/routines/{routine_id}/tasks",
        json={"title": "Short", "target_minutes": 10},
    ).get_json()["task"]
    second = client.post(
        f"/api/routines/{routine_id}/tasks",
        json={"title": "Long", "target_minutes": 100},
    ).get_json()["task"]
    month = "2026-09"
    with sqlite3.connect(app_module.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        routines = conn.execute("SELECT * FROM routines WHERE id = ?", (routine_id,)).fetchall()
        monthly = get_monthly_progress(conn, routines, month)
    assert len(monthly["days"]) == 30
    assert monthly["planned_minutes"] == 3300
    assert monthly["completed_minutes"] == 0
    assert monthly["completed_days"] == 0
    assert monthly["percentage"] == 0
    assert sum(day["planned_minutes"] == 110 for day in monthly["days"]) == 30

    today = __import__("datetime").date.today().isoformat()
    client.post(f"/api/tasks/{first['id']}/toggle", json={"completed": True})
    partial = client.get(f"/api/monthly?month={today[:7]}").get_json()
    assert partial["completed_minutes"] == 10
    assert partial["percentage"] == 0
    assert partial["completed_days"] == 0

    client.post(f"/api/tasks/{second['id']}/toggle", json={"completed": True})
    complete = client.get(f"/api/monthly?month={today[:7]}").get_json()
    assert complete["completed_minutes"] == 110
    assert complete["percentage"] == 3
    assert complete["completed_days"] == 1

    client.post(f"/api/tasks/{second['id']}/toggle", json={"completed": False})
    undone = client.get(f"/api/monthly?month={today[:7]}").get_json()
    assert undone["completed_minutes"] == 10
    assert undone["completed_days"] == 0


def test_monthly_aggregation_handles_empty_month(client):
    response = client.get("/api/monthly?month=2026-02")
    assert response.status_code == 200
    monthly = response.get_json()
    assert len(monthly["days"]) == 28
    assert monthly["planned_minutes"] == 0
    assert monthly["completed_minutes"] == 0
    assert monthly["completed_tasks"] == 0
    assert monthly["total_tasks"] == 0
    assert monthly["completed_days"] == 0
    assert monthly["percentage"] == 0
    assert all(day["planned_minutes"] == 0 and not day["complete"] for day in monthly["days"])


def test_calendar_consumes_daily_task_progress_without_reload(client):
    client.post("/add", data={"title": "Morning", "category": "Health", "target_minutes": 20})
    routine_id = latest_routine_id()
    first = client.post(
        f"/api/routines/{routine_id}/tasks",
        json={"title": "Workout", "target_minutes": 20},
    ).get_json()["task"]
    second = client.post(
        f"/api/routines/{routine_id}/tasks",
        json={"title": "Stretch", "target_minutes": 30},
    ).get_json()["task"]
    today = __import__("datetime").date.today().isoformat()
    month = today[:7]

    initial = client.get(f"/api/calendar?month={month}").get_json()
    current = next(day for day in initial["days"] if day["date"] == today)
    assert current["planned_minutes"] == 50
    assert current["completed_minutes"] == 0
    assert current["complete"] is False

    client.post(f"/api/tasks/{first['id']}/toggle", json={"completed": True})
    partial = client.get(f"/api/calendar?month={month}").get_json()
    current = next(day for day in partial["days"] if day["date"] == today)
    assert current["completed_minutes"] == 20
    assert current["percentage"] == 40
    assert current["complete"] is False

    client.post(f"/api/tasks/{second['id']}/toggle", json={"completed": True})
    complete = client.get(f"/api/history?date={today}").get_json()
    assert complete["completed_minutes"] == 50
    assert complete["percentage"] == 100
    assert complete["complete"] is True
    assert {task["title"] for task in complete["routines"][0]["tasks"]} == {"Workout", "Stretch"}

    client.post(f"/api/tasks/{first['id']}/toggle", json={"completed": False})
    undone = client.get(f"/api/calendar?month={month}").get_json()
    current = next(day for day in undone["days"] if day["date"] == today)
    assert current["completed_minutes"] == 30
    assert current["percentage"] == 60
    assert current["complete"] is False


def test_tasks_are_isolated_between_users(client):
    client.post("/add", data={"title": "Private routine", "category": "Work", "target_minutes": 20})
    routine_id = latest_routine_id()

    client.post("/logout")
    client.post("/register", data={"email": f"other-{uuid4().hex}@example.com", "password": "password123"})

    add_task = client.post(
        f"/api/routines/{routine_id}/tasks",
        json={"title": "Should be denied"},
    )
    assert add_task.status_code == 404


def test_routine_lifecycle_preserves_history_and_copies_tasks(client):
    client.post("/add", data={"title": "Evening routine", "category": "Personal", "target_minutes": 30})
    routine_id = latest_routine_id()
    today = __import__("datetime").date.today().isoformat()
    client.post(f"/api/log_day/{routine_id}", json={"date": today, "checked": True, "minutes": 30})
    first_task = client.post(f"/api/routines/{routine_id}/tasks", json={"title": "Read"}).get_json()["task"]
    second_task = client.post(f"/api/routines/{routine_id}/tasks", json={"title": "Stretch"}).get_json()["task"]

    moved = client.post(f"/api/tasks/{second_task['id']}/move", json={"direction": "up"})
    assert moved.status_code == 200

    paused = client.post(f"/api/routines/{routine_id}/pause")
    assert paused.status_code == 200
    assert paused.get_json()["paused"] is True

    duplicate = client.post(f"/api/routines/{routine_id}/duplicate")
    assert duplicate.status_code == 201
    duplicate_id = duplicate.get_json()["routine_id"]

    archived = client.post(f"/api/routines/{routine_id}/archive")
    assert archived.status_code == 200
    page = client.get("/")
    assert f'data-routine-id="{routine_id}"'.encode() not in page.data
    assert b"Evening routine copy" in page.data

    with sqlite3.connect(app_module.DB_PATH) as conn:
        history = conn.execute(
            "SELECT completed FROM routine_history WHERE routine_id = ? AND completion_date = ?",
            (routine_id, today),
        ).fetchone()
        copied_history = conn.execute(
            "SELECT COUNT(*) FROM routine_history WHERE routine_id = ?",
            (duplicate_id,),
        ).fetchone()[0]
        copied_tasks = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE routine_id = ?",
            (duplicate_id,),
        ).fetchone()[0]
    assert history[0] == 1
    assert copied_history == 0
    assert copied_tasks == 2
    assert first_task["title"] == "Read"


def test_categories_and_schedules_persist_for_owner(client):
    created = client.post("/api/categories", json={"name": "Focus", "color": "#123456"})
    assert created.status_code == 201
    category_id = created.get_json()["category"]["id"]

    client.post("/add", data={"title": "Deep work", "category": "Focus", "target_minutes": 50})
    routine_id = latest_routine_id()
    schedule = client.post(
        f"/api/routines/{routine_id}/schedule",
        json={
            "frequency": "weekly",
            "time_of_day": "08:30",
            "weekdays": ["0", "2", "4"],
            "timezone": "Europe/London",
            "enabled": True,
        },
    )
    assert schedule.status_code == 200

    page = client.get("/")
    assert b"Focus" in page.data
    assert b"data-routine-schedule" in page.data

    renamed = client.patch(f"/api/categories/{category_id}", json={"name": "Work"})
    assert renamed.status_code == 200
    assert b"Work" in client.get("/").data


def test_category_can_be_deleted_and_routines_move_to_general(client):
    created = client.post("/api/categories", json={"name": "Temporary"})
    category_id = created.get_json()["category"]["id"]
    client.post("/add", data={"title": "Temporary routine", "category": "Temporary", "target_minutes": 20})
    routine_id = latest_routine_id()

    deleted = client.delete(f"/api/categories/{category_id}", json={"replacement": "General"})

    assert deleted.status_code == 200
    with sqlite3.connect(app_module.DB_PATH) as conn:
        routine = conn.execute("SELECT category FROM routines WHERE id = ?", (routine_id,)).fetchone()
        category = conn.execute("SELECT id FROM categories WHERE id = ?", (category_id,)).fetchone()
    assert routine[0] == "General"
    assert category is None
    assert client.delete(f"/api/categories/{category_id}").status_code == 404


def test_weekly_progress_excludes_unscheduled_days(client):
    client.post("/add", data={"title": "Weekday routine", "category": "Work", "target_minutes": 20})
    routine_id = latest_routine_id()
    client.post(
        f"/api/routines/{routine_id}/schedule",
        json={"frequency": "weekly", "weekdays": ["0", "1", "2", "3", "4"], "enabled": True},
    )
    response = client.post(
        f"/api/log_day/{routine_id}",
        json={"date": __import__("datetime").date.today().isoformat(), "checked": True, "minutes": 20},
    )
    assert response.status_code == 200
    assert response.get_json()["stats"]["week_percent"] == 20

    page = client.get("/")
    assert b"Record a day" in page.data
    assert b"data-record-date" in page.data
    assert page.data.count(b"class=\"day-checkbox\"") == 7


def test_day_can_be_recorded_for_any_selected_date(client):
    client.post("/add", data={"title": "Flexible routine", "category": "Work", "target_minutes": 20})
    routine_id = latest_routine_id()
    selected_date = "2026-01-15"

    saved = client.post(
        f"/api/log_day/{routine_id}",
        json={"date": selected_date, "checked": True, "minutes": 20},
    )
    loaded = client.get(f"/api/log_day/{routine_id}?date={selected_date}")

    assert saved.status_code == 200
    assert loaded.status_code == 200
    assert loaded.get_json()["day"]["checked"] is True
    assert loaded.get_json()["day"]["minutes_done"] == 20


def test_reminders_support_snooze_and_notification_preferences(client):
    due_at = "2026-08-10T08:00:00+00:00"
    created = client.post("/api/reminders", json={"title": "Start work", "due_at": due_at})
    assert created.status_code == 201
    reminder_id = created.get_json()["reminder"]["id"]

    listed = client.get("/api/reminders")
    assert listed.status_code == 200
    assert listed.get_json()["reminders"][0]["title"] == "Start work"

    snoozed = client.post(f"/api/reminders/{reminder_id}/snooze", json={"minutes": 10})
    assert snoozed.status_code == 200
    assert snoozed.get_json()["status"] == "snooze"

    preferences = client.post("/api/notification-preferences", json={"enabled": False, "permission_state": "denied"})
    assert preferences.status_code == 200
    assert preferences.get_json()["enabled"] == 0

    completed = client.post(f"/api/reminders/{reminder_id}/complete")
    assert completed.status_code == 200
    assert client.get("/api/reminders").get_json()["reminders"] == []


def test_reminders_are_isolated_between_users(client):
    created = client.post("/api/reminders", json={"title": "Private", "due_at": "2026-08-10T08:00:00+00:00"})
    reminder_id = created.get_json()["reminder"]["id"]
    client.post("/logout")
    client.post("/register", data={"email": f"reminder-other-{uuid4().hex}@example.com", "password": "password123"})
    assert client.post(f"/api/reminders/{reminder_id}/dismiss").status_code == 404


def test_timer_state_persists_and_is_owned(client):
    created = client.post("/api/timers", json={"title": "Focus", "duration_seconds": 120})
    assert created.status_code == 201
    timer_id = created.get_json()["timer"]["id"]
    started = client.post(f"/api/timers/{timer_id}/start")
    assert started.status_code == 200
    assert started.get_json()["timer"]["status"] == "running"
    paused = client.post(f"/api/timers/{timer_id}/pause")
    assert paused.get_json()["timer"]["status"] == "paused"
    resumed = client.post(f"/api/timers/{timer_id}/resume")
    assert resumed.get_json()["timer"]["status"] == "running"
    assert resumed.get_json()["timer"]["remaining_seconds"] <= 120

    client.post("/logout")
    client.post("/register", data={"email": f"timer-other-{uuid4().hex}@example.com", "password": "password123"})
    assert client.post(f"/api/timers/{timer_id}/cancel").status_code == 404


def test_account_trial_export_profile_goals_and_milestones(client):
    account = client.get("/api/account")
    assert account.status_code == 200
    assert account.get_json()["subscription"]["status"] == "trial"
    assert account.get_json()["subscription"]["pro"] is True

    updated = client.patch("/api/account", json={"display_name": "Routine User", "timezone": "Europe/London"})
    assert updated.status_code == 200
    assert client.get("/api/account").get_json()["account"]["timezone"] == "Europe/London"

    created = client.post("/api/goals", json={"name": "Read books", "target": 20})
    assert created.status_code == 201
    assert client.get("/api/goals").get_json()["goals"][0]["name"] == "Read books"

    export = client.get("/api/export.json")
    assert export.status_code == 200
    assert b"password_hash" not in export.data
    assert client.get("/api/milestones").status_code == 200


def test_account_deletion_removes_owned_data_and_preserves_other_user(client):
    with client.session_transaction() as session_data:
        owner_id = session_data["user_id"]

    def seed_user(conn, user_id, suffix):
        routine_cursor = conn.execute(
            "INSERT INTO routines (title, created_at, user_id) VALUES (?, ?, ?)",
            (f"Routine {suffix}", "2026-09-10", user_id),
        )
        routine_id = routine_cursor.lastrowid
        task_cursor = conn.execute(
            "INSERT INTO tasks (routine_id, title, created_at) VALUES (?, ?, ?)",
            (routine_id, f"Task {suffix}", "2026-09-10"),
        )
        task_id = task_cursor.lastrowid
        conn.execute(
            "INSERT INTO routine_history (routine_id, completion_date, completed, minutes_done) VALUES (?, ?, 1, 20)",
            (routine_id, "2026-09-10"),
        )
        conn.execute(
            "INSERT INTO task_history (task_id, completion_date, completed) VALUES (?, ?, 1)",
            (task_id, "2026-09-10"),
        )
        conn.execute("INSERT INTO schedules (routine_id) VALUES (?)", (routine_id,))
        conn.execute("INSERT INTO categories (user_id, name) VALUES (?, ?)", (user_id, f"Category {suffix}"))
        conn.execute(
            "INSERT INTO reminders (user_id, routine_id, task_id, title, due_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, routine_id, task_id, f"Reminder {suffix}", "2026-09-10T08:00:00+00:00", "2026-09-10"),
        )
        conn.execute(
            "INSERT INTO timers (user_id, routine_id, task_id, title, duration_seconds, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, routine_id, task_id, f"Timer {suffix}", 60, "2026-09-10"),
        )
        conn.execute(
            "INSERT INTO goals (user_id, name, target, created_at) VALUES (?, ?, ?, ?)",
            (user_id, f"Goal {suffix}", 10, "2026-09-10"),
        )
        conn.execute(
            "INSERT INTO milestones (user_id, name, achieved_at) VALUES (?, ?, ?)",
            (user_id, f"Milestone {suffix}", "2026-09-10T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions (user_id, status, trial_start, trial_end, updated_at) VALUES (?, 'trial', ?, ?, ?)",
            (user_id, "2026-09-10", "2026-10-10", "2026-09-10T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO notification_preferences (user_id, enabled, permission_state) VALUES (?, 1, 'granted')",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO automations (user_id, trigger_type, trigger_task_id, action_type, title, created_at) VALUES (?, 'task_completed', ?, 'create_reminder', ?, ?)",
            (user_id, task_id, f"Automation {suffix}", "2026-09-10T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO password_reset_tokens (user_id, token_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (user_id, f"hash-{suffix}", "2026-10-10T00:00:00+00:00", "2026-09-10T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO push_subscriptions (user_id, endpoint, subscription_json, created_at) VALUES (?, ?, ?, ?)",
            (user_id, f"https://push.example/{suffix}", "{}", "2026-09-10T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO idempotency_keys (user_id, operation_key, created_at) VALUES (?, ?, ?)",
            (user_id, f"operation-{suffix}", "2026-09-10T00:00:00+00:00"),
        )
        return routine_id, task_id

    with sqlite3.connect(app_module.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        other_cursor = conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (f"other-{uuid4().hex}@example.com", hash_password("password123"), "2026-09-10"),
        )
        other_id = other_cursor.lastrowid
        owner_routine_id, owner_task_id = seed_user(conn, owner_id, "owner")
        other_routine_id, other_task_id = seed_user(conn, other_id, "other")
        conn.commit()

    invalid = client.delete("/api/account", json={"confirmation": "NO"})
    assert invalid.status_code == 400
    with sqlite3.connect(app_module.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE id = ?", (owner_id,)).fetchone()[0] == 1

    deleted = client.delete("/api/account", json={"confirmation": "DELETE"})
    assert deleted.status_code == 200
    assert deleted.get_json() == {"status": "deleted"}
    with client.session_transaction() as session_data:
        assert "user_id" not in session_data

    with sqlite3.connect(app_module.DB_PATH) as conn:
        user_tables = (
            "automations", "reminders", "timers", "categories", "goals", "milestones",
            "subscriptions", "notification_preferences", "password_reset_tokens",
            "push_subscriptions", "idempotency_keys",
        )
        for table in user_tables:
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (owner_id,)).fetchone()[0] == 0
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (other_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM users WHERE id = ?", (owner_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM users WHERE id = ?", (other_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM routines WHERE id = ?", (owner_routine_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE id = ?", (owner_task_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM routine_history WHERE routine_id = ?", (owner_routine_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_history WHERE task_id = ?", (owner_task_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM routines WHERE id = ?", (other_routine_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE id = ?", (other_task_id,)).fetchone()[0] == 1


def test_account_deletion_rolls_back_on_sqlite_failure(client):
    with client.session_transaction() as session_data:
        user_id = session_data["user_id"]
        csrf = session_data["csrf_token"]
    with sqlite3.connect(app_module.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO automations (user_id, trigger_type, action_type, title, created_at) VALUES (?, 'task_completed', 'create_reminder', 'rollback', ?)",
            (user_id, "2026-09-10T00:00:00+00:00"),
        )
        conn.execute(
            """
            CREATE TRIGGER force_account_delete_failure
            AFTER DELETE ON automations
            BEGIN
                SELECT RAISE(ABORT, 'forced rollback');
            END
            """
        )
        conn.commit()
    app.config["TESTING"] = False
    try:
        response = client.delete("/api/account", json={"confirmation": "DELETE"}, headers={"X-CSRFToken": csrf})
    finally:
        app.config["TESTING"] = True
        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.execute("DROP TRIGGER IF EXISTS force_account_delete_failure")
            conn.commit()
    assert response.status_code == 500
    with sqlite3.connect(app_module.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE id = ?", (user_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM automations WHERE user_id = ?", (user_id,)).fetchone()[0] == 1


def test_task_completion_automation_creates_reminder(client):
    client.post("/add", data={"title": "Workout", "category": "Health", "target_minutes": 20})
    routine_id = latest_routine_id()
    task_a = client.post(f"/api/routines/{routine_id}/tasks", json={"title": "Start workout"}).get_json()["task"]["id"]
    task_b = client.post(f"/api/routines/{routine_id}/tasks", json={"title": "Drink water"}).get_json()["task"]["id"]
    automation = client.post(
        "/api/automations",
        json={"trigger_type": "task_completed", "trigger_task_id": task_a, "action_type": "create_reminder", "target_task_id": task_b, "delay_minutes": 20, "title": "Drink water"},
    )
    assert automation.status_code == 201
    completed = client.post(f"/api/tasks/{task_a}/toggle", json={"completed": True})
    assert completed.status_code == 200
    assert client.get("/api/reminders").get_json()["reminders"][0]["title"] == "Drink water"


def test_analytics_calendar_and_automation_management(client):
    client.post("/add", data={"title": "Read", "category": "Learning", "target_minutes": 20})
    routine_id = latest_routine_id()
    task_id = client.post(f"/api/routines/{routine_id}/tasks", json={"title": "Read chapter"}).get_json()["task"]["id"]
    automation = client.post("/api/automations", json={"trigger_type": "task_completed", "trigger_task_id": task_id, "action_type": "create_reminder", "title": "Review"})
    automation_id = automation.get_json()["automation"]["id"]
    assert client.patch(f"/api/automations/{automation_id}", json={"enabled": False, "title": "Review later"}).status_code == 200
    assert client.delete(f"/api/automations/{automation_id}").status_code == 200
    assert client.get("/api/analytics?days=7").status_code == 200
    assert client.get("/calendar").status_code == 200


def test_csrf_rejects_forged_state_change(client):
    app.config["TESTING"] = False
    try:
        forged = client.post("/add", data={"title": "Forged", "category": "Unsafe", "target_minutes": 20})
        assert forged.status_code == 400
    finally:
        app.config["TESTING"] = True


def test_password_reset_token_is_one_time(client):
    assert client.get("/forgot-password").status_code == 200
    assert client.get("/reset-password/not-a-real-token").status_code == 400


def test_password_reset_token_expires_after_one_use(client, monkeypatch):
    with sqlite3.connect(app_module.DB_PATH) as conn:
        email = conn.execute("SELECT email FROM users LIMIT 1").fetchone()[0]
    delivered = {}

    def capture_reset_email(recipient, reset_url):
        delivered["recipient"] = recipient
        delivered["url"] = reset_url
        return True

    monkeypatch.setattr(app_module, "send_password_reset_email", capture_reset_email)
    response = client.post("/forgot-password", data={"email": email})
    assert response.status_code == 200
    assert delivered["recipient"] == email
    token = delivered["url"].rsplit("/", 1)[-1]
    assert client.get(f"/reset-password/{token}").status_code == 200
    assert client.post(f"/reset-password/{token}", data={"password": "newpassword123"}).status_code == 302
    assert client.post(f"/reset-password/{token}", data={"password": "anotherpassword123"}).status_code == 400
