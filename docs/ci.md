# Continuous integration

The backend's CI pipeline is defined in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) and runs on GitHub Actions on every push to `main` and on every pull request. It has two jobs that run in parallel.

## `test` (required)

1. Checks out the repository and sets up Python 3.12. The pip cache is keyed on `requirements.txt` and `requirements-dev.txt`.
2. Installs `requirements-dev.txt` (the runtime requirements plus pytest and ruff).
3. Runs `ruff check .`, which checks style, unused imports, import order and line length (120).
4. Runs `pytest -q -m "not db"`, the full unit and API suite without the database tests.

The suite never connects to a database or the network. The job sets only a few obviously fake environment variables so the settings can load: a placeholder `JWT_SECRET`, `EMAIL_PROVIDER=console`, the sandbox M-Pesa environment and URL, and an empty `DATABASE_URL`.

If ruff or any test fails, the job fails and the pull request shows a failed check. Besides the module tests, the suite fails when:

- a route is missing from `tests/route_matrix.py`, or a role outside a route's allowed set is not refused with 401 or 403;
- a route has no single `### METHOD /api/v1/path` heading in `docs/api.md`;
- `docs/openapi.json` differs from the application. Regenerate it with `python scripts/export_openapi.py`; the test skips while the file does not exist;
- the source scan in `tests/test_source_security.py` finds an unsafe pattern, such as formatted SQL in `text()`, `eval`, `pickle`, `subprocess`, disabled TLS verification, `print` in `app/`, float money, a secret-looking literal or a production Daraja URL.

## `audit` (advisory)

Installs `pip-audit` and runs `pip-audit -r requirements.txt` to check the runtime dependencies against known vulnerability databases. The job has `continue-on-error: true`: a finding is visible in the run but never fails the pipeline. Review the findings and upgrade the affected pins in `requirements.txt`.

## What CI does not do

- **No secrets.** The workflow has no `secrets.*` references, and every environment value in it is a fake placeholder. Keep it that way.
- **No deployment.** There is no deploy job; deployments follow [deployment.md](deployment.md).
- **No database tests.** Tests marked `db` need a real migrated database. Run them locally with `RUN_DB_TESTS=1` (see [testing.md](testing.md)).
- **Read-only access.** `permissions: contents: read` limits the workflow's token to reading the repository.

## Running the same checks locally

```bash
ruff check .
python -m pytest -q -m "not db"
pip-audit -r requirements.txt   # optional; install pip-audit first
```
