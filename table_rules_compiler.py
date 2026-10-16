"""
table_rules_compiler.py — одноразова компіляція правил запису в таблицю.

Власник пише природною мовою "куди що писати" — LLM ОДИН раз перетворює
це на структурований JSON. Далі клієнтський бот користується ним без
жодного звернення до AI.
"""
import json
import re

COMPILE_SYSTEM_PROMPT = """You compile table-writing rules.
Input: (1) table structure (row-1 headers with column letters); (2) the owner's free text on what to write where.
Output STRICT JSON only, no prose, no ```:
{"headers_row": <int, usually 1>, "data_start_row": <int, usually 2>,
 "columns": {"<field>": "<COLUMN LETTER>"}, "fixed_cells": {"<field>": "<A1 address>"}, "notes": ["<short note>"]}
Rules:
- "columns": value goes into a new row each time (name, phone, email...). "fixed_cells": value always goes to the same cell ("order total in F2").
- Fields: semantic latin ids (name, phone, order_total...). Columns: uppercase letters only ("B", "AA").
- Don't invent fields absent from the owner's text. "Write to column B" without a field -> use the matching row-1 header (headers beat letters).
- Ambiguous -> skip the field and add a "notes" entry.
- If an "Available fields" section is given, use ONLY those ids exactly as written (the client bot passes data under them); match the owner's words by name and meaning. Fields not in the list: omit, mention in "notes".
"""


def _parse_header_line(header_cells: dict, max_cols: int = 26) -> str:
    from table_store import col_index_to_letter
    parts = []
    for i in range(max_cols):
        cid = col_index_to_letter(i)
        val = (header_cells.get(f"{cid}1") or "").strip()
        if val:
            parts.append(f"{cid}1: {val}")
    return "   ".join(parts) if parts else "(no headers found)"


def compile_rules(groq_client, model: str, table_store,
                  user_id: int, table_id: str,
                  source_text: str, known_fields=None) -> dict:
    if not groq_client:
        raise ValueError("AI unavailable")

    cells = table_store.get_cells(user_id, table_id)
    header_line = _parse_header_line(cells)

    known_fields = [f for f in (known_fields or []) if (f.get("id") or "").strip()]
    fields_block = ""
    if known_fields:
        fields_block = "Available fields (id: label):\n" + "\n".join(
            f"- {f['id']}: {f.get('label') or f['id']}" for f in known_fields) + "\n\n"

    user_msg = (
        f"Table headers (row 1):\n{header_line}\n\n"
        f"{fields_block}"
        f"Owner text:\n\"\"\"\n{source_text.strip()}\n\"\"\"\n\n"
        f"Return the JSON."
    )

    resp = groq_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": COMPILE_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.1,
        max_tokens=4000,
        reasoning_effort="low",
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

    headers_row = int(data.get("headers_row") or 1)
    data_start_row = int(data.get("data_start_row") or (headers_row + 1))

    columns = data.get("columns") or {}
    if not isinstance(columns, dict):
        columns = {}
    clean_columns = {}
    for k, v in columns.items():
        col = str(v or "").strip().upper()
        if col and col.isalpha():
            clean_columns[str(k).strip().lower()] = col

    fixed = data.get("fixed_cells") or {}
    if not isinstance(fixed, dict):
        fixed = {}
    clean_fixed = {}
    for k, v in fixed.items():
        a1 = str(v or "").strip().upper()
        if a1 and re.match(r"^[A-Z]+\d+$", a1):
            clean_fixed[str(k).strip().lower()] = a1

    notes = data.get("notes") or []
    if not isinstance(notes, list):
        notes = []
    clean_notes = [str(n)[:200] for n in notes if str(n).strip()]

    # Бот пише дані під id полів інтеграції — ключі, яких там нема, ніколи
    # не спрацюють, тому відкидаємо їх і чесно повідомляємо власнику.
    known_ids = {str(f["id"]).strip().lower() for f in known_fields}
    if known_ids:
        dropped = []
        for d in (clean_columns, clean_fixed):
            for k in list(d):
                if k not in known_ids:
                    dropped.append(k)
                    del d[k]
        if dropped:
            clean_notes.insert(0, "Пропущено (немає серед полів збору даних): " + ", ".join(dropped))
        if not clean_columns and not clean_fixed:
            raise ValueError("Не вдалося зіставити жодне з полів збору даних із таблицею. "
                             "Вкажіть у тексті, яке поле в яку колонку писати.")

    return {
        "headers_row": headers_row,
        "data_start_row": data_start_row,
        "columns": clean_columns,
        "fixed_cells": clean_fixed,
        "notes": clean_notes[:5],
    }
