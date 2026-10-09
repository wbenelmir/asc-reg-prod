"""A small, bounded Office Open XML (.xlsx) writer and reader, standard library only.

The project has no spreadsheet dependency, and the decision workbook
(`apps.reviews.workbook`) needs only plain text cells, so this module writes
and reads exactly that instead of adding a new runtime dependency.

Writer: inline-string cells only (a cell is a formula only when it carries an
`<f>` element, which is never written, so every value is literal text; display
columns additionally carry `quotePrefix`), a frozen header row, an auto
filter, column widths, list validations (drop-downs) and an optional sheet
protection WITHOUT password. Protection is an editing convenience, never a
security boundary: the server re-verifies everything it reads back.

Reader (untrusted input): the archive size, member count, member sizes and
total expanded size are bounded before anything is parsed; macro-enabled
content and external links are refused; any XML part declaring a DOCTYPE or
an ENTITY is refused before parsing (no entity expansion, no external
fetch); formulas are never evaluated -- a cell holding one is reported with
`has_formula=True` so the caller can reject it. Only the text needed is
returned: shared strings, inline strings, booleans and numbers as text.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

# ---------------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------------

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

#: Characters XML 1.0 cannot carry at all (C0 controls except TAB, LF, CR).
_INVALID_XML_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_CELL_REF = re.compile(r"^([A-Z]{1,3})([0-9]{1,7})$")


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def column_index(letters: str) -> int:
    value = 0
    for char in letters:
        value = value * 26 + (ord(char) - 64)
    return value - 1


def _clean(value: object) -> str:
    return _INVALID_XML_CHARS.sub("", "" if value is None else str(value))


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

# Cell styles (cellXfs indexes) written by `_styles_xml`.
STYLE_DEFAULT = 0
STYLE_HEADER = 1
STYLE_TEXT = 2  # literal display text (quotePrefix), locked
STYLE_EDITABLE = 3  # unlocked, text format, highlighted
STYLE_TECHNICAL = 4  # locked, grey
STYLE_WRAP = 5  # instructions text, wrapped
STYLE_TITLE = 6  # instructions title, bold


@dataclass(frozen=True)
class Column:
    header: str
    width: float = 18
    style: int = STYLE_TEXT


@dataclass
class Sheet:
    name: str
    columns: list[Column]
    rows: list[list[str]]
    #: (column index, allowed values): a drop-down over every data row.
    validations: list[tuple[int, tuple[str, ...]]] = field(default_factory=list)
    header: bool = True
    freeze_header: bool = True
    auto_filter: bool = True
    protect: bool = False
    #: Per-row style override for instructions-like sheets (column index -> style).
    row_styles: dict[int, int] = field(default_factory=dict)


def _cell_xml(ref: str, value: str, style: int) -> str:
    text = _clean(value)
    space = ' xml:space="preserve"' if text != text.strip() or "\n" in text else ""
    return f'<c r="{ref}" t="inlineStr" s="{style}"><is><t{space}>{escape(text)}</t></is></c>'


def _sheet_xml(sheet: Sheet) -> str:
    column_count = len(sheet.columns)
    header_rows = 1 if sheet.header else 0
    last_row = header_rows + len(sheet.rows)
    last_col = column_letter(max(column_count - 1, 0))
    parts = [
        f'<worksheet xmlns="{MAIN_NS}" xmlns:r="{REL_NS}">',
        f'<dimension ref="A1:{last_col}{max(last_row, 1)}"/>',
        '<sheetViews><sheetView workbookViewId="0">',
    ]
    if sheet.header and sheet.freeze_header:
        parts.append(
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
        )
    parts.append("</sheetView></sheetViews>")
    parts.append('<sheetFormatPr defaultRowHeight="15"/>')
    parts.append("<cols>")
    for index, column in enumerate(sheet.columns, start=1):
        parts.append(
            f'<col min="{index}" max="{index}" width="{column.width:.1f}" customWidth="1"'
            f' style="{column.style}"/>'
        )
    parts.append("</cols><sheetData>")
    if sheet.header:
        cells = "".join(
            _cell_xml(f"{column_letter(i)}1", column.header, STYLE_HEADER)
            for i, column in enumerate(sheet.columns)
        )
        parts.append(f'<row r="1">{cells}</row>')
    for offset, values in enumerate(sheet.rows):
        row_number = header_rows + offset + 1
        override = sheet.row_styles.get(offset)
        cells = "".join(
            _cell_xml(
                f"{column_letter(i)}{row_number}",
                values[i] if i < len(values) else "",
                override if override is not None else sheet.columns[i].style,
            )
            for i in range(column_count)
        )
        parts.append(f'<row r="{row_number}">{cells}</row>')
    parts.append("</sheetData>")
    if sheet.protect:
        # Unlocked without any secret (an editing convenience only); filtering and
        # sorting stay allowed.
        parts.append(
            '<sheetProtection sheet="1" objects="1" scenarios="1" autoFilter="0" sort="0"'
            ' formatColumns="0"/>'
        )
    if sheet.header and sheet.auto_filter and column_count:
        parts.append(f'<autoFilter ref="A1:{last_col}{max(last_row, 1)}"/>')
    if sheet.validations and last_row > header_rows:
        parts.append(f'<dataValidations count="{len(sheet.validations)}">')
        for column, choices in sheet.validations:
            letter = column_letter(column)
            listed = ",".join(choices)
            parts.append(
                '<dataValidation type="list" allowBlank="1" showErrorMessage="1"'
                f' sqref="{letter}{header_rows + 1}:{letter}{last_row}">'
                f"<formula1>{escape(quoteattr(listed))}</formula1></dataValidation>"
            )
        parts.append("</dataValidations>")
    parts.append(
        '<pageMargins left="0.5" right="0.5" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
    )
    parts.append("</worksheet>")
    return "".join(parts)


def _styles_xml() -> str:
    return (
        f'<styleSheet xmlns="{MAIN_NS}">'
        '<fonts count="3">'
        '<font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
        '<font><sz val="10"/><color rgb="FF6B7280"/><name val="Calibri"/><family val="2"/></font>'
        "</fonts>"
        '<fills count="4">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFD9E1F2"/>'
        '<bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFFF2CC"/>'
        '<bgColor indexed="64"/></patternFill></fill>'
        "</fills>"
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
        "</cellStyleXfs>"
        '<cellXfs count="7">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="49" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1"'
        ' applyFill="1" applyNumberFormat="1"/>'
        '<xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" quotePrefix="1"'
        ' applyNumberFormat="1"/>'
        '<xf numFmtId="49" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1"'
        ' applyNumberFormat="1" applyProtection="1"><protection locked="0"/></xf>'
        '<xf numFmtId="49" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"'
        ' quotePrefix="1" applyNumberFormat="1"/>'
        '<xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" quotePrefix="1"'
        ' applyNumberFormat="1" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>'
        '<xf numFmtId="49" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"'
        ' quotePrefix="1" applyNumberFormat="1"/>'
        "</cellXfs>"
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )


_OOXML_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml"


def write_workbook(sheets: list[Sheet]) -> bytes:
    """Serialize `sheets` to .xlsx bytes (deterministic for the same input)."""
    content_types = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        f'<Override PartName="/xl/workbook.xml" ContentType="{_OOXML_TYPE}.sheet.main+xml"/>',
        f'<Override PartName="/xl/styles.xml" ContentType="{_OOXML_TYPE}.styles+xml"/>',
    ]
    for index in range(1, len(sheets) + 1):
        content_types.append(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            f'ContentType="{_OOXML_TYPE}.worksheet+xml"/>'
        )
    content_types.append("</Types>")

    sheet_entries = []
    rels = []
    defined_names = []
    for index, sheet in enumerate(sheets, start=1):
        name = quoteattr(_clean(sheet.name)[:31])
        sheet_entries.append(f'<sheet name={name} sheetId="{index}" r:id="rId{index}"/>')
        rels.append(
            f'<Relationship Id="rId{index}" Type="{REL_NS}/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
        if sheet.header and sheet.auto_filter and sheet.columns:
            last = column_letter(len(sheet.columns) - 1)
            quoted = "'" + _clean(sheet.name)[:31].replace("'", "''") + "'"
            defined_names.append(
                f'<definedName name="_xlnm._FilterDatabase" localSheetId="{index - 1}" hidden="1">'
                f"{escape(quoted)}!$A$1:${last}${len(sheet.rows) + 1}</definedName>"
            )
    rels.append(
        f'<Relationship Id="rId{len(sheets) + 1}" Type="{REL_NS}/styles" Target="styles.xml"/>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{MAIN_NS}" xmlns:r="{REL_NS}">'
        '<bookViews><workbookView activeTab="0"/></bookViews>'
        f"<sheets>{''.join(sheet_entries)}</sheets>"
        + (f"<definedNames>{''.join(defined_names)}</definedNames>" if defined_names else "")
        + "</workbook>"
    )
    package_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{PKG_REL_NS}">'
        f'<Relationship Id="rId1" Type="{REL_NS}/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{PKG_REL_NS}">{"".join(rels)}</Relationships>'
    )
    buffer = io.BytesIO()
    fixed_time = (2026, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:

        def add(name: str, text: str) -> None:
            info = zipfile.ZipInfo(name, date_time=fixed_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text.encode("utf-8"))

        add("[Content_Types].xml", "".join(content_types))
        add("_rels/.rels", package_rels)
        add("xl/workbook.xml", workbook)
        add("xl/_rels/workbook.xml.rels", workbook_rels)
        add(
            "xl/styles.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + _styles_xml(),
        )
        for index, sheet in enumerate(sheets, start=1):
            add(
                f"xl/worksheets/sheet{index}.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + _sheet_xml(sheet),
            )
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class WorkbookReadError(Exception):
    """The upload is not a readable, acceptable .xlsx file. `code` is safe to show."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Cell:
    value: str
    has_formula: bool = False
    is_error: bool = False


@dataclass
class SheetData:
    name: str
    #: (row number starting at 1, column index starting at 0) -> Cell
    cells: dict[tuple[int, int], Cell]

    @property
    def max_row(self) -> int:
        return max((row for row, _column in self.cells), default=0)

    def value(self, row: int, column: int) -> str:
        cell = self.cells.get((row, column))
        return cell.value if cell is not None else ""

    def row_has_formula(self, row: int) -> bool:
        return any(cell.has_formula for (r, _c), cell in self.cells.items() if r == row)

    def row_values(self, row: int, width: int) -> list[str]:
        return [self.value(row, column) for column in range(width)]


@dataclass(frozen=True)
class ReadLimits:
    max_bytes: int
    max_members: int = 64
    max_member_bytes: int = 32 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    max_cells: int = 200_000


_FORBIDDEN_XML = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_xml(data: bytes):
    if _FORBIDDEN_XML.search(data):
        raise WorkbookReadError("MALFORMED_FILE")
    try:
        # Safe here: a DOCTYPE or ENTITY declaration was refused just above, so
        # there is no entity to expand and nothing external to fetch.
        return ElementTree.fromstring(data)  # noqa: S314
    except ElementTree.ParseError:
        raise WorkbookReadError("MALFORMED_FILE") from None


def _text_of(element) -> str:
    """Concatenated `<t>` text of a shared or inline string (phonetic runs skipped)."""
    pieces: list[str] = []
    for child in element:
        name = _local(child.tag)
        if name == "t":
            pieces.append(child.text or "")
        elif name == "r":
            for run_child in child:
                if _local(run_child.tag) == "t":
                    pieces.append(run_child.text or "")
    return "".join(pieces)


def _numeric_text(raw: str) -> str:
    try:
        number = float(raw)
    except ValueError:
        return raw
    if number.is_integer() and abs(number) < 1e15:
        return str(int(number))
    return raw


def read_workbook(data: bytes, *, limits: ReadLimits) -> dict[str, SheetData]:
    """Read every worksheet's text cells. Raises `WorkbookReadError` on any problem."""
    if len(data) > limits.max_bytes:
        raise WorkbookReadError("FILE_TOO_LARGE")
    if not data or not zipfile.is_zipfile(io.BytesIO(data)):
        raise WorkbookReadError("NOT_XLSX")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError):  # fmt: skip
        raise WorkbookReadError("NOT_XLSX") from None
    with archive:
        members = archive.infolist()
        if len(members) > limits.max_members:
            raise WorkbookReadError("MALFORMED_FILE")
        names = set()
        total = 0
        for member in members:
            name = member.filename
            if name.startswith("/") or ".." in name.split("/") or "\\" in name:
                raise WorkbookReadError("MALFORMED_FILE")
            if member.file_size > limits.max_member_bytes:
                raise WorkbookReadError("FILE_TOO_LARGE")
            total += member.file_size
            names.add(name)
        if total > limits.max_total_bytes:
            raise WorkbookReadError("FILE_TOO_LARGE")
        lowered = {name.lower() for name in names}
        if any("vbaproject" in name or name.endswith(".bin") for name in lowered):
            raise WorkbookReadError("MACROS_NOT_ALLOWED")
        if any(name.startswith("xl/externallinks/") for name in lowered):
            raise WorkbookReadError("EXTERNAL_LINKS_NOT_ALLOWED")
        if "xl/workbook.xml" not in names or "[Content_Types].xml" not in names:
            raise WorkbookReadError("NOT_XLSX")

        def read(name: str) -> bytes:
            with archive.open(name) as stream:
                content = stream.read(limits.max_member_bytes + 1)
            if len(content) > limits.max_member_bytes:
                raise WorkbookReadError("FILE_TOO_LARGE")
            return content

        content_types = read("[Content_Types].xml")
        if b"macroEnabled" in content_types:
            raise WorkbookReadError("MACROS_NOT_ALLOWED")

        workbook = _parse_xml(read("xl/workbook.xml"))
        rels_targets: dict[str, str] = {}
        if "xl/_rels/workbook.xml.rels" in names:
            for rel in _parse_xml(read("xl/_rels/workbook.xml.rels")):
                if _local(rel.tag) != "Relationship":
                    continue
                if rel.get("TargetMode", "") == "External":
                    raise WorkbookReadError("EXTERNAL_LINKS_NOT_ALLOWED")
                target = rel.get("Target", "")
                target = target[1:] if target.startswith("/") else f"xl/{target}"
                rels_targets[rel.get("Id", "")] = target

        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for item in _parse_xml(read("xl/sharedStrings.xml")):
                if _local(item.tag) == "si":
                    shared.append(_text_of(item))

        sheets: dict[str, SheetData] = {}
        cell_budget = limits.max_cells
        for element in workbook.iter():
            if _local(element.tag) != "sheet":
                continue
            name = element.get("name", "")
            rel_id = next(
                (value for key, value in element.attrib.items() if _local(key) == "id"), ""
            )
            target = rels_targets.get(rel_id, "")
            if not target or target not in names:
                continue
            root = _parse_xml(read(target))
            cells: dict[tuple[int, int], Cell] = {}
            for row_number, row in enumerate(
                (node for node in root.iter() if _local(node.tag) == "row"), start=1
            ):
                if row.get("r", "").isdigit():
                    row_number = int(row.get("r"))
                next_column = 0
                for cell in row:
                    if _local(cell.tag) != "c":
                        continue
                    cell_budget -= 1
                    if cell_budget < 0:
                        raise WorkbookReadError("ROW_LIMIT_EXCEEDED")
                    match = _CELL_REF.match(cell.get("r", ""))
                    column = column_index(match.group(1)) if match else next_column
                    if match:
                        row_number = int(match.group(2))
                    next_column = column + 1
                    kind = cell.get("t", "n")
                    has_formula = False
                    raw = ""
                    inline = None
                    for child in cell:
                        child_name = _local(child.tag)
                        if child_name == "f":
                            has_formula = True
                        elif child_name == "v":
                            raw = child.text or ""
                        elif child_name == "is":
                            inline = child
                    if kind == "s":
                        try:
                            value = shared[int(raw)]
                        except (ValueError, IndexError):  # fmt: skip
                            raise WorkbookReadError("MALFORMED_FILE") from None
                    elif kind == "inlineStr":
                        value = _text_of(inline) if inline is not None else ""
                    elif kind == "b":
                        value = "TRUE" if raw == "1" else "FALSE"
                    elif kind in ("str", "e"):
                        value = raw
                    else:
                        value = _numeric_text(raw)
                    if value or has_formula:
                        cells[(row_number, column)] = Cell(
                            value=value, has_formula=has_formula, is_error=kind == "e"
                        )
            sheets[name] = SheetData(name=name, cells=cells)
        return sheets
