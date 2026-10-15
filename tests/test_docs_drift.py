"""``docs/api.md`` documents every route of the matrix exactly once, and nothing else.

Route headings use the form ``### METHOD /api/v1/path``. Backticks are ignored, a path without the
``/api/v1`` prefix gets it, and path parameter names are not compared (``{id}`` matches ``{listing_id}``).
"""

import re
from collections import Counter
from pathlib import Path

from route_matrix import ROUTE_MATRIX

API_DOC = Path(__file__).resolve().parents[1] / "docs" / "api.md"
_HEADING = re.compile(r"^###\s+`?(GET|POST|PATCH|PUT|DELETE)\s+(\S+?)`?\s*$")
_PARAM = re.compile(r"\{[^}]*\}")
_UNPREFIXED = ("/docs", "/redoc", "/openapi.json")


def normalise(method: str, path: str) -> tuple[str, str]:
    path = path.strip("`")
    if not path.startswith("/api/v1") and not path.startswith(_UNPREFIXED):
        path = "/api/v1" + path
    return method.upper(), _PARAM.sub("{}", path)


def documented_routes() -> Counter[tuple[str, str]]:
    found: Counter[tuple[str, str]] = Counter()
    for line in API_DOC.read_text(encoding="utf-8").splitlines():
        match = _HEADING.match(line.strip())
        if match:
            found[normalise(match.group(1), match.group(2))] += 1
    return found


def test_every_matrix_route_is_documented_once() -> None:
    documented = documented_routes()
    for method, path in ROUTE_MATRIX:
        key = normalise(method, path)
        assert documented[key] == 1, f"### {method} {path} appears {documented[key]} times in docs/api.md"


def test_no_documented_route_is_missing_from_the_application() -> None:
    expected = {normalise(method, path) for method, path in ROUTE_MATRIX}
    extra = sorted(set(documented_routes()) - expected)
    assert extra == [], f"docs/api.md documents routes the application does not have: {extra}"
