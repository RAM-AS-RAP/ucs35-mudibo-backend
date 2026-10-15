"""The route matrix lists every route, and callers outside a route's roles get 401 or 403.

Allowed roles are not exercised here; the module tests cover them. Authorization must be decided
before validation and before any database access, so the requests carry an empty JSON body only where
the route takes JSON, random UUIDs in the path, and a session that fails the test if it is touched.
"""

import re
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI, params
from fastapi.routing import APIRoute
from route_matrix import API_ROUTES, DOCS_ROUTES, ROLES, ROUTE_MATRIX

from app.core.dependencies import get_current_user_any
from app.db import get_db, get_session_factory
from app.main import create_app
from app.models.enums import UserRole

_PATH_PARAM = re.compile(r"\{[^}]+\}")


class UntouchableSession:
    """A database session that fails the request (and so the test) if anything uses it."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"authorization check touched the database session ({name})")


async def untouchable_db():
    yield UntouchableSession()


def untouchable_session_factory():
    def factory() -> object:
        raise AssertionError("authorization check opened a database session")

    return factory


def fake_user(role: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        name=f"Test {role}",
        email=f"{role}@example.com",
        phone_number="254700000000",
        role=UserRole.ADMIN if role in ("admin", "super_admin") else UserRole(role),
        is_super_admin=role == "super_admin",
        must_change_password=False,
        is_disabled=False,
        password_changed_at=None,
    )


def flat_routes(routes: list[Any]) -> Iterator[tuple[str, Any]]:
    """Yield ``(full path, route)`` for every route, expanding included routers.

    FastAPI 0.143 keeps ``include_router`` results as lazy ``_IncludedRouter`` entries in ``app.routes``;
    their ``effective_candidates()`` give the prefixed routes (each wrapping the original ``APIRoute``).
    """
    for route in routes:
        if hasattr(route, "effective_candidates"):
            yield from flat_routes(route.effective_candidates())
        else:
            yield route.path, getattr(route, "original_route", route)


def registered_routes(application: FastAPI) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path, route in flat_routes(application.routes):
        for method in getattr(route, "methods", None) or ():
            if method != "HEAD":  # Starlette adds HEAD to plain GET routes such as the docs pages
                found.add((method, path))
    return found


def expected_routes(application: FastAPI) -> set[tuple[str, str]]:
    expected = set(API_ROUTES)
    if application.docs_url is not None:
        expected |= set(DOCS_ROUTES)
    return expected


def test_every_route_is_in_the_matrix_and_every_entry_exists() -> None:
    application = create_app()
    registered = registered_routes(application)
    expected = expected_routes(application)
    assert sorted(registered - expected) == [], "routes missing from tests/route_matrix.py"
    assert sorted(expected - registered) == [], "matrix entries that are not routes of the application"


def test_matrix_values_are_public_or_known_roles() -> None:
    for key, access in ROUTE_MATRIX.items():
        assert access == "public" or (isinstance(access, frozenset) and access and access <= set(ROLES)), key


def _api_route(application: FastAPI, method: str, path: str) -> APIRoute:
    for full_path, route in flat_routes(application.routes):
        if isinstance(route, APIRoute) and full_path == path and method in route.methods:
            return route
    raise AssertionError(f"no route {method} {path}")


def _request_kwargs(route: APIRoute) -> dict[str, object]:
    body_params = route.dependant.body_params
    if body_params and not any(isinstance(param.field_info, params.Form) for param in body_params):
        return {"json": {}}
    return {}


def _cases() -> list[tuple[str, str, str]]:
    cases = []
    for (method, path), access in sorted(API_ROUTES.items()):
        if access == "public":
            continue
        cases.append((method, path, "anonymous"))
        cases.extend((method, path, role) for role in ROLES if role not in access)
    return cases


@pytest.mark.parametrize(("method", "path", "caller"), _cases())
async def test_disallowed_callers_are_refused_before_validation(api_client, application, method, path, caller):
    application.dependency_overrides[get_db] = untouchable_db
    application.dependency_overrides[get_session_factory] = untouchable_session_factory
    if caller != "anonymous":
        user = fake_user(caller)
        application.dependency_overrides[get_current_user_any] = lambda: user
    route = _api_route(application, method, path)
    url = _PATH_PARAM.sub(lambda _: str(uuid4()), path)
    headers = {} if caller == "anonymous" else {"Authorization": "Bearer not-checked-because-overridden"}
    response = await api_client.request(method, url, headers=headers, **_request_kwargs(route))
    expected = 401 if caller == "anonymous" else 403
    assert response.status_code == expected, (method, path, caller, response.status_code, response.text)
    assert response.json()["error"]["code"] in {"invalid_credentials", "forbidden"}
