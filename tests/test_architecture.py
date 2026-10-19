import ast
from pathlib import Path


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_domain_does_not_import_application_or_infrastructure():
    for path in Path("app/domain").glob("*.py"):
        modules = _imports(path)
        assert not any(
            name.startswith("app.application") or name.startswith("app.infrastructure")
            for name in modules
        ), path


def test_application_does_not_import_infrastructure():
    for path in Path("app/application").glob("*.py"):
        modules = _imports(path)
        assert not any(name.startswith("app.infrastructure") for name in modules), path


def test_analytics_domain_does_not_import_application_or_infrastructure():
    for path in Path("analytics/domain").rglob("*.py"):
        modules = _imports(path)
        assert not any(
            name.startswith("analytics.application") or name.startswith("analytics.infrastructure")
            for name in modules
        ), path


def test_analytics_application_does_not_import_infrastructure():
    for path in Path("analytics/application").rglob("*.py"):
        modules = _imports(path)
        assert not any(name.startswith("analytics.infrastructure") for name in modules), path


def test_shared_package_does_not_import_app_or_analytics():
    for path in Path("packages/navtracker_common").rglob("*.py"):
        modules = _imports(path)
        assert not any(
            name.startswith("app.") or name.startswith("analytics.") or name in {"app", "analytics"}
            for name in modules
        ), path
