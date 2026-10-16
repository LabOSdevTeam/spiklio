"""
crm_store.py — SQLite-шар для CRM у /uadmin.

Таблиці:
  crm_messages  — усі вхідні/вихідні повідомлення (раніше історія жила лише в пам'яті)
  crm_contacts  — статус, ім'я, нотатки, флаг "замовлено" (is_won) по розмові
  crm_pending   — дії, які запропонував AI-асистент і які чекають підтвердження власника
  crm_audit     — журнал усіх змін (хто: owner/assistant, що, коли)

Про is_won: AI додає марку <<ready>> у відповідь, коли клієнт ЯВНО
підтвердив замовлення. worker.py вирізає марку перед відправкою і викликає
mark_won() — це ставить is_won=1 у crm_contacts. Вкладка Clients у /uadmin
показує ТІЛЬКИ тих, у кого is_won=1 (це "список клієнтів, які замовили").

Усі вибірки приймають список account_ids — ВЛАСНИК акаунтів визначає route-шар
(crm_routes.py), тут ми ніколи не довіряємо account_id ззовні.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

from storage import _BaseSQLite, _now

STATUSES = ["new", "talking", "qualified", "won", "lost"]


def _in(ids):
    return ",".join("?" for _ in ids)


def since_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


class CRMStore(_BaseSQLite):
    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS crm_messages (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id      TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    direction       TEXT NOT NULL CHECK (direction IN ('in','out')),
                    text            TEXT NOT NULL,
                    sender_name     TEXT,
                    created_at      TEXT NOT NULL
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_crm_msg_conv ON crm_messages(account_id, conversation_id, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_crm_msg_time ON crm_messages(account_id, created_at)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS crm_contacts (
                    account_id      TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    display_name    TEXT,
                    status          TEXT NOT NULL DEFAULT 'new',
                    notes           TEXT NOT NULL DEFAULT '',
                    is_won          INTEGER NOT NULL DEFAULT 0,
                    won_at          TEXT,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL,
                    PRIMARY KEY (account_id, conversation_id)
                )""")
            # Міграція старих БД: додаємо is_won / won_at якщо їх немає
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(crm_contacts)").fetchall()}
            if "is_won" not in cols:
                conn.execute("ALTER TABLE crm_contacts ADD COLUMN is_won INTEGER NOT NULL DEFAULT 0")
            if "won_at" not in cols:
                conn.execute("ALTER TABLE crm_contacts ADD COLUMN won_at TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_crm_contacts_won ON crm_contacts(is_won)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS crm_pending (
                    id          TEXT PRIMARY KEY,
                    user_id     INTEGER NOT NULL,
                    kind        TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    summary     TEXT NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'pending',
                    created_at  TEXT NOT NULL,
                    resolved_at TEXT
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS crm_audit (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER NOT NULL,
                    actor      TEXT NOT NULL,
                    action     TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )""")

    # ---------- messages ----------
    def add_message(self, account_id, conversation_id, direction, text, sender_name=None):
        if not account_id or not conversation_id or not (text or "").strip():
            return
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO crm_messages (account_id, conversation_id, direction, text, sender_name, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (account_id, conversation_id, direction, text[:4000], sender_name, now))
            conn.execute(
                "INSERT OR IGNORE INTO crm_contacts (account_id, conversation_id, display_name, created_at, updated_at) "
                "VALUES (?,?,?,?,?)", (account_id, conversation_id, sender_name, now, now))
            if sender_name:
                conn.execute(
                    "UPDATE crm_contacts SET display_name = ? WHERE account_id = ? AND conversation_id = ? "
                    "AND (display_name IS NULL OR display_name = '')", (sender_name, account_id, conversation_id))

    def conversations(self, account_ids, since=None, limit=100):
        if not account_ids:
            return []
        q = f"""
            SELECT m.account_id, m.conversation_id, m.text, m.direction, m.created_at,
                   c.display_name, COALESCE(c.status,'new') AS status,
                   COALESCE(c.is_won, 0) AS is_won, c.won_at,
                   (SELECT COUNT(*) FROM crm_messages x
                     WHERE x.account_id = m.account_id AND x.conversation_id = m.conversation_id) AS msg_count
            FROM crm_messages m
            LEFT JOIN crm_contacts c ON c.account_id = m.account_id AND c.conversation_id = m.conversation_id
            WHERE m.id IN (SELECT MAX(id) FROM crm_messages WHERE account_id IN ({_in(account_ids)})
                           {'AND created_at >= ?' if since else ''} GROUP BY account_id, conversation_id)
            ORDER BY m.id DESC LIMIT ?"""
        args = list(account_ids) + ([since] if since else []) + [limit]
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(q, args).fetchall()]

    def thread(self, account_id, conversation_id, limit=200):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT direction, text, created_at FROM (SELECT * FROM crm_messages WHERE account_id = ? "
                "AND conversation_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (account_id, conversation_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def period_messages(self, account_id, conversation_id, since, limit=12):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT direction, text, created_at FROM (SELECT * FROM crm_messages WHERE account_id = ? "
                "AND conversation_id = ? AND created_at >= ? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (account_id, conversation_id, since, limit)).fetchall()
        return [dict(r) for r in rows]

    # ---------- contacts ----------
    def get_contact(self, account_id, conversation_id):
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM crm_contacts WHERE account_id = ? AND conversation_id = ?",
                             (account_id, conversation_id)).fetchone()
        return dict(r) if r else None

    def contacts(self, account_ids, only_won=False, limit=300):
        """
        only_won=True — повертає ЛИШЕ клієнтів з is_won=1.
        Використовується у вкладці Clients у /uadmin: показуємо тільки тих,
        хто реально щось замовив, а не всіх, хто колись писав.
        """
        if not account_ids:
            return []
        won_clause = "AND c.is_won = 1" if only_won else ""
        q = f"""
            SELECT c.account_id, c.conversation_id, c.display_name, c.status, c.notes,
                   COALESCE(c.is_won, 0) AS is_won, c.won_at, c.created_at,
                   (SELECT MAX(created_at) FROM crm_messages m
                     WHERE m.account_id = c.account_id AND m.conversation_id = c.conversation_id) AS last_at,
                   (SELECT COUNT(*) FROM crm_messages m
                     WHERE m.account_id = c.account_id AND m.conversation_id = c.conversation_id) AS msg_count
            FROM crm_contacts c
            WHERE c.account_id IN ({_in(account_ids)}) {won_clause}
            ORDER BY COALESCE(c.won_at, last_at) DESC LIMIT ?"""
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(q, list(account_ids) + [limit]).fetchall()]

    def update_contact(self, account_id, conversation_id, status=None, notes=None, display_name=None):
        if status is not None and status not in STATUSES:
            raise ValueError("bad status")
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO crm_contacts (account_id, conversation_id, created_at, updated_at) VALUES (?,?,?,?)",
                (account_id, conversation_id, now, now))
            if status is not None:
                conn.execute("UPDATE crm_contacts SET status=?, updated_at=? WHERE account_id=? AND conversation_id=?",
                             (status, now, account_id, conversation_id))
            if notes is not None:
                conn.execute("UPDATE crm_contacts SET notes=?, updated_at=? WHERE account_id=? AND conversation_id=?",
                             (notes[:2000], now, account_id, conversation_id))
            if display_name is not None:
                conn.execute("UPDATE crm_contacts SET display_name=?, updated_at=? WHERE account_id=? AND conversation_id=?",
                             (display_name[:120], now, account_id, conversation_id))

    def mark_won(self, account_id, conversation_id, source="assistant"):
        """
        Ставить is_won=1 (ідемпотентно). Викликається з worker.py, коли AI
        додав <<ready>> у відповідь. Якщо вже було won — НЕ перезаписує won_at,
        але оновлює updated_at. Повертає True, якщо це ПЕРШЕ замовлення.
        """
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO crm_contacts (account_id, conversation_id, created_at, updated_at) VALUES (?,?,?,?)",
                (account_id, conversation_id, now, now))
            row = conn.execute(
                "SELECT is_won FROM crm_contacts WHERE account_id = ? AND conversation_id = ?",
                (account_id, conversation_id)).fetchone()
            was_already = bool(row and row["is_won"])
            if not was_already:
                conn.execute(
                    "UPDATE crm_contacts SET is_won = 1, won_at = ?, updated_at = ? WHERE account_id = ? AND conversation_id = ?",
                    (now, now, account_id, conversation_id))
            else:
                conn.execute(
                    "UPDATE crm_contacts SET updated_at = ? WHERE account_id = ? AND conversation_id = ?",
                    (now, account_id, conversation_id))
        return not was_already

    def unmark_won(self, account_id, conversation_id):
        """Знімає флаг is_won (для випадків, коли AI помилився)."""
        now = _now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE crm_contacts SET is_won = 0, won_at = NULL, updated_at = ? WHERE account_id = ? AND conversation_id = ? AND is_won = 1",
                (now, account_id, conversation_id))
            return cur.rowcount > 0

    def delete_conversation(self, account_id, conversation_id):
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM crm_messages WHERE account_id=? AND conversation_id=?", (account_id, conversation_id))
            conn.execute("DELETE FROM crm_contacts WHERE account_id=? AND conversation_id=?", (account_id, conversation_id))

    # ---------- stats / search ----------
    def stats(self, account_ids, days=14):
        if not account_ids:
            return {"messages": [], "new_conversations": [], "statuses": {}, "wins": 0}
        since = since_iso(days)
        ph = _in(account_ids)
        with self._connect() as conn:
            msgs = conn.execute(
                f"SELECT substr(created_at,1,10) d, direction, COUNT(*) n FROM crm_messages "
                f"WHERE account_id IN ({ph}) AND created_at >= ? GROUP BY d, direction",
                list(account_ids) + [since]).fetchall()
            newc = conn.execute(
                f"SELECT substr(first,1,10) d, COUNT(*) n FROM (SELECT MIN(created_at) first FROM crm_messages "
                f"WHERE account_id IN ({ph}) GROUP BY account_id, conversation_id) WHERE first >= ? GROUP BY d",
                list(account_ids) + [since]).fetchall()
            st = conn.execute(
                f"SELECT status, COUNT(*) n FROM crm_contacts WHERE account_id IN ({ph}) GROUP BY status",
                list(account_ids)).fetchall()
            won = conn.execute(
                f"SELECT COUNT(*) n FROM crm_contacts WHERE account_id IN ({ph}) AND is_won = 1",
                list(account_ids)).fetchone()
        by_day = {}
        for r in msgs:
            by_day.setdefault(r["d"], {"in": 0, "out": 0})[r["direction"]] = r["n"]
        newmap = {r["d"]: r["n"] for r in newc}
        today = datetime.now(timezone.utc).date()
        labels = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
        return {
            "days": labels,
            "messages": [by_day.get(d, {"in": 0, "out": 0}) for d in labels],
            "new_conversations": [newmap.get(d, 0) for d in labels],
            "statuses": {r["status"]: r["n"] for r in st},
            "wins": won["n"] if won else 0,
        }

    def search(self, account_ids, q, limit=15):
        if not account_ids or not q.strip():
            return {"contacts": [], "messages": []}
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        ph = _in(account_ids)
        with self._connect() as conn:
            c = conn.execute(
                f"SELECT account_id, conversation_id, display_name, status, COALESCE(is_won,0) AS is_won FROM crm_contacts "
                f"WHERE account_id IN ({ph}) AND (display_name LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\') LIMIT ?",
                list(account_ids) + [like, like, limit]).fetchall()
            m = conn.execute(
                f"SELECT account_id, conversation_id, direction, text, created_at FROM crm_messages "
                f"WHERE account_id IN ({ph}) AND text LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
                list(account_ids) + [like, limit]).fetchall()
        return {"contacts": [dict(r) for r in c], "messages": [dict(r) for r in m]}

    # ---------- pending actions ----------
    def create_pending(self, user_id, kind, params, summary):
        pid = uuid.uuid4().hex[:16]
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO crm_pending (id, user_id, kind, params_json, summary, created_at) VALUES (?,?,?,?,?,?)",
                         (pid, user_id, kind, json.dumps(params, ensure_ascii=False), summary, _now()))
        return pid

    def get_pending(self, pid, user_id):
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM crm_pending WHERE id = ? AND user_id = ?", (pid, user_id)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["params"] = json.loads(d.pop("params_json"))
        return d

    def resolve_pending(self, pid, user_id, status):
        """Атомарно: перевести pending → done/cancelled лише один раз."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE crm_pending SET status=?, resolved_at=? WHERE id=? AND user_id=? AND status='pending'",
                (status, _now(), pid, user_id))
            return cur.rowcount > 0

    # ---------- audit ----------
    def audit(self, user_id, actor, action):
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO crm_audit (user_id, actor, action, created_at) VALUES (?,?,?,?)",
                         (user_id, actor, action[:300], _now()))

    def audit_list(self, user_id, limit=50):
        with self._connect() as conn:
            rows = conn.execute("SELECT actor, action, created_at FROM crm_audit WHERE user_id=? ORDER BY id DESC LIMIT ?",
                                (user_id, limit)).fetchall()
        return [dict(r) for r in rows]
