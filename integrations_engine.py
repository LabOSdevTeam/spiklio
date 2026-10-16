"""
integrations_engine.py — schema для tool_call save_client_info.

Адмін/клієнт описує список полів (id, label, type, required) у
account_integrations (storage.py). Звідси будуємо ЛИШЕ tool-схему
(function calling), яку AI викликає, коли клієнт назвав значення поля.

ПРАВИЛА ПОВЕДІНКИ (як питати, що не показувати клієнту, коли які поля
збирати) — НЕ тут. Їх пише власник акаунта в системному промпті в /uadmin.
Цей модуль суто технічний: дає LLM список id-полів для tool_call і
короткий опис самої функції (без опису неможливо викликати tool).
"""

TOOL_NAME = "save_client_info"

_TYPE_SCHEMA = {
    "text":   {"type": "string"},
    "number": {"type": "number"},
    "email":  {"type": "string", "description_suffix": " (формат: email)"},
    "phone":  {"type": "string", "description_suffix": " (формат: номер телефону, як його назвав клієнт)"},
    "date":   {"type": "string", "description_suffix": " (формат: дата, як її назвав клієнт)"},
}


def is_empty(data: dict) -> bool:
    if not isinstance(data, dict):
        return True
    fields = data.get("fields") or []
    return not any((f.get("id") or "").strip() and (f.get("label") or "").strip() for f in fields)


def build_integration_tool(data: dict) -> dict:
    """Повертає tool-опис у форматі Chat Completions (OpenAI-сумісний, який
    приймає і Groq), або None якщо полів не налаштовано."""
    if is_empty(data):
        return None

    fields = [f for f in (data.get("fields") or []) if (f.get("id") or "").strip()]
    properties = {}
    for f in fields:
        field_id = f["id"]
        ftype = f.get("type") or "text"
        schema = dict(_TYPE_SCHEMA.get(ftype, _TYPE_SCHEMA["text"]))
        suffix = schema.pop("description_suffix", "")
        properties[field_id] = {
            "type": schema["type"],
            "description": (f.get("label") or field_id) + suffix,
        }

    parameters = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }

    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "Зберегти в CRM-таблицю ті дані клієнта, які він ЩОЙНО і ЯВНО "
                "повідомив у цій розмові. Викликай тільки з полями, значення "
                "яких клієнт справді назвав — ніколи не вигадуй і не вгадуй "
                "значення. Можна викликати кілька разів у розмові, кожного "
                "разу передаючи лише нові/уточнені поля; попередньо збережені "
                "поля не загубляться."
            ),
            "parameters": parameters,
        },
    }
