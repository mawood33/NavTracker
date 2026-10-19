import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from analytics.infrastructure.reference_parse import (
    load_reference_file,
    parse_csv_sites,
    parse_dms,
    parse_kml,
    parse_kmz,
)

FIXTURES = Path("tests/fixtures/analytics")


def test_dms_and_csv_player_columns():
    assert parse_dms("36°26'02\"N") == 36 + 26 / 60 + 2 / 3600
    assert parse_dms("129°02'33\"W") < 0
    sites = parse_csv_sites(
        (FIXTURES / "player_data.csv").read_text(encoding="utf-8"),
        complex_name="Eastern Complex",
    ).sites
    assert len(sites) == 1
    assert sites[0].name == "06001"
    assert sites[0].moa == "06"
    assert sites[0].complex_name == "Eastern Complex"
    assert sites[0].mgrs == "32AAA0000000000"
    assert abs(sites[0].latitude - 10) < 1e-9
    assert abs(sites[0].longitude - 20) < 1e-9
    assert sites[0].site_types == frozenset()


def test_google_earth_kml_is_a_site_list_grouped_by_moa():
    data = parse_kml((FIXTURES / "player_points.kml").read_bytes())
    assert data.msrs == ()
    assert len(data.sites) == 1
    site = data.sites[0]
    assert site.name == "06001"
    assert site.moa == "06"
    assert site.complex_name == "Master Eastern Complex"
    assert site.latitude == 10
    assert site.longitude == 20


def test_polygon_extended_data_sets_the_site_type():
    data = parse_kml((FIXTURES / "site_polygon.kml").read_bytes())
    site = data.sites[0]
    assert site.name == "Pad"
    assert site.site_types == frozenset({"launch"})
    assert site.complex_name == "Example Complex"
    assert len(site.polygon) == 4


def test_kmz_lines_are_msrs_and_the_member_need_not_be_doc_kml():
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>MoW Draw Export-KML</name>
<Folder><name>Drawing Group 2</name>
<Placemark><name>MSR RED</name>
<description>&lt;b&gt;Label:&lt;/b&gt; MSR RED&lt;br&gt;Length: 1.5 Kilometers</description>
<LineString><coordinates>20,10 20.01,10.01</coordinates></LineString>
</Placemark></Folder></Document></kml>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("MoW-Draw-Export-KML.kml", kml)
    data = parse_kmz(buffer.getvalue())
    assert data.sites == ()
    assert data.msrs[0].name == "MSR RED"
    assert data.msrs[0].length_text == "1.5 Kilometers"
    assert data.msrs[0].points[0] == (10.0, 20.0)


def test_xlsx_uses_the_player_data_headers_and_sheet_complex(tmp_path):
    path = tmp_path / "Example Player Data Eastern Complex.xlsx"
    _write_xlsx(
        path,
        "(U) Eastern Complex",
        [
            ["Name/ID", "DMS Lat", "DMS Long", "MGRS"],
            ["11007", "11°30'00\"N", "21°15'00\"E", "32BBB"],
        ],
    )
    data = load_reference_file(path)
    assert data.sites[0].name == "11007"
    assert data.sites[0].moa == "11"
    assert data.sites[0].complex_name == "Eastern Complex"
    assert data.sites[0].mgrs == "32BBB"


def _write_xlsx(path, sheet_name, rows):
    strings = []
    index = {}

    def shared(value):
        if value not in index:
            index[value] = len(strings)
            strings.append(value)
        return index[value]

    cells = []
    for row_number, row in enumerate(rows, start=1):
        bits = []
        for column, value in enumerate(row):
            letter = chr(ord("A") + column)
            bits.append(
                f'<c r="{letter}{row_number}" t="s"><v>{shared(value)}</v></c>'
            )
        cells.append(f'<row r="{row_number}">{"".join(bits)}</row>')
    shared_xml = "".join(f"<si><t>{_xml(text)}</t></si>" for text in strings)
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            "</Types>"
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{_xml(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>"
        ),
        "xl/sharedStrings.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"{shared_xml}</sst>"
        ),
        "xl/worksheets/sheet1.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(cells)}</sheetData></worksheet>"
        ),
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    ET.fromstring(files["xl/worksheets/sheet1.xml"])


def _xml(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace('"', "&quot;")
    )
