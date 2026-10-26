"""Identity v2 schema and source checks: no phone on accounts, no secret in any response schema.

The OpenAPI walk covers every component schema the application publishes; the source checks parse the
auth files without importing or running them.
"""

import ast
import re
from pathlib import Path

import pytest

from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
AUTH_FILES = [
    APP_DIR / "core" / "codes.py",
    APP_DIR / "core" / "auth_result.py",
    APP_DIR / "core" / "password_policy.py",
    APP_DIR / "services" / "auth_service.py",
    APP_DIR / "services" / "registration_service.py",
    APP_DIR / "services" / "invite_service.py",
    APP_DIR / "services" / "password_reset_service.py",
    APP_DIR / "services" / "email_delivery.py",
    APP_DIR / "services" / "google_auth_service.py",
    APP_DIR / "integrations" / "google.py",
    APP_DIR / "routers" / "auth.py",
    ROOT / "scripts" / "admin_recovery.py",
    ROOT / "scripts" / "seed_admin.py",
]
PHONE_FREE_FILES = [
    APP_DIR / "models" / "user.py",
    APP_DIR / "schemas" / "user.py",
    APP_DIR / "schemas" / "auth.py",
    APP_DIR / "schemas" / "invite.py",
    APP_DIR / "services" / "auth_service.py",
    APP_DIR / "services" / "registration_service.py",
    APP_DIR / "core" / "auth_result.py",
]
SECRET_NAMES = {"code", "token", "challenge_token", "registration_token", "password", "new_password", "password_hash"}
SECRET_PROPERTIES = {"password_hash", "code_hash", "token_hash"}
# The only schema that may carry a phone_number property: the number entered when paying.
PHONE_SCHEMA = "PayRequest"


def component_schemas() -> dict:
    return create_app().openapi()["components"]["schemas"]


def referenced(schema: object) -> set[str]:
    """Names of the component schemas referenced anywhere inside ``schema``."""
    found: set[str] = set()
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[1])
        for value in schema.values():
            found |= referenced(value)
    elif isinstance(schema, list):
        for value in schema:
            found |= referenced(value)
    return found


def response_schemas() -> set[str]:
    """Every component schema reachable from a response (directly or through nested references)."""
    document = create_app().openapi()
    components = document["components"]["schemas"]
    pending: set[str] = set()
    for operations in document["paths"].values():
        for operation in operations.values():
            pending |= referenced(operation.get("responses", {}))
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        pending |= referenced(components.get(name, {}))
    return seen


def test_phone_number_appears_only_in_the_pay_request():
    offenders = [
        f"{name}.phone_number"
        for name, schema in component_schemas().items()
        if "phone_number" in schema.get("properties", {}) and name != PHONE_SCHEMA
    ]
    assert offenders == []
    assert "phone_number" in component_schemas()[PHONE_SCHEMA]["properties"]


def test_no_schema_has_a_hash_property():
    offenders = [
        f"{name}.{prop}"
        for name, schema in component_schemas().items()
        for prop in schema.get("properties", {})
        if prop in SECRET_PROPERTIES
    ]
    assert offenders == []


def test_no_response_schema_has_a_code_property():
    components = component_schemas()
    names = response_schemas()
    assert "AuthResult" in names and "RegistrationAccepted" in names
    offenders = [name for name in names if "code" in components.get(name, {}).get("properties", {})]
    assert offenders == []


def test_account_schemas_have_no_phone_and_admin_users_show_email_verification():
    components = component_schemas()
    for name in ("UserOut", "UserAdminOut", "InviteSentOut", "InviteOut", "AuthResult", "CreateInviteRequest"):
        properties = components[name]["properties"]
        assert not any("phone" in prop for prop in properties), name
    assert "email_verified_at" in components["UserAdminOut"]["properties"]
    assert set(components["LoginRequest"]["properties"]) == {"email", "password"}
    assert set(components["RegisterRequest"]["properties"]) == {"name", "email", "password"}


@pytest.mark.parametrize("path", PHONE_FREE_FILES, ids=lambda path: path.name)
def test_phone_does_not_appear_in_account_code(path):
    assert "phone" not in path.read_text(encoding="utf-8").lower(), path


@pytest.mark.parametrize("path", AUTH_FILES, ids=lambda path: path.name)
def test_auth_code_uses_secrets_not_random(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] != "random" for alias in node.names), path
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "random", path


def _logger_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
        ):
            yield node


@pytest.mark.parametrize("path", AUTH_FILES, ids=lambda path: path.name)
def test_auth_code_never_logs_a_code_token_or_password(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for call in _logger_calls(tree):
        for argument in [*call.args, *(keyword.value for keyword in call.keywords)]:
            for node in ast.walk(argument):
                if isinstance(node, ast.Name):
                    assert node.id not in SECRET_NAMES, f"{path.name}:{call.lineno} logs {node.id}"
                if isinstance(node, ast.Attribute):
                    assert node.attr not in SECRET_NAMES, f"{path.name}:{call.lineno} logs .{node.attr}"
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    assert not re.search(r"email=%s", node.value) or "mask_email" in ast.unparse(call), (
                        f"{path.name}:{call.lineno} logs an unmasked email"
                    )


def test_tokens_come_from_secrets_token_urlsafe():
    source = (APP_DIR / "core" / "codes.py").read_text(encoding="utf-8")
    assert "secrets.token_urlsafe(TOKEN_BYTES)" in source and "TOKEN_BYTES = 32" in source
    assert "hashlib.sha256" in source


def test_the_migration_drops_every_phone_object_and_keeps_the_guard_first():
    migration = (ROOT / "supabase" / "migrations" / "20261026000100_identity_v2.sql").read_text(encoding="utf-8")
    statements = [line for line in migration.splitlines() if line.strip() and not line.strip().startswith("--")]
    assert statements[0].strip() == "do $$"
    assert "identity v2 requires an empty users table" in migration
    for fragment in (
        "drop constraint if exists users_phone_number_key",
        "drop constraint if exists users_phone_number_format",
        "drop column phone_number",
        "drop index if exists public.admin_invites_one_pending_phone_idx",
        "create unique index users_email_lower_unique_idx on public.users (lower(email))",
        "email_verified_at timestamptz not null",
        "cleanup-auth-codes",
    ):
        assert fragment in migration, fragment
    assert "—" not in migration  # no em dashes
