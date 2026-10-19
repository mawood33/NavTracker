"""Reference-file parsers.

Player data (``<Exercise> Player Data <Complex>``) is an ``.xlsx`` or
``.csv`` sheet. The inspected workbook has one sheet named like
``(U) Eastern Complex`` and columns ``Name/ID``, ``DMS Lat``,
``DMS Long``, ``DMS LatLong``, ``MGRS``. ``Name/ID`` is five digits:
the first two are the MOA and the last three are the unique id. There
is no hide/launch/transload column. A cell note says the same thing.

The Google Earth ``.kml`` export of that list is points, not routes.
Folders are ``MOA <n>`` under a complex folder such as
``Master Eastern Complex``. Placemark names are the same five-digit
ids. Coordinates are ``lon,lat,altitude``. Styles are icons only.
There is no ExtendedData. A placemark may also be a polygon; the outer
ring is the site outline.

MSR files (``<Exercise> <Complex> MSRs``) are KMZ or KML. The inspected
KMZ is a zip whose member is a ``.kml`` (not necessarily ``doc.kml``)
exported by MoW Draw. Each placemark is a ``LineString`` named
``MSR <label>``. The description HTML repeats the label, a length, and
vertex text; the geometry is the LineString, which is ``lon,lat``.

CSV and XLSX column names can be overridden. Unset fields use the
headers above, plus plain ``lat``/``lon`` and ``type``.
"""

import csv
import io
import json
import logging
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from analytics.domain.columns import ColumnMap
from analytics.domain.reference import (
    ArchiveLimits,
    Bmoa,
    Msr,
    ReferenceData,
    Site,
    parse_site_types,
)

logger = logging.getLogger(__name__)
_CHUNK = 65_536

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DMS_RE = re.compile(
    r"(\d+)\s*°\s*(\d+)\s*['′]\s*([\d.]+)\s*[\"″]?\s*([NnSsEeWw])"
)
_MOA_FOLDER_RE = re.compile(r"MOA\s+(\d+)\s*$", re.IGNORECASE)
_LENGTH_RE = re.compile(
    r"Length:\s*(?:<[^>]+>\s*)*([0-9.]+\s*[A-Za-z]+)",
    re.IGNORECASE,
)
_CLASSIFICATION_RE = re.compile(r"^\([A-Za-z]+\)\s*")
_IGNORED_FOLDER_NAMES = {"untitled project"}


def parse_dms(text: str) -> float:
    """Parse one hemisphere DMS token such as ``36°26'02"N``."""

    match = _DMS_RE.search(text)
    if match is None:
        raise ValueError(f"Invalid DMS coordinate: {text!r}")
    degrees, minutes, seconds, hemisphere = match.groups()
    value = int(degrees) + int(minutes) / 60 + float(seconds) / 3600
    if hemisphere.upper() in {"S", "W"}:
        value = -value
    return value


def complex_name_from_sheet(sheet_name: str) -> str:
    """Drop a leading classification mark such as ``(U)``."""

    return _CLASSIFICATION_RE.sub("", sheet_name.strip()).strip()


def moa_from_site_name(name: str) -> str:
    """Five-digit ids encode the MOA in the first two digits."""

    if len(name) == 5 and name.isdigit():
        return name[:2]
    return ""


def parse_csv_sites(
    text: str,
    *,
    columns: ColumnMap | None = None,
    complex_name: str = "",
) -> ReferenceData:
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    return _sites_from_rows(rows, columns or ColumnMap(), complex_name)


def parse_xlsx_sites(
    data: bytes,
    *,
    columns: ColumnMap | None = None,
    limits: ArchiveLimits | None = None,
) -> ReferenceData:
    sheet_name, rows = _xlsx_table(data, limits or ArchiveLimits())
    return _sites_from_rows(
        rows,
        columns or ColumnMap(),
        complex_name_from_sheet(sheet_name),
    )


def parse_kml(data: bytes) -> ReferenceData:
    try:
        root = ET.fromstring(_bind_kml_prefix(data))
    except ET.ParseError as exc:
        raise ValueError("KML was not valid XML") from exc
    sites: list[Site] = []
    msrs: list[Msr] = []
    _walk_kml(root, "", "", sites, msrs)
    return _split_bmoas(sites, msrs)


def parse_geojson(data: bytes) -> ReferenceData:
    """Read points as sites, lines as MSRs, and BMOA polygons as areas."""

    try:
        payload = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise ValueError("GeoJSON was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("GeoJSON must be a Feature or FeatureCollection")
    kind = str(payload.get("type") or "")
    if kind == "FeatureCollection":
        raw = payload.get("features")
        features = raw if isinstance(raw, list) else []
    elif kind == "Feature":
        features = [payload]
    else:
        raise ValueError("GeoJSON must be a Feature or FeatureCollection")
    sites: list[Site] = []
    msrs: list[Msr] = []
    for feature in features:
        _read_geojson_feature(feature, sites, msrs)
    return _split_bmoas(sites, msrs)


def _bind_kml_prefix(data: bytes) -> bytes:
    """Google Earth writes ``kml:id`` without declaring the ``kml`` prefix."""

    text = data.decode("utf-8", errors="replace")
    if "kml:" not in text or "xmlns:kml=" in text:
        return data
    return text.replace(
        "<kml ",
        '<kml xmlns:kml="http://www.opengis.net/kml/2.2" ',
        1,
    ).encode("utf-8")


def parse_kmz(data: bytes, *, limits: ArchiveLimits | None = None) -> ReferenceData:
    """Read every ``.kml`` member. The sample archive is not named doc.kml."""

    caps = limits or ArchiveLimits()
    combined = ReferenceData()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        _reject_archive(archive, caps)
        names = [
            info
            for info in archive.infolist()
            if info.filename.lower().endswith(".kml") and not info.is_dir()
        ]
        if not names:
            raise ValueError("KMZ archive does not contain a KML file")
        for info in names:
            combined = combined.merge(parse_kml(_read_member(archive, info, caps)))
    return combined


def load_reference_file(
    path: Path,
    *,
    columns: ColumnMap | None = None,
    limits: ArchiveLimits | None = None,
) -> ReferenceData:
    return parse_reference_bytes(
        path.name,
        path.read_bytes(),
        columns=columns,
        limits=limits,
    )


def parse_reference_bytes(
    filename: str,
    data: bytes,
    *,
    columns: ColumnMap | None = None,
    limits: ArchiveLimits | None = None,
) -> ReferenceData:
    """Parse one reference file from memory. Nothing is written."""

    suffix = Path(filename).suffix.lower()
    name = Path(filename).name.lower()
    caps = limits or ArchiveLimits()
    if suffix == ".kmz" or name.endswith(".kmz.zip"):
        return parse_kmz(data, limits=caps)
    if suffix == ".kml":
        return parse_kml(data)
    if suffix == ".geojson":
        return parse_geojson(data)
    if suffix == ".csv":
        complex_name = _complex_from_filename(Path(filename).name)
        return parse_csv_sites(
            data.decode("utf-8-sig"),
            columns=columns,
            complex_name=complex_name,
        )
    if suffix == ".xlsx":
        return parse_xlsx_sites(data, columns=columns, limits=caps)
    raise ValueError(f"Unsupported reference file: {Path(filename).name}")


def load_reference_dir(
    directory: Path,
    *,
    columns: ColumnMap | None = None,
    limits: ArchiveLimits | None = None,
) -> ReferenceData:
    if not directory.is_dir():
        return ReferenceData()
    combined = ReferenceData()
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in {".kml", ".kmz", ".csv", ".xlsx", ".geojson"} and not (
            path.name.lower().endswith(".kmz.zip")
        ):
            continue
        try:
            combined = combined.merge(
                load_reference_file(path, columns=columns, limits=limits)
            )
        except Exception:
            logger.exception("Skipping unreadable reference file %s", path)
    return combined


def _reject_archive(archive: zipfile.ZipFile, limits: ArchiveLimits) -> None:
    infos = archive.infolist()
    if len(infos) > limits.max_entries:
        raise ValueError(
            f"archive entries {len(infos)} exceed {limits.max_entries}"
        )
    declared = 0
    for info in infos:
        size = info.file_size
        if size > limits.max_bytes:
            raise ValueError(
                f"declared decompressed size {size} exceeds {limits.max_bytes}"
            )
        declared += max(size, 0)
        if declared > limits.max_bytes:
            raise ValueError(
                f"declared decompressed size {declared} exceeds {limits.max_bytes}"
            )


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limits: ArchiveLimits) -> bytes:
    if info.file_size > limits.max_bytes:
        raise ValueError(
            f"declared decompressed size {info.file_size} exceeds {limits.max_bytes}"
        )
    chunks: list[bytes] = []
    total = 0
    with archive.open(info, "r") as handle:
        while True:
            block = handle.read(_CHUNK)
            if not block:
                break
            total += len(block)
            if total > limits.max_bytes:
                raise ValueError(f"decompressed size exceeds {limits.max_bytes}")
            chunks.append(block)
    return b"".join(chunks)


def _split_bmoas(sites: list[Site], msrs: list[Msr]) -> ReferenceData:
    kept: list[Site] = []
    areas: list[Bmoa] = []
    for site in sites:
        if site.polygon and "bmoa" in site.name.lower():
            areas.append(Bmoa(name=site.name, polygon=site.polygon))
        else:
            kept.append(site)
    return ReferenceData(sites=tuple(kept), msrs=tuple(msrs), bmoas=tuple(areas))


def _read_geojson_feature(
    feature: object,
    sites: list[Site],
    msrs: list[Msr],
) -> None:
    if not isinstance(feature, dict):
        return
    properties = feature.get("properties")
    props = properties if isinstance(properties, dict) else {}
    name = str(props.get("name") or props.get("Name") or props.get("id") or "unnamed")
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict):
        return
    kind = str(geometry.get("type") or "")
    coordinates = geometry.get("coordinates")
    if kind == "Point":
        point = _lonlat_pair(coordinates)
        if point is None:
            return
        latitude, longitude = point
        sites.append(
            Site(
                name=name,
                latitude=latitude,
                longitude=longitude,
                moa=moa_from_site_name(name),
            )
        )
        return
    if kind == "LineString" and isinstance(coordinates, list):
        line = _lonlat_pairs(coordinates)
        if len(line) >= 2:
            msrs.append(Msr(name=name, points=line))
        return
    if kind == "Polygon" and isinstance(coordinates, list) and coordinates:
        ring_raw = coordinates[0]
        if not isinstance(ring_raw, list):
            return
        ring = _lonlat_pairs(ring_raw)
        if len(ring) < 3:
            return
        sites.append(
            Site(
                name=name,
                latitude=sum(point[0] for point in ring) / len(ring),
                longitude=sum(point[1] for point in ring) / len(ring),
                polygon=ring,
                moa=moa_from_site_name(name),
            )
        )


def _lonlat_pairs(raw: list[object]) -> tuple[tuple[float, float], ...]:
    found: list[tuple[float, float]] = []
    for item in raw:
        pair = _lonlat_pair(item)
        if pair is not None:
            found.append(pair)
    return tuple(found)


def _lonlat_pair(value: object) -> tuple[float, float] | None:
    if not isinstance(value, list) or len(value) < 2:
        return None
    if isinstance(value[0], bool) or isinstance(value[1], bool):
        return None
    if not isinstance(value[0], (int, float)) or not isinstance(value[1], (int, float)):
        return None
    return float(value[1]), float(value[0])


def _sites_from_rows(
    rows: list[list[str]],
    columns: ColumnMap,
    complex_name: str,
) -> ReferenceData:
    if not rows:
        return ReferenceData()
    header = [_norm(cell) for cell in rows[0]]
    index = _header_index(header, columns)
    if "name" not in index:
        raise ValueError(
            "Reference table needs a name column "
            f"({', '.join(columns.name)}). Headers: {rows[0]!r}"
        )
    sites: list[Site] = []
    for row in rows[1:]:
        name = _cell(row, index.get("name"))
        if not name:
            continue
        latitude, longitude = _row_coordinates(row, index)
        if latitude is None or longitude is None:
            continue
        sites.append(
            Site(
                name=name,
                site_types=parse_site_types(_cell(row, index.get("site_type"))),
                latitude=latitude,
                longitude=longitude,
                moa=moa_from_site_name(name),
                complex_name=complex_name,
                mgrs=_cell(row, index.get("mgrs")),
            )
        )
    return ReferenceData(sites=tuple(sites))


def _row_coordinates(
    row: list[str],
    index: dict[str, int],
) -> tuple[float | None, float | None]:
    lat_text = _cell(row, index.get("latitude"))
    lon_text = _cell(row, index.get("longitude"))
    if lat_text and lon_text:
        try:
            return float(lat_text), float(lon_text)
        except ValueError:
            pass
    lat_dms = _cell(row, index.get("latitude_dms"))
    lon_dms = _cell(row, index.get("longitude_dms"))
    if lat_dms and lon_dms:
        return parse_dms(lat_dms), parse_dms(lon_dms)
    return None, None


def _header_index(header: list[str], columns: ColumnMap) -> dict[str, int]:
    aliases = {
        "name": columns.name,
        "site_type": columns.site_type,
        "latitude_dms": columns.latitude_dms,
        "longitude_dms": columns.longitude_dms,
        "latitude": columns.latitude,
        "longitude": columns.longitude,
        "mgrs": columns.mgrs,
    }
    found: dict[str, int] = {}
    for field, names in aliases.items():
        wanted = {_norm(name) for name in names}
        for position, cell in enumerate(header):
            if cell in wanted:
                found[field] = position
                break
    return found


def _walk_kml(
    element: ET.Element,
    complex_name: str,
    moa: str,
    sites: list[Site],
    msrs: list[Msr],
) -> None:
    tag = _local(element.tag)
    if tag in {"kml", "Document", "Folder"}:
        next_complex, next_moa = _folder_context(element, complex_name, moa, tag)
        for child in list(element):
            _walk_kml(child, next_complex, next_moa, sites, msrs)
        return
    if tag == "Placemark":
        _read_placemark(element, complex_name, moa, sites, msrs)
        return
    for child in list(element):
        _walk_kml(child, complex_name, moa, sites, msrs)


def _folder_context(
    element: ET.Element,
    complex_name: str,
    moa: str,
    tag: str,
) -> tuple[str, str]:
    name = _child_text(element, "name")
    if not name:
        return complex_name, moa
    moa_match = _MOA_FOLDER_RE.match(name)
    if moa_match:
        return complex_name, moa_match.group(1).zfill(2)
    lowered = name.lower()
    if lowered in _IGNORED_FOLDER_NAMES or lowered.startswith("mow draw"):
        return complex_name, moa
    if lowered.startswith("drawing group"):
        return complex_name, moa
    if tag == "Document":
        return complex_name_from_sheet(name), moa
    return complex_name_from_sheet(name), moa


def _read_placemark(
    element: ET.Element,
    complex_name: str,
    moa: str,
    sites: list[Site],
    msrs: list[Msr],
) -> None:
    name = _child_text(element, "name") or "unnamed"
    site_types = parse_site_types(_extended_type(element))
    description = _child_text(element, "description")
    length = ""
    if description:
        found = _LENGTH_RE.search(description)
        if found:
            length = found.group(1).strip()
    _read_geometries(
        element,
        name,
        site_types,
        complex_name,
        moa,
        length,
        sites,
        msrs,
    )


def _read_geometries(
    element: ET.Element,
    name: str,
    site_types: frozenset[str],
    complex_name: str,
    moa: str,
    length: str,
    sites: list[Site],
    msrs: list[Msr],
) -> None:
    for child in list(element):
        tag = _local(child.tag)
        if tag == "Point":
            points = _coordinates(_child_text(child, "coordinates"))
            if points:
                lat, lon = points[0]
                sites.append(
                    Site(
                        name=name,
                        site_types=site_types,
                        latitude=lat,
                        longitude=lon,
                        moa=moa or moa_from_site_name(name),
                        complex_name=complex_name,
                    )
                )
        elif tag == "LineString":
            line = tuple(_coordinates(_child_text(child, "coordinates")))
            if len(line) >= 2:
                msrs.append(Msr(name=name, points=line, length_text=length))
        elif tag == "Polygon":
            ring = _outer_ring(child)
            if len(ring) >= 3:
                sites.append(
                    Site(
                        name=name,
                        site_types=site_types,
                        latitude=sum(point[0] for point in ring) / len(ring),
                        longitude=sum(point[1] for point in ring) / len(ring),
                        polygon=ring,
                        moa=moa or moa_from_site_name(name),
                        complex_name=complex_name,
                    )
                )
        elif tag == "MultiGeometry":
            _read_geometries(
                child,
                name,
                site_types,
                complex_name,
                moa,
                length,
                sites,
                msrs,
            )


def _outer_ring(polygon: ET.Element) -> tuple[tuple[float, float], ...]:
    for element in polygon.iter():
        if _local(element.tag) == "coordinates" and element.text:
            return tuple(_coordinates(element.text))
    return ()


def _coordinates(text: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for token in text.split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        longitude = float(parts[0])
        latitude = float(parts[1])
        points.append((latitude, longitude))
    return points


def _extended_type(element: ET.Element) -> str:
    type_names = {"type", "site type", "site_type"}
    for child in element.iter():
        tag = _local(child.tag)
        attr = child.attrib.get("name", "").strip().lower()
        if tag == "Data" and attr in type_names:
            value = _child_text(child, "value")
            if value:
                return value
        if tag == "SimpleData" and attr in type_names and child.text:
            return child.text.strip()
    return ""


def _xlsx_table(data: bytes, limits: ArchiveLimits) -> tuple[str, list[list[str]]]:
    ns = {"m": _MAIN_NS}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        _reject_archive(archive, limits)
        strings: list[str] = []
        shared_info = _member(archive, "xl/sharedStrings.xml")
        if shared_info is not None:
            shared = ET.fromstring(_read_member(archive, shared_info, limits))
            for item in shared.findall("m:si", ns):
                strings.append("".join(text.text or "" for text in item.iter(f"{{{_MAIN_NS}}}t")))
        workbook_info = _required_member(archive, "xl/workbook.xml")
        workbook = ET.fromstring(_read_member(archive, workbook_info, limits))
        sheet = workbook.find("m:sheets/m:sheet", ns)
        sheet_name = "" if sheet is None else sheet.attrib.get("name", "")
        worksheet = ET.fromstring(
            _read_member(archive, _required_member(archive, "xl/worksheets/sheet1.xml"), limits)
        )
        rows: list[list[str]] = []
        for row in worksheet.findall("m:sheetData/m:row", ns):
            values: dict[int, str] = {}
            for cell in row.findall("m:c", ns):
                values[_column_index(cell.attrib.get("r", "A1"))] = _xlsx_cell(
                    cell, strings, ns
                )
            if not any(value.strip() for value in values.values()):
                continue
            width = max(values) + 1
            rows.append([values.get(index, "") for index in range(width)])
    return sheet_name, rows


def _xlsx_cell(cell: ET.Element, strings: list[str], ns: dict[str, str]) -> str:
    kind = cell.attrib.get("t")
    value = cell.find("m:v", ns)
    if kind == "inlineStr":
        return "".join(text.text or "" for text in cell.iter(f"{{{_MAIN_NS}}}t"))
    if value is None or value.text is None:
        return ""
    if kind == "s":
        return strings[int(value.text)]
    return value.text


def _column_index(cell_ref: str) -> int:
    letters = "".join(char for char in cell_ref if char.isalpha())
    index = 0
    for char in letters:
        index = index * 26 + (ord(char.upper()) - 64)
    return index - 1


def _member(archive: zipfile.ZipFile, name: str) -> zipfile.ZipInfo | None:
    for info in archive.infolist():
        if info.filename == name:
            return info
    return None


def _required_member(archive: zipfile.ZipFile, name: str) -> zipfile.ZipInfo:
    info = _member(archive, name)
    if info is None:
        raise ValueError(f"XLSX archive is missing {name}")
    return info


class FileReferences:
    """Directory and upload adapter. Parsing happens before any write."""

    def load_dir(
        self,
        directory: Path,
        *,
        columns: ColumnMap | None = None,
        limits: ArchiveLimits | None = None,
    ) -> ReferenceData:
        return load_reference_dir(directory, columns=columns, limits=limits)

    def parse_bytes(
        self,
        filename: str,
        data: bytes,
        *,
        columns: ColumnMap | None = None,
        limits: ArchiveLimits | None = None,
    ) -> ReferenceData:
        return parse_reference_bytes(filename, data, columns=columns, limits=limits)


def _complex_from_filename(name: str) -> str:
    stem = Path(name).stem
    marker = "player data"
    lowered = stem.lower()
    if marker in lowered:
        tail = stem[lowered.index(marker) + len(marker) :].strip(" -_")
        return tail
    return ""


def _child_text(element: ET.Element, name: str) -> str:
    for child in list(element):
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _norm(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index].strip()
