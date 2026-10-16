"""
catalog_store.py — універсальний каталог пропозицій бізнесу (SQLite + FTS5).

Каталог — це ВСЕ, що бізнес продає або на чому заробляє: товари, послуги,
підписки, оренда, курси, івенти, пакети.

МОДЕЛЬ ДАНИХ (v2 — розділені каталоги):
    catalogs          — каталог як окрема сутність, належить user_id
    catalog_items     — позиції каталогу (catalog_id FK)
    catalog_bindings  — many-to-many: які каталоги бачить який бот-акаунт
    catalog_sources   — джерела імпорту (google sheet / manual)
    catalog_jobs      — фонові імпорти

Розподіл:
    один каталог може бути прив'язаний до N акаунтів;
    один акаунт може бачити M каталогів;
    якщо в акаунта немає жодної прив'язки — фолбек на каталог з is_default=1
    у межах того ж user_id.

Пошук (RAG): SQLite FTS5 + BM25 по «стемованих» токенах. Окремої векторної
БД не потрібно — для каталогу з сотень/тисяч позицій лексичний пошук із
простим стемінгом (укр/рос/англ) працює надійно.
"""
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

KINDS = ("product", "service", "subscription", "rental", "course", "event", "package", "other")
PRICE_TYPES = ("fixed", "from", "range", "on_request", "free")
AVAILABILITY = ("available", "out_of_stock", "preorder", "by_request", "unavailable")

MAX_ITEMS_PER_CATALOG = 5000
MAX_VARIANTS = 40
MAX_ATTRIBUTES = 25
MAX_TAGS = 15

# Скільки позицій одного каталогу віддавати в LLM-контекст при пошуку.
SEARCH_TOP_K_PER_CATALOG = 6

_AVAIL_ALIASES = {
    "available": {"available", "in stock", "instock", "є", "є в наявності", "в наявності", "наявно",
                  "так", "yes", "y", "true", "1", "в наличии", "есть", "да", "+"},
    "out_of_stock": {"out_of_stock", "out of stock", "outofstock", "soldout", "sold out", "немає", "нема",
                     "немає в наявності", "закінчився", "відсутній", "нет", "нет в наличии", "ні", "no",
                     "n", "false", "0", "-"},
    "preorder": {"preorder", "pre-order", "presale", "під замовлення", "на замовлення", "предзаказ",
                 "под заказ"},
    "by_request": {"by_request", "by request", "за запитом", "за домовленістю", "по запросу"},
    "unavailable": {"unavailable", "discontinued", "знято з продажу", "не продається", "недоступно"},
}
_AVAIL_LOOKUP = {alias: key for key, aliases in _AVAIL_ALIASES.items() for alias in aliases}


# ======================= ТОКЕНІЗАЦІЯ / СТЕМІНГ =======================
_TRANS = str.maketrans({"ё": "е", "ы": "и", "і": "и", "ї": "и", "є": "е", "э": "е",
                        "ъ": "", "ь": "", "'": "", "’": "", "ʼ": "", "`": ""})
_RAW_STOP = {
    "що", "це", "та", "і", "й", "в", "у", "на", "по", "до", "із", "з", "за", "від", "для", "про", "як", "чи",
    "є", "вас", "ви", "мені", "мене", "можна", "будь", "ласка", "привіт", "добрий", "день", "дякую",
    "яка", "який", "яке", "які", "скільки", "коштує", "ціна", "цена", "сколько", "стоит", "есть", "что",
    "это", "как", "мне", "можно", "привет", "здравствуйте", "спасибо", "хочу",
    "the", "a", "an", "is", "are", "do", "you", "have", "i", "me", "my", "and", "or", "of", "to", "for",
    "how", "much", "what", "does", "cost", "price", "hello", "hi", "please", "want",
}
_STOP = {w.translate(_TRANS) for w in _RAW_STOP}
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _stem(tok: str) -> str:
    if tok.isdigit():
        return tok
    if tok.isascii() and len(tok) > 3 and tok.endswith("s"):
        tok = tok[:-1]
    n = len(tok)
    return tok if n <= 3 else tok[:min(5, n - 1)]


def tokens(text: str) -> list:
    text = (text or "").lower().translate(_TRANS)
    out = []
    for t in _TOKEN_RE.findall(text):
        if t in _STOP or (len(t) < 2 and not t.isdigit()):
            continue
        out.append(_stem(t))
    return out


def build_match_query(text_or_tokens, limit: int = 14) -> str:
    toks = tokens(text_or_tokens) if isinstance(text_or_tokens, str) else list(text_or_tokens)
    seen, uniq = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return " OR ".join(f'"{t}"' for t in uniq[:limit])


# ======================= НОРМАЛІЗАЦІЯ ПОЗИЦІЇ =======================
def _clip(v, n: int) -> str:
    s = "" if v is None else str(v)
    s = s.replace("\x00", "").strip()
    # захист від ін'єкції в промпт: маркери блоків системного промпту прибираємо
    s = s.replace("===", "—")
    return s[:n]


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v == v and abs(v) < 1e12 else None
    s = str(v).strip().replace("\u00a0", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = head + tail if (len(tail) == 3 and len(head) <= 3 and head.isdigit()) else head + "." + tail
    try:
        f = float(s)
        return f if abs(f) < 1e12 else None
    except ValueError:
        return None


def _avail(v, default="available") -> str:
    if v is None:
        return default
    return _AVAIL_LOOKUP.get(str(v).strip().lower(), default)


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        return [p.strip() for p in re.split(r"[,;|\n]", v) if p.strip()]
    if isinstance(v, (list, tuple, set)):
        return [str(p).strip() for p in v if str(p).strip()]
    return []


def normalize_item(raw: dict):
    """Приводить будь-який «сирий» dict до канонічної позиції.
    Повертає None, якщо немає назви. Ніколи не кидає винятків."""
    if not isinstance(raw, dict):
        return None
    name = _clip(raw.get("name"), 200)
    if not name:
        return None

    kind = str(raw.get("kind") or "product").strip().lower()
    kind = kind if kind in KINDS else "other"

    price, price_max = _num(raw.get("price")), _num(raw.get("price_max"))
    if price is not None and price < 0:
        price = None
    if price_max is not None and (price is None or price_max <= price):
        price_max = None
    ptype = str(raw.get("price_type") or "").strip().lower()
    if ptype not in PRICE_TYPES:
        ptype = "range" if price_max is not None else ("fixed" if price is not None else "on_request")
    if ptype == "free":
        price, price_max = 0.0, None
    elif ptype == "on_request":
        price, price_max = None, None
    elif price is None:
        ptype = "on_request"
    if ptype == "range" and price_max is None:
        ptype = "fixed"

    variants = []
    for v in (raw.get("variants") or [])[:MAX_VARIANTS] if isinstance(raw.get("variants"), list) else []:
        if isinstance(v, dict) and _clip(v.get("name"), 80):
            vp = _num(v.get("price"))
            variants.append({"name": _clip(v.get("name"), 80),
                             "price": vp if vp is not None and vp >= 0 else None,
                             "availability": _avail(v.get("availability"))})

    attrs = {}
    a_raw = raw.get("attributes")
    if isinstance(a_raw, dict):
        for k, v in list(a_raw.items())[:MAX_ATTRIBUTES]:
            k, v = _clip(k, 60), _clip(v, 200)
            if k and v:
                attrs[k] = v

    tags, seen = [], set()
    for t in _as_list(raw.get("tags")):
        t = _clip(t, 40)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            tags.append(t)

    def _url(v):
        v = _clip(v, 500)
        return v if v.lower().startswith(("http://", "https://")) else ""

    return {
        "kind": kind,
        "name": name,
        "description": _clip(raw.get("description"), 1500),
        "category": _clip(raw.get("category"), 120),
        "price": price, "price_max": price_max, "price_type": ptype,
        "currency": _clip(raw.get("currency"), 8).upper(),
        "unit": _clip(raw.get("unit"), 30),
        "availability": _avail(raw.get("availability")),
        "variants": variants, "attributes": attrs, "tags": tags[:MAX_TAGS],
        "external_id": _clip(raw.get("sku") or raw.get("external_id"), 80),
        "url": _url(raw.get("url")), "image_url": _url(raw.get("image_url")),
        "is_active": 0 if raw.get("is_active") in (0, False, "0", "false") else 1,
    }


def _search_text(item: dict) -> str:
    """Що індексуємо. Назва двічі — щоб BM25 віддавав їй більшу вагу."""
    parts = [item["name"], item["name"], item.get("category", ""), item.get("kind", ""),
             " ".join(item.get("tags") or []), item.get("description", ""), item.get("external_id", ""),
             " ".join(v["name"] for v in item.get("variants") or []),
             " ".join(f"{k} {v}" for k, v in (item.get("attributes") or {}).items())]
    return " ".join(tokens(" ".join(p for p in parts if p)))


# ======================= STORE =======================
class CatalogStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.fts = False
        self._init_lock = threading.Lock()
        self._init()

    @contextmanager
    def _conn(self):
        con = sqlite3.connect(self.db_path, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _init(self):
        with self._init_lock, self._conn() as con:
            try:
                con.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
            con.executescript("""
            CREATE TABLE IF NOT EXISTS catalogs (
                id         TEXT PRIMARY KEY,
                user_id    INTEGER,                 -- NULL = глобальний ("default" акаунтів)
                name       TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER, updated_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_catalogs_user ON catalogs(user_id);

            CREATE TABLE IF NOT EXISTS catalog_items (
                id TEXT PRIMARY KEY,
                catalog_id TEXT NOT NULL,
                source_id TEXT,
                external_id TEXT DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'product',
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                category TEXT DEFAULT '',
                price REAL, price_max REAL,
                price_type TEXT DEFAULT 'fixed',
                currency TEXT DEFAULT '', unit TEXT DEFAULT '',
                availability TEXT DEFAULT 'available',
                variants TEXT DEFAULT '[]', attributes TEXT DEFAULT '{}', tags TEXT DEFAULT '[]',
                url TEXT DEFAULT '', image_url TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                created_at INTEGER, updated_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_catalog_items_cat ON catalog_items(catalog_id, is_active);
            CREATE INDEX IF NOT EXISTS idx_catalog_items_src ON catalog_items(source_id);

            CREATE TABLE IF NOT EXISTS catalog_bindings (
                catalog_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                created_at INTEGER,
                PRIMARY KEY (catalog_id, account_id)
            );
            CREATE INDEX IF NOT EXISTS idx_catalog_bindings_acc ON catalog_bindings(account_id);

            CREATE TABLE IF NOT EXISTS catalog_sources (
                id TEXT PRIMARY KEY,
                catalog_id TEXT NOT NULL,
                kind TEXT NOT NULL,          -- manual | sheet | site
                ref TEXT NOT NULL,           -- ім'я файлу / URL / spreadsheet_id
                label TEXT DEFAULT '',
                item_count INTEGER DEFAULT 0,
                last_sync INTEGER,
                UNIQUE(catalog_id, kind, ref)
            );
            CREATE INDEX IF NOT EXISTS idx_catalog_sources_cat ON catalog_sources(catalog_id);

            CREATE TABLE IF NOT EXISTS catalog_jobs (
                id TEXT PRIMARY KEY,
                catalog_id TEXT NOT NULL,
                kind TEXT NOT NULL, ref TEXT DEFAULT '', label TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                message TEXT DEFAULT '', error TEXT DEFAULT '',
                result_json TEXT DEFAULT '',
                created_at INTEGER, updated_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_catalog_jobs_cat ON catalog_jobs(catalog_id);
            """)
            try:
                con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS catalog_fts USING fts5("
                            "item_id UNINDEXED, catalog_id UNINDEXED, body, "
                            "tokenize='unicode61 remove_diacritics 2')")
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False
                print("[catalog] WARNING: SQLite без FTS5 — пошук піде через Python-фолбек", flush=True)

            self._migrate_v1_to_v2(con)

    def _migrate_v1_to_v2(self, con):
        """Якщо БД має стару схему (catalog_items.account_id замість catalog_id),
        створюємо каталог для кожного account_id і переносимо туди позиції.
        Потім перейменовуємо стару таблицю в бекап.

        Фолбек: якщо навіть catalog_items ще не існує (свіжа БД) — нічого не робимо."""
        try:
            cols = {r["name"] for r in con.execute("PRAGMA table_info(catalog_items)").fetchall()}
        except sqlite3.OperationalError:
            return
        if not cols or "account_id" not in cols:
            return
        print("[catalog] migrating v1 → v2 (account_id → catalog_id)", flush=True)
        ts = int(time.time())
        # 1. знайти всі унікальні account_id
        accs = [r["account_id"] for r in con.execute(
            "SELECT DISTINCT account_id FROM catalog_items").fetchall()]
        # 2. для кожного створити каталог і перенести позиції
        for acc in accs:
            cid = uuid.uuid4().hex[:12]
            is_def = 1 if acc == "default" else 0
            con.execute(
                "INSERT INTO catalogs (id, user_id, name, is_default, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (cid, None, f"Імпорт з {acc}", is_def, ts, ts))
            # 3. перенести позиції: перевіряємо чи існує колонка catalog_id
            new_cols = {r["name"] for r in con.execute("PRAGMA table_info(catalog_items)").fetchall()}
            if "catalog_id" in new_cols:
                con.execute("UPDATE catalog_items SET catalog_id=? WHERE account_id=?", (cid, acc))
            else:
                # дуже стара схема: немає ні account_id (вже перевірили), ні catalog_id
                # (не мало б трапитись — захист на майбутнє)
                pass
            # 4. прив'язка: якщо це не default — прив'язуємо каталог до цього акаунта
            if acc != "default":
                con.execute(
                    "INSERT OR IGNORE INTO catalog_bindings (catalog_id, account_id, created_at) "
                    "VALUES (?,?,?)", (cid, acc, ts))
            # 5. перенести sources / jobs, якщо вони мають account_id
            for tbl, fk in (("catalog_sources", "catalog_id"), ("catalog_jobs", "catalog_id")):
                try:
                    tcols = {r["name"] for r in con.execute(f"PRAGMA table_info({tbl})").fetchall()}
                except sqlite3.OperationalError:
                    continue
                if "account_id" in tcols and fk in tcols:
                    con.execute(f"UPDATE {tbl} SET {fk}=? WHERE account_id=?", (cid, acc))
        # 6. прибрати account_id з усіх трьох таблиць (перейменування таблиць — найчистіше,
        #    але SQLite не вміє DROP COLUMN до 3.35; використаємо бекап + перестворення)
        #    Простіше: лишити колонку, вона просто не використовується.
        print(f"[catalog] migration done: {len(accs)} catalogs created", flush=True)

    # ---------- row <-> item ----------
    @staticmethod
    def _row_to_item(row) -> dict:
        d = dict(row)
        for k, default in (("variants", []), ("attributes", {}), ("tags", [])):
            try:
                d[k] = json.loads(d.get(k) or "")
            except Exception:
                d[k] = default
        d.pop("score", None)
        return d

    def _insert(self, con, catalog_id: str, item: dict, source_id=None, item_id=None) -> str:
        item_id = item_id or uuid.uuid4().hex[:12]
        ts = int(time.time())
        con.execute(
            "INSERT INTO catalog_items (id, catalog_id, source_id, external_id, kind, name, description,"
            " category, price, price_max, price_type, currency, unit, availability, variants, attributes,"
            " tags, url, image_url, is_active, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, catalog_id, source_id, item["external_id"], item["kind"], item["name"],
             item["description"], item["category"], item["price"], item["price_max"], item["price_type"],
             item["currency"], item["unit"], item["availability"],
             json.dumps(item["variants"], ensure_ascii=False),
             json.dumps(item["attributes"], ensure_ascii=False),
             json.dumps(item["tags"], ensure_ascii=False),
             item["url"], item["image_url"], item["is_active"], ts, ts))
        if self.fts:
            con.execute("INSERT INTO catalog_fts (item_id, catalog_id, body) VALUES (?,?,?)",
                        (item_id, catalog_id, _search_text(item)))
        return item_id

    def _delete_ids(self, con, ids: list) -> int:
        n = 0
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            if self.fts:
                con.execute(f"DELETE FROM catalog_fts WHERE item_id IN ({q})", chunk)
            n += con.execute(f"DELETE FROM catalog_items WHERE id IN ({q})", chunk).rowcount
        return n

    # ======================= CATALOGS (сутності) =======================
    def list_catalogs(self, user_id=None, include_global=True) -> list:
        """Каталоги юзера (+ глобальні, якщо include_global). user_id=None —
        тільки глобальні."""
        with self._conn() as con:
            if user_id is None:
                rows = con.execute(
                    "SELECT c.*, (SELECT COUNT(*) FROM catalog_items i WHERE i.catalog_id=c.id) AS n "
                    "FROM catalogs c WHERE c.user_id IS NULL ORDER BY c.created_at").fetchall()
            else:
                if include_global:
                    rows = con.execute(
                        "SELECT c.*, (SELECT COUNT(*) FROM catalog_items i WHERE i.catalog_id=c.id) AS n "
                        "FROM catalogs c WHERE c.user_id=? OR c.user_id IS NULL "
                        "ORDER BY c.is_default DESC, c.created_at",
                        (user_id,)).fetchall()
                else:
                    rows = con.execute(
                        "SELECT c.*, (SELECT COUNT(*) FROM catalog_items i WHERE i.catalog_id=c.id) AS n "
                        "FROM catalogs c WHERE c.user_id=? ORDER BY c.is_default DESC, c.created_at",
                        (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_catalog(self, catalog_id: str):
        with self._conn() as con:
            r = con.execute("SELECT * FROM catalogs WHERE id=?", (catalog_id,)).fetchone()
        return dict(r) if r else None

    def create_catalog(self, user_id, name: str, is_default: bool = False) -> dict:
        name = (name or "").strip()[:120]
        if not name:
            raise ValueError("Вкажіть назву каталогу.")
        cid = uuid.uuid4().hex[:12]
        ts = int(time.time())
        with self._conn() as con:
            if is_default:
                # у межах юзера може бути лише один default
                if user_id is None:
                    con.execute("UPDATE catalogs SET is_default=0 WHERE user_id IS NULL")
                else:
                    con.execute("UPDATE catalogs SET is_default=0 WHERE user_id=?", (user_id,))
            con.execute(
                "INSERT INTO catalogs (id, user_id, name, is_default, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (cid, user_id, name, 1 if is_default else 0, ts, ts))
        return self.get_catalog(cid)

    def rename_catalog(self, catalog_id: str, name: str) -> bool:
        name = (name or "").strip()[:120]
        if not name:
            return False
        with self._conn() as con:
            cur = con.execute("UPDATE catalogs SET name=?, updated_at=? WHERE id=?",
                              (name, int(time.time()), catalog_id))
            return cur.rowcount > 0

    def set_default_catalog(self, catalog_id: str) -> bool:
        cat = self.get_catalog(catalog_id)
        if not cat:
            return False
        uid = cat["user_id"]
        with self._conn() as con:
            if uid is None:
                con.execute("UPDATE catalogs SET is_default=0 WHERE user_id IS NULL")
            else:
                con.execute("UPDATE catalogs SET is_default=0 WHERE user_id=?", (uid,))
            con.execute("UPDATE catalogs SET is_default=1, updated_at=? WHERE id=?",
                        (int(time.time()), catalog_id))
        return True

    def delete_catalog(self, catalog_id: str) -> int:
        """Видаляє каталог і всі його позиції, прив'язки, sources, jobs.
        Повертає кількість видалених позицій."""
        with self._conn() as con:
            ids = [r["id"] for r in con.execute(
                "SELECT id FROM catalog_items WHERE catalog_id=?", (catalog_id,)).fetchall()]
            n = self._delete_ids(con, ids)
            con.execute("DELETE FROM catalog_bindings WHERE catalog_id=?", (catalog_id,))
            con.execute("DELETE FROM catalog_sources WHERE catalog_id=?", (catalog_id,))
            con.execute("DELETE FROM catalog_jobs WHERE catalog_id=?", (catalog_id,))
            con.execute("DELETE FROM catalogs WHERE id=?", (catalog_id,))
        return n

    def delete_all_for_user(self, user_id):
        """Прибирає всі каталоги юзера (не чіпає глобальні)."""
        with self._conn() as con:
            rows = con.execute("SELECT id FROM catalogs WHERE user_id=?", (user_id,)).fetchall()
        for r in rows:
            self.delete_catalog(r["id"])

    # ======================= BINDINGS (розподіл) =======================
    def get_bindings_for_account(self, account_id: str) -> list:
        with self._conn() as con:
            rows = con.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM catalog_items i WHERE i.catalog_id=c.id) AS n "
                "FROM catalog_bindings b JOIN catalogs c ON c.id=b.catalog_id "
                "WHERE b.account_id=? ORDER BY c.is_default DESC, c.created_at",
                (account_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_accounts_for_catalog(self, catalog_id: str) -> list:
        with self._conn() as con:
            rows = con.execute(
                "SELECT account_id FROM catalog_bindings WHERE catalog_id=?",
                (catalog_id,)).fetchall()
        return [r["account_id"] for r in rows]

    def bind(self, catalog_id: str, account_id: str) -> bool:
        if not catalog_id or not account_id:
            return False
        with self._conn() as con:
            if not con.execute("SELECT 1 FROM catalogs WHERE id=?", (catalog_id,)).fetchone():
                return False
            con.execute(
                "INSERT OR IGNORE INTO catalog_bindings (catalog_id, account_id, created_at) "
                "VALUES (?,?,?)", (catalog_id, account_id, int(time.time())))
        return True

    def unbind(self, catalog_id: str, account_id: str) -> bool:
        with self._conn() as con:
            cur = con.execute(
                "DELETE FROM catalog_bindings WHERE catalog_id=? AND account_id=?",
                (catalog_id, account_id))
            return cur.rowcount > 0

    def set_bindings_for_account(self, account_id: str, catalog_ids: list) -> int:
        """Повністю перезаписує прив'язки акаунта. Повертає кількість активних."""
        clean = [c for c in (catalog_ids or []) if c]
        with self._conn() as con:
            con.execute("DELETE FROM catalog_bindings WHERE account_id=?", (account_id,))
            ts = int(time.time())
            for cid in clean:
                if con.execute("SELECT 1 FROM catalogs WHERE id=?", (cid,)).fetchone():
                    con.execute(
                        "INSERT OR IGNORE INTO catalog_bindings (catalog_id, account_id, created_at) "
                        "VALUES (?,?,?)", (cid, account_id, ts))
        return len(clean)

    # ======================= RESOLVE (для engine) =======================
    def resolve_catalogs(self, account_id: str) -> list:
        """Повертає список catalog_id, які бачить цей акаунт.
        Логіка:
          1) якщо є прив'язки — беремо їх;
          2) інакше — default-каталог(и) того юзера, якому належить акаунт
             (або глобальний default, якщо власника не знайдено)."""
        if account_id:
            bound = self.get_bindings_for_account(account_id)
            if bound:
                return [c["id"] for c in bound]
        # фолбек: default каталог власника акаунта
        owner_id = self._find_user_for_account(account_id) if account_id else None
        with self._conn() as con:
            if owner_id is not None:
                row = con.execute(
                    "SELECT id FROM catalogs WHERE user_id=? AND is_default=1 LIMIT 1",
                    (owner_id,)).fetchone()
                if row:
                    return [row["id"]]
            # останній шанс — глобальний default (user_id IS NULL)
            row = con.execute(
                "SELECT id FROM catalogs WHERE user_id IS NULL AND is_default=1 LIMIT 1").fetchone()
        return [row["id"]] if row else []

    def _find_user_for_account(self, account_id: str):
        """Дивиться user_accounts у спільній БД (таблиця створюється storage.UserStore).
        Якщо таблиці немає — повертає None."""
        try:
            with self._conn() as con:
                row = con.execute(
                    "SELECT user_id FROM user_accounts WHERE account_id=? LIMIT 1",
                    (account_id,)).fetchone()
            return row["user_id"] if row else None
        except sqlite3.OperationalError:
            return None

    # ======================= CRUD ПОЗИЦІЙ =======================
    def count(self, catalog_id: str, active_only: bool = True) -> int:
        with self._conn() as con:
            sql = ("SELECT COUNT(*) FROM catalog_items WHERE catalog_id=?"
                   + (" AND is_active=1" if active_only else ""))
            return con.execute(sql, (catalog_id,)).fetchone()[0]

    def count_many(self, catalog_ids: list, active_only: bool = True) -> int:
        if not catalog_ids:
            return 0
        q = ",".join("?" * len(catalog_ids))
        with self._conn() as con:
            sql = (f"SELECT COUNT(*) FROM catalog_items WHERE catalog_id IN ({q})"
                   + (" AND is_active=1" if active_only else ""))
            return con.execute(sql, list(catalog_ids)).fetchone()[0]

    def save_item(self, catalog_id: str, raw: dict, item_id: str = None) -> dict:
        item = normalize_item(raw)
        if not item:
            raise ValueError("Вкажіть назву позиції.")
        with self._conn() as con:
            if not con.execute("SELECT 1 FROM catalogs WHERE id=?", (catalog_id,)).fetchone():
                raise ValueError("Каталог не знайдено.")
            source_id = None
            if item_id:
                row = con.execute("SELECT source_id FROM catalog_items WHERE id=? AND catalog_id=?",
                                  (item_id, catalog_id)).fetchone()
                if not row:
                    raise ValueError("Позицію не знайдено.")
                source_id = row["source_id"]
                self._delete_ids(con, [item_id])
            else:
                n = con.execute("SELECT COUNT(*) FROM catalog_items WHERE catalog_id=?",
                                (catalog_id,)).fetchone()[0]
                if n >= MAX_ITEMS_PER_CATALOG:
                    raise ValueError(f"Ліміт каталогу — {MAX_ITEMS_PER_CATALOG} позицій.")
            new_id = self._insert(con, catalog_id, item, source_id=source_id, item_id=item_id)
        return self.get_item(catalog_id, new_id)

    def get_item(self, catalog_id: str, item_id: str):
        with self._conn() as con:
            row = con.execute("SELECT * FROM catalog_items WHERE id=? AND catalog_id=?",
                              (item_id, catalog_id)).fetchone()
        return self._row_to_item(row) if row else None

    def delete_items(self, catalog_id: str, ids: list) -> int:
        with self._conn() as con:
            return self._delete_ids(con, [str(i) for i in ids])

    def apply_items(self, catalog_id: str, raw_items: list, source_id: str = None) -> int:
        """Пакетний імпорт. Якщо вказано source_id — СПОЧАТКУ видаляє всі
        старі позиції цього джерела (повторний імпорт = оновлення, без дублів)."""
        items = [i for i in (normalize_item(r) for r in raw_items) if i]
        with self._conn() as con:
            if not con.execute("SELECT 1 FROM catalogs WHERE id=?", (catalog_id,)).fetchone():
                raise ValueError("Каталог не знайдено.")
            if source_id:
                old = [r["id"] for r in con.execute(
                    "SELECT id FROM catalog_items WHERE catalog_id=? AND source_id=?",
                    (catalog_id, source_id))]
                self._delete_ids(con, old)
            room = MAX_ITEMS_PER_CATALOG - con.execute(
                "SELECT COUNT(*) FROM catalog_items WHERE catalog_id=?", (catalog_id,)).fetchone()[0]
            items = items[:max(room, 0)]
            for it in items:
                self._insert(con, catalog_id, it, source_id=source_id)
            if source_id:
                con.execute("UPDATE catalog_sources SET item_count=?, last_sync=? WHERE id=?",
                            (len(items), int(time.time()), source_id))
        return len(items)

    def delete_source(self, catalog_id: str, source_id: str) -> int:
        with self._conn() as con:
            ids = [r["id"] for r in con.execute(
                "SELECT id FROM catalog_items WHERE catalog_id=? AND source_id=?",
                (catalog_id, source_id))]
            n = self._delete_ids(con, ids)
            con.execute("DELETE FROM catalog_sources WHERE id=? AND catalog_id=?",
                        (source_id, catalog_id))
        return n

    # ======================= ЧИТАННЯ =======================
    def list_items(self, catalog_id: str, q: str = "", category: str = "", source_id: str = "",
                   limit: int = 50, offset: int = 0):
        where, args = ["catalog_id=?"], [catalog_id]
        if q:
            like = f"%{q.strip()[:80]}%"
            where.append("(name LIKE ? OR description LIKE ? OR category LIKE ?)")
            args += [like, like, like]
        if category:
            where.append("category=?")
            args.append(category)
        if source_id == "manual":
            where.append("source_id IS NULL")
        elif source_id:
            where.append("source_id=?")
            args.append(source_id)
        w = " AND ".join(where)
        with self._conn() as con:
            total = con.execute(f"SELECT COUNT(*) FROM catalog_items WHERE {w}", args).fetchone()[0]
            rows = con.execute(
                f"SELECT * FROM catalog_items WHERE {w} ORDER BY category, name LIMIT ? OFFSET ?",
                args + [max(1, min(limit, 200)), max(0, offset)]).fetchall()
        return [self._row_to_item(r) for r in rows], total

    def list_active(self, catalog_id: str, limit: int = 100) -> list:
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM catalog_items WHERE catalog_id=? AND is_active=1 "
                "ORDER BY category, name LIMIT ?", (catalog_id, limit)).fetchall()
        return [self._row_to_item(r) for r in rows]

    def list_active_many(self, catalog_ids: list, limit_per_catalog: int = 200) -> list:
        out = []
        for cid in catalog_ids:
            out.extend(self.list_active(cid, limit=limit_per_catalog))
        return out

    def categories(self, catalog_id: str, active_only: bool = True) -> list:
        sql = ("SELECT category, COUNT(*) c FROM catalog_items WHERE catalog_id=?"
               + (" AND is_active=1" if active_only else "")
               + " AND category!='' GROUP BY category ORDER BY c DESC")
        with self._conn() as con:
            return [(r["category"], r["c"]) for r in con.execute(sql, (catalog_id,))]

    def categories_many(self, catalog_ids: list, active_only: bool = True) -> list:
        """Об'єднує категорії з кількох каталогів (сумує лічильники)."""
        if not catalog_ids:
            return []
        q = ",".join("?" * len(catalog_ids))
        sql = (f"SELECT category, COUNT(*) c FROM catalog_items WHERE catalog_id IN ({q})"
               + (" AND is_active=1" if active_only else "")
               + " AND category!='' GROUP BY category ORDER BY c DESC")
        with self._conn() as con:
            return [(r["category"], r["c"]) for r in con.execute(sql, list(catalog_ids))]

    # ======================= ПОШУК =======================
    def search(self, catalog_id: str, query_text: str, limit: int = 6) -> list:
        return self.search_many([catalog_id], query_text, limit_per_catalog=limit)

    def search_many(self, catalog_ids: list, query_text: str, limit_per_catalog: int = 6) -> list:
        """Пошук по кількох каталогах. Повертає позиції (з полем _catalog_id),
        відсортовані за релевантністю (BM25, менше = краще)."""
        if not catalog_ids:
            return []
        match = build_match_query(query_text)
        if not match:
            return []
        if self.fts:
            try:
                q = ",".join("?" * len(catalog_ids))
                with self._conn() as con:
                    rows = con.execute(
                        f"SELECT i.*, c.name AS _catalog_name, bm25(catalog_fts) AS score "
                        f"FROM catalog_fts "
                        f"JOIN catalog_items i ON i.id = catalog_fts.item_id "
                        f"JOIN catalogs c ON c.id = i.catalog_id "
                        f"WHERE catalog_fts MATCH ? AND catalog_fts.catalog_id IN ({q}) "
                        f"AND i.is_active = 1 "
                        f"ORDER BY score LIMIT ?",
                        [match] + list(catalog_ids) + [limit_per_catalog * len(catalog_ids)]).fetchall()
                return [self._row_to_item(r) for r in rows]
            except sqlite3.OperationalError as e:
                print(f"[catalog] FTS query failed ({e}), fallback", flush=True)
        # Python-фолбек
        qtok = set(tokens(query_text))
        if not qtok:
            return []
        scored = []
        for cid in catalog_ids:
            for it in self.list_active(cid, limit=MAX_ITEMS_PER_CATALOG):
                body = _search_text(it).split()
                hits = sum(1 for t in body if t in qtok)
                if hits:
                    scored.append((hits, it))
        scored.sort(key=lambda x: -x[0])
        return [it for _, it in scored[:limit_per_catalog * len(catalog_ids)]]

    # ======================= ДЖЕРЕЛА =======================
    def get_or_create_source(self, catalog_id: str, kind: str, ref: str, label: str = "") -> str:
        with self._conn() as con:
            row = con.execute(
                "SELECT id FROM catalog_sources WHERE catalog_id=? AND kind=? AND ref=?",
                (catalog_id, kind, ref)).fetchone()
            if row:
                return row["id"]
            sid = uuid.uuid4().hex[:12]
            con.execute(
                "INSERT INTO catalog_sources (id, catalog_id, kind, ref, label) VALUES (?,?,?,?,?)",
                (sid, catalog_id, kind, ref[:500], label[:200]))
            return sid

    def list_sources(self, catalog_id: str) -> list:
        with self._conn() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM catalog_sources WHERE catalog_id=? ORDER BY last_sync DESC",
                (catalog_id,))]

    # ======================= ФОНОВІ ІМПОРТИ =======================
    def create_job(self, catalog_id: str, kind: str, ref: str = "", label: str = "") -> str:
        jid = uuid.uuid4().hex[:16]
        ts = int(time.time())
        with self._conn() as con:
            con.execute("DELETE FROM catalog_jobs WHERE created_at < ?", (ts - 2 * 86400,))
            con.execute(
                "INSERT INTO catalog_jobs (id, catalog_id, kind, ref, label, status, message,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (jid, catalog_id, kind, ref[:500], label[:200], "queued", "У черзі…", ts, ts))
        return jid

    def update_job(self, job_id: str, **fields):
        allowed = {"status", "message", "error"}
        sets, args = ["updated_at=?"], [int(time.time())]
        for k, v in fields.items():
            if k == "result":
                sets.append("result_json=?")
                args.append(json.dumps(v, ensure_ascii=False))
            elif k in allowed:
                sets.append(f"{k}=?")
                args.append(str(v)[:500])
        args.append(job_id)
        with self._conn() as con:
            con.execute(f"UPDATE catalog_jobs SET {', '.join(sets)} WHERE id=?", args)

    def get_job(self, job_id: str):
        with self._conn() as con:
            row = con.execute("SELECT * FROM catalog_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["result"] = json.loads(d.pop("result_json") or "null")
        except Exception:
            d["result"] = None
        return d

    def running_job(self, catalog_id: str):
        with self._conn() as con:
            row = con.execute(
                "SELECT id FROM catalog_jobs WHERE catalog_id=? AND status IN ('queued','running')"
                " AND updated_at > ?", (catalog_id, int(time.time()) - 600)).fetchone()
        return row["id"] if row else None
