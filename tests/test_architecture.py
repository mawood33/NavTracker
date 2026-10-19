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


def test_focused_services_are_composed_in_wire_service():
    text = Path("analytics/composition.py").read_text(encoding="utf-8")
    for name in (
        "QueryService",
        "IngestService",
        "RoutingService",
        "CatalogService",
        "PublishService",
        "AnalysisService",
        "AnalysisLoop",
    ):
        assert name in text
    service = Path("analytics/application/service.py").read_text(encoding="utf-8")
    assert "def analyze" in service
    assert "detect_slowdowns" not in service


def test_config_loader_delegates_to_section_modules():
    text = Path("analytics/infrastructure/config.py").read_text(encoding="utf-8")
    for name in (
        "http_settings",
        "source_settings",
        "grid_settings",
        "map_settings",
        "coord_settings",
        "room_settings",
    ):
        assert name in text
    assert "def _thresholds" not in text
    assert "def _map_provider" not in text
    assert "def _typed_rooms" not in text


def test_shared_package_does_not_import_app_or_analytics():
    for path in Path("packages/navtracker_common").rglob("*.py"):
        modules = _imports(path)
        assert not any(
            name.startswith("app.") or name.startswith("analytics.") or name in {"app", "analytics"}
            for name in modules
        ), path
