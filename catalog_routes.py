"""
catalog_routes.py — маршрути для керування каталогами.

Один файл обслуговує ДВІ адмінки:
  /uadmin/catalog/*  — клієнт редагує СВОЇ каталоги
  /admin/catalog/*   — глобальний адмін редагує ВСЕ (включно з глобальними)

Різниця тільки в тому, який scope передається:
  user  = dict (з user_store) -> каталоги цього юзера
  user  = None                 -> глобальні каталоги (user_id IS NULL)

Ключові ендпоінти:
  GET  /catalogs                    — список каталогів у scope
  POST /catalogs/create             — створити каталог
  POST /catalogs/rename             — перейменувати
  POST /catalogs/delete             — видалити (разом з позиціями і прив'язками)
  POST /catalogs/default            — зробити default'ом
  GET  /catalog/items               — список позицій (з пошуком + пагінацією)
  POST /catalog/item/save           — створити/оновити позицію
  POST /catalog/item/delete         — видалити позицію
  POST /catalog/items/bulk-delete   — видалити кілька
  GET  /bindings                    — прив'язки для акаунта (з боку каталогу чи акаунта)
  POST /bindings/set                — перезаписати прив'язки
  POST /bindings/bind               — прив'язати один каталог
  POST /bindings/unbind             — відв'язати
  POST /import/sheet/start          — запустити імпорт з Google Sheet у каталог
  GET  /import/job                  — статус імпорту
"""
import json
import threading
import traceback

from flask import jsonify, request, session


def register_catalog(app, *, catalog_store, user_store, sheet_store,
                     groq_client=None, log=print):
    """Реєструє всі каталогові роути. Викликається з worker.py один раз."""

    # ==================== ХЕЛПЕРИ ====================

    def _uadmin_user():
        """Повертає dict user або None, якщо не залогований у /uadmin."""
        uid = session.get("user_id")
        if not uid:
            return None
        return user_store.get_user(uid)

    def _is_global_admin():
        return bool(session.get("admin_logged_in"))

    def _scope_user():
        """Визначає, у якому scope працюємо:
        - global admin (session admin_logged_in) -> None (глобальні каталоги)
        - uadmin user -> dict user
        Повертає (scope_user_dict_or_None, is_global, error_response_or_None)."""
        if session.get("admin_logged_in"):
            return None, True, None
        u = _uadmin_user()
        if not u:
            return None, False, (jsonify(ok=False, error="unauthorized"), 401)
        return u, False, None

    def _scope_user_id(user):
        """user_id для фільтру catalogs.user_id. Для global -> None."""
        return None if user is None else user["id"]

    def _can_touch_catalog(catalog, user):
        """Перевірка прав: юзер може чіпати лише СВОЇ каталоги.
        Global admin — будь-які."""
        if session.get("admin_logged_in"):
            return True
        if not user or not catalog:
            return False
        return catalog.get("user_id") == user["id"]

    def _can_touch_account(account_id, user):
        """Чи має юзер право прив'язувати каталоги до цього акаунта.
        Global admin — будь-який акаунт."""
        if session.get("admin_logged_in"):
            return True
        if not user:
            return False
        owned = {a["account_id"] for a in user_store.get_user_accounts(user["id"])}
        return account_id in owned

    def _user_owned_account_ids(user):
        if user is None:
            return None  # global admin — без обмежень
        return [a["account_id"] for a in user_store.get_user_accounts(user["id"])]

    def _check_csrf():
        """CSRF-токен у нас спільний для /uadmin і /admin.
        У /uadmin POST-форми завжди несуть csrf_token — перевіряємо його тут."""
        expected = session.get("csrf_token")
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not expected or not supplied:
            return False
        import hmac as _hmac
        return _hmac.compare_digest(expected, supplied)

    def _json_body():
        return request.get_json(silent=True) or {}

    def _ok(**kw):
        return jsonify(ok=True, **kw)

    def _err(msg, code=400):
        return jsonify(ok=False, error=msg), code

    def _csrf_ok_or_err():
        if not _check_csrf():
            return _err("csrf", 403)
        return None

    # ==================== CATALOGS (сутності) ====================

    @app.route("/uadmin/catalog/catalogs", methods=["GET"])
    @app.route("/admin/catalog/catalogs", methods=["GET"])
    def catalog_list():
        user, is_global, err = _scope_user()
        if err:
            return err
        items = catalog_store.list_catalogs(user_id=_scope_user_id(user),
                                            include_global=False if is_global else True)
        # для uadmin — підтягуємо скільки акаунтів прив'язано
        for c in items:
            c["bound_accounts"] = catalog_store.get_accounts_for_catalog(c["id"])
        return _ok(catalogs=items, is_global=is_global)

    @app.route("/uadmin/catalog/catalogs/create", methods=["POST"])
    @app.route("/admin/catalog/catalogs/create", methods=["POST"])
    def catalog_create():
        user, is_global, err = _scope_user()
        if err:
            return err
        e = _csrf_ok_or_err()
        if e:
            return e
        body = _json_body()
        name = (body.get("name") or "").strip()
        if not name:
            return _err("Вкажіть назву каталогу.")
        try:
            c = catalog_store.create_catalog(_scope_user_id(user), name,
                                             is_default=bool(body.get("is_default")))
        except ValueError as ex:
            return _err(str(ex))
        log(f"[catalog] create catalog id={c['id']} name={name!r} scope={'global' if is_global else user['id']}")
        return _ok(catalog=c)

    @app.route("/uadmin/catalog/catalogs/rename", methods=["POST"])
    @app.route("/admin/catalog/catalogs/rename", methods=["POST"])
    def catalog_rename():
        user, is_global, err = _scope_user()
        if err:
            return err
        e = _csrf_ok_or_err()
        if e:
            return e
        body = _json_body()
        cid = (body.get("catalog_id") or "").strip()
        name = (body.get("name") or "").strip()
        if not cid or not name:
            return _err("catalog_id і name обов'язкові.")
        cat = catalog_store.get_catalog(cid)
        if not cat:
            return _err("Каталог не знайдено.", 404)
        if not _can_touch_catalog(cat, user):
            return _err("Немає прав на цей каталог.", 403)
        ok = catalog_store.rename_catalog(cid, name)
        return _ok(renamed=ok)

    @app.route("/uadmin/catalog/catalogs/delete", methods=["POST"])
    @app.route("/admin/catalog/catalogs/delete", methods=["POST"])
    def catalog_delete():
        user, is_global, err = _scope_user()
        if err:
            return err
        e = _csrf_ok_or_err()
        if e:
            return e
        body = _json_body()
        cid = (body.get("catalog_id") or "").strip()
        if not cid:
            return _err("catalog_id обов'язковий.")
        cat = catalog_store.get_catalog(cid)
        if not cat:
            return _err("Каталог не знайдено.", 404)
        if not _can_touch_catalog(cat, user):
            return _err("Немає прав на цей каталог.", 403)
        n = catalog_store.delete_catalog(cid)
        log(f"[catalog] delete catalog id={cid} items_removed={n}")
        return _ok(deleted_items=n)

    @app.route("/uadmin/catalog/catalogs/default", methods=["POST"])
    @app.route("/admin/catalog/catalogs/default", methods=["POST"])
    def catalog_set_default():
        user, is_global, err = _scope_user()
        if err:
            return err
        e = _csrf_ok_or_err()
        if e:
            return e
        body = _json_body()
        cid = (body.get("catalog_id") or "").strip()
        cat = catalog_store.get_catalog(cid)
        if not cat:
            return _err("Каталог не знайдено.", 404)
        if not _can_touch_catalog(cat, user):
            return _err("Немає прав на цей каталог.", 403)
        ok = catalog_store.set_default_catalog(cid)
        return _ok(is_default=ok)

    # ==================== ITEMS ====================

    @app.route("/uadmin/catalog/items", methods=["GET"])
    @app.route("/admin/catalog/items", methods=["GET"])
    def catalog_items():
        user, is_global, err = _scope_user()
        if err:
            return err
        cid = request.args.get("catalog_id", "").strip()
        if not cid:
            return _err("catalog_id обов'язковий.")
        cat = catalog_store.get_catalog(cid)
        if not cat:
            return _err("Каталог не знайдено.", 404)
        if not _can_touch_catalog(cat, user):
            return _err("Немає прав на цей каталог.", 403)
        q = request.args.get("q", "").strip()
        try:
            limit = max(1, min(200, int(request.args.get("limit", 50))))
            offset = max(0, int(request.args.get("offset", 0)))
        except ValueError:
            limit, offset = 50, 0
        items, total = catalog_store.list_items(cid, q=q, limit=limit, offset=offset)
        return _ok(items=items, total=total, limit=limit, offset=offset, catalog=cat)

    @app.route("/uadmin/catalog/item/save", methods=["POST"])
    @app.route("/admin/catalog/item/save", methods=["POST"])
    def catalog_item_save():
        user, is_global, err = _scope_user()
        if err:
            return err
        e = _csrf_ok_or_err()
        if e:
            return e
        body = _json_body()
        cid = (body.get("catalog_id") or "").strip()
        if not cid:
            return _err("catalog_id обов'язковий.")
        cat = catalog_store.get_catalog(cid)
        if not cat:
            return _err("Каталог не знайдено.", 404)
        if not _can_touch_catalog(cat, user):
            return _err("Немає прав на цей каталог.", 403)
        item_id = (body.get("item_id") or "").strip() or None
        payload = body.get("item") or {}
        if not isinstance(payload, dict):
            return _err("item має бути об'єктом.")
        try:
            item = catalog_store.save_item(cid, payload, item_id=item_id)
        except ValueError as ex:
            return _err(str(ex))
        return _ok(item=item)

    @app.route("/uadmin/catalog/item/delete", methods=["POST"])
    @app.route("/admin/catalog/item/delete", methods=["POST"])
    def catalog_item_delete():
        user, is_global, err = _scope_user()
        if err:
            return err
        e = _csrf_ok_or_err()
        if e:
            return e
        body = _json_body()
        cid = (body.get("catalog_id") or "").strip()
        item_id = (body.get("item_id") or "").strip()
        if not cid or not item_id:
            return _err("catalog_id і item_id обов'язкові.")
        cat = catalog_store.get_catalog(cid)
        if not cat:
            return _err("Каталог не знайдено.", 404)
        if not _can_touch_catalog(cat, user):
            return _err("Немає прав на цей каталог.", 403)
        n = catalog_store.delete_items(cid, [item_id])
        return _ok(deleted=n)

    @app.route("/uadmin/catalog/items/bulk-delete", methods=["POST"])
    @app.route("/admin/catalog/items/bulk-delete", methods=["POST"])
    def catalog_items_bulk_delete():
        user, is_global, err = _scope_user()
        if err:
            return err
        e = _csrf_ok_or_err()
        if e:
            return e
        body = _json_body()
        cid = (body.get("catalog_id") or "").strip()
        ids = body.get("item_ids") or []
        if not cid or not isinstance(ids, list) or not ids:
            return _err("catalog_id і непорожній item_ids обов'язкові.")
        cat = catalog_store.get_catalog(cid)
        if not cat:
            return _err("Каталог не знайдено.", 404)
        if not _can_touch_catalog(cat, user):
            return _err("Немає прав на цей каталог.", 403)
        n = catalog_store.delete_items(cid, [str(i) for i in ids])
        return _ok(deleted=n)

    # ==================== BINDINGS (розподіл) ====================

    @app.route("/uadmin/catalog/bindings", methods=["GET"])
    @app.route("/admin/catalog/bindings", methods=["GET"])
    def catalog_bindings():
        """Два режими:
        ?account_id=X          -> які каталоги бачить цей акаунт
        ?catalog_id=Y          -> які акаунти бачать цей каталог
        """
        user, is_global, err = _scope_user()
        if err:
            return err
        account_id = request.args.get("account_id", "").strip()
        catalog_id = request.args.get("catalog_id", "").strip()
        if account_id:
            if not _can_touch_account(account_id, user):
                return _err("Немає прав на цей акаунт.", 403)
            return _ok(account_id=account_id,
                       catalogs=catalog_store.get_bindings_for_account(account_id))
        if catalog_id:
            cat = catalog_store.get_catalog(catalog_id)
            if not cat:
                return _err("Каталог не знайдено.", 404)
            if not _can_touch_catalog(cat, user):
                return _err("Немає прав на цей каталог.", 403)
            return _ok(catalog_id=catalog_id,
                       accounts=catalog_store.get_accounts_for_catalog(catalog_id))
        return _err("Вкажіть account_id або catalog_id.")

    @app.route("/uadmin/catalog/bindings/set", methods=["POST"])
    @app.route("/admin/catalog/bindings/set", methods=["POST"])
    def catalog_bindings_set():
        """Повністю перезаписує прив'язки для одного акаунта.
        body: {account_id, catalog_ids: [..]}"""
        user, is_global, err = _scope_user()
        if err:
            return err
        e = _csrf_ok_or_err()
        if e:
            return e
        body = _json_body()
        account_id = (body.get("account_id") or "").strip()
        catalog_ids = body.get("catalog_ids") or []
        if not account_id:
            return _err("account_id обов'язковий.")
        if not isinstance(catalog_ids, list):
            return _err("catalog_ids має бути списком.")
        if not _can_touch_account(account_id, user):
            return _err("Немає прав на цей акаунт.", 403)
        # фільтруємо каталоги, які юзер має право прив'язувати
        # (global admin — будь-які, uadmin — тільки свої або глобальні)
        allowed = []
        for cid in catalog_ids:
            cat = catalog_store.get_catalog(cid)
            if not cat:
                continue
            if _can_touch_catalog(cat, user) or (cat.get("user_id") is None):
                allowed.append(cid)
        n = catalog_store.set_bindings_for_account(account_id, allowed)
        log(f"[catalog] set bindings account={account_id} catalogs={n}")
        return _ok(bound=n)

    @app.route("/uadmin/catalog/bindings/bind", methods=["POST"])
    @app.route("/admin/catalog/bindings/bind", methods=["POST"])
    def catalog_bind_one():
        user, is_global, err = _scope_user()
        if err:
            return err
        e = _csrf_ok_or_err()
        if e:
            return e
        body = _json_body()
        account_id = (body.get("account_id") or "").strip()
        catalog_id = (body.get("catalog_id") or "").strip()
        if not account_id or not catalog_id:
            return _err("account_id і catalog_id обов'язкові.")
        if not _can_touch_account(account_id, user):
            return _err("Немає прав на цей акаунт.", 403)
        cat = catalog_store.get_catalog(catalog_id)
        if not cat:
            return _err("Каталог не знайдено.", 404)
        if not _can_touch_catalog(cat, user) and cat.get("user_id") is not None:
            return _err("Немає прав на цей каталог.", 403)
        ok = catalog_store.bind(catalog_id, account_id)
        return _ok(bound=ok)

    @app.route("/uadmin/catalog/bindings/unbind", methods=["POST"])
    @app.route("/admin/catalog/bindings/unbind", methods=["POST"])
    def catalog_unbind_one():
        user, is_global, err = _scope_user()
        if err:
            return err
        e = _csrf_ok_or_err()
        if e:
            return e
        body = _json_body()
        account_id = (body.get("account_id") or "").strip()
        catalog_id = (body.get("catalog_id") or "").strip()
        if not account_id or not catalog_id:
            return _err("account_id і catalog_id обов'язкові.")
        if not _can_touch_account(account_id, user):
            return _err("Немає прав на цей акаунт.", 403)
        ok = catalog_store.unbind(catalog_id, account_id)
        return _ok(unbound=ok)

    # ==================== ІМПОРТ З GOOGLE SHEET ====================

    @app.route("/uadmin/catalog/import/sheet/start", methods=["POST"])
    @app.route("/admin/catalog/import/sheet/start", methods=["POST"])
    def catalog_import_sheet_start():
        """Запускає фоновий імпорт позицій з Google Sheet у каталог.
        body: {catalog_id, spreadsheet_id, sheet_title, column_map?}
          column_map — мапа "name" -> "B", "price" -> "C" і т.д.
          Якщо не вказано — беремо перші 5 колонок з евристикою (A=name, B=price).
        """
        user, is_global, err = _scope_user()
        if err:
            return err
        e = _csrf_ok_or_err()
        if e:
            return e
        body = _json_body()
        cid = (body.get("catalog_id") or "").strip()
        spreadsheet_id = (body.get("spreadsheet_id") or "").strip()
        sheet_title = (body.get("sheet_title") or "").strip()
        column_map = body.get("column_map") or None
        if not cid or not spreadsheet_id:
            return _err("catalog_id і spreadsheet_id обов'язкові.")
        cat = catalog_store.get_catalog(cid)
        if not cat:
            return _err("Каталог не знайдено.", 404)
        if not _can_touch_catalog(cat, user):
            return _err("Немає прав на цей каталог.", 403)

        # знайти refresh_token: у юзера може бути прив'язаний Google для
        # збору клієнтських даних; використаємо його ж для каталогу
        owner_id = cat.get("user_id") if cat.get("user_id") is not None else (
            user["id"] if user else None)
        refresh_token = None
        if owner_id is not None:
            for acc in user_store.get_user_accounts(owner_id):
                conn = sheet_store.get(acc["account_id"])
                if conn and conn.get("refresh_token"):
                    refresh_token = conn["refresh_token"]
                    break
        if not refresh_token:
            # у крайньому разі — глобальний токен (з "default")
            conn = sheet_store.get("default")
            if conn and conn.get("refresh_token"):
                refresh_token = conn["refresh_token"]
        if not refresh_token:
            return _err("Спочатку підключіть Google до будь-якого акаунта (вкладка Google Sheets).")

        jid = catalog_store.create_job(cid, "sheet", ref=spreadsheet_id,
                                       label=sheet_title or spreadsheet_id)
        log(f"[catalog] import sheet job={jid} catalog={cid} spreadsheet={spreadsheet_id}")
        threading.Thread(
            target=_run_sheet_import,
            args=(jid, cid, refresh_token, spreadsheet_id, sheet_title, column_map),
            daemon=True,
            name=f"cat-import-{jid[:6]}",
        ).start()
        return _ok(job_id=jid)

    @app.route("/uadmin/catalog/import/job", methods=["GET"])
    @app.route("/admin/catalog/import/job", methods=["GET"])
    def catalog_import_job():
        user, is_global, err = _scope_user()
        if err:
            return err
        jid = request.args.get("job_id", "").strip()
        if not jid:
            return _err("job_id обов'язковий.")
        job = catalog_store.get_job(jid)
        if not job:
            return _err("Job не знайдено.", 404)
        cat = catalog_store.get_catalog(job["catalog_id"])
        if not cat or not _can_touch_catalog(cat, user):
            return _err("Немає прав.", 403)
        return _ok(job=job)

    # ---------- внутрішнє: фоновий імпорт ----------
    def _run_sheet_import(job_id, catalog_id, refresh_token, spreadsheet_id,
                          sheet_title, column_map):
        """Читає Google Sheet і заливає позиції в каталог. Best-effort:
        будь-яка помилка -> status=error у job, без падінь."""
        import google_sheets  # локальний імпорт, щоб роути не тягли його без потреби
        try:
            catalog_store.update_job(job_id, status="running", message="Читаємо таблицю…")
            access_token = google_sheets.refresh_access_token(refresh_token)
            if not sheet_title:
                sheet_title = google_sheets.get_first_sheet_title(access_token, spreadsheet_id)

            # тягнемо як A1:Z10000 (обмежуємо зверху, щоб не вбити API)
            rows = google_sheets._values_get(
                access_token, spreadsheet_id, f"'{sheet_title}'!A1:Z10000")
            if not rows:
                catalog_store.update_job(job_id, status="error", error="Таблиця порожня.")
                return

            header = [str(c).strip().lower() for c in (rows[0] or [])]
            data_rows = rows[1:]

            # якщо column_map не задано — евристика за заголовками
            cmap = column_map or _guess_column_map(header)
            if "name" not in cmap:
                catalog_store.update_job(
                    job_id, status="error",
                    error="Не знайдено колонку з назвою. Додайте заголовок 'name' або 'назва' "
                          "у першому рядку таблиці.")
                return

            def col_idx(letter):
                letter = (letter or "").upper().strip()
                if not letter:
                    return None
                n = 0
                for ch in letter:
                    n = n * 26 + (ord(ch) - ord("A") + 1)
                return n - 1

            def get_val(row, letter):
                i = col_idx(letter)
                if i is None or i >= len(row):
                    return ""
                return str(row[i] or "").strip()

            items_raw = []
            for r in data_rows:
                name = get_val(r, cmap.get("name"))
                if not name:
                    continue
                item = {"name": name}
                for field, letter in cmap.items():
                    if field == "name":
                        continue
                    v = get_val(r, letter)
                    if not v:
                        continue
                    if field == "price":
                        item["price"] = v
                    elif field == "price_max":
                        item["price_max"] = v
                    elif field == "price_type":
                        item["price_type"] = v.lower()
                    elif field == "currency":
                        item["currency"] = v
                    elif field == "unit":
                        item["unit"] = v
                    elif field == "category":
                        item["category"] = v
                    elif field == "description":
                        item["description"] = v
                    elif field == "kind":
                        item["kind"] = v.lower()
                    elif field == "availability":
                        item["availability"] = v
                    elif field == "tags":
                        item["tags"] = [t.strip() for t in v.split(",") if t.strip()]
                    elif field == "url":
                        item["url"] = v
                    elif field == "sku":
                        item["external_id"] = v
                items_raw.append(item)

            if not items_raw:
                catalog_store.update_job(job_id, status="error",
                                         error="Не знайдено жодної позиції з назвою.")
                return

            catalog_store.update_job(job_id, status="running",
                                     message=f"Імпортуємо {len(items_raw)} позицій…")

            # позначаємо джерело, щоб повторний імпорт замінював старе
            src_id = catalog_store.get_or_create_source(
                catalog_id, "sheet", spreadsheet_id, sheet_title)
            n = catalog_store.apply_items(catalog_id, items_raw, source_id=src_id)
            catalog_store.update_job(job_id, status="applied",
                                     message=f"Імпортовано {n} позицій.",
                                     result={"imported": n, "sheet": sheet_title})
            log(f"[catalog] import job={job_id} done, {n} items")
        except Exception as e:
            log(f"[catalog] _run_sheet_import EXCEPTION:\n{traceback.format_exc()}")
            try:
                catalog_store.update_job(job_id, status="error",
                                         error=f"{type(e).__name__}: {e}")
            except Exception:
                pass

    def _guess_column_map(header):
        """Евристика: шукає в заголовках ключові слова і повертає {"field": "A"}."""
        def letter(i):
            s = ""
            i += 1
            while i:
                i, r = divmod(i - 1, 26)
                s = chr(65 + r) + s
            return s
        synonyms = {
            "name": ["name", "назва", "позиція", "товар", "услуга", "послуга", "product"],
            "price": ["price", "ціна", "цена", "вартість", "стоимость", "cost"],
            "price_max": ["price_max", "ціна_до", "цена_до", "до"],
            "currency": ["currency", "валюта"],
            "unit": ["unit", "одиниця", "единица", "за"],
            "category": ["category", "категорія", "категория", "розділ", "раздел", "group"],
            "description": ["description", "опис", "описание", "деталі", "details"],
            "kind": ["kind", "тип", "type"],
            "availability": ["availability", "наявність", "наличие", "stock"],
            "tags": ["tags", "теги", "keywords", "ключові"],
            "url": ["url", "link", "посилання", "ссылка"],
            "sku": ["sku", "артикул", "id", "код"],
        }
        out = {}
        for i, h in enumerate(header):
            h = h.strip().lower()
            if not h:
                continue
            for field, keys in synonyms.items():
                if field in out:
                    continue
                if any(k == h or k in h for k in keys):
                    out[field] = letter(i)
                    break
        # фолбек: якщо name не знайшли — перша колонка
        if "name" not in out and header:
            out["name"] = letter(0)
        return out

    log("[catalog] routes registered: /uadmin/catalog/* + /admin/catalog/*")
