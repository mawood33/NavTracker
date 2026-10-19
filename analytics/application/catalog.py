"""Reference files, the exercise coord card, and its schema catalog."""

import logging
from datetime import UTC, datetime
from pathlib import Path

from analytics.application.dispatch import (
    active_publish_table,
    ingest_room_list,
)
from analytics.application.state import AnalyticsState, schema_destination
from analytics.domain.assessment import (
    SiteActivity,
    assess_site,
    merge_catalog,
    stamp_import,
)
from analytics.domain.card_schema import (
    SchemaCatalogError,
    bundled_catalog,
    catalog_from_text,
    dump_catalog,
    interpret_schema_upload,
    validate_card_document,
)
from analytics.domain.card_schema import (
    merge_catalog as merge_schema_catalog,
)
from analytics.domain.coordcard import CoordCard, CoordCardError, read_coord_card
from analytics.domain.outputs import site_fields
from analytics.domain.publish import (
    describe_table,
)
from analytics.domain.reference import (
    Evidence,
    Msr,
    Site,
)

logger = logging.getLogger(__name__)


class CatalogService:
    def __init__(self, state: AnalyticsState) -> None:
        self._state = state

    def load_reference(self) -> dict[str, int]:
        state = self._state
        directory = state.settings.reference_dir.strip()
        if not directory:
            state._imported = ()
            state._sites = tuple(
                site for site in state._sites if site.origin == "discovered"
            )
            state._msrs = ()
            return {"sites": len(state._sites), "msrs": 0}
        loaded = state._references.load_dir(
            Path(directory),
            columns=state.settings.columns,
            limits=state._archive_limits(),
        )
        state._bmoas = loaded.bmoas
        self._replace_catalog(loaded.sites, loaded.msrs)
        logger.info(
            "Loaded reference sites=%s msrs=%s from %s",
            len(state._sites),
            len(state._msrs),
            directory,
        )
        return {"sites": len(state._sites), "msrs": len(state._msrs)}

    def reload_reference(self) -> dict[str, int]:
        return self.load_reference()

    def save_upload(self, filename: str, body: bytes) -> dict[str, int]:
        state = self._state
        name = Path(filename).name
        if not name or name != filename or name.startswith("."):
            raise ValueError("reference upload name must be a single file name")
        if len(body) > state.settings.max_upload_bytes:
            raise ValueError("upload exceeds max_upload_bytes")
        state._references.parse_bytes(
            name,
            body,
            columns=state.settings.columns,
            limits=state._archive_limits(),
        )
        directory = Path(state.settings.reference_dir.strip() or "reference")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / name
        temporary = directory / f".{name}.upload"
        temporary.write_bytes(body)
        temporary.replace(target)
        return self.load_reference()

    def open(self) -> None:
        """Load the schema catalog, then the coord card. A bad file is logged."""

        self._load_schema()
        self._load_coord_card_file()

    def save_coord_card(
        self,
        filename: str,
        body: bytes,
        content_type: str = "",
    ) -> dict[str, int]:
        """Validate, then replace the card. A bad card leaves the previous one."""

        state = self._state
        if len(body) > state.settings.max_upload_bytes:
            raise ValueError("coord card exceeds the upload limit")
        name = filename.strip() or "coord-card.yaml"
        if "/" in name or "\\" in name or name.startswith("."):
            raise ValueError("coord card filename must be a single file name")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CoordCardError("Coord card must be UTF-8.") from exc
        card, document, version = self._read_card(text, name, content_type)
        target = self._coord_card_target(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.upload"
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(target)
        state._coord_card_file = target
        self._install_card(card, document, version)
        return {"types": len(card.types), "templates": len(card.templates)}

    def save_coord_card_schema(self, body: bytes, version: str = "") -> dict[str, object]:
        """Validate a schema, keep it only when the current card still matches."""

        state = self._state
        if len(body) > state.settings.max_upload_bytes:
            raise ValueError("coord card schema exceeds the upload limit")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SchemaCatalogError("Schema must be UTF-8.") from exc
        upload = interpret_schema_upload(text, version_hint=version)
        current = state._schema_catalog
        proposed = (
            upload.catalog if upload.replaces else merge_schema_catalog(current, upload.catalog)
        )
        document = state._card_document
        if document is not None:
            try:
                validate_card_document(
                    document,
                    proposed,
                    remembered=state._card_schema_version,
                )
            except SchemaCatalogError as exc:
                raise SchemaCatalogError(
                    f"Current coord card does not match the new schema: {exc}"
                ) from exc
        target = state._schema_path or schema_destination(state.settings)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.upload"
        temporary.write_text(dump_catalog(proposed), encoding="utf-8")
        temporary.replace(target)
        logger.info(
            "Coord card schema changed from versions %s to %s",
            list(current.version_ids()),
            list(proposed.version_ids()),
        )
        state._schema_catalog = proposed
        state._schema_path = target
        return {
            "versions": list(proposed.version_ids()),
            "default_version": proposed.default_version,
        }

    def verify_site(self, name: str, as_of: datetime | None = None) -> dict[str, object]:
        state = self._state
        moment = as_of or datetime.now(UTC)
        updated: list[Site] = []
        found = False
        for site in state._sites:
            if site.name != name:
                updated.append(site)
                continue
            found = True
            evidence = site.evidence
            if not any(item.kind == "truth" for item in evidence):
                evidence = evidence + (
                    Evidence(
                        kind="truth",
                        weight=state.settings.confidence.confirmed_at,
                        detail="manual verification",
                        observed_at=moment,
                    ),
                )
            stamped = Site(
                name=site.name,
                site_types=site.site_types,
                status=site.status,
                confidence=site.confidence,
                evidence=evidence,
                origin=site.origin,
                latitude=site.latitude,
                longitude=site.longitude,
                polygon=site.polygon,
                moa=site.moa,
                complex_name=site.complex_name,
                mgrs=site.mgrs,
                activity_status=site.activity_status,
                verified=True,
            )
            updated.append(
                assess_site(stamped, _quiet_activity(moment), state.settings.confidence)
            )
        if not found:
            raise KeyError(name)
        state._sites = tuple(updated)
        return site_fields(state._named_site(name))

    def _replace_catalog(
        self,
        imported: tuple[Site, ...],
        msrs: tuple[Msr, ...],
    ) -> None:
        state = self._state
        stamped = tuple(
            site if site.origin == "import" else stamp_import(site, state.settings.confidence)
            for site in imported
        )
        state._imported = stamped
        state._msrs = msrs
        state._sites = merge_catalog(stamped, state._sites)

    def _coord_card_target(self, filename: str) -> Path:
        state = self._state
        configured = state.settings.coord_card_path.strip()
        if configured:
            return Path(configured)
        directory = Path(state.settings.reference_dir.strip() or "reference")
        return directory / filename

    def _load_coord_card_file(self) -> None:
        state = self._state
        path = state._coord_card_file
        if path is None or not path.is_file():
            return
        try:
            text = path.read_text(encoding="utf-8")
            card, document, version = self._read_card(text, path.name, "")
            self._install_card(card, document, version)
        except ValueError:
            logger.exception("Coord card at %s was not loaded", path)

    def _load_schema(self) -> None:
        state = self._state
        configured = state.settings.coord_card_schema_path.strip()
        path = Path(configured) if configured else state._schema_path
        if path is None or not path.is_file():
            state._schema_catalog = bundled_catalog()
            return
        try:
            state._schema_catalog = catalog_from_text(path.read_text(encoding="utf-8"))
            logger.info(
                "Loaded coord card schema versions %s from %s",
                list(state._schema_catalog.version_ids()),
                path,
            )
        except (OSError, SchemaCatalogError):
            logger.exception("Coord card schema at %s was not loaded", path)
            state._schema_catalog = bundled_catalog()

    def _read_card(
        self,
        text: str,
        name: str,
        content_type: str,
    ) -> tuple[CoordCard, dict[str, object], str]:
        state = self._state
        return read_coord_card(
            text,
            suffix=name,
            content_type=content_type,
            mismatch_meters=state.settings.coord_mismatch_meters,
            ellipse_unit=state.settings.ellipse_unit,
            magnetic_declination_degrees=state.settings.magnetic_declination_degrees,
            cat_ce=state._cat_ce(),
            confidence_terms=state._terms(),
            schema=state._schema_catalog,
        )

    def _install_card(
        self,
        card: CoordCard,
        document: dict[str, object],
        version: str,
    ) -> None:
        state = self._state
        previous = ingest_room_list(state._coord_card, state.settings)
        previous_routes = describe_table(active_publish_table(state._coord_card, state.settings))
        state._coord_card = card
        state._card_document = document
        state._card_schema_version = version
        current = ingest_room_list(card, state.settings)
        if previous != current:
            logger.info(
                "ChatSurfer ingest rooms changed from %s to %s",
                list(previous),
                list(current),
            )
        routes = describe_table(active_publish_table(card, state.settings))
        if routes != previous_routes:
            logger.info("ChatSurfer publish routing %s", routes)


def _quiet_activity(as_of: datetime) -> SiteActivity:
    return SiteActivity(
        as_of=as_of,
        stops=0,
        dwell_seconds=0.0,
        distinct_vehicles=0,
        revisits=0,
        milling=False,
        on_msr=False,
        long_dwell=False,
        cell_id="",
        cell_score=0.0,
        cell_hotspot=False,
        reports=(),
        last_activity_at=None,
    )

def _coord_card_file(path: str) -> Path | None:
    text = path.strip()
    if not text:
        return None
    return Path(text)

