"""
storage.py — SQLite-шар для Chatwave.

Таблиці:
  account_prompts       — кастомні промпти для акаунтів
  account_token_limits  — добовий ліміт токенів на акаунт (0 = без ліміту)
  account_flow_rules    — нодова карта дій ("правила потоку") на акаунт
  account_integrations  — які поля AI повинен збирати з клієнта ("інтеграція")
  client_records        — зібрані дані клієнтів (один рядок = одна розмова)
  google_sheets         — підключення Google Таблиць
  users                 — клієнти /uadmin (email, password_hash, max_accounts, zernio_profile_id)
  user_accounts         — прив'язки клієнт↔акаунт
  message_counters      — скільки токенів витрачено сьогодні на акаунт
"""
import os
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import bcrypt


DEFAULT_ACCOUNT_ID = "default"
DEFAULT_PROMPT = "Ти — дружній помічник. Відповідай українською, коротко і по суті."
DEFAULT_MAX_ACCOUNTS = 3
DEFAULT_TOKEN_LIMIT = 0  # 0 = без ліміту


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class _BaseSQLite:
    """Спільна логіка для всіх store-класів."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        raise NotImplementedError

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ==================== PROMPTS ====================
class PromptStore(_BaseSQLite):
    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_prompts (
                    account_id TEXT PRIMARY KEY,
                    prompt     TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
        if DEFAULT_ACCOUNT_ID not in self:
            self[DEFAULT_ACCOUNT_ID] = DEFAULT_PROMPT

    def __getitem__(self, account_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT prompt FROM account_prompts WHERE account_id = ?",
                (account_id,)
            ).fetchone()
        if row is None:
            raise KeyError(account_id)
        return row["prompt"]

    def __setitem__(self, account_id: str, prompt: str):
        if not account_id:
            raise ValueError("account_id must be non-empty")
        prompt = prompt if prompt is not None else ""
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO account_prompts (account_id, prompt, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    prompt = excluded.prompt,
                    updated_at = excluded.updated_at
            """, (account_id, prompt, _now()))

    def __delitem__(self, account_id: str):
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM account_prompts WHERE account_id = ?", (account_id,)
            )
            if cur.rowcount == 0:
                raise KeyError(account_id)

    def __contains__(self, account_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM account_prompts WHERE account_id = ? LIMIT 1",
                (account_id,)
            ).fetchone()
        return row is not None

    def __iter__(self):
        return iter(self.keys())

    def __len__(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM account_prompts").fetchone()
        return row["n"]

    def get(self, account_id: str, default=None):
        try:
            return self[account_id]
        except KeyError:
            return default

    def pop(self, account_id: str, default=None):
        try:
            value = self[account_id]
            del self[account_id]
            return value
        except KeyError:
            return default

    def keys(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT account_id FROM account_prompts ORDER BY account_id"
            ).fetchall()
        return [r["account_id"] for r in rows]

    def values(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT prompt FROM account_prompts ORDER BY account_id"
            ).fetchall()
        return [r["prompt"] for r in rows]

    def items(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT account_id, prompt FROM account_prompts ORDER BY account_id"
            ).fetchall()
        return [(r["account_id"], r["prompt"]) for r in rows]


# ==================== TOKEN LIMITS ====================
class TokenLimitStore(_BaseSQLite):
    """Добовий ліміт токенів на акаунт. 0 = без ліміту."""

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_token_limits (
                    account_id  TEXT PRIMARY KEY,
                    daily_limit INTEGER NOT NULL DEFAULT 0,
                    updated_at  TEXT NOT NULL
                )
            """)

    def get(self, account_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT daily_limit FROM account_token_limits WHERE account_id = ?",
                (account_id,)
            ).fetchone()
        return row["daily_limit"] if row else DEFAULT_TOKEN_LIMIT

    def set(self, account_id: str, limit: int):
        limit = max(0, int(limit))
        with self._lock, self._connect() as conn:
            if limit == 0:
                conn.execute(
                    "DELETE FROM account_token_limits WHERE account_id = ?",
                    (account_id,)
                )
            else:
                conn.execute("""
                    INSERT INTO account_token_limits (account_id, daily_limit, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        daily_limit = excluded.daily_limit,
                        updated_at = excluded.updated_at
                """, (account_id, limit, _now()))

    def all(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT account_id, daily_limit FROM account_token_limits"
            ).fetchall()
        return {r["account_id"]: r["daily_limit"] for r in rows}


# ==================== MESSAGE COUNTERS (для лімітів) ====================
class CounterStore(_BaseSQLite):
    """Скільки токенів витрачено сьогодні на акаунт. Скидається неявно через date-PK."""

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS message_counters (
                    account_id  TEXT NOT NULL,
                    date        TEXT NOT NULL,
                    tokens_used INTEGER NOT NULL DEFAULT 0,
                    msg_count   INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (account_id, date)
                )
            """)

    def get_today(self, account_id: str) -> dict:
        today = _today_utc()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tokens_used, msg_count FROM message_counters "
                "WHERE account_id = ? AND date = ?",
                (account_id, today)
            ).fetchone()
        if row is None:
            return {"tokens_used": 0, "msg_count": 0}
        return {"tokens_used": row["tokens_used"], "msg_count": row["msg_count"]}

    def add(self, account_id: str, tokens: int):
        today = _today_utc()
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO message_counters (account_id, date, tokens_used, msg_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(account_id, date) DO UPDATE SET
                    tokens_used = tokens_used + excluded.tokens_used,
                    msg_count   = msg_count + 1
            """, (account_id, today, max(0, int(tokens))))

    def cleanup_old(self, keep_days: int = 30):
        cutoff = (
            datetime.now(timezone.utc).date() - __import__("datetime").timedelta(days=keep_days)
        ).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM message_counters WHERE date < ?", (cutoff,))


# ==================== FLOW RULES (нодова карта) ====================
class FlowRuleStore(_BaseSQLite):
    """
    Нодова карта дій на акаунт. Зберігається як JSON:
      {"nodes": [{"id": "...", "title": "Привітання"}, ...], "updated_at": "..."}
    """

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_flow_rules (
                    account_id TEXT PRIMARY KEY,
                    nodes_json TEXT NOT NULL DEFAULT '{"nodes": []}',
                    updated_at TEXT NOT NULL
                )
            """)

    def get(self, account_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT nodes_json FROM account_flow_rules WHERE account_id = ?",
                (account_id,)
            ).fetchone()
        if not row:
            return {"nodes": []}
        try:
            return json.loads(row["nodes_json"])
        except Exception:
            return {"nodes": []}

    def set(self, account_id: str, data: dict):
        if not isinstance(data, dict):
            data = {"nodes": []}
        data.setdefault("nodes", [])
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO account_flow_rules (account_id, nodes_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    nodes_json = excluded.nodes_json,
                    updated_at = excluded.updated_at
            """, (account_id, json.dumps(data, ensure_ascii=False), _now()))

    def delete(self, account_id: str):
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM account_flow_rules WHERE account_id = ?", (account_id,)
            )

    def all(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT account_id, nodes_json FROM account_flow_rules"
            ).fetchall()
        result = {}
        for r in rows:
            try:
                result[r["account_id"]] = json.loads(r["nodes_json"])
            except Exception:
                result[r["account_id"]] = {"nodes": []}
        return result

    def add_node(self, account_id: str, title: str, node_id: str = None) -> dict:
        """
        Атомарно read-modify-write: дописує одну ноду до існуючого списку.
        """
        if not account_id:
            raise ValueError("account_id must be non-empty")
        title = (title or "").strip()
        if not title:
            raise ValueError("title must be non-empty")

        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT nodes_json FROM account_flow_rules WHERE account_id = ?",
                (account_id,)
            ).fetchone()
            try:
                data = json.loads(row["nodes_json"]) if row else {"nodes": []}
            except Exception:
                data = {"nodes": []}
            if not isinstance(data, dict):
                data = {"nodes": []}
            nodes = data.get("nodes") or []
            if not isinstance(nodes, list):
                nodes = []

            existing_ids = {n.get("id") for n in nodes if isinstance(n, dict)}
            new_id = node_id or f"n{len(nodes) + 1}"
            suffix = 2
            while new_id in existing_ids:
                new_id = f"{node_id or f'n{len(nodes) + 1}'}_{suffix}"
                suffix += 1

            new_node = {"id": new_id, "title": title}
            nodes.append(new_node)
            data["nodes"] = nodes

            conn.execute("""
                INSERT INTO account_flow_rules (account_id, nodes_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    nodes_json = excluded.nodes_json,
                    updated_at = excluded.updated_at
            """, (account_id, json.dumps(data, ensure_ascii=False), _now()))
            return new_node

    def remove_node(self, account_id: str, node_id: str) -> bool:
        """Атомарно видаляє одну ноду за id, не чіпаючи решту списку."""
        if not account_id or not node_id:
            return False
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT nodes_json FROM account_flow_rules WHERE account_id = ?",
                (account_id,)
            ).fetchone()
            if not row:
                return False
            try:
                data = json.loads(row["nodes_json"])
            except Exception:
                return False
            if not isinstance(data, dict):
                return False
            nodes = data.get("nodes") or []
            new_nodes = [n for n in nodes if not (isinstance(n, dict) and n.get("id") == node_id)]
            if len(new_nodes) == len(nodes):
                return False
            data["nodes"] = new_nodes
            conn.execute("""
                UPDATE account_flow_rules SET nodes_json = ?, updated_at = ?
                WHERE account_id = ?
            """, (json.dumps(data, ensure_ascii=False), _now(), account_id))
            return True


# ==================== INTEGRATIONS (збір даних клієнта) ====================
INTEGRATION_FIELD_TYPES = ("text", "number", "email", "phone", "date")


def _normalize_field(f: dict) -> dict:
    if not isinstance(f, dict):
        return None
    field_id = str(f.get("id") or "").strip()
    label = str(f.get("label") or "").strip()
    if not field_id or not label:
        return None
    ftype = str(f.get("type") or "text").strip().lower()
    if ftype not in INTEGRATION_FIELD_TYPES:
        ftype = "text"
    return {
        "id": field_id,
        "label": label,
        "type": ftype,
        "required": bool(f.get("required")),
    }


class IntegrationStore(_BaseSQLite):
    """
    Схема полів для збору даних на акаунт. Зберігається як JSON:
      {"fields": [{"id": "phone", "label": "Номер телефону", "type": "phone",
                   "required": true}, ...]}
    """

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_integrations (
                    account_id TEXT PRIMARY KEY,
                    fields_json TEXT NOT NULL DEFAULT '{"fields": []}',
                    updated_at TEXT NOT NULL
                )
            """)

    def get(self, account_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT fields_json FROM account_integrations WHERE account_id = ?",
                (account_id,)
            ).fetchone()
        if not row:
            return {"fields": []}
        try:
            data = json.loads(row["fields_json"])
        except Exception:
            return {"fields": []}
        if not isinstance(data, dict):
            return {"fields": []}
        data.setdefault("fields", [])
        return data

    def set(self, account_id: str, fields: list):
        if not account_id:
            raise ValueError("account_id must be non-empty")
        clean = []
        seen_ids = set()
        for f in (fields or []):
            nf = _normalize_field(f)
            if nf and nf["id"] not in seen_ids:
                clean.append(nf)
                seen_ids.add(nf["id"])
        data = {k: v for k, v in self.get(account_id).items() if k != "fields"}  # зберігаємо "collect" тощо
        data["fields"] = clean
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO account_integrations (account_id, fields_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    fields_json = excluded.fields_json,
                    updated_at = excluded.updated_at
            """, (account_id, json.dumps(data, ensure_ascii=False), _now()))
        return clean

    def delete(self, account_id: str):
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM account_integrations WHERE account_id = ?", (account_id,)
            )

    def set_collect(self, account_id: str, source_text: str, rules: dict):
        """Зберігає скомпільовані правила збору поруч із fields (див. collect_engine)."""
        if not account_id:
            raise ValueError("account_id must be non-empty")
        data = self.get(account_id)
        data["collect"] = {"source_text": source_text, "rules": rules}
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO account_integrations (account_id, fields_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    fields_json = excluded.fields_json,
                    updated_at = excluded.updated_at
            """, (account_id, json.dumps(data, ensure_ascii=False), _now()))

    def delete_collect(self, account_id: str):
        data = self.get(account_id)
        if "collect" not in data:
            return
        data.pop("collect", None)
        with self._lock, self._connect() as conn:
            conn.execute("""
                UPDATE account_integrations SET fields_json = ?, updated_at = ?
                WHERE account_id = ?
            """, (json.dumps(data, ensure_ascii=False), _now(), account_id))

    def all(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT account_id, fields_json FROM account_integrations"
            ).fetchall()
        result = {}
        for r in rows:
            try:
                data = json.loads(r["fields_json"])
                data.setdefault("fields", [])
            except Exception:
                data = {"fields": []}
            result[r["account_id"]] = data
        return result


class ClientRecordStore(_BaseSQLite):
    """
    Зібрані дані клієнтів. Один рядок на (account_id, conversation_id) -
    поля накопичуються по мірі того, як AI дізнається їх у розмові.
    """

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS client_records (
                    account_id      TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    data_json       TEXT NOT NULL DEFAULT '{}',
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL,
                    PRIMARY KEY (account_id, conversation_id)
                )
            """)

    def upsert(self, account_id: str, conversation_id: str, new_fields: dict) -> dict:
        """Атомарно read-modify-write: домержує new_fields у вже збережені дані."""
        if not account_id or not conversation_id:
            raise ValueError("account_id and conversation_id must be non-empty")
        new_fields = {
            k: v for k, v in (new_fields or {}).items()
            if v is not None and str(v).strip() != ""
        }
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT data_json, created_at FROM client_records "
                "WHERE account_id = ? AND conversation_id = ?",
                (account_id, conversation_id)
            ).fetchone()
            try:
                data = json.loads(row["data_json"]) if row else {}
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            data.update(new_fields)
            created_at = row["created_at"] if row else _now()
            conn.execute("""
                INSERT INTO client_records (account_id, conversation_id, data_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_id, conversation_id) DO UPDATE SET
                    data_json = excluded.data_json,
                    updated_at = excluded.updated_at
            """, (account_id, conversation_id, json.dumps(data, ensure_ascii=False), created_at, _now()))
            return data

    def get(self, account_id: str, conversation_id: str):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data_json, created_at, updated_at FROM client_records "
                "WHERE account_id = ? AND conversation_id = ?",
                (account_id, conversation_id)
            ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["data_json"])
        except Exception:
            data = {}
        return {"data": data, "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def list_for_account(self, account_id: str, limit: int = 200) -> list:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT conversation_id, data_json, created_at, updated_at
                FROM client_records WHERE account_id = ?
                ORDER BY updated_at DESC LIMIT ?
            """, (account_id, limit)).fetchall()
        out = []
        for r in rows:
            try:
                data = json.loads(r["data_json"])
            except Exception:
                data = {}
            out.append({
                "conversation_id": r["conversation_id"],
                "data": data,
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            })
        return out

    def all(self, limit: int = 500) -> list:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT account_id, conversation_id, data_json, created_at, updated_at
                FROM client_records ORDER BY updated_at DESC LIMIT ?
            """, (limit,)).fetchall()
        out = []
        for r in rows:
            try:
                data = json.loads(r["data_json"])
            except Exception:
                data = {}
            out.append({
                "account_id": r["account_id"],
                "conversation_id": r["conversation_id"],
                "data": data,
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            })
        return out

    def delete(self, account_id: str, conversation_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM client_records WHERE account_id = ? AND conversation_id = ?",
                (account_id, conversation_id)
            )
            return cur.rowcount > 0


class GoogleSheetStore(_BaseSQLite):
    """
    Підключення Google Таблиці на акаунт. Зберігаємо тільки refresh_token.
    """

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS google_sheets (
                    account_id     TEXT PRIMARY KEY,
                    refresh_token  TEXT NOT NULL,
                    spreadsheet_id TEXT,
                    sheet_title    TEXT,
                    connected_at   TEXT NOT NULL,
                    updated_at     TEXT NOT NULL
                )
            """)

    def save_token(self, account_id: str, refresh_token: str):
        if not account_id or not refresh_token:
            raise ValueError("account_id and refresh_token must be non-empty")
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT connected_at FROM google_sheets WHERE account_id = ?",
                (account_id,)
            ).fetchone()
            connected_at = existing["connected_at"] if existing else _now()
            conn.execute("""
                INSERT INTO google_sheets (account_id, refresh_token, spreadsheet_id, sheet_title, connected_at, updated_at)
                VALUES (?, ?, NULL, NULL, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    refresh_token = excluded.refresh_token,
                    updated_at = excluded.updated_at
            """, (account_id, refresh_token, connected_at, _now()))

    def set_spreadsheet(self, account_id: str, spreadsheet_id: str, sheet_title: str):
        with self._lock, self._connect() as conn:
            conn.execute("""
                UPDATE google_sheets SET spreadsheet_id = ?, sheet_title = ?, updated_at = ?
                WHERE account_id = ?
            """, (spreadsheet_id, sheet_title, _now(), account_id))

    def get(self, account_id: str):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM google_sheets WHERE account_id = ?", (account_id,)
            ).fetchone()
        return dict(row) if row else None

    def delete(self, account_id: str):
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM google_sheets WHERE account_id = ?", (account_id,))

    def all(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM google_sheets").fetchall()
        return {r["account_id"]: dict(r) for r in rows}


# ==================== USERS ====================
class UserStore(_BaseSQLite):
    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    email             TEXT UNIQUE NOT NULL,
                    password_hash     TEXT NOT NULL,
                    max_accounts      INTEGER NOT NULL DEFAULT 3,
                    zernio_profile_id TEXT,
                    created_at        TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_accounts (
                    user_id      INTEGER NOT NULL,
                    account_id   TEXT NOT NULL,
                    platform     TEXT,
                    display_name TEXT,
                    connected_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, account_id),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            """)
            # міграції старих БД
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "max_accounts" not in cols:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN max_accounts INTEGER NOT NULL DEFAULT 3"
                )
            if "zernio_profile_id" not in cols:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN zernio_profile_id TEXT"
                )

    # ---------- USERS ----------
    def create_user(self, email: str, password: str) -> int:
        email = email.strip().lower()
        if not email or "@" not in email:
            raise ValueError("Invalid email")
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        with self._lock, self._connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                    (email, pw_hash, _now())
                )
                return cur.lastrowid
            except sqlite3.IntegrityError:
                raise ValueError(f"User {email} already exists")

    def authenticate(self, email: str, password: str):
        email = email.strip().lower()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, password_hash FROM users WHERE email = ?",
                (email,)
            ).fetchone()
        if row is None:
            return None
        if not bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8")):
            return None
        return {"id": row["id"], "email": row["email"]}

    def change_password(self, user_id: int, new_password: str):
        if len(new_password) < 8:
            raise ValueError("Password must be at least 8 characters")
        pw_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (pw_hash, user_id)
            )

    def delete_user(self, user_id: int):
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def get_user(self, user_id: int):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, max_accounts, zernio_profile_id, created_at FROM users WHERE id = ?",
                (user_id,)
            ).fetchone()
        return dict(row) if row else None
        
    def delete_user_completely(self, user_id: int):
        """Повне видалення юзера і всіх його даних.
        Повертає dict з email та списком account_id (для логування)."""
        user = self.get_user(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found")

        accounts = self.get_user_accounts(user_id)
        account_ids = [a["account_id"] for a in accounts]

        with self._lock, self._connect() as conn:
            # 1. Прив'язки (CASCADE мав би спрацювати, але явно надійніше)
            conn.execute("DELETE FROM user_accounts WHERE user_id = ?", (user_id,))
            # 2. Сам юзер
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            # 3. Дані по кожному акаунту
            for acc_id in account_ids:
                conn.execute("DELETE FROM account_prompts WHERE account_id = ?", (acc_id,))
                conn.execute("DELETE FROM account_integrations WHERE account_id = ?", (acc_id,))
                conn.execute("DELETE FROM client_records WHERE account_id = ?", (acc_id,))
                conn.execute("DELETE FROM google_sheets WHERE account_id = ?", (acc_id,))
                conn.execute("DELETE FROM account_token_limits WHERE account_id = ?", (acc_id,))

        return {"email": user["email"], "account_ids": account_ids}

    def set_max_accounts(self, user_id: int, max_accounts: int):
        max_accounts = max(0, min(100, int(max_accounts)))
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET max_accounts = ? WHERE id = ?",
                (max_accounts, user_id)
            )

    def set_zernio_profile_id(self, user_id: int, profile_id: str):
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET zernio_profile_id = ? WHERE id = ?",
                (profile_id, user_id)
            )

    def get_zernio_profile_id(self, user_id: int):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT zernio_profile_id FROM users WHERE id = ?",
                (user_id,)
            ).fetchone()
        return row["zernio_profile_id"] if row else None

    def get_user_by_email(self, email: str):
        email = (email or "").strip().lower()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, max_accounts, zernio_profile_id, created_at FROM users WHERE email = ?",
                (email,)
            ).fetchone()
        return dict(row) if row else None

    def list_users(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, email, max_accounts, zernio_profile_id, created_at FROM users ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def list_users_with_accounts(self):
        users = self.list_users()
        for u in users:
            u["accounts"] = self.get_user_accounts(u["id"])
        return users

    # ---------- USER ↔ ACCOUNT ----------
    def assign_account(self, user_id: int, account_id: str,
                       platform: str = None, display_name: str = None):
        if not account_id:
            raise ValueError("account_id required")
        user = self.get_user(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found")

        max_acc = user.get("max_accounts", DEFAULT_MAX_ACCOUNTS)
        if max_acc > 0:
            current = len(self.get_user_accounts(user_id))
            if current >= max_acc:
                raise ValueError(
                    f"Ліміт акаунтів досягнуто ({current}/{max_acc}). "
                    f"Підвищте ліміт або видаліть зайвий акаунт."
                )

        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT user_id FROM user_accounts WHERE account_id = ? AND user_id != ?",
                (account_id, user_id)
            ).fetchone()
            if existing:
                raise ValueError(f"Account {account_id} is already assigned to another user")
            conn.execute("""
                INSERT INTO user_accounts (user_id, account_id, platform, display_name, connected_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, account_id) DO UPDATE SET
                    platform = excluded.platform,
                    display_name = excluded.display_name
            """, (user_id, account_id, platform, display_name, _now()))

    def unassign_account(self, user_id: int, account_id: str):
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM user_accounts WHERE user_id = ? AND account_id = ?",
                (user_id, account_id)
            )

    def get_user_accounts(self, user_id: int):
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT account_id, platform, display_name, connected_at
                FROM user_accounts WHERE user_id = ?
                ORDER BY connected_at
            """, (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def count_user_accounts(self, user_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM user_accounts WHERE user_id = ?",
                (user_id,)
            ).fetchone()
        return row["n"]

    def find_user_by_account(self, account_id: str):
        with self._connect() as conn:
            row = conn.execute("""
                SELECT u.id, u.email
                FROM users u
                JOIN user_accounts ua ON ua.user_id = u.id
                WHERE ua.account_id = ?
            """, (account_id,)).fetchone()
        return dict(row) if row else None
