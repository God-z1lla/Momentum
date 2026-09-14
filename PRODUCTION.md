# Production deployment

The web application runs with Flask and SQLite. Use a production WSGI server such as Waitress on Windows or Gunicorn on Linux:

```powershell
python -m waitress --listen=127.0.0.1:8000 wsgi:application
```

Install the runtime and optional production-server dependencies with `python -m pip install -r requirements.txt -r requirements-integrations.txt`. Never run Flask's development server publicly.

Copy `.env.example` to `.env` or configure the variables in the hosting provider. Required production values are:

- `ROUTINE_TRACKER_SECRET`: long random session secret
- `ROUTINE_TRACKER_HTTPS=1`: enables secure cookies behind HTTPS
- `ROUTINE_TRACKER_DB`: writable persistent SQLite path, if the default is not suitable

Advanced analytics requests are entitlement-checked server-side with `premium=1`; expired trials receive HTTP 402 while core tracking and calendar data remain available.

Optional integrations:

- Password reset email: SMTP variables. The app must have a real SMTP account before reset mail can be delivered.
- Stripe: secret key, monthly/yearly price IDs, webhook signing secret, and success/cancel URLs. Configure the webhook endpoint after payment routes are enabled.
- Web Push: VAPID public/private keys and subject. Push requires HTTPS in browsers other than localhost.

Run the push delivery worker from cron, Task Scheduler, or a process supervisor at a one-minute interval:

```powershell
python worker.py
```

The worker finds due reminders, sends them to active subscriptions, retries up to five times, and removes subscriptions that return HTTP 404/410. It requires `VAPID_PRIVATE_KEY` and `VAPID_SUBJECT`.

Back up the SQLite database before deployments and run `init_db()` during startup so additive migrations execute. Put the app behind HTTPS and a reverse proxy, restrict inbound access to the proxy, and configure logs/backup retention at the host level.

## Backup and restore

Back up `database/tracker.db` at least daily while the app is stopped or using SQLite's online backup API. Keep encrypted copies in separate storage and test restoration monthly:

```powershell
Copy-Item database\tracker.db backups\tracker-$(Get-Date -Format yyyyMMdd-HHmm).db
```

Restore by stopping the app, replacing the database file with a verified backup, and starting the app so migrations can run. Do not store backups in the public web root.
