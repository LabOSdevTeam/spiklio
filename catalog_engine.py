"""
catalog_engine.py — RAG for the catalog: builds a compact DATA block about items,
appended by worker.get_ai_reply() to the context. No behaviour rules here
(the owner writes those in the system prompt in /uadmin) — only data.
Budget-tuned: small inline limit, top-3 search, short descriptions, URLs only on request.
"""
import re
from catalog_store import tokens

FULL_INLINE_MAX_ITEMS = 12
FULL_INLINE_MAX_CHARS = 1800
SEARCH_TOP_K = 3
CONTEXT_CHAR_BUDGET = 1500
MAX_CATEGORIES_SHOWN = 8

_KIND_LABEL = {"product": "", "service": "service", "subscription": "subscription",
               "rental": "rental", "course": "course", "event": "event",
               "package": "package", "other": ""}
_AVAIL_LABEL = {"available": "", "out_of_stock": "OUT OF STOCK",
                "preorder": "preorder", "by_request": "on request",
                "unavailable": "discontinued"}
_WANT_URL_RE = re.compile(r"(link|url|http|site|website|where to buy|посилан|ссылк|сайт|де купити|де замовити)", re.I)


def _fmt_num(x) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".") if x is not None else ""


def format_price(it: dict) -> str:
    cur = (it.get("currency") or "").strip()
    unit = (it.get("unit") or "").strip()
    suffix = (f" {cur}" if cur else "") + (f"/{unit}" if unit else "")
    t, p, pm = it.get("price_type"), it.get("price"), it.get("price_max")
    if t == "free":
        return "free"
    if t == "on_request" or p is None:
        return "price on request"
    if t == "from":
        return f"from {_fmt_num(p)}{suffix}"
    if t == "range" and pm is not None:
        return f"{_fmt_num(p)}-{_fmt_num(pm)}{suffix}"
    return f"{_fmt_num(p)}{suffix}"


def render_item(it: dict, desc_len: int = 90, show_catalog: bool = False, with_url: bool = False) -> str:
    head = it["name"]
    meta_bits = [_KIND_LABEL.get(it.get("kind"), ""), it.get("category", "")]
    if show_catalog and it.get("_catalog_name"):
        meta_bits.append(it["_catalog_name"])
    meta = ", ".join(x for x in meta_bits if x)
    if meta:
        head += f" ({meta})"
    parts = [head, format_price(it)]
    av = _AVAIL_LABEL.get(it.get("availability", "available"), "")
    if av:
        parts.append(av)
    if it.get("description"):
        d = it["description"].replace("\n", " ")
        parts.append(d if len(d) <= desc_len else d[:desc_len].rsplit(" ", 1)[0] + "…")
    variants = it.get("variants") or []
    if variants:
        vs = []
        for v in variants[:4]:
            s = v["name"]
            if v.get("price") is not None:
                s += f" {_fmt_num(v['price'])}"
            if v.get("availability") == "out_of_stock":
                s += " (out)"
            vs.append(s)
        parts.append("variants: " + "; ".join(vs))
    attrs = it.get("attributes") or {}
    if attrs:
        parts.append("; ".join(f"{k}: {v}" for k, v in list(attrs.items())[:3]))
    if with_url and it.get("url"):
        parts.append(it["url"])
    return "• " + " | ".join(parts)


def _categories_line(store, catalog_ids: list) -> str:
    cats = store.categories_many(catalog_ids)
    if not cats:
        return ""
    shown = ", ".join(f"{c} ({n})" for c, n in cats[:MAX_CATEGORIES_SHOWN])
    extra = f" +{len(cats) - MAX_CATEGORIES_SHOWN}" if len(cats) > MAX_CATEGORIES_SHOWN else ""
    return f"Categories: {shown}{extra}\n"


def _retrieval_text(user_text: str, history) -> str:
    text = user_text or ""
    if len(tokens(text)) <= 3 and history:
        last = history[-1]
        text += " " + (last.get("assistant") or "")[:200] + " " + (last.get("user") or "")[:120]
    return text


def build_catalog_context(store, account_id: str, user_text: str, history=None) -> str:
    """DATA block for the prompt or "" when there are no catalogs. No instructions."""
    catalog_ids = store.resolve_catalogs(account_id)
    if not catalog_ids:
        return ""
    total = store.count_many(catalog_ids)
    if total == 0:
        return ""

    multi = len(catalog_ids) > 1
    want_url = bool(_WANT_URL_RE.search(user_text or ""))
    header = "=== CATALOG (data) ===\n"
    footer = "\n=== END CATALOG ==="

    if total <= FULL_INLINE_MAX_ITEMS:
        items = store.list_active_many(catalog_ids, limit_per_catalog=FULL_INLINE_MAX_ITEMS)
        lines = [render_item(i, show_catalog=multi, with_url=want_url) for i in items]
        if sum(len(l) + 1 for l in lines) <= FULL_INLINE_MAX_CHARS:
            return header + f"All items ({total}):\n" + "\n".join(lines) + footer

    cats = _categories_line(store, catalog_ids)
    hits = store.search_many(catalog_ids, _retrieval_text(user_text, history),
                             limit_per_catalog=SEARCH_TOP_K)
    used = len(header) + len(cats) + len(footer) + 60
    lines = []
    for it in hits[:SEARCH_TOP_K]:
        line = render_item(it, show_catalog=multi, with_url=want_url)
        if used + len(line) > CONTEXT_CHAR_BUDGET:
            break
        used += len(line) + 1
        lines.append(line)

    body = f"Large catalog ({total} items). Top matches for the last message:\n"
    body += "\n".join(lines) if lines else "(nothing specific found — answer naturally)"
    if cats:
        body += "\n" + cats.rstrip("\n")
    return header + body + footer
