"""Versioned JSON Schema catalog for exercise coord cards.

Several Draft 2020-12 schemas can sit side by side. A card's
``schema_version`` selects one of them. When the card omits the field,
the catalog's ``default_version`` is used. Unknown properties are
allowed by the bundled schema so a new field or template does not
require a schema change.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

_BUNDLED_PATH = Path(__file__).resolve().parent / "schemas" / "coord_card.schema.json"
_BUNDLED: "SchemaCatalog | None" = None


class SchemaCatalogError(ValueError):
    """The schema text cannot replace the catalog already loaded."""


@dataclass(frozen=True)
class SchemaCatalog:
    """Schemas keyed by the version a card declares."""

    default_version: str
    versions: Mapping[str, Mapping[str, object]]

    def version_ids(self) -> tuple[str, ...]:
        return tuple(self.versions)


@dataclass(frozen=True)
class SchemaUpload:
    """One upload. ``replaces`` is a whole catalog; otherwise merge one version."""

    catalog: SchemaCatalog
    replaces: bool


def bundled_catalog() -> SchemaCatalog:
    """The schema shipped with the process. Used when no file is configured."""

    global _BUNDLED
    if _BUNDLED is None:
        _BUNDLED = catalog_from_text(_BUNDLED_PATH.read_text(encoding="utf-8"))
    return _BUNDLED


def catalog_from_text(text: str) -> SchemaCatalog:
    """Read a catalog document. Each version must be a Draft 2020-12 schema."""

    return _catalog_from_object(_json_object(text))


def interpret_schema_upload(text: str, *, version_hint: str = "") -> SchemaUpload:
    """A ``versions`` object replaces the catalog. One schema is merged."""

    payload = _json_object(text)
    if "versions" in payload or "default_version" in payload:
        return SchemaUpload(catalog=_catalog_from_object(payload), replaces=True)
    _ensure_schema(payload)
    version = version_hint.strip() or _const_version(payload)
    if not version:
        raise SchemaCatalogError(
            "Schema upload needs X-Schema-Version or properties.schema_version.const."
        )
    return SchemaUpload(
        catalog=SchemaCatalog(default_version=version, versions={version: payload}),
        replaces=False,
    )


def merge_catalog(current: SchemaCatalog, addition: SchemaCatalog) -> SchemaCatalog:
    """Keep every current version and overlay the uploaded ones."""

    versions: dict[str, Mapping[str, object]] = dict(current.versions)
    versions.update(addition.versions)
    default = current.default_version
    if default not in versions:
        default = addition.default_version
    return SchemaCatalog(default_version=default, versions=versions)


def normalize_card_document(document: Mapping[str, object]) -> dict[str, object]:
    """Copy a card and store ``schema_version`` as text when it is present."""

    copied = dict(document)
    if "schema_version" not in copied:
        return copied
    raw = copied.get("schema_version")
    if raw is None or raw == "":
        copied.pop("schema_version", None)
        return copied
    copied["schema_version"] = version_text(raw)
    return copied


def version_text(value: object) -> str:
    """The version id a card or upload names."""

    if isinstance(value, bool):
        raise SchemaCatalogError("schema_version must be a string.")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise SchemaCatalogError("schema_version must be a string.")


def validate_card_document(
    document: Mapping[str, object],
    catalog: SchemaCatalog,
    *,
    remembered: str = "",
) -> str:
    """Check one card mapping. Returns the version that was selected."""

    declared = ""
    if "schema_version" in document and document.get("schema_version") not in (None, ""):
        declared = version_text(document.get("schema_version"))
    version = declared or remembered or catalog.default_version
    schema = catalog.versions.get(version)
    if schema is None:
        known = ", ".join(catalog.versions) or "(none)"
        raise SchemaCatalogError(
            f"Unknown coord card schema_version {version!r}. Known versions: {known}."
        )
    try:
        Draft202012Validator(dict(schema)).validate(dict(document))
    except ValidationError as exc:
        path = "/".join(str(part) for part in exc.absolute_path)
        detail = exc.message
        if path:
            detail = f"{path}: {detail}"
        raise SchemaCatalogError(
            f"Coord card does not match schema {version}: {detail}"
        ) from exc
    return version


def dump_catalog(catalog: SchemaCatalog) -> str:
    """Pretty JSON for the schema file. Versions stay side by side."""

    payload = {
        "default_version": catalog.default_version,
        "versions": {key: dict(value) for key, value in catalog.versions.items()},
    }
    return json.dumps(payload, indent=2) + "\n"


def _catalog_from_object(payload: Mapping[str, object]) -> SchemaCatalog:
    versions = payload.get("versions")
    if not isinstance(versions, dict) or not versions:
        raise SchemaCatalogError("Schema catalog needs a versions object.")
    checked: dict[str, Mapping[str, object]] = {}
    for key, value in versions.items():
        name = version_text(key)
        if not isinstance(value, dict):
            raise SchemaCatalogError(f"Schema {name} must be a JSON Schema object.")
        _ensure_schema(value)
        checked[name] = value
    raw_default = payload.get("default_version")
    if raw_default is None or raw_default == "":
        if len(checked) != 1:
            raise SchemaCatalogError("Schema catalog needs default_version.")
        default = next(iter(checked))
    else:
        default = version_text(raw_default)
    if default not in checked:
        raise SchemaCatalogError(
            f"default_version {default!r} is not in versions."
        )
    return SchemaCatalog(default_version=default, versions=checked)


def _ensure_schema(schema: Mapping[str, object]) -> None:
    try:
        Draft202012Validator.check_schema(dict(schema))
    except SchemaError as exc:
        raise SchemaCatalogError(
            f"Schema is not a valid JSON Schema (Draft 2020-12): {exc.message}"
        ) from exc


def _const_version(schema: Mapping[str, object]) -> str:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return ""
    field = properties.get("schema_version")
    if not isinstance(field, dict):
        return ""
    const = field.get("const")
    if isinstance(const, (str, int)) and not isinstance(const, bool):
        return version_text(const)
    return ""


def _json_object(text: str) -> dict[str, object]:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaCatalogError("Schema is not valid JSON.") from exc
    if not isinstance(loaded, dict):
        raise SchemaCatalogError("Schema must be a JSON object.")
    return loaded
