# Repository Guidelines

## Project Structure & Module Organization

This is a Python service for analyzing Moroccan public procurement results. `main.py` defines the local FastAPI web app and HTML UI. Core logic lives in `scraper.py` for consultation parsing, `calculator.py` for bidder ranking, and `company_city.py` for company city lookup. `database.py` owns PostgreSQL access, quotas, and notification watches. Vercel serverless entry points are in `api/webhook.py` for Telegram updates and `api/check_notifications.py` for scheduled checks. Deployment routing is configured in `vercel.json`.

## Build, Test, and Development Commands

- `./run.sh`: creates `.venv` with `uv`, installs `requirements.txt` if needed, and starts `uvicorn main:app --reload` on port 8000.
- `.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --reload`: run the app directly after dependency install.
- `uv pip install -r requirements.txt`: install pinned runtime dependencies into the active environment.

No build step is required; Vercel uses `@vercel/python` for the files under `api/`.

## Coding Style & Naming Conventions

Use Python 3 style with 4-space indentation, dataclasses for structured records, and type hints on public helpers where practical. Keep constants in `UPPER_SNAKE_CASE`, functions and variables in `snake_case`, and classes/dataclasses in `PascalCase`. Follow existing patterns: parser helpers prefixed with `_`, explicit `Optional[...]` for nullable scraped values, and concise comments only around non-obvious procurement or scraping rules.

## Testing Guidelines

There is currently no committed test suite. When adding tests, use `pytest` and place them under `tests/`, mirroring module names such as `tests/test_calculator.py` and `tests/test_scraper.py`. Prioritize deterministic unit tests for `calculate_winners`, price parsing, lot handling, and database URL cleaning. Avoid live network tests by using saved HTML fixtures or mocked `httpx` responses. Run tests with `pytest` once the test dependency is added.

## Commit & Pull Request Guidelines

Recent commits use short, imperative, uppercase summaries, for example `ADD MENU` and `UPGRADE NOTIFICATIONS`. Keep commit titles concise and focused on one change. Pull requests should include a short description, affected entry points (`main.py`, `api/webhook.py`, etc.), manual test steps or command output, and screenshots when UI or Telegram message formatting changes. Link related issues or deployment notes when changing Vercel routes, cron behavior, or database schema.

## Security & Configuration Tips

Do not commit secrets. Local database settings may be loaded from `.env.local`; supported database variables include `DATABASE_URL`, `POSTGRES_URL`, or `SUPABASE_DB_URL`. Telegram and cron features require `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_ID`, optional `TELEGRAM_ADMIN_USERNAME`, and `CRON_SECRET`. Treat scraped external pages as untrusted input and escape text before sending HTML-formatted Telegram messages.
