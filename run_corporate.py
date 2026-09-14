import os


os.environ.setdefault("ROUTINE_TRACKER_AUTH", "accounts")
os.environ.setdefault("ROUTINE_TRACKER_LOCAL", "0")
os.environ.setdefault("FLASK_DEBUG", "0")

from app import app, init_db


def get_port():
    requested = input("Port [5000]: ").strip()
    if not requested:
        return 5000
    try:
        port = int(requested)
    except ValueError as exc:
        raise SystemExit("Port must be a number between 1 and 65535.") from exc
    if not 1 <= port <= 65535:
        raise SystemExit("Port must be a number between 1 and 65535.")
    return port


if __name__ == "__main__":
    init_db()
    app.run(
        debug=False,
        host=os.environ.get("ROUTINE_TRACKER_HOST", "0.0.0.0"),
        port=get_port(),
    )
