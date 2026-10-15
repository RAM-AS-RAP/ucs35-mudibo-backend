"""Write ``docs/openapi.json`` from the application's OpenAPI schema.

Usage (from the repository root): ``python scripts/export_openapi.py`` or ``python -m scripts.export_openapi``.

The output is deterministic: keys are sorted, indentation is two spaces, there is no timestamp, and the
file ends with one newline, so the file only changes when the API changes. ``tests/test_openapi_snapshot.py``
fails when the committed file differs from the application. The frontend generates its TypeScript types
from this file (see ``docs/frontend_contract.md``).
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = ROOT / "docs" / "openapi.json"


def render_openapi(application: Any) -> str:
    """Return the schema as stable JSON text."""
    schema = application.openapi()
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    # Importing the app needs a JWT secret; a placeholder is enough because nothing is signed here.
    if len(os.environ.get("JWT_SECRET", "")) < 32:
        os.environ["JWT_SECRET"] = "openapi-export-placeholder-secret-not-used-for-signing"
    from app.main import create_app

    OPENAPI_PATH.parent.mkdir(parents=True, exist_ok=True)
    OPENAPI_PATH.write_text(render_openapi(create_app()), encoding="utf-8", newline="\n")
    print(f"Wrote {OPENAPI_PATH.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
