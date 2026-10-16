"""
table_import.py — імпорт таблиць у вбудовану таблицю Spiklio.

Формати:  .xlsx / .xlsm  (чистий stdlib: zipfile + xml)
          .ods           (stdlib)
          .csv / .tsv / .txt  (кодування й роздільник визначаються автоматично)
          .xls           (лише якщо встановлено xlrd; інакше — зрозуміла помилка)
          Google Sheets  (за посиланням: публічне «усі, хто має посилання» —
                          без OAuth; або приватне — через токен, якщо він є)

Усе повертається як список «аркушів»:
    {"name": str, "rows": [[str, ...], ...], "merges": [(r1, c1, r2, c2), ...], "warnings": [str]}
Індекси 0-based, включно. Формули імпортуються як ЗНАЧЕННЯ (кешований результат).
Жодних зовнішніх залежностей, крім requests (вже є в проєкті) для Google.
"""
import csv
import io
import re
import zipfile
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

MAX_ROWS = 20000          # = table_store.MAX_ROWS
MAX_COLS = 702            # = table_store.MAX_COLS
MAX_CELL_CHARS = 4000     # = ліміт set_cells_batch
MAX_SHEETS = 20
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_UNPACKED_BYTES = 120 * 1024 * 1024   # захист від zip-бомб
SUPPORTED = (".xlsx", ".xlsm", ".ods", ".csv", ".tsv", ".txt", ".xls")

_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class TableImportError(ValueError):
    """Помилка з текстом, який можна показати користувачу."""


# ------------------------------------------------------------------ helpers
def _clean(v) -> str:
    s = "" if v is None else str(v)
    s = _ILLEGAL_RE.sub("", s).replace("\r\n", "\n").replace("\r", "\n")
    if s.startswith("="):                     # значення не повинні ставати формулами
        s = "'" + s
    return s[:MAX_CELL_CHARS]


def _fmt_number(text: str) -> str:
    try:
        f = float(text)
    except (TypeError, ValueError):
        return text
    if f != f or f in (float("inf"), float("-inf")):
        return text
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return "%.15g" % f


def _col_from_ref(ref: str):
    m = re.match(r"^([A-Za-z]+)(\d+)$", ref or "")
    if not m:
        return None
    n = 0
    for ch in m.group(1).upper():
        n = n * 26 + (ord(ch) - 64)
    return int(m.group(2)) - 1, n - 1


def _finish(name: str, rows: list, merges: list, warnings: list = None) -> dict:
    """Обрізає хвостові порожні рядки/колонки та ліміти, чистить значення."""
    warnings = list(warnings or [])
    if len(rows) > MAX_ROWS:
        warnings.append(f"Sheet {name!r}: only the first {MAX_ROWS} rows were imported.")
        rows = rows[:MAX_ROWS]
    clean = []
    max_c = 0
    for r in rows:
        r = [_clean(c) for c in r[:MAX_COLS]]
        while r and r[-1] == "":
            r.pop()
        max_c = max(max_c, len(r))
        clean.append(r)
    if any(len(r) > MAX_COLS for r in rows):
        warnings.append(f"Sheet {name!r}: only the first {MAX_COLS} columns were imported.")
    while clean and not clean[-1]:
        clean.pop()
    ok_merges = []
    for (r1, c1, r2, c2) in merges:
        if r1 < len(clean) and c1 < MAX_COLS and r2 < MAX_ROWS and (r2 > r1 or c2 > c1):
            ok_merges.append((r1, c1, min(r2, len(clean) - 1), min(c2, MAX_COLS - 1)))
    return {"name": (name or "Sheet")[:80], "rows": clean, "merges": ok_merges, "warnings": warnings}


def sheet_dims(sheet: dict):
    return len(sheet["rows"]), max((len(r) for r in sheet["rows"]), default=0)


# ------------------------------------------------------------------ CSV / TSV
def _decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1251")          # типовий експорт Excel в українській локалі
    except UnicodeDecodeError:
        return data.decode("latin-1")


def parse_csv(data: bytes, name: str = "Sheet1") -> list:
    text = _decode(data)
    if not text.strip():
        raise TableImportError("The file is empty.")
    sample = text[:20000]
    delim = None
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        pass
    if delim is None:
        first = sample.split("\n", 1)[0]
        delim = max(",;\t|", key=first.count)
    csv.field_size_limit(10 ** 7)
    rows = []
    for i, row in enumerate(csv.reader(io.StringIO(text, newline=""), delimiter=delim)):
        if i >= MAX_ROWS + 1:
            break
        rows.append(row)
    return [_finish(name, rows, [])]


# ------------------------------------------------------------------ XLSX
_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_M = "{%s}" % _NS_MAIN

_BUILTIN_DATE = set(range(14, 18)) | set(range(27, 37)) | set(range(50, 59))
_BUILTIN_TIME = {18, 19, 20, 21, 45, 46, 47}
_BUILTIN_DATETIME = {22}


def _zip_open(data: bytes) -> zipfile.ZipFile:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise TableImportError("The file is corrupted or is not an .xlsx/.ods.")
    if sum(i.file_size for i in z.infolist()) > MAX_UNPACKED_BYTES:
        raise TableImportError("The file is too large once unpacked.")
    return z


def _date_kind(code: str) -> str:
    """'date' | 'time' | 'datetime' | '' за кодом формату числа."""
    c = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]', "", (code or "").lower())
    if not c or c == "general":
        return ""
    has_date = bool(re.search(r"[yd]", c))
    has_time = bool(re.search(r"[hs]", c)) or "am/pm" in c
    if has_date and has_time:
        return "datetime"
    if has_date:
        return "date"
    if has_time:
        return "time"
    return ""


def _xlsx_styles(z: zipfile.ZipFile) -> list:
    """Повертає список виду комірки для кожного індексу xf: '' | date | time | datetime."""
    try:
        root = ET.fromstring(z.read("xl/styles.xml"))
    except KeyError:
        return []
    custom = {}
    nf = root.find(_M + "numFmts")
    if nf is not None:
        for f in nf.findall(_M + "numFmt"):
            try:
                custom[int(f.get("numFmtId"))] = f.get("formatCode") or ""
            except (TypeError, ValueError):
                pass
    kinds = []
    xfs = root.find(_M + "cellXfs")
    if xfs is not None:
        for xf in xfs.findall(_M + "xf"):
            try:
                fid = int(xf.get("numFmtId") or 0)
            except ValueError:
                fid = 0
            if fid in custom:
                kinds.append(_date_kind(custom[fid]))
            elif fid in _BUILTIN_DATETIME:
                kinds.append("datetime")
            elif fid in _BUILTIN_DATE:
                kinds.append("date")
            elif fid in _BUILTIN_TIME:
                kinds.append("time")
            else:
                kinds.append("")
    return kinds


def _serial_to_text(serial: float, kind: str, d1904: bool) -> str:
    try:
        epoch = datetime(1904, 1, 1) if d1904 else datetime(1899, 12, 30)
        dt = epoch + timedelta(days=serial)
        dt += timedelta(seconds=0.5)          # округлення до секунди
        if kind == "time":
            return dt.strftime("%H:%M") if dt.second == 0 else dt.strftime("%H:%M:%S")
        if kind == "date" or (kind == "datetime" and dt.hour == 0 and dt.minute == 0 and dt.second == 0):
            return dt.strftime("%d.%m.%Y")
        return dt.strftime("%d.%m.%Y %H:%M")
    except (OverflowError, ValueError):
        return _fmt_number(str(serial))


def _xlsx_shared_strings(z: zipfile.ZipFile) -> list:
    try:
        f = z.open("xl/sharedStrings.xml")
    except KeyError:
        return []
    out = []
    with f:
        for _, el in ET.iterparse(f, events=("end",)):
            if el.tag == _M + "si":
                parts = []
                for ch in el:
                    if ch.tag == _M + "t":
                        parts.append(ch.text or "")
                    elif ch.tag == _M + "r":
                        t = ch.find(_M + "t")
                        if t is not None:
                            parts.append(t.text or "")
                out.append("".join(parts))
                el.clear()
    return out


def parse_xlsx(data: bytes) -> list:
    z = _zip_open(data)
    if "xl/workbook.xml" not in z.namelist():
        raise TableImportError("This does not look like an .xlsx file (no xl/workbook.xml).")
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    d1904 = False
    pr = wb.find(_M + "workbookPr")
    if pr is not None and (pr.get("date1904") or "").lower() in ("1", "true"):
        d1904 = True

    rels = {}
    try:
        for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")):
            tgt = r.get("Target") or ""
            tgt = tgt.lstrip("/") if tgt.startswith("/") else "xl/" + tgt
            rels[r.get("Id")] = tgt
    except KeyError:
        pass

    shared = _xlsx_shared_strings(z)
    kinds = _xlsx_styles(z)

    sheets = []
    sheets_el = wb.find(_M + "sheets")
    if sheets_el is None:
        raise TableImportError("The file has no sheets.")
    for s in sheets_el.findall(_M + "sheet"):
        if len(sheets) >= MAX_SHEETS:
            break
        if (s.get("state") or "visible") != "visible":
            continue                                   # приховані аркуші пропускаємо
        path = rels.get(s.get("{%s}id" % _NS_REL))
        if not path or path not in z.namelist():
            continue
        sheets.append(_xlsx_sheet(z, s.get("name") or "Sheet", path, shared, kinds, d1904))
    if not sheets:
        raise TableImportError("The file has no visible sheets.")
    return sheets


def _xlsx_sheet(z, name, path, shared, kinds, d1904) -> dict:
    rows = []
    merges = []
    warnings = []
    cut = False
    with z.open(path) as f:
        for _, el in ET.iterparse(f, events=("end",)):
            tag = el.tag
            if tag == _M + "c":
                pos = _col_from_ref(el.get("r"))
                if pos is None:
                    el.clear()
                    continue
                r, c = pos
                if r >= MAX_ROWS or c >= MAX_COLS:
                    cut = True
                    el.clear()
                    continue
                t = el.get("t")
                v_el = el.find(_M + "v")
                v = v_el.text if v_el is not None and v_el.text is not None else None
                val = ""
                if t == "s":
                    try:
                        val = shared[int(v)]
                    except (TypeError, ValueError, IndexError):
                        val = ""
                elif t == "inlineStr":
                    val = "".join((x.text or "") for x in el.iter(_M + "t"))
                elif t == "b":
                    val = "TRUE" if v == "1" else "FALSE"
                elif t in ("str", "e"):
                    val = v or ""
                elif v is not None:                    # число або дата
                    try:
                        sidx = int(el.get("s") or 0)
                    except ValueError:
                        sidx = 0
                    kind = kinds[sidx] if sidx < len(kinds) else ""
                    try:
                        val = _serial_to_text(float(v), kind, d1904) if kind else _fmt_number(v)
                    except ValueError:
                        val = v
                if val != "":
                    while len(rows) <= r:
                        rows.append([])
                    row = rows[r]
                    if len(row) <= c:
                        row.extend([""] * (c + 1 - len(row)))
                    row[c] = val
                el.clear()
            elif tag == _M + "mergeCell":
                ref = el.get("ref") or ""
                if ":" in ref:
                    a, b = ref.split(":", 1)
                    pa, pb = _col_from_ref(a), _col_from_ref(b)
                    if pa and pb:
                        merges.append((min(pa[0], pb[0]), min(pa[1], pb[1]),
                                       max(pa[0], pb[0]), max(pa[1], pb[1])))
    if cut:
        warnings.append(f"Sheet {name!r}: data beyond {MAX_ROWS}×{MAX_COLS} was dropped.")
    return _finish(name, rows, merges, warnings)


# ------------------------------------------------------------------ ODS
_O = {
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
}


def _oq(prefix, local):
    return "{%s}%s" % (_O[prefix], local)


def _ods_cell_text(cell) -> str:
    paras = [("".join(p.itertext())) for p in cell.findall(_oq("text", "p"))]
    if paras:
        return "\n".join(paras)
    vt = cell.get(_oq("office", "value-type"))
    if vt in ("float", "percentage", "currency"):
        return _fmt_number(cell.get(_oq("office", "value")) or "")
    if vt == "date":
        d = cell.get(_oq("office", "date-value")) or ""
        try:
            dt = datetime.fromisoformat(d)
            return dt.strftime("%d.%m.%Y") if "T" not in d else dt.strftime("%d.%m.%Y %H:%M")
        except ValueError:
            return d
    if vt == "boolean":
        return "TRUE" if (cell.get(_oq("office", "boolean-value")) == "true") else "FALSE"
    return cell.get(_oq("office", "string-value")) or ""


def parse_ods(data: bytes) -> list:
    z = _zip_open(data)
    try:
        root = ET.fromstring(z.read("content.xml"))
    except KeyError:
        raise TableImportError("This does not look like an .ods file (no content.xml).")
    sheets = []
    for tbl in root.iter(_oq("table", "table")):
        if len(sheets) >= MAX_SHEETS:
            break
        name = tbl.get(_oq("table", "name")) or "Sheet"
        rows, merges = [], []
        r = 0
        for row in tbl.iter(_oq("table", "table-row")):
            if r >= MAX_ROWS:
                break                                   # усе далі однаково відкидається
            rep_r = int(row.get(_oq("table", "number-rows-repeated")) or 1)
            cells = []
            c = 0
            for cell in row:
                if cell.tag not in (_oq("table", "table-cell"), _oq("table", "covered-table-cell")):
                    continue
                rep_c = int(cell.get(_oq("table", "number-columns-repeated")) or 1)
                text = "" if cell.tag.endswith("covered-table-cell") else _ods_cell_text(cell)
                span_c = int(cell.get(_oq("table", "number-columns-spanned")) or 1)
                span_r = int(cell.get(_oq("table", "number-rows-spanned")) or 1)
                if text == "":
                    c += rep_c
                    continue
                for k in range(min(rep_c, 50)):
                    if c + k < MAX_COLS:
                        cells.extend([""] * (c + k + 1 - len(cells)))
                        cells[c + k] = text
                if (span_c > 1 or span_r > 1) and rep_c == 1:
                    merges.append((r, c, r + span_r - 1, c + span_c - 1))
                c += rep_c
            if not any(cells):
                r += rep_r                              # порожні повтори не роздуваємо
                continue
            for k in range(min(rep_r, 50)):
                while len(rows) < r + k:
                    rows.append([])
                rows.append(list(cells))
            r += rep_r
        sheets.append(_finish(name, rows, merges))
    if not sheets:
        raise TableImportError("The file has no sheets.")
    return sheets


# ------------------------------------------------------------------ XLS (опційно)
def parse_xls(data: bytes) -> list:
    try:
        import xlrd  # noqa
    except ImportError:
        raise TableImportError("The legacy .xls format is not supported on this server. "
                               "Save the file as .xlsx or .csv and upload it again.")
    try:
        book = xlrd.open_workbook(file_contents=data)
    except Exception:
        raise TableImportError("Could not read the .xls file. Save it as .xlsx or .csv.")
    sheets = []
    for sh in book.sheets()[:MAX_SHEETS]:
        rows = []
        for r in range(min(sh.nrows, MAX_ROWS)):
            row = []
            for c in range(min(sh.ncols, MAX_COLS)):
                cell = sh.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        dt = xlrd.xldate_as_datetime(cell.value, book.datemode)
                        row.append(dt.strftime("%d.%m.%Y") if (dt.hour, dt.minute, dt.second) == (0, 0, 0)
                                   else dt.strftime("%d.%m.%Y %H:%M"))
                    except Exception:
                        row.append(str(cell.value))
                elif cell.ctype == xlrd.XL_CELL_NUMBER:
                    row.append(_fmt_number(str(cell.value)))
                elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                    row.append("TRUE" if cell.value else "FALSE")
                else:
                    row.append(str(cell.value))
            rows.append(row)
        merges = [(a, c, b - 1, d - 1) for (a, b, c, d) in getattr(sh, "merged_cells", [])]
        sheets.append(_finish(sh.name, rows, merges))
    return sheets


# ------------------------------------------------------------------ dispatcher
def parse_file(filename: str, data: bytes) -> list:
    if not data:
        raise TableImportError("The file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise TableImportError(f"The file is too large ({MAX_FILE_BYTES // 1024 // 1024} MB max).")
    fn = (filename or "").lower().strip()
    stem = re.sub(r"\.[^.]+$", "", fn) or "Sheet1"
    ext = fn[fn.rfind("."):] if "." in fn else ""
    # визначаємо за вмістом, якщо розширення брехливе
    if data[:2] == b"PK":
        try:
            names = _zip_open(data).namelist()
        except TableImportError:
            raise
        if "xl/workbook.xml" in names:
            return parse_xlsx(data)
        if "content.xml" in names:
            return parse_ods(data)
        raise TableImportError("Unknown archive format. Supported: .xlsx, .ods, .csv, .tsv.")
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return parse_xls(data)
    if ext in (".csv", ".tsv", ".txt", ""):
        return parse_csv(data, name=(filename or "Sheet1").rsplit(".", 1)[0][:80] or "Sheet1")
    if ext in (".xlsx", ".xlsm", ".ods"):
        raise TableImportError("The file is corrupted or has the wrong extension.")
    raise TableImportError("Unsupported format. Supported: .xlsx, .xlsm, .ods, .csv, .tsv, .txt "
                           "(for .xls, save as .xlsx first).")


# ------------------------------------------------------------------ Google Sheets
_GS_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]{20,})")
_GS_BARE_RE = re.compile(r"^[a-zA-Z0-9_-]{25,}$")


def extract_spreadsheet_id(url_or_id: str):
    s = (url_or_id or "").strip()
    m = _GS_ID_RE.search(s)
    if m:
        return m.group(1)
    if _GS_BARE_RE.match(s):
        return s
    return None


def _read_capped(resp, limit=MAX_FILE_BYTES) -> bytes:
    buf = b""
    for chunk in resp.iter_content(65536):
        buf += chunk
        if len(buf) > limit:
            raise TableImportError("The spreadsheet is too large to import.")
    return buf


def _google_public(sid: str):
    """Експорт публічної (за посиланням) таблиці в xlsx. None, якщо доступу нема."""
    import requests
    try:
        r = requests.get(f"https://docs.google.com/spreadsheets/d/{sid}/export",
                         params={"format": "xlsx"}, timeout=30, stream=True,
                         headers={"User-Agent": "Mozilla/5.0 (Spiklio import)"})
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "html" in ctype:
        return None                                    # сторінка логіну
    body = _read_capped(r)
    if body[:2] != b"PK":
        return None
    return parse_xlsx(body)


def _google_api(sid: str, refresh_token: str):
    """Читання через Sheets API (працює для таблиць, обраних клієнтом через Picker)."""
    import requests
    import google_sheets
    token = google_sheets.refresh_access_token(refresh_token)
    h = {"Authorization": f"Bearer {token}"}
    meta = requests.get(f"{google_sheets.SHEETS_API}/{sid}",
                        params={"fields": "sheets(properties(title,hidden,sheetType),merges)"},
                        headers=h, timeout=20)
    meta.raise_for_status()
    sheets = []
    for sh in (meta.json().get("sheets") or []):
        if len(sheets) >= MAX_SHEETS:
            break
        p = sh.get("properties") or {}
        if p.get("hidden") or (p.get("sheetType") or "GRID") != "GRID":
            continue
        title = p.get("title") or "Sheet"
        quoted = "'" + title.replace("'", "''") + "'"
        rows = google_sheets._values_get(token, sid, f"{quoted}!A1:ZZ{MAX_ROWS}")
        merges = []
        for m in (sh.get("merges") or []):
            merges.append((m.get("startRowIndex", 0), m.get("startColumnIndex", 0),
                           m.get("endRowIndex", 1) - 1, m.get("endColumnIndex", 1) - 1))
        sheets.append(_finish(title, rows, merges))
    if not sheets:
        raise TableImportError("The spreadsheet has no sheets.")
    return sheets


def fetch_google(url_or_id: str, refresh_tokens=None) -> list:
    sid = extract_spreadsheet_id(url_or_id)
    if not sid:
        raise TableImportError("This does not look like a Google Sheets link "
                               "(expected https://docs.google.com/spreadsheets/d/…).")
    pub = _google_public(sid)
    if pub:
        return pub
    for tok in (refresh_tokens or []):
        try:
            return _google_api(sid, tok)
        except TableImportError:
            raise
        except Exception:
            continue
    raise TableImportError(
        "No access to this spreadsheet. In Google Sheets open Share and set general access to "
        "'Anyone with the link' as Viewer, then try again.")


# ------------------------------------------------------------------ preview
def preview(sheet: dict, n_rows: int = 8, n_cols: int = 12) -> dict:
    r, c = sheet_dims(sheet)
    return {"name": sheet["name"], "rows": r, "cols": c,
            "merges": len(sheet["merges"]), "warnings": sheet["warnings"],
            "preview": [row[:n_cols] + [""] * (min(c, n_cols) - len(row[:n_cols]))
                        for row in sheet["rows"][:n_rows]]}
