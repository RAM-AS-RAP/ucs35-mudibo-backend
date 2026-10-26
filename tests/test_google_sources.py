"""Google sign in: response schema checks and static source checks (nothing is run or fetched).

The OpenAPI walk covers every component schema reachable from a response. The source checks parse the
Google files and fail on a disabled signature check, an algorithm list other than RS256, any URL other
than the fixed JWKS address, the ``random`` module, ``print`` and a log call that mentions the
credential, a nonce, a ticket or the Google subject.
"""

import ast
import re
from pathlib import Path

import pytest

from app.integrations import google
from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
GOOGLE_INTEGRATION = APP_DIR / "integrations" / "google.py"
GOOGLE_FILES = [
    GOOGLE_INTEGRATION,
    APP_DIR / "services" / "google_auth_service.py",
    APP_DIR / "routers" / "auth.py",
    APP_DIR / "models" / "identity.py",
]
# Properties that carry a one time secret, and the only response schemas allowed to carry each.
SECRET_RESPONSE_PROPERTIES = {
    "credential": set(),
    "nonce": {"GoogleNonceOut"},
    "signup_token": {"GoogleAuthResult"},
}
NEVER_IN_A_RESPONSE = {"google_subject", "sub", "subject", "google_sub"}
SECRET_LOG_NAMES = {"credential", "nonce", "signup_token", "token", "claims", "header", "subject", "google_sub"}


def openapi() -> dict:
    return create_app().openapi()


def referenced(schema: object) -> set[str]:
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


def response_schema_names(document: dict) -> set[str]:
    components = document["components"]["schemas"]
    pending: set[str] = set()
    for operations in document["paths"].values():
        for operation in operations.values():
            pending |= referenced(operation.get("responses", {}))
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name not in seen:
            seen.add(name)
            pending |= referenced(components.get(name, {}))
    return seen


# Schemas ---------------------------------------------------------------------------------------


def test_one_time_secrets_appear_only_in_their_own_response_schemas():
    document = openapi()
    components = document["components"]["schemas"]
    names = response_schema_names(document)
    assert {"GoogleNonceOut", "GoogleAuthResult", "UserOut"} <= names
    offenders = []
    for name in names:
        properties = components.get(name, {}).get("properties", {})
        for prop, allowed in SECRET_RESPONSE_PROPERTIES.items():
            if prop in properties and name not in allowed:
                offenders.append(f"{name}.{prop}")
        offenders.extend(f"{name}.{prop}" for prop in properties if prop in NEVER_IN_A_RESPONSE)
    assert offenders == []


def test_no_schema_has_a_password_hash_or_a_google_subject():
    for name, schema in openapi()["components"]["schemas"].items():
        properties = set(schema.get("properties", {}))
        assert "password_hash" not in properties, name
        assert not properties & {"google_subject", "google_sub"}, name


def test_google_request_and_response_shapes():
    components = openapi()["components"]["schemas"]
    assert set(components["GoogleSignInRequest"]["properties"]) == {"credential", "nonce"}
    assert components["GoogleSignInRequest"].get("additionalProperties") is False
    signup = components["GoogleSignupRequest"]
    assert set(signup["properties"]) == {"signup_token", "role"}
    assert signup.get("additionalProperties") is False
    assert sorted(signup["properties"]["role"]["enum"]) == ["buyer", "farmer"]
    assert set(components["GoogleNonceOut"]["properties"]) == {"nonce", "expires_in"}
    assert set(components["GoogleAuthResult"]["properties"]["status"]["enum"]) == {"signed_in", "role_required"}
    assert {"password_set", "google_linked"} <= set(components["UserOut"]["properties"])


def test_the_google_routes_document_their_responses():
    paths = openapi()["paths"]
    assert "/api/v1/auth/google/nonce" in paths
    assert "201" in paths["/api/v1/auth/google/complete"]["post"]["responses"]
    assert "200" in paths["/api/v1/auth/google"]["post"]["responses"]


# Sources ---------------------------------------------------------------------------------------


def parsed(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_no_signature_check_is_ever_disabled():
    problems = []
    for path in sorted(APP_DIR.rglob("*.py")):
        for node in ast.walk(parsed(path)):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if (
                        isinstance(key, ast.Constant)
                        and isinstance(key.value, str)
                        and key.value.startswith("verify_")
                        and isinstance(value, ast.Constant)
                        and value.value is False
                    ):
                        problems.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    assert problems == [], "a JWT check is disabled: " + ", ".join(problems)


def test_the_google_code_allows_only_rs256():
    assert google.ALLOWED_ALGORITHM == "RS256"
    tree = parsed(GOOGLE_INTEGRATION)
    algorithm_lists = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "algorithms":
            algorithm_lists.append(ast.unparse(node.value))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.strip().lower()
            assert value != "none" and not re.fullmatch(r"hs\d+", value), node.value
    assert algorithm_lists == ["[ALLOWED_ALGORITHM]"]


def test_the_verifier_fetches_only_the_fixed_jwks_url():
    assert google.GOOGLE_JWKS_URL == "https://www.googleapis.com/oauth2/v3/certs"
    tree = parsed(GOOGLE_INTEGRATION)
    urls = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "://" in node.value
    }
    assert urls == {google.GOOGLE_JWKS_URL, *google.GOOGLE_ISSUERS} - {"accounts.google.com"}
    requests = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "post", "put", "request", "stream", "send"}
            and ast.unparse(node.func.value) in {"self._client", "client", "httpx"}
        ):
            requests.append(node)
    assert requests, "the JWKS download was not found"
    for call in requests:
        targets = [ast.unparse(argument) for argument in call.args]
        assert "GOOGLE_JWKS_URL" in targets, ast.unparse(call)
        assert any(
            keyword.arg == "follow_redirects" and ast.unparse(keyword.value) == "False" for keyword in call.keywords
        ), "redirects must not be followed"
    # The application never configures another key URL.
    for path in sorted(APP_DIR.rglob("*.py")):
        if path != GOOGLE_INTEGRATION:
            assert "googleapis.com" not in path.read_text(encoding="utf-8"), path


@pytest.mark.parametrize("path", GOOGLE_FILES, ids=lambda path: path.name)
def test_google_code_uses_secrets_not_random_and_never_prints(path):
    for node in ast.walk(parsed(path)):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] != "random" for alias in node.names), path
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "random", path
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "print", path


@pytest.mark.parametrize("path", GOOGLE_FILES, ids=lambda path: path.name)
def test_google_code_never_logs_a_credential_nonce_ticket_or_subject(path):
    for node in ast.walk(parsed(path)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
        ):
            continue
        source = ast.unparse(node)
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            for inner in ast.walk(argument):
                if isinstance(inner, ast.Name):
                    assert inner.id not in SECRET_LOG_NAMES, f"{path.name}:{node.lineno} logs {inner.id}"
                if isinstance(inner, ast.Attribute):
                    assert inner.attr not in SECRET_LOG_NAMES, f"{path.name}:{node.lineno} logs .{inner.attr}"
                    if inner.attr == "email":
                        assert "mask_email" in source, f"{path.name}:{node.lineno} logs an unmasked email"


def test_the_migration_is_additive_and_has_no_em_dashes():
    migration = (ROOT / "supabase" / "migrations" / "20261027000100_google_sign_in.sql").read_text(encoding="utf-8")
    assert "—" not in migration
    for fragment in (
        "alter table public.users add column password_set boolean not null default true",
        "create table public.user_identities",
        "create table public.oauth_nonces",
        "create table public.google_signups",
        "google_signups_one_open_subject_idx",
        "enable row level security",
        "cleanup-google-auth",
        "'30 2 * * *'",
        "set search_path = public",
    ):
        assert fragment in migration, fragment
    statements = migration.lower()
    for forbidden in ("drop table", "drop column", "delete from public.users", "delete from public.user_identities"):
        assert forbidden not in statements, forbidden
    verification = (ROOT / "supabase" / "verification" / "google_check.sql").read_text(encoding="utf-8")
    assert verification.lstrip().splitlines()[0].startswith("--")
    assert "begin;" in verification and verification.rstrip().endswith("rollback;")
    assert "select * from check_results order by name;" in verification
