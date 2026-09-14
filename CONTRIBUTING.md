# Contributing

Thank you for your interest in contributing to Routine Tracker. The project is actively developed, and focused improvements, bug fixes, documentation updates, and tests are welcome.

## Before contributing

- Read the [README](README.md) and [SECURITY.md](SECURITY.md).
- Check existing GitHub issues and discussions before opening a duplicate.
- Keep changes focused on the problem being solved.
- Avoid unrelated refactoring or broad rewrites.
- Never include secrets, personal data, local databases, generated artifacts, or machine-specific configuration.
- Report security vulnerabilities according to [SECURITY.md](SECURITY.md), not through a normal public issue.

## Development setup

The current development environment uses Python 3.14. Other Python versions may work, but the project does not currently declare a formal Python-version support policy.

Create and activate a virtual environment:

### Windows PowerShell

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### Linux/macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install the project dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

Copy [.env.example](.env.example) to `.env` and configure the variables needed for your environment. Do not commit `.env`.

Start the personal shared-profile server with:

```bash
.venv/bin/python run.py
```

The launcher initializes the SQLite database and asks for a port, defaulting to 5000. On Windows, use `.venv\Scripts\python.exe run.py`. For corporate account mode, use:

```bash
.venv/bin/python run_corporate.py
```

## Running tests

Run the complete test suite with:

```bash
python -m pytest -q
```

Run relevant focused tests while developing, then run the full suite before submitting a change. The current baseline is 43 passing tests, but this is not a permanent contribution requirement.

Tests use isolated temporary SQLite databases where appropriate. Do not make tests depend on a running local application or a persistent personal database.

## Making changes

- Make small, focused changes.
- Preserve existing behavior unless the change intentionally changes it.
- Keep security checks and ownership checks intact.
- Use validation appropriate to the input and endpoint.
- Use parameterized SQL for database operations.
- Do not modify production authentication or security behavior merely to make tests pass.
- Add or update appropriate tests when behavior changes.
- Keep the existing progress architecture consistent:

  ```text
  Tasks -> Daily -> Weekly -> Monthly
  ```

  Daily progress is the source of truth. Weekly and Monthly progress aggregate Daily results.

## Database changes

Database schema changes must use the existing versioned migration system and its `schema_migrations` records/checksums.

- Test migrations against fresh and appropriate existing databases.
- Keep migrations backward-safe where possible.
- Avoid destructive data changes.
- Back up existing databases before applying migrations that alter data or schema.
- Never commit SQLite database files or runtime `-wal`/`-shm` files.

## Pull requests

When submitting a pull request:

- Explain what changed.
- Explain why the change was needed.
- List the tests you ran and their results.
- Keep the pull request focused.
- Include screenshots when they help explain a UI change.
- Clearly call out database or schema changes.
- Clearly call out security-sensitive changes.

There is no required pull-request template or additional workflow at this time.

## Code and style

Follow the existing code structure and conventions. Keep code readable and changes understandable. Prefer small functions and targeted edits where practical.

For database code, use parameterized queries and validate user input. Preserve the existing security protections, CSRF handling, ownership checks, and migration validation.

The repository does not currently require a separate formatter, linter, or type checker. Use the existing test suite and the project's established patterns when validating changes.

## UI changes

For frontend changes:

- Preserve responsive behavior.
- Avoid unnecessary redesigns.
- Test the affected views and interactions.
- Verify navigation and state updates.
- Keep Calendar and progress displays consistent with the Daily/Weekly/Monthly model.

## Commit messages

Use clear, descriptive commit messages that explain the change. No specific commit-message convention is currently required.

## What not to submit

Do not submit:

- `.env` files.
- Passwords, API keys, tokens, or other credentials.
- SQLite databases containing user data.
- SQLite `-wal` or `-shm` runtime files.
- Virtual environments.
- Build or `dist` output.
- Test and tool caches.
- Personal configuration.
- Generated artifacts.

## License

Contributions are expected to be compatible with the project's planned GNU AGPL-3.0 licensing. See [LICENSE](LICENSE).

No Contributor License Agreement or additional legal requirement has been established.

## Questions and discussion

Once the repository is public, use normal GitHub issues or discussions for non-security questions and project discussion.

For security vulnerabilities, follow [SECURITY.md](SECURITY.md) and use GitHub's private vulnerability reporting mechanism.
