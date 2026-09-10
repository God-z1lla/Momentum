# Security

Security issues are taken seriously. Responsible disclosure is preferred so that issues can be investigated and addressed before they are made public.

## Supported versions

Routine Tracker is under active development and is preparing for its first public release. Security fixes will be applied to the maintained public version when appropriate. No formal version support policy has been established yet.

## Reporting a vulnerability

Please do **not** publicly post an undisclosed vulnerability in a GitHub issue, discussion, pull request, or any other public channel.

Before publication, the repository maintainer should enable GitHub's private vulnerability reporting mechanism and document it here. Until then, do not disclose an undisclosed vulnerability publicly.

## What to include

Where possible, include:

- A clear description of the vulnerability.
- The affected component or endpoint.
- Steps to reproduce the issue.
- Expected and actual behavior.
- The likely security impact.
- The affected version or commit, if known.
- Relevant logs or error messages with secrets removed.

Do not send passwords, API keys, session cookies, private tokens, or other secrets in a report.

## Responsible disclosure

Please allow reasonable time for investigation and remediation before publicly disclosing the issue. No specific response or remediation timeframe has been established.

## Security practices

The current application includes security measures such as:

- Password hashing using salted PBKDF2-SHA256.
- CSRF checks for state-changing requests in accounts mode.
- Ownership checks for user-owned resources.
- Parameterized SQLite queries in application database operations.
- A production requirement for `ROUTINE_TRACKER_SECRET` to be at least 32 characters.
- SQLite foreign-key enforcement for web application connections.
- Versioned database migrations recorded with checksums.

These measures do not guarantee complete security. In production:

- Configure `ROUTINE_TRACKER_SECRET` with a strong, unique secret.
- Never commit `.env` files, database files, backups, or credentials.
- Use `ROUTINE_TRACKER_AUTH=accounts` for internet-facing or multi-user deployments that require per-user isolation.
- The default shared profile is intended for a self-hosted instance trusted by its users.

## Security limitations / deployment notes

This project is under active development. Self-hosters are responsible for securing their deployment environment, network, host, backups, secrets, and external integrations.

The Flask development server should not be treated as a production internet-facing server. Optional SMTP, Stripe, and Web Push integrations require appropriate secret and configuration handling.

## Scope

Vulnerabilities in the Routine Tracker application source, including its server routes, database access, templates, and frontend assets, are in scope.

No bug bounty program or compensation policy is offered.

## Contact

The private reporting contact/method will be confirmed here before the public repository is published.
