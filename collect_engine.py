"""
collect_engine.py — правила ЗБОРУ даних: компіляція + рантайм.

Власник пише природною мовою, як збирати поля ("спочатку тип прибирання,
кімнати й ванни питай разом, нотатки не питай"). LLM ОДИН раз перетворює це
на JSON (compile_rules). Далі КОД (а не модель) вирішує, про що питати зараз:
build_collect_block() віддає моделі лише одне наступне поле / групу.

Правила зберігаються в account_integrations поруч із fields:
  {"fields": [...], "collect": {"source_text": "...", "rules": {...}}}

Формат rules:
  {"mode": "sequential" | "batch" | "free",
   "order": ["service_type", ...],      # порядок, решта полів — у порядку списку
   "groups": [["bedrooms", "bathrooms"]],# питати разом (в sequential)
   "optional": ["notes"],               # не питати активно
   "batch_size": 4,                     # для mode=batch
   "notes": ["..."]}

Без правил — безпечний дефолт: sequential у порядку списку полів.
"""
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

MODES = ("sequential", "batch", "free")
DEFAULT_RULES = {"mode": "sequential", "order": [], "groups": [], "optional": [],
                 "batch_size": 4, "notes": [],
                 "normalize": {"language": "", "timezone": "", "date_format": "", "notes": ""}}

# Оформлення значень перед записом (мова, дати, одиниці). Налаштовує власник у тому ж
# тексті правил збору; без жодного налаштування значення лишаються «як сказав клієнт».
NORMALIZE_KEYS = ("language", "timezone", "date_format", "notes")
DEFAULT_TZ = "UTC"
DEFAULT_DATE_FORMAT = "YYYY-MM-DD HH:mm"

FIELDS_MARKER = "###FIELDS###"

COMPILE_SYSTEM_PROMPT = """You are a compiler of data-collection rules for a chat with a client.
You are given:
  1. A list of available fields (id: label).
  2. The business owner's text, in natural language (it may be written in ANY language,
     e.g. Ukrainian or English): in what order and how to collect the data.

Turn it into STRICT JSON. No text outside the JSON, no ``` fences.

Response format:
{
  "mode": "sequential" | "batch" | "free",
  "order": ["<id>", ...],
  "groups": [["<id>", "<id>"], ...],
  "optional": ["<id>", ...],
  "batch_size": <number 2-6>,
  "normalize": {
    "language": "<language of the values in the table, English name, e.g. English, Ukrainian; \\"client\\" if no translation is needed; \\"\\" if not mentioned>",
    "timezone": "<IANA timezone, e.g. America/Toronto; \\"\\" if not named>",
    "date_format": "<date/time format as the owner described it; \\"\\" if not named>",
    "notes": "<other rules for FORMATTING the values, short, in the owner's words; \\"\\" if none>"
  },
  "notes": ["<short note>", "..."]
}

Rules:
- mode "sequential" is the default: one question (or one group) at a time.
- mode "batch" only if the owner explicitly asks for a questionnaire / asking several fields
  in one message. Then set batch_size.
- mode "free" only if the owner explicitly says the bot decides the order and pace itself.
- "order": the order in which the owner wants to ask. Only ids from the list.
  If no order is given, an empty array.
- "groups": fields the owner wants asked TOGETHER in one message
  ("rooms and bathrooms together"). Each group has at least 2 ids.
- "optional": fields that must NOT be asked actively ("don't ask for notes",
  "only if the client says it themselves").
- Fill "normalize" ONLY with what the owner explicitly said about formatting values in the table
  (language, date format, units, transliteration, phone format, etc.). Do not invent anything:
  if not named, use an empty string. Set a timezone only if the owner named it or named a
  city/country directly.
- Use ONLY ids from the field list, exactly as written. Do not invent fields.
- If something is ambiguous or does not map to the fields, skip it and add it to "notes".
"""



def _fields_of(integration_data):
    return [f for f in ((integration_data or {}).get("fields") or [])
            if (f.get("id") or "").strip()]


def normalize_rules(data, known_ids):
    """Чистить сирий JSON від LLM. Повертає (rules, dropped_ids)."""
    if not isinstance(data, dict):
        data = {}
    known = {str(i).strip(): str(i).strip() for i in known_ids}
    lower = {k.lower(): k for k in known}
    dropped = []

    def canon(x):
        s = str(x or "").strip()
        if s in known:
            return s
        c = lower.get(s.lower())
        if c is None and s:
            dropped.append(s)
        return c

    mode = str(data.get("mode") or "sequential").strip().lower()
    if mode not in MODES:
        mode = "sequential"

    order, seen = [], set()
    for x in (data.get("order") or []) if isinstance(data.get("order"), list) else []:
        c = canon(x)
        if c and c not in seen:
            order.append(c)
            seen.add(c)

    groups, used = [], set()
    for g in (data.get("groups") or []) if isinstance(data.get("groups"), list) else []:
        if not isinstance(g, list):
            continue
        cg = []
        for x in g:
            c = canon(x)
            if c and c not in used and c not in cg:
                cg.append(c)
        if len(cg) >= 2:
            groups.append(cg)
            used.update(cg)

    optional, oseen = [], set()
    for x in (data.get("optional") or []) if isinstance(data.get("optional"), list) else []:
        c = canon(x)
        if c and c not in oseen:
            optional.append(c)
            oseen.add(c)

    try:
        batch_size = int(data.get("batch_size") or 4)
    except (TypeError, ValueError):
        batch_size = 4
    batch_size = max(2, min(6, batch_size))

    notes = data.get("notes") if isinstance(data.get("notes"), list) else []
    notes = [str(n)[:200] for n in notes if str(n).strip()][:5]
    if dropped:
        notes.insert(0, "Skipped (not among the fields): " + ", ".join(sorted(set(dropped))))

    nz_raw = data.get("normalize") if isinstance(data.get("normalize"), dict) else {}
    norm = {k: str(nz_raw.get(k) or "").strip()[:300 if k == "notes" else 60] for k in NORMALIZE_KEYS}
    if norm["timezone"]:
        try:
            ZoneInfo(norm["timezone"])
        except Exception:
            notes.insert(0, f"Unknown timezone: {norm['timezone']}")
            norm["timezone"] = ""

    return {"mode": mode, "order": order, "groups": groups, "optional": optional,
            "batch_size": batch_size, "notes": notes[:5], "normalize": norm}, dropped


def compile_rules(groq_client, model: str, integration_data: dict, source_text: str) -> dict:
    """Одноразова компіляція тексту власника у rules. Кидає ValueError."""
    if not groq_client:
        raise ValueError("AI unavailable")
    fields = _fields_of(integration_data)
    if not fields:
        raise ValueError("Add the fields to collect first (Fields tab).")
    text = (source_text or "").strip()
    if not text:
        raise ValueError("Rules text is empty.")

    fields_block = "\n".join(f"- {f['id']}: {f.get('label') or f['id']}" for f in fields)
    user_msg = (f"Available fields (id: label):\n{fields_block}\n\n"
                f"Owner's text about data collection:\n\"\"\"\n{text}\n\"\"\"\n\n"
                f"Return JSON per the schema.")
    resp = groq_client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": COMPILE_SYSTEM_PROMPT},
                  {"role": "user", "content": user_msg}],
        temperature=0.1, max_tokens=2000, reasoning_effort="low",
        response_format={"type": "json_object"},
    )
    raw = (resp.choices[0].message.content or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except Exception as e:
        raise ValueError(f"LLM returned invalid JSON: {e}; raw={raw[:300]!r}")
    if not isinstance(data, dict):
        raise ValueError("LLM JSON is not an object")
    rules, _ = normalize_rules(data, [f["id"] for f in fields])
    return rules


# ==================== RUNTIME ====================

def get_rules(integration_data) -> dict:
    c = (integration_data or {}).get("collect") or {}
    r = c.get("rules") if isinstance(c, dict) else None
    if not isinstance(r, dict):
        return dict(DEFAULT_RULES)
    ids = [f["id"] for f in _fields_of(integration_data)]
    rules, _ = normalize_rules(r, ids)  # відсікає id, яких уже нема (поле видалили)
    return rules


def _ordered_ids(fields, rules):
    ids = [f["id"] for f in fields]
    head = [i for i in rules.get("order", []) if i in ids]
    return head + [i for i in ids if i not in head]


def pending_ids(integration_data, collected) -> list:
    """Поля, про які ще треба активно питати (у потрібному порядку)."""
    fields = _fields_of(integration_data)
    rules = get_rules(integration_data)
    optional = set(rules.get("optional", []))
    return [i for i in _ordered_ids(fields, rules)
            if i not in (collected or {}) and i not in optional]


def next_targets(integration_data, collected) -> list:
    """Що питати ЗАРАЗ: [id] або кілька id (група / batch). [] — усе зібрано."""
    pend = pending_ids(integration_data, collected)
    if not pend:
        return []
    rules = get_rules(integration_data)
    mode = rules["mode"]
    if mode == "batch":
        return pend[:rules["batch_size"]]
    first = pend[0]
    for g in rules.get("groups", []):
        if first in g:
            members = [i for i in g if i in pend]
            return members or [first]
    return [first]


def _label(fields, fid):
    for f in fields:
        if f["id"] == fid:
            return f.get("label") or fid
    return fid


def inline_supported(integration_data) -> bool:
    """One-call mode (no separate extractor): free mode, or sequential mode without groups."""
    rules = get_rules(integration_data)
    return rules["mode"] == "free" or (rules["mode"] == "sequential" and not rules.get("groups"))


def _marker_rules() -> str:
    return (f'If the client gives NEW field values, end your reply with a line: '
            f'{FIELDS_MARKER} {{"id":"value"}} (ids from Fields only, values from THIS message only, '
            f'omit if none; hidden from the client).\n')


def build_collect_block(integration_data, collected, inline: bool = False) -> str:
    """DATA block: pending fields, compact collected values and a narrow directive."""
    fields = _fields_of(integration_data)
    if not fields:
        return ""
    valid_ids = {f["id"] for f in fields}
    collected = {k: v for k, v in (collected or {}).items() if k in valid_ids}
    rules_all = get_rules(integration_data)
    mode = rules_all["mode"]
    inline = bool(inline) and inline_supported(integration_data)

    pend = [f for f in fields if f["id"] not in collected]
    pend_line = ("Fields (id: label): " + "; ".join(f"{f['id']}: {_label(fields, f['id'])}" for f in pend)
                 + "\n") if pend else ""

    def head(show_collected=True):
        h = "\n\n=== DATA: client info ===\n" + pend_line
        if show_collected and collected:
            h += "Collected: " + "; ".join(f"{k}={str(v).strip()[:120]}" for k, v in collected.items()) + "\n"
        h += _marker_rules()
        if inline:
            norm = rules_all.get("normalize") or {}
            nb = build_normalize_block(norm)
            if nb:
                h += _now_line(norm) + nb
        return h

    end = "=== END DATA ==="
    if mode == "free":
        todo = pending_ids(integration_data, collected)
        return head() + ("Missing: " + (", ".join(todo) if todo else "none")
                         + ". Don't re-ask collected. No placeholders like {id}.\n") + end

    targets = next_targets(integration_data, collected)
    if not targets:
        vals = "\n".join(f"- {_label(fields, f['id'])}: {str(collected[f['id']]).strip()[:200]}"
                         for f in fields if str(collected.get(f['id'], '')).strip())
        return head(False) + ("DIRECTIVE: all required details collected; ask nothing more. Write ONE short "
                              "summary using ONLY these values, then ask to confirm:\n" + vals
                              + "\nNo placeholders like {id}.\n") + end

    if inline:
        order = " > ".join(pending_ids(integration_data, collected))
        body = (f"DIRECTIVE: you last asked about {targets[0]} ({_label(fields, targets[0])}). Order of pending: {order}. "
                "FIRST record in the marker every pending field THIS message answers (a short reply answers the "
                "field you last asked about). THEN: if pending fields remain unanswered, ask ONE short question about "
                "the first of them in that order (if the client asked something else, answer it briefly first); "
                "if nothing remains, write ONE short summary of Collected + this message's values and ask to confirm. "
                "Never ask about a field this message answers.\n")
    elif len(targets) == 1:
        body = (f"DIRECTIVE: ask only about {targets[0]} ({_label(fields, targets[0])}). ONE short question; "
                "if the client asked something else, answer it briefly first. No summary yet.\n")
    elif mode == "batch":
        lines = "\n".join(f"{i}. {_label(fields, t)}" for i, t in enumerate(targets, 1))
        body = "DIRECTIVE: in ONE message ask for all of these as a short numbered list, nothing else:\n" + lines + "\n"
    else:
        names = ", ".join(f"{t} ({_label(fields, t)})" for t in targets)
        body = f"DIRECTIVE: in ONE natural sentence ask about: {names}. Nothing else; no summary yet.\n"
    return head() + body + "No placeholders like {id}.\n" + end


def enforce_reply_shape(reply: str, integration_data, collected) -> str:
    """Страховка: у sequential з одним полем лишаємо тільки перше питання
    (відсікає склеювання «…?Могли б…?…»). В інших режимах/станах не чіпаємо."""
    if not reply or not integration_data:
        return reply
    rules = get_rules(integration_data)
    if rules["mode"] != "sequential":
        return reply
    targets = next_targets(integration_data, collected)
    if len(targets) != 1:
        return reply
    m = re.search(r"\?", reply)
    if not m or "http" in reply[:m.end()].lower():
        return reply
    cut = reply[:m.end()].strip()
    return cut or reply


# ==================== EXTRACTION (окремий виклик, не залежить від маркера) ====================

EXTRACT_SYSTEM_PROMPT = """Extract client data from the message. Output JSON only: {"<field id>": "<value>"}.
- Only ids from "Pending", only values the client EXPLICITLY stated (any language). Never guess.
- A short answer ("2", "House", "1pm") belongs to the field the bot's last question asked about; one message may fill several fields.
- Nothing stated (question, greeting, yes/no, small talk) -> {}."""

# Acknowledgements that never carry field values: skip the LLM call entirely.
_TRIVIAL = {"ok", "okay", "ок", "окей", "так", "ні", "да", "нет", "yes", "no", "yep", "nope", "thanks",
            "thx", "thank you", "дякую", "дякуємо", "спасибі", "спасибо", "добре", "ага", "угу", "ясно",
            "привіт", "вітаю", "hi", "hello", "hey", "добрий день", "доброго дня", "добрий вечір"}


def _is_trivial(text: str) -> bool:
    t = re.sub(r"[^\w\s]", "", (text or "").lower(), flags=re.UNICODE).strip()
    return not t or t in _TRIVIAL


def _is_pure_question(text: str) -> bool:
    """Very short question without digits ('how much?'): carries no field values. The main reply's
    ###FIELDS### marker still catches anything that slips through."""
    t = (text or "").strip()
    return t.endswith("?") and len(t) <= 40 and not any(c.isdigit() for c in t)


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
_EMAIL_FIELD_RE = re.compile(r"(mail|пошт|почт)", re.I)
_PHONE_FIELD_RE = re.compile(r"(phone|tel|телеф|моб|mobile|viber|whatsapp)", re.I)

# A date/time must never be taken for a phone ("14.10.26 8:00" has 7 digits!).
_DATE_LIKE_RE = re.compile(r"(?<![\d.])\d{1,2}[./-]\d{1,2}(?:[./-](?:\d{4}|\d{2}))?(?![\d])")
_TIME_RE = re.compile(r"\d{1,2}:\d{2}")
_PHONE_KIND_RE = re.compile(r"\b(phone|telephone|tel|mobile|viber|whatsapp)\b|телеф|номер\s+тел|моб", re.I)
_EMAIL_KIND_RE = re.compile(r"\b(e-?mail)\b|пошт|почт", re.I)


def _looks_like_date_or_time(v: str) -> bool:
    v = (v or "").strip()
    return bool(_DATE_LIKE_RE.match(v) or _TIME_RE.search(v))


def _kind(f: dict) -> str:
    t = str(f.get("type") or "").lower()
    if t in ("phone", "email"):
        return t
    s = f"{f.get('id', '')} {f.get('label') or ''}"
    if _PHONE_KIND_RE.search(s):
        return "phone"
    if _EMAIL_KIND_RE.search(s):
        return "email"
    return "text"


def validate_value(f: dict, value, last_assistant: str = "") -> bool:
    """Last line of defence before a value is written to the record/table.
    Rejects: bare punctuation ("?"), echo of the bot's own question, a date/time or
    wrong-length number in a phone field, a non-email in an email field."""
    v = str(value if value is not None else "").strip()
    if not v or not re.search(r"[^\W_]", v, re.U):
        return False
    vl = " ".join(v.lower().split())
    la = " ".join((last_assistant or "").lower().split())
    if la and len(vl) >= 12 and (vl in la or (len(la) >= 12 and la in vl)):
        return False
    kind = _kind(f)
    if kind == "phone":
        digits = re.sub(r"\D", "", v)
        if not (7 <= len(digits) <= 15) or _looks_like_date_or_time(v):
            return False
    elif kind == "email" and not _EMAIL_RE.search(v):
        return False
    return True


def clean_values(integration_data, new_fields: dict, last_assistant: str = "", log=None) -> dict:
    """Filters {id: value} through validate_value. Safe to call on any source (extractor, marker, fallback)."""
    if not new_fields:
        return {}
    by_id = {f["id"]: f for f in _fields_of(integration_data)}
    out = {}
    for k, v in new_fields.items():
        f = by_id.get(k) or {"id": k}
        if validate_value(f, v, last_assistant):
            out[k] = v
        elif log:
            log(f"clean_values() REJECTED {k}={str(v)[:60]!r}")
    return out


def _local_extract(todo, text):
    """No-LLM path: the message is only a phone or only an email and exactly one pending field fits."""
    t = (text or "").strip()
    out = {}
    m = _EMAIL_RE.search(t)
    if m and len(t) <= len(m.group(0)) + 4:
        c = [f for f in todo if _EMAIL_FIELD_RE.search(f"{f['id']} {f.get('label') or ''}")]
        if len(c) == 1:
            out[c[0]["id"]] = m.group(0)
    m = _PHONE_RE.search(t)
    if m and (_looks_like_date_or_time(m.group(0)) or _TIME_RE.search(t)):
        m = None   # that's a date/time, not a phone
    if not out and m and len(re.sub(r"\D", "", m.group(0))) >= 7 and len(t) <= len(m.group(0)) + 4:
        c = [f for f in todo if _PHONE_FIELD_RE.search(f"{f['id']} {f.get('label') or ''}")]
        if len(c) == 1:
            out[c[0]["id"]] = m.group(0).strip()
    return out


def _normalize_active(norm) -> bool:
    return isinstance(norm, dict) and any((norm.get(k) or "").strip() for k in NORMALIZE_KEYS)


def build_normalize_block(norm: dict) -> str:
    if not _normalize_active(norm):
        return ""
    lang = (norm.get("language") or "").strip()
    datefmt = (norm.get("date_format") or "").strip() or DEFAULT_DATE_FORMAT
    lines = ["VALUE FORMAT (overrides 'as the client said' for form, not content):"]
    if lang and lang.lower() not in ("client", "as client wrote", "same"):
        lines.append(f"- Language: {lang} (translate words, transliterate names/addresses, keep digits).")
    else:
        lines.append("- Keep the client's language.")
    lines += [
        "- Number words -> digits. Bare number: take the unit from the bot's last question (no conversion); "
        "'about 700' -> '~700'.",
        f"- Date/time: {datefmt}; resolve relative words ('tomorrow') from the current date; 'd.m' = day.month; "
        "year unnamed -> nearest future; time unnamed -> date only.",
        "- Phones/emails unchanged. Add nothing the client didn't say.",
    ]
    notes = (norm.get("notes") or "").strip()
    if notes:
        lines.append(f"- Owner rules (value formatting only): {notes}")
    return "\n".join(lines) + "\n"


def _now_line(norm: dict) -> str:
    tz = (norm or {}).get("timezone") or DEFAULT_TZ
    try:
        now = datetime.now(ZoneInfo(tz))
    except Exception:
        tz = DEFAULT_TZ
        now = datetime.now(ZoneInfo(tz))
    return f"Current date: {now.strftime('%a, %Y-%m-%d, %H:%M')} ({tz})\n\n"


# Client changes/corrects something already given (or the order is complete and he sends a number).
_CHANGE_WORDS_RE = re.compile(
    r"(змін|замін|перенес|перенест|поміня|помін|виправ|уточн|насправді|помилив|не\s+так|а\s+не\b|"
    r"change|reschedul|postpone|instead|actually|correct|wrong|update)", re.I)

EXTRACT_EDIT_RULES = ("- 'Current' fields may be overwritten ONLY if the client explicitly changes or corrects "
                      "that value; then return the NEW value. Never repeat unchanged Current values.\n"
                      "- Use the bot's last question to decide which field a value belongs to; never move a value "
                      "into a different field than the one asked about.")


def extract_fields(groq_client, model: str, integration_data: dict, collected: dict,
                   user_text: str, last_assistant: str = "", edit_only: bool = False) -> dict:
    """Витягує з повідомлення клієнта значення ще не зібраних полів. Не кидає винятків:
    при будь-якій помилці повертає {} (тоді лишається запасний маркер ###FIELDS###)."""
    try:
        if not groq_client or _is_trivial(user_text) or _is_pure_question(user_text):
            return {}
        fields = _fields_of(integration_data)
        collected = collected or {}
        todo = [f for f in fields if f["id"] not in collected]
        have = [f for f in fields if f["id"] in collected]
        if edit_only and todo:
            return {}          # inline mode: the main reply handles pending fields itself
        change = bool(_CHANGE_WORDS_RE.search(user_text))
        if not todo and not (change or re.search(r"\d", user_text)):
            return {}          # complete record, nothing that looks like a change -> no LLM call
        edit = bool(have) and (not todo or change)
        scope = fields if edit else todo
        if not scope:
            return {}
        loc = _local_extract(scope, user_text)
        if loc:
            return loc
        lines = "\n".join(f"- {f['id']}: {f.get('label') or f['id']}" for f in todo) or "(none)"
        norm = get_rules(integration_data).get("normalize") or {}
        norm_block = build_normalize_block(norm)
        system_prompt = EXTRACT_SYSTEM_PROMPT + ("\n" + norm_block if norm_block else "")
        cur_block = ""
        if edit:
            system_prompt += "\n" + EXTRACT_EDIT_RULES
            cur_block = "Current (id: label = value):\n" + "\n".join(
                f"- {f['id']}: {f.get('label') or f['id']} = {str(collected[f['id']]).strip()[:80]}"
                for f in have) + "\n"
        date_line = _now_line(norm) if (norm_block or edit) else ""
        user_msg = (f"{date_line}Pending (id: label):\n{lines}\n{cur_block}"
                    f"Bot's last question: {(last_assistant or '(none)')[:250]}\n"
                    f"Client: {user_text.strip()[:1000]}")
        resp = groq_client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_msg}],
            temperature=0, max_tokens=300, reasoning_effort="low",
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        ids = {f["id"]: f["id"] for f in scope}
        lower = {k.lower(): k for k in ids}
        out = {}
        for k, v in data.items():
            cid = ids.get(str(k).strip()) or lower.get(str(k).strip().lower())
            if not cid or isinstance(v, (dict, list)):
                continue
            val = str(v).strip()
            if val and val.lower() not in ("null", "none", "n/a", "unknown"):
                if cid in collected and str(collected[cid]).strip() == val:
                    continue   # unchanged
                out[cid] = val[:200]
        return out
    except Exception:
        return {}
