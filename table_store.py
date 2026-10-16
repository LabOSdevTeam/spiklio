"""
table_store.py — Excel-подібна клітинкова таблиця + скомпільовані правила.

Модель:
  user_tables          — таблиця (id, name, is_default)
  user_table_columns   — колонки з width (px), col_index
  user_table_rows      — рядки з height (px), row_index
  user_table_cells     — клітинки (row_id × column_id) зі значенням
  user_table_merges    — об'єднані діапазони
  user_table_rules     — скомпільовані LLM правила запису
"""
import json
import re
import uuid
from datetime import datetime, timezone

from storage import _BaseSQLite, _now

_COL_RE = re.compile(r"^([A-Z]+)(\d+)$")

DEFAULT_COL_WIDTH = 100
DEFAULT_ROW_HEIGHT = 25
MAX_COLS = 702      # A..ZZ
MAX_ROWS = 20000


def _safe(v) -> str:
    """Значення від AI/клієнтів не повинні ставати формулами: '=...' -> текст."""
    s = str(v).strip()
    return "'" + s if s.startswith("=") else s


def col_letter_to_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        if not ("A" <= ch <= "Z"):
            raise ValueError(f"bad column letter: {ch!r}")
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def col_index_to_letter(idx: int) -> str:
    if idx < 0:
        raise ValueError("negative index")
    out = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def parse_a1(addr: str):
    m = _COL_RE.match((addr or "").strip().upper())
    if not m:
        raise ValueError(f"bad A1 address: {addr!r}")
    return int(m.group(2)) - 1, col_letter_to_index(m.group(1))


def parse_range(a1: str):
    a1 = (a1 or "").strip().upper()
    if ":" in a1:
        left, right = a1.split(":", 1)
        r1, c1 = parse_a1(left)
        r2, c2 = parse_a1(right)
    else:
        r1, c1 = parse_a1(a1)
        r2, c2 = r1, c1
    return (min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2))


class TableStore(_BaseSQLite):
    # Колонки, які ОБОВ'ЯЗКОВО мають бути в таблицях нової (клітинкової) схеми.
    _REQUIRED_COLS = {
        "user_table_columns": {"user_id", "table_id", "column_id", "col_index", "label", "width", "created_at"},
        "user_table_rows":    {"user_id", "table_id", "row_id", "row_index", "height", "created_at"},
        "user_table_cells":   {"user_id", "table_id", "row_id", "column_id", "value", "updated_at"},
        "user_table_merges":  {"user_id", "table_id", "merge_id", "r1", "c1", "r2", "c2", "created_at"},
    }

    def _migrate_legacy_schema(self, conn):
        """CREATE TABLE IF NOT EXISTS не змінює вже існуючі таблиці. Якщо в БД лишилась
        стара схема (до переходу на клітинкову модель) - list_columns() падає з
        'no such column', /snapshot віддає 500 і UI висить на Loading.
        Стару таблицю НЕ видаляємо, а перейменовуємо в <name>_legacy_<ts> (бекап),
        нову створює наступний CREATE TABLE IF NOT EXISTS."""
        import time as _t
        renamed = False
        for tbl, need in self._REQUIRED_COLS.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
            if not have or need.issubset(have):
                continue
            backup = f"{tbl}_legacy_{int(_t.time())}"
            conn.execute(f"ALTER TABLE {tbl} RENAME TO {backup}")
            renamed = True
            print(f"[table_store] legacy schema in {tbl} (missing: {sorted(need - have)}) "
                  f"-> renamed to {backup}", flush=True)
        if renamed:
            # індекси лишаються прив'язаними до перейменованих таблиць - скидаємо,
            # щоб CREATE INDEX IF NOT EXISTS створив їх заново на нових таблицях
            conn.execute("DROP INDEX IF EXISTS ix_utc_cells")
            conn.execute("DROP INDEX IF EXISTS ix_utc_merges")

    def _init_db(self):
        with self._connect() as conn:
            self._migrate_legacy_schema(conn)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_tables (
                    table_id   TEXT NOT NULL,
                    user_id    INTEGER NOT NULL,
                    name       TEXT NOT NULL,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, table_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_table_columns (
                    user_id    INTEGER NOT NULL,
                    table_id   TEXT NOT NULL,
                    column_id  TEXT NOT NULL,
                    col_index  INTEGER NOT NULL,
                    label      TEXT NOT NULL DEFAULT '',
                    width      INTEGER NOT NULL DEFAULT 140,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, table_id, column_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_table_rows (
                    user_id    INTEGER NOT NULL,
                    table_id   TEXT NOT NULL,
                    row_id     TEXT NOT NULL,
                    row_index  INTEGER NOT NULL,
                    height     INTEGER NOT NULL DEFAULT 32,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, table_id, row_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_table_cells (
                    user_id    INTEGER NOT NULL,
                    table_id   TEXT NOT NULL,
                    row_id     TEXT NOT NULL,
                    column_id  TEXT NOT NULL,
                    value      TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, table_id, row_id, column_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_table_merges (
                    user_id    INTEGER NOT NULL,
                    table_id   TEXT NOT NULL,
                    merge_id   TEXT NOT NULL,
                    r1         INTEGER NOT NULL,
                    c1         INTEGER NOT NULL,
                    r2         INTEGER NOT NULL,
                    c2         INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, table_id, merge_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_table_rules (
                    user_id     INTEGER NOT NULL,
                    table_id    TEXT NOT NULL,
                    rules_json  TEXT NOT NULL,
                    source_text TEXT NOT NULL DEFAULT '',
                    compiled_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, table_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_table_cell_styles (
                    user_id    INTEGER NOT NULL,
                    table_id   TEXT NOT NULL,
                    row_id     TEXT NOT NULL,
                    column_id  TEXT NOT NULL,
                    style_json TEXT NOT NULL,
                    PRIMARY KEY (user_id, table_id, row_id, column_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_table_row_refs (
                    user_id    INTEGER NOT NULL,
                    table_id   TEXT NOT NULL,
                    client_ref TEXT NOT NULL,
                    row_id     TEXT NOT NULL,
                    PRIMARY KEY (user_id, table_id, client_ref)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS ix_utc_cells ON user_table_cells(user_id, table_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_utc_merges ON user_table_merges(user_id, table_id)")

    # ---------- TABLES ----------
    def list_tables(self, user_id: int) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT table_id, name, is_default, created_at, updated_at "
                "FROM user_tables WHERE user_id = ? "
                "ORDER BY is_default DESC, created_at ASC",
                (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_table(self, user_id: int, table_id: str):
        with self._connect() as conn:
            r = conn.execute(
                "SELECT table_id, name, is_default, created_at, updated_at "
                "FROM user_tables WHERE user_id = ? AND table_id = ?",
                (user_id, table_id)).fetchone()
        return dict(r) if r else None

    def create_table(self, user_id: int, name: str, is_default: bool = False) -> dict:
        name = (name or "").strip()[:120]
        if not name:
            raise ValueError("name required")
        tid = uuid.uuid4().hex[:12]
        with self._lock, self._connect() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM user_tables WHERE user_id = ?",
                             (user_id,)).fetchone()["n"]
            if n == 0:
                is_default = True
            if is_default:
                conn.execute("UPDATE user_tables SET is_default = 0 WHERE user_id = ?", (user_id,))
            now = _now()
            conn.execute(
                "INSERT INTO user_tables (table_id, user_id, name, is_default, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (tid, user_id, name, 1 if is_default else 0, now, now))
            self._ensure_grid(conn, user_id, tid, n_cols=26, n_rows=1000)
        return {"table_id": tid, "name": name, "is_default": is_default,
                "created_at": now, "updated_at": now}

    def _ensure_grid(self, conn, user_id, table_id, n_cols=None, n_rows=None):
        if n_cols is not None:
            have = {r["column_id"] for r in conn.execute(
                "SELECT column_id FROM user_table_columns WHERE user_id=? AND table_id=?",
                (user_id, table_id)).fetchall()}
            for i in range(min(n_cols, MAX_COLS)):
                cid = col_index_to_letter(i)
                if cid not in have:
                    conn.execute(
                        "INSERT INTO user_table_columns "
                        "(user_id, table_id, column_id, col_index, label, width, created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (user_id, table_id, cid, i, "", DEFAULT_COL_WIDTH, _now()))
        if n_rows is not None:
            have = {r["row_id"] for r in conn.execute(
                "SELECT row_id FROM user_table_rows WHERE user_id=? AND table_id=?",
                (user_id, table_id)).fetchall()}
            for i in range(min(n_rows, MAX_ROWS)):
                rid = str(i + 1)
                if rid not in have:
                    conn.execute(
                        "INSERT INTO user_table_rows "
                        "(user_id, table_id, row_id, row_index, height, created_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (user_id, table_id, rid, i, DEFAULT_ROW_HEIGHT, _now()))

    def rename_table(self, user_id: int, table_id: str, new_name: str) -> bool:
        new_name = (new_name or "").strip()[:120]
        if not new_name:
            return False
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE user_tables SET name=?, updated_at=? WHERE user_id=? AND table_id=?",
                (new_name, _now(), user_id, table_id))
            return cur.rowcount > 0

    def delete_table(self, user_id: int, table_id: str) -> bool:
        with self._lock, self._connect() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM user_tables WHERE user_id=?",
                             (user_id,)).fetchone()["n"]
            if n <= 1:
                return False
            t = conn.execute(
                "SELECT is_default FROM user_tables WHERE user_id=? AND table_id=?",
                (user_id, table_id)).fetchone()
            if not t:
                return False
            cur = conn.execute("DELETE FROM user_tables WHERE user_id=? AND table_id=?",
                               (user_id, table_id))
            if cur.rowcount == 0:
                return False
            for tbl in ("user_table_columns", "user_table_rows",
                        "user_table_cells", "user_table_merges", "user_table_rules",
                        "user_table_cell_styles", "user_table_row_refs"):
                conn.execute(f"DELETE FROM {tbl} WHERE user_id=? AND table_id=?",
                             (user_id, table_id))
            if t["is_default"]:
                first = conn.execute(
                    "SELECT table_id FROM user_tables WHERE user_id=? ORDER BY created_at LIMIT 1",
                    (user_id,)).fetchone()
                if first:
                    conn.execute(
                        "UPDATE user_tables SET is_default=1 WHERE user_id=? AND table_id=?",
                        (user_id, first["table_id"]))
            return True

    def set_default_table(self, user_id: int, table_id: str) -> bool:
        with self._lock, self._connect() as conn:
            if not conn.execute("SELECT 1 FROM user_tables WHERE user_id=? AND table_id=?",
                                (user_id, table_id)).fetchone():
                return False
            conn.execute("UPDATE user_tables SET is_default=0 WHERE user_id=?", (user_id,))
            conn.execute(
                "UPDATE user_tables SET is_default=1, updated_at=? WHERE user_id=? AND table_id=?",
                (_now(), user_id, table_id))
            return True

    def get_default_table(self, user_id: int):
        with self._connect() as conn:
            r = conn.execute(
                "SELECT table_id, name FROM user_tables WHERE user_id=? AND is_default=1 LIMIT 1",
                (user_id,)).fetchone()
            if not r:
                r = conn.execute(
                    "SELECT table_id, name FROM user_tables WHERE user_id=? ORDER BY created_at LIMIT 1",
                    (user_id,)).fetchone()
        return dict(r) if r else None

    # ---------- COLUMNS ----------
    def list_columns(self, user_id: int, table_id: str) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT column_id, col_index, label, width FROM user_table_columns "
                "WHERE user_id=? AND table_id=? ORDER BY col_index",
                (user_id, table_id)).fetchall()
        return [dict(r) for r in rows]

    def set_column_width(self, user_id: int, table_id: str, column_id: str, width: int) -> bool:
        width = max(40, min(600, int(width)))
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE user_table_columns SET width=? WHERE user_id=? AND table_id=? AND column_id=?",
                (width, user_id, table_id, column_id))
            return cur.rowcount > 0

    def set_column_label(self, user_id: int, table_id: str, column_id: str, label: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE user_table_columns SET label=? WHERE user_id=? AND table_id=? AND column_id=?",
                (label[:120], user_id, table_id, column_id))
            return cur.rowcount > 0

    def add_columns(self, user_id: int, table_id: str, count: int = 1) -> list:
        added = []
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(col_index), -1) AS m FROM user_table_columns "
                "WHERE user_id=? AND table_id=?",
                (user_id, table_id)).fetchone()
            start = row["m"] + 1
            for k in range(count):
                if start + k >= MAX_COLS:
                    break
                cid = col_index_to_letter(start + k)
                conn.execute(
                    "INSERT OR IGNORE INTO user_table_columns "
                    "(user_id, table_id, column_id, col_index, label, width, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (user_id, table_id, cid, start + k, "", DEFAULT_COL_WIDTH, _now()))
                added.append(cid)
        return added

    # ---------- ROWS ----------
    def list_rows(self, user_id: int, table_id: str) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT row_id, row_index, height FROM user_table_rows "
                "WHERE user_id=? AND table_id=? ORDER BY row_index",
                (user_id, table_id)).fetchall()
        return [dict(r) for r in rows]

    def set_row_height(self, user_id: int, table_id: str, row_id: str, height: int) -> bool:
        height = max(20, min(400, int(height)))
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE user_table_rows SET height=? WHERE user_id=? AND table_id=? AND row_id=?",
                (height, user_id, table_id, row_id))
            return cur.rowcount > 0

    def add_rows(self, user_id: int, table_id: str, count: int = 1) -> list:
        added = []
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(row_index), -1) AS m FROM user_table_rows "
                "WHERE user_id=? AND table_id=?",
                (user_id, table_id)).fetchone()
            start = row["m"] + 1
            for k in range(count):
                if start + k >= MAX_ROWS:
                    break
                rid = str(start + k + 1)
                conn.execute(
                    "INSERT OR IGNORE INTO user_table_rows "
                    "(user_id, table_id, row_id, row_index, height, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (user_id, table_id, rid, start + k, DEFAULT_ROW_HEIGHT, _now()))
                added.append(rid)
        return added

    # ---------- CELLS ----------
    def _cell_map(self, conn, user_id, table_id) -> dict:
        rows = conn.execute(
            "SELECT row_id, column_id, value FROM user_table_cells "
            "WHERE user_id=? AND table_id=?",
            (user_id, table_id)).fetchall()
        return {(r["row_id"], r["column_id"]): r["value"] for r in rows}

    def get_cells(self, user_id: int, table_id: str, r1=None, c1=None, r2=None, c2=None) -> dict:
        with self._connect() as conn:
            cols = {r["column_id"]: r["col_index"] for r in conn.execute(
                "SELECT column_id, col_index FROM user_table_columns WHERE user_id=? AND table_id=?",
                (user_id, table_id)).fetchall()}
            rows = {r["row_id"]: r["row_index"] for r in conn.execute(
                "SELECT row_id, row_index FROM user_table_rows WHERE user_id=? AND table_id=?",
                (user_id, table_id)).fetchall()}
            cm = self._cell_map(conn, user_id, table_id)
        out = {}
        for (rid, cid), val in cm.items():
            if rid not in rows or cid not in cols:
                continue
            ri, ci = rows[rid], cols[cid]
            if r1 is not None and not (r1 <= ri <= r2):
                continue
            if c1 is not None and not (c1 <= ci <= c2):
                continue
            out[f"{cid}{rid}"] = val
        return out

    def get_cell(self, user_id: int, table_id: str, row_id: str, column_id: str) -> str:
        with self._connect() as conn:
            r = conn.execute(
                "SELECT value FROM user_table_cells "
                "WHERE user_id=? AND table_id=? AND row_id=? AND column_id=?",
                (user_id, table_id, row_id, column_id)).fetchone()
        return r["value"] if r else ""

    def set_cell(self, user_id: int, table_id: str, row_id: str, column_id: str, value) -> bool:
        value = "" if value is None else str(value)[:4000]
        with self._lock, self._connect() as conn:
            if not conn.execute(
                    "SELECT 1 FROM user_table_rows WHERE user_id=? AND table_id=? AND row_id=?",
                    (user_id, table_id, row_id)).fetchone():
                raise ValueError(f"row {row_id!r} does not exist")
            if not conn.execute(
                    "SELECT 1 FROM user_table_columns WHERE user_id=? AND table_id=? AND column_id=?",
                    (user_id, table_id, column_id)).fetchone():
                raise ValueError(f"column {column_id!r} does not exist")
            conn.execute(
                "INSERT INTO user_table_cells (user_id, table_id, row_id, column_id, value, updated_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(user_id, table_id, row_id, column_id) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (user_id, table_id, row_id, column_id, value, _now()))
            conn.execute("UPDATE user_tables SET updated_at=? WHERE user_id=? AND table_id=?",
                         (_now(), user_id, table_id))
        return True

    def set_cell_by_a1(self, user_id: int, table_id: str, a1: str, value) -> dict:
        r, c = parse_a1(a1)
        if r >= MAX_ROWS or c >= MAX_COLS:
            raise ValueError(f"address {a1} out of bounds")
        row_id = str(r + 1)
        column_id = col_index_to_letter(c)
        with self._lock, self._connect() as conn:
            self._ensure_grid(conn, user_id, table_id, n_cols=c + 1, n_rows=r + 1)
        self.set_cell(user_id, table_id, row_id, column_id, value)
        return {"row_id": row_id, "column_id": column_id, "a1": f"{column_id}{row_id}"}

    def set_range_by_a1(self, user_id: int, table_id: str, a1_range: str, rows_values: list) -> int:
        r1, c1, r2, c2 = parse_range(a1_range)
        if r2 >= MAX_ROWS or c2 >= MAX_COLS:
            raise ValueError("range out of bounds")
        with self._lock, self._connect() as conn:
            self._ensure_grid(conn, user_id, table_id, n_cols=c2 + 1, n_rows=r2 + 1)
        written = 0
        for dr, row_vals in enumerate(rows_values or []):
            if r1 + dr > r2:
                break
            if not isinstance(row_vals, (list, tuple)):
                row_vals = [row_vals]
            for dc, val in enumerate(row_vals):
                if c1 + dc > c2:
                    break
                self.set_cell(user_id, table_id, str(r1 + dr + 1),
                              col_index_to_letter(c1 + dc), val)
                written += 1
        return written

    # ---------- MERGES ----------
    def list_merges(self, user_id: int, table_id: str) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT merge_id, r1, c1, r2, c2 FROM user_table_merges "
                "WHERE user_id=? AND table_id=?",
                (user_id, table_id)).fetchall()
        return [dict(r) for r in rows]

    def _overlaps(self, conn, user_id, table_id, r1, c1, r2, c2):
        for m in conn.execute(
                "SELECT r1,c1,r2,c2 FROM user_table_merges WHERE user_id=? AND table_id=?",
                (user_id, table_id)).fetchall():
            if not (r2 < m["r1"] or r1 > m["r2"] or c2 < m["c1"] or c1 > m["c2"]):
                return True
        return False

    def add_merge(self, user_id: int, table_id: str, a1_range: str) -> dict:
        r1, c1, r2, c2 = parse_range(a1_range)
        if r1 == r2 and c1 == c2:
            raise ValueError("range must cover at least 2 cells")
        mid = uuid.uuid4().hex[:12]
        with self._lock, self._connect() as conn:
            if self._overlaps(conn, user_id, table_id, r1, c1, r2, c2):
                raise ValueError("range overlaps an existing merge")
            conn.execute(
                "INSERT INTO user_table_merges "
                "(user_id, table_id, merge_id, r1, c1, r2, c2, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (user_id, table_id, mid, r1, c1, r2, c2, _now()))
        return {"merge_id": mid, "r1": r1, "c1": c1, "r2": r2, "c2": c2}

    def remove_merge(self, user_id: int, table_id: str, merge_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM user_table_merges WHERE user_id=? AND table_id=? AND merge_id=?",
                (user_id, table_id, merge_id))
            return cur.rowcount > 0

    # ---------- SNAPSHOT ----------
    def snapshot(self, user_id: int, table_id: str) -> dict:
        self._migrate_legacy_rows(user_id, table_id)
        cols = self.list_columns(user_id, table_id)
        if not cols:
            # таблиця є в user_tables, але сітки нема (напр. після міграції старої схеми)
            with self._lock, self._connect() as conn:
                self._ensure_grid(conn, user_id, table_id, n_cols=5, n_rows=20)
            cols = self.list_columns(user_id, table_id)
        rows = self.list_rows(user_id, table_id)
        cells = self.get_cells(user_id, table_id)
        merges = self.list_merges(user_id, table_id)
        rules = self.get_rules(user_id, table_id)
        styles = self.get_styles(user_id, table_id)
        return {"columns": cols, "rows": rows, "cells": cells, "merges": merges,
                "rules": rules, "styles": styles}

    # ---------- STYLES + BATCH (Google Sheets-подібний UI) ----------
    def get_styles(self, user_id: int, table_id: str) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT row_id, column_id, style_json FROM user_table_cell_styles "
                "WHERE user_id=? AND table_id=?", (user_id, table_id)).fetchall()
        out = {}
        for r in rows:
            try:
                out[f"{r['column_id']}{r['row_id']}"] = json.loads(r["style_json"])
            except Exception:
                pass
        return out

    def set_cells_batch(self, user_id: int, table_id: str, cells: dict, styles: dict) -> int:
        """Атомарно пише багато клітинок і стилів. cells: {A1: value},
        styles: {A1: {..}|None}. Сітка розширюється автоматично."""
        cells, styles = cells or {}, styles or {}
        if len(cells) + len(styles) > 5000:
            raise ValueError("too many cells in one batch")
        parsed = {}
        for a1 in set(cells) | set(styles):
            r, c = parse_a1(a1)
            if r >= MAX_ROWS or c >= MAX_COLS:
                raise ValueError(f"address {a1} out of bounds")
            parsed[a1.strip().upper()] = (str(r + 1), col_index_to_letter(c), r, c)
        if not parsed:
            return 0
        max_r = max(p[2] for p in parsed.values())
        max_c = max(p[3] for p in parsed.values())
        now = _now()
        with self._lock, self._connect() as conn:
            self._ensure_grid(conn, user_id, table_id, n_cols=max_c + 1, n_rows=max_r + 1)
            for a1, v in cells.items():
                rid, cid, _, _ = parsed[a1.strip().upper()]
                v = "" if v is None else str(v)[:4000]
                conn.execute(
                    "INSERT INTO user_table_cells (user_id, table_id, row_id, column_id, value, updated_at) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(user_id, table_id, row_id, column_id) "
                    "DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (user_id, table_id, rid, cid, v, now))
            for a1, st in styles.items():
                rid, cid, _, _ = parsed[a1.strip().upper()]
                if not st:
                    conn.execute(
                        "DELETE FROM user_table_cell_styles WHERE user_id=? AND table_id=? AND row_id=? AND column_id=?",
                        (user_id, table_id, rid, cid))
                else:
                    conn.execute(
                        "INSERT INTO user_table_cell_styles (user_id, table_id, row_id, column_id, style_json) "
                        "VALUES (?,?,?,?,?) ON CONFLICT(user_id, table_id, row_id, column_id) "
                        "DO UPDATE SET style_json=excluded.style_json",
                        (user_id, table_id, rid, cid, json.dumps(st, ensure_ascii=False)[:1000]))
            conn.execute("UPDATE user_tables SET updated_at=? WHERE user_id=? AND table_id=?",
                         (now, user_id, table_id))
        return len(parsed)

    # ---------- BULK IMPORT (xlsx / csv / Google Sheets) ----------
    def import_grid(self, user_id: int, table_id: str, rows: list, merges=None,
                    mode: str = "replace", skip_header: bool = False) -> dict:
        """Масовий запис готової сітки значень в ОДНІЙ транзакції.
        mode="replace" — спершу повністю очищає таблицю (клітинки, стилі, об'єднання, зв'язки клієнтів);
        mode="append"  — дописує нижче останнього рядка з даними (і не вище data_start_row з правил);
        skip_header    — для append: пропустити перший рядок імпортованого файлу (його шапку).
        Правила запису (user_table_rules) не чіпаємо."""
        if mode not in ("replace", "append"):
            raise ValueError("bad mode")
        rows = [list(r) for r in (rows or [])]
        merges = list(merges or [])
        if skip_header and rows:
            rows = rows[1:]
            merges = [(a - 1, b, c - 1, d) for (a, b, c, d) in merges if a >= 1]
        if not rows:
            raise ValueError("nothing to import")
        n_cols = max((len(r) for r in rows), default=0)
        if n_cols > MAX_COLS:
            raise ValueError(f"too many columns (max {MAX_COLS})")
        now = _now()
        with self._lock, self._connect() as conn:
            if mode == "replace":
                for tbl in ("user_table_cells", "user_table_cell_styles",
                            "user_table_merges", "user_table_row_refs"):
                    conn.execute(f"DELETE FROM {tbl} WHERE user_id=? AND table_id=?", (user_id, table_id))
                offset = 0
            else:
                last = conn.execute(
                    "SELECT MAX(CAST(row_id AS INTEGER)) AS m FROM user_table_cells "
                    "WHERE user_id=? AND table_id=? AND value != ''", (user_id, table_id)).fetchone()["m"] or 0
                offset = last
                rr = conn.execute("SELECT rules_json FROM user_table_rules WHERE user_id=? AND table_id=?",
                                  (user_id, table_id)).fetchone()
                if rr:
                    try:
                        ds = int((json.loads(rr["rules_json"]) or {}).get("data_start_row") or 0)
                        offset = max(offset, ds - 1)
                    except Exception:
                        pass
            if offset + len(rows) > MAX_ROWS:
                raise ValueError(f"table would exceed {MAX_ROWS} rows")
            self._ensure_grid(conn, user_id, table_id, n_cols=max(n_cols, 1), n_rows=offset + len(rows))
            batch = []
            for i, row in enumerate(rows):
                rid = str(offset + i + 1)
                for j, v in enumerate(row):
                    if v is None or str(v) == "":
                        continue
                    batch.append((user_id, table_id, rid, col_index_to_letter(j), str(v)[:4000], now))
            conn.executemany(
                "INSERT INTO user_table_cells (user_id, table_id, row_id, column_id, value, updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(user_id, table_id, row_id, column_id) "
                "DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", batch)
            n_merges = 0
            for (r1, c1, r2, c2) in merges:
                r1, r2 = r1 + offset, r2 + offset
                if r1 < 0 or c1 < 0 or r2 >= MAX_ROWS or c2 >= MAX_COLS or (r1 == r2 and c1 == c2):
                    continue
                if self._overlaps(conn, user_id, table_id, r1, c1, r2, c2):
                    continue
                conn.execute(
                    "INSERT INTO user_table_merges (user_id, table_id, merge_id, r1, c1, r2, c2, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (user_id, table_id, uuid.uuid4().hex[:12], r1, c1, r2, c2, now))
                n_merges += 1
            conn.execute("UPDATE user_tables SET updated_at=? WHERE user_id=? AND table_id=?",
                         (now, user_id, table_id))
        return {"rows": len(rows), "cols": n_cols, "cells": len(batch),
                "merges": n_merges, "start_row": offset + 1}

    # ---------- COMPILED RULES ----------
    def get_rules(self, user_id: int, table_id: str):
        with self._connect() as conn:
            r = conn.execute(
                "SELECT rules_json, source_text, compiled_at "
                "FROM user_table_rules WHERE user_id=? AND table_id=?",
                (user_id, table_id)).fetchone()
        if not r:
            return None
        try:
            data = json.loads(r["rules_json"])
        except Exception:
            return None
        data["_source_text"] = r["source_text"]
        data["_compiled_at"] = r["compiled_at"]
        return data

    def save_rules(self, user_id: int, table_id: str, rules: dict, source_text: str):
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO user_table_rules
                    (user_id, table_id, rules_json, source_text, compiled_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, table_id) DO UPDATE SET
                    rules_json = excluded.rules_json,
                    source_text = excluded.source_text,
                    compiled_at = excluded.compiled_at
            """, (user_id, table_id,
                  json.dumps(rules, ensure_ascii=False),
                  source_text[:4000],
                  _now()))

    def delete_rules(self, user_id: int, table_id: str):
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM user_table_rules WHERE user_id=? AND table_id=?",
                (user_id, table_id))

    # ---------- AI-орієнтовані ----------
    def write_by_rules(self, user_id: int, table_id: str,
                       data: dict, client_ref: str = "") -> dict:
        rules = self.get_rules(user_id, table_id)
        if not rules:
            raise ValueError("no compiled rules for this table")

        headers_row = int(rules.get("headers_row") or 1)
        data_start_row = int(rules.get("data_start_row") or (headers_row + 1))
        columns = rules.get("columns") or {}
        fixed = rules.get("fixed_cells") or {}

        # Рядок клієнта тримаємо в окремій таблиці зв'язків (а не в колонці A),
        # щоб усі колонки таблиці були повністю під контролем власника.
        row_id = None
        with self._connect() as conn:
            if client_ref:
                r = conn.execute(
                    "SELECT row_id FROM user_table_row_refs WHERE user_id=? AND table_id=? AND client_ref=?",
                    (user_id, table_id, client_ref)).fetchone()
                if r:
                    row_id = r["row_id"]
                    # Зв'язок дійсний лише поки в рядку є дані. Якщо власник очистив
                    # рядок вручну — зв'язок застарів: прибираємо і шукаємо вільний рядок.
                    has = conn.execute(
                        "SELECT 1 FROM user_table_cells WHERE user_id=? AND table_id=? "
                        "AND row_id=? AND value != '' LIMIT 1",
                        (user_id, table_id, row_id)).fetchone()
                    if not has:
                        with self._lock:
                            conn.execute(
                                "DELETE FROM user_table_row_refs "
                                "WHERE user_id=? AND table_id=? AND client_ref=?",
                                (user_id, table_id, client_ref))
                        row_id = None
            if row_id is None:
                # «Зайнятий» = у рядку є дані. Самі зв'язки (refs) зайнятість не задають,
                # інакше після ручного очищення запис не повертається в рядок 2.
                taken = {r["row_id"] for r in conn.execute(
                    "SELECT DISTINCT row_id FROM user_table_cells WHERE user_id=? AND table_id=? AND value != ''",
                    (user_id, table_id)).fetchall()}
                i = data_start_row
                while str(i) in taken:
                    i += 1
                if i > MAX_ROWS:
                    raise ValueError("table is full")
                row_id = str(i)
                if client_ref:
                    with self._lock:
                        conn.execute(
                            "INSERT OR REPLACE INTO user_table_row_refs (user_id, table_id, client_ref, row_id) "
                            "VALUES (?,?,?,?)", (user_id, table_id, client_ref, row_id))

        written = {}
        for field, value in (data or {}).items():
            if value is None or str(value).strip() == "":
                continue
            key = str(field).strip().lower()
            val = _safe(value)
            if key in fixed:
                a1 = fixed[key]
                self.set_cell_by_a1(user_id, table_id, a1, val)
                written[a1] = val
                continue
            col = columns.get(key)
            if col:
                a1 = f"{col}{row_id}"
                self.set_cell_by_a1(user_id, table_id, a1, val)
                written[a1] = val

        return {"row_id": row_id, "cells": written}

    def append_row_by_labels(self, user_id: int, table_id: str, data: dict) -> dict:
        with self._lock, self._connect() as conn:
            self._ensure_grid(conn, user_id, table_id, n_cols=1, n_rows=1)
            max_row = conn.execute(
                "SELECT COALESCE(MAX(row_index), -1) AS m FROM user_table_rows "
                "WHERE user_id=? AND table_id=?",
                (user_id, table_id)).fetchone()["m"]
            occupied = set()
            for r in conn.execute(
                    "SELECT DISTINCT row_id FROM user_table_cells "
                    "WHERE user_id=? AND table_id=? AND value != ''",
                    (user_id, table_id)).fetchall():
                occupied.add(r["row_id"])
            target_row_index = None
            for i in range(max_row + 1):
                rid = str(i + 1)
                if rid not in occupied:
                    target_row_index = i
                    break
            if target_row_index is None:
                target_row_index = max_row + 1
                if target_row_index >= MAX_ROWS:
                    raise ValueError("table is full")
                conn.execute(
                    "INSERT INTO user_table_rows (user_id, table_id, row_id, row_index, height, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (user_id, table_id, str(target_row_index + 1),
                     target_row_index, DEFAULT_ROW_HEIGHT, _now()))
            row_id = str(target_row_index + 1)

            header = {}
            for (rid, cid), val in self._cell_map(conn, user_id, table_id).items():
                if rid == "1" and val and val.strip():
                    header[val.strip().lower()] = cid

            cols = conn.execute(
                "SELECT column_id, col_index FROM user_table_columns "
                "WHERE user_id=? AND table_id=? ORDER BY col_index",
                (user_id, table_id)).fetchall()

            mapping = {}
            for key, val in (data or {}).items():
                k = str(key).strip().lower()
                if k in header:
                    mapping[header[k]] = val
                else:
                    for c in cols:
                        if c["column_id"].lower() == k:
                            mapping[c["column_id"]] = val
                            break
            if not mapping and data:
                values = list(data.values())
                for i, val in enumerate(values):
                    if i < len(cols):
                        mapping[cols[i]["column_id"]] = val

        written = {}
        for cid, val in mapping.items():
            self.set_cell(user_id, table_id, row_id, cid, val)
            written[f"{cid}{row_id}"] = str(val)
        return {"row_id": row_id, "cells": written}

    # ---------- Legacy migration ----------
    def _migrate_legacy_rows(self, user_id, table_id):
        try:
            with self._connect() as conn:
                cols = {r["name"] for r in conn.execute(
                    "PRAGMA table_info(user_table_rows)").fetchall()}
                if "data_json" not in cols:
                    return
                rows = conn.execute(
                    "SELECT row_id, data_json FROM user_table_rows "
                    "WHERE user_id=? AND table_id=? AND data_json IS NOT NULL AND data_json != ''",
                    (user_id, table_id)).fetchall()
        except Exception:
            return
        if not rows:
            return
        with self._lock, self._connect() as conn:
            for r in rows:
                try:
                    data = json.loads(r["data_json"])
                except Exception:
                    continue
                if not isinstance(data, dict) or not data:
                    continue
                has = conn.execute(
                    "SELECT 1 FROM user_table_cells WHERE user_id=? AND table_id=? AND row_id=? LIMIT 1",
                    (user_id, table_id, r["row_id"])).fetchone()
                if has:
                    continue
                cols_list = conn.execute(
                    "SELECT column_id, col_index, label FROM user_table_columns "
                    "WHERE user_id=? AND table_id=? ORDER BY col_index",
                    (user_id, table_id)).fetchall()
                col_ids = [c["column_id"] for c in cols_list]
                for i, (k, v) in enumerate(data.items()):
                    cid = col_ids[i] if i < len(col_ids) else None
                    if not cid:
                        break
                    conn.execute(
                        "INSERT OR IGNORE INTO user_table_cells "
                        "(user_id, table_id, row_id, column_id, value, updated_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (user_id, table_id, r["row_id"], cid, str(v), _now()))

    # ---------- Сумісність зі старим API ----------
    def upsert_row(self, user_id: int, table_id: str, account_id: str,
                   conversation_id: str, data: dict) -> dict:
        try:
            rules = self.get_rules(user_id, table_id)
            if rules:
                return self.write_by_rules(user_id, table_id, data, client_ref=conversation_id)
        except Exception:
            pass
        payload = {"client": conversation_id}
        for k, v in (data or {}).items():
            if v is not None and str(v).strip() != "":
                payload[k] = v
        return self.append_row_by_labels(user_id, table_id, payload)

    def ensure_column(self, user_id: int, table_id: str, column_id: str, label: str = None) -> bool:
        column_id = (column_id or "").strip()
        if not user_id or not table_id or not column_id or not column_id.isalpha():
            return False
        column_id = column_id.upper()
        cidx = col_letter_to_index(column_id)
        with self._lock, self._connect() as conn:
            if conn.execute(
                    "SELECT 1 FROM user_table_columns WHERE user_id=? AND table_id=? AND column_id=?",
                    (user_id, table_id, column_id)).fetchone():
                if label:
                    conn.execute(
                        "UPDATE user_table_columns SET label=? WHERE user_id=? AND table_id=? AND column_id=?",
                        (label[:120], user_id, table_id, column_id))
                return False
            self._ensure_grid(conn, user_id, table_id, n_cols=cidx + 1, n_rows=0)
            conn.execute(
                "UPDATE user_table_columns SET label=? WHERE user_id=? AND table_id=? AND column_id=?",
                ((label or "")[:120], user_id, table_id, column_id))
        return True

    def delete_row(self, user_id: int, table_id: str, row_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM user_table_rows WHERE user_id=? AND table_id=? AND row_id=?",
                (user_id, table_id, row_id))
            if cur.rowcount == 0:
                return False
            conn.execute(
                "DELETE FROM user_table_cells WHERE user_id=? AND table_id=? AND row_id=?",
                (user_id, table_id, row_id))
            conn.execute(
                "DELETE FROM user_table_cell_styles WHERE user_id=? AND table_id=? AND row_id=?",
                (user_id, table_id, row_id))
            conn.execute(
                "DELETE FROM user_table_row_refs WHERE user_id=? AND table_id=? AND row_id=?",
                (user_id, table_id, row_id))
            return True

    def update_row(self, user_id: int, table_id: str, row_id: str, data: dict) -> bool:
        try:
            for cid, val in (data or {}).items():
                cid = str(cid).strip().upper()
                if cid.isalpha():
                    try:
                        self.set_cell(user_id, table_id, row_id, cid, val)
                    except ValueError:
                        pass
            return True
        except Exception:
            return False
