"""Static security checks over ``app/`` and ``scripts/`` (source only; nothing is imported or run).

Each check parses the Python files and fails with ``path:line`` for every offending place. A justified
exception is marked in the source with a comment on the same or the previous line (``float-ok`` for a
display serializer that turns a Decimal into a JSON number).
"""

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("app", "scripts")
MONEY_DIRS = (ROOT / "app" / "services", ROOT / "app" / "schemas")
MONEY_NAME = re.compile(r"price|quantity|amount|total|cost", re.IGNORECASE)

SECRET_LITERALS = [
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"sk_live_[A-Za-z0-9]+"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)\b\w*(?:key|secret|password)\w*\s*[:=]\s*[\"'][A-Za-z0-9+/_-]{32,}={0,2}[\"']"),
]


def python_files(*roots: str) -> Iterator[Path]:
    for root in roots:
        yield from sorted((ROOT / root).rglob("*.py"))


def parsed(path: Path) -> tuple[ast.Module, list[str]]:
    source = path.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(path)), source.splitlines()


def where(path: Path, node: ast.AST) -> str:
    return f"{path.relative_to(ROOT).as_posix()}:{getattr(node, 'lineno', 0)}"


def call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        prefix = func.value.id + "." if isinstance(func.value, ast.Name) else ""
        return prefix + func.attr
    return ""


def imported_modules(tree: ast.Module) -> Iterator[tuple[ast.AST, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node, node.module


def is_formatted_string(node: ast.AST) -> bool:
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return isinstance(node.left, ast.Constant | ast.JoinedStr) and isinstance(getattr(node.left, "value", ""), str)
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format"


def test_no_formatted_sql_inside_text_calls() -> None:
    problems = []
    for path in python_files(*SCANNED):
        tree, _ = parsed(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and call_name(node).split(".")[-1] == "text" and node.args:
                if any(is_formatted_string(inner) for inner in ast.walk(node.args[0])):
                    problems.append(where(path, node))
    assert problems == [], "f-string, .format() or % formatting inside sqlalchemy.text(): " + ", ".join(problems)


def test_no_dangerous_calls_or_modules() -> None:
    problems = []
    banned_modules = {"pickle", "subprocess", "smtplib", "sklearn", "xgboost", "shap", "cPickle", "dill"}
    for path in python_files(*SCANNED):
        tree, _ = parsed(path)
        for node, module in imported_modules(tree):
            if module.split(".")[0] in banned_modules:
                problems.append(f"{where(path, node)} imports {module}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = call_name(node)
                if name in {"eval", "exec", "os.system", "ssl._create_unverified_context"}:
                    problems.append(f"{where(path, node)} calls {name}")
                if name == "yaml.load" and not any(
                    keyword.arg == "Loader" and "Safe" in ast.unparse(keyword.value) for keyword in node.keywords
                ):
                    problems.append(f"{where(path, node)} calls yaml.load without a safe loader")
                for keyword in node.keywords:
                    value = keyword.value
                    if keyword.arg == "verify" and isinstance(value, ast.Constant) and value.value is False:
                        problems.append(f"{where(path, node)} disables TLS verification")
            if isinstance(node, ast.Attribute) and node.attr in {"_create_unverified_context", "system"}:
                if isinstance(node.value, ast.Name) and node.value.id in {"ssl", "os"}:
                    problems.append(f"{where(path, node)} uses {node.value.id}.{node.attr}")
    assert problems == [], "\n".join(problems)


def test_no_print_in_app() -> None:
    problems = []
    for path in python_files("app"):
        tree, _ = parsed(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                problems.append(where(path, node))
    assert problems == [], "print() in app/ (use logging): " + ", ".join(problems)


def _annotation_mentions_float(annotation: ast.AST | None) -> bool:
    return annotation is not None and any(
        isinstance(node, ast.Name) and node.id == "float" for node in ast.walk(annotation)
    )


def test_money_is_never_float_in_services_and_schemas() -> None:
    problems = []
    for directory in MONEY_DIRS:
        for path in sorted(directory.rglob("*.py")):
            tree, lines = parsed(path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float":
                    context = " ".join(lines[max(0, node.lineno - 2) : node.lineno])
                    if "float-ok" not in context:
                        problems.append(f"{where(path, node)} float() without a float-ok justification")
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    if MONEY_NAME.search(node.target.id) and _annotation_mentions_float(node.annotation):
                        problems.append(f"{where(path, node)} money field {node.target.id} annotated as float")
                elif isinstance(node, ast.arg):
                    if MONEY_NAME.search(node.arg) and _annotation_mentions_float(node.annotation):
                        problems.append(f"{where(path, node)} money argument {node.arg} annotated as float")
                elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    if MONEY_NAME.search(node.name) and _annotation_mentions_float(node.returns):
                        problems.append(f"{where(path, node)} money function {node.name} returns float")
    assert problems == [], "\n".join(problems)


def test_no_secret_looking_literals() -> None:
    problems = []
    for path in python_files(*SCANNED):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if any(pattern.search(line) for pattern in SECRET_LITERALS):
                problems.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
    assert problems == [], "secret-looking literal (values are not shown): " + ", ".join(problems)


def test_no_production_daraja_url_or_option() -> None:
    problems = []
    for path in python_files(*SCANNED):
        if "api.safaricom.co.ke" in path.read_text(encoding="utf-8"):
            problems.append(f"{path.relative_to(ROOT).as_posix()} mentions api.safaricom.co.ke")
    tree, _ = parsed(ROOT / "app" / "integrations" / "daraja.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.lower() == "production":
            problems.append(f"{where(ROOT / 'app' / 'integrations' / 'daraja.py', node)} has a production option")
    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize(
    "overrides",
    [{"mpesa_env": "production"}, {"mpesa_base_url": "https://api.safaricom.co.ke"}],
)
def test_settings_refuse_production_daraja(overrides) -> None:
    values = {"_env_file": None, "jwt_secret": "test-only-jwt-secret-at-least-thirty-two-characters"}
    with pytest.raises(ValidationError):
        Settings(**{**values, **overrides})


def test_gitignore_covers_secrets_and_local_artifacts() -> None:
    entries = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    for required in (".env", ".dev_mailbox/", "artifacts/"):
        assert required in entries, f".gitignore must contain {required}"
