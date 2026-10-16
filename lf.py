"""
Одноразовий скрипт: показує всі твої workflow в Zernio та id/тип кожного
вузла всередині них. Використовує ту саму venv і той самий ZERNIO_API_KEY,
що й worker.py -- запускати прямо на VPS, в тій самій папці.

Запуск:
    source venv/bin/activate   # той самий venv, що і в WORKER.service
    python3 list_workflows.py
"""
import os
from zernio import Zernio

api_key = os.environ.get("ZERNIO_API_KEY")
if not api_key:
    raise SystemExit("ZERNIO_API_KEY не знайдено в env -- запускай там, де крутиться сам бот.")

client = Zernio(api_key=api_key)


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


print("Шукаю твої workflow...\n")

workflows_resp = client.workflows.list()
workflows = _get(workflows_resp, "workflows", []) or []

if not workflows:
    print("Жодного workflow не знайдено. Перевір, чи цей API-ключ бачить той самий акаунт, що і в браузері.")

for wf in workflows:
    wf_id = _get(wf, "id")
    wf_name = _get(wf, "name") or "(без назви)"
    wf_status = _get(wf, "status")
    print(f"=== Workflow: \"{wf_name}\"  id={wf_id}  status={wf_status} ===")

    # Повний граф (вузли + ребра) віддається тільки по конкретному id
    full = client.workflows.get(wf_id)
    nodes = _get(full, "nodes", []) or []

    if not nodes:
        print("  (вузлів не знайдено)")
    for node in nodes:
        node_id = _get(node, "id")
        node_type = _get(node, "type")
        node_label = _get(node, "label") or ""
        config = _get(node, "config") or {}
        print(f"  - node_id={node_id}  type={node_type}  label={node_label!r}")
        if node_type in ("set_variable", "set_field"):
            print(f"      поточний config: {config}")
    print()

print("Скопіюй сюди workflow id та потрібний node_id (той, що type=set_variable або set_field).")
