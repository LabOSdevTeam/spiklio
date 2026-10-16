"""
flow_engine.py — генератор тексту "план дій" з нодової карти.

Карта зберігається як JSON:
  {
    "nodes": [
      {"id": "n1", "title": "Привітання!"},
      {"id": "n2", "title": "Дізнатися яку саме послугу хоче клієнт"},
      ...
    ]
  }

Функція build_flow_prompt(data) повертає готовий текст для вставки
в системний промпт AI. Модель бачить його як "план дій" і старається
провести користувача через ці кроки.
"""


def build_flow_prompt(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    nodes = data.get("nodes") or []
    if not isinstance(nodes, list) or not nodes:
        return ""

    lines = []
    for i, n in enumerate(nodes, 1):
        title = (n.get("title") or "").strip()
        if not title:
            continue
        lines.append(f"{i}. {title}")

    if not lines:
        return ""

    return (
        "=== FLOW RULES (план дій, якого ти маєш дотримуватися) ===\n"
        "Це рекомендований порядок кроків для діалогу з цим користувачем. "
        "Ти МОЖЕШ відхилятися від нього, якщо користувач іде в іншу тему, "
        "але ЗАВЖДИ старайся м'яко направляти розмову до цих кроків. "
        "Не перераховуй кроки користувачу і не згадуй, що в тебе є \"план\" — "
        "просто природно веди діалог.\n\n"
        "Кроки:\n" + "\n".join(lines) + "\n"
        "=== END FLOW RULES ==="
    )


def is_empty(data: dict) -> bool:
    if not isinstance(data, dict):
        return True
    nodes = data.get("nodes") or []
    return not any((n.get("title") or "").strip() for n in nodes)
