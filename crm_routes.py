"""
crm_routes.py — маршрути CRM для /uadmin.
"""
from functools import wraps

from flask import Response, jsonify, redirect, request, session, url_for

import crm_assistant
import table_rules_compiler
import collect_engine
import table_import
import table_import_page
from crm_store import STATUSES, since_iso


def register_crm(app, *, user_store, record_store, crm_store, groq_client, groq_model,
                 limiter, log, check_csrf, hook_globals=None, table_store=None,
                 integration_store=None, sheet_store=None):

    def _user():
        uid = session.get("user_id")
        return user_store.get_user(uid) if uid else None

    def api(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            user = _user()
            if not user:
                return jsonify(ok=False, error="unauthorized"), 401
            if request.method == "POST" and not check_csrf():
                return jsonify(ok=False, error="csrf"), 403
            return fn(user, *a, **kw)
        return wrapper

    def _owned(user):
        return user_store.get_user_accounts(user["id"])

    def _scope(user, account_id=None):
        ids = [a["account_id"] for a in _owned(user)]
        if account_id:
            if account_id not in ids:
                return None, (jsonify(ok=False, error="forbidden"), 403)
            return [account_id], None
        return ids, None

    def _days(default=3, lo=1, hi=90):
        try:
            return max(lo, min(hi, int(request.values.get("days", default))))
        except ValueError:
            return default

    # ---------- capture messages ----------
    if hook_globals is not None and "get_ai_reply" in hook_globals \
            and not getattr(hook_globals["get_ai_reply"], "_crm_wrapped", False):
        _orig = hook_globals["get_ai_reply"]
        _FAILS = ("Oops, something went wrong", "Sorry, AI is temporarily unavailable",
                  "Hi! Write something and I'll reply.")

        def _wrapped(user_text, account_id=None, conversation_id=None, *a, **kw):
            override = kw.get("override_prompt", a[0] if a else None)
            track = bool(account_id and conversation_id and override is None)
            if track:
                try:
                    crm_store.add_message(account_id, conversation_id, "in", user_text)
                except Exception as e:
                    log(f"[crm] capture in failed: {type(e).__name__}: {e}")
            reply = _orig(user_text, account_id, conversation_id, *a, **kw)
            if track and reply and not reply.startswith(_FAILS):
                try:
                    clean, was_ready = _strip_ready_marker(reply)
                    if was_ready:
                        first_time = crm_store.mark_won(account_id, conversation_id, source="assistant")
                        if first_time:
                            log(f"[crm] <<ready>> detected — client marked WON: "
                                f"acc={account_id} conv={conversation_id}")
                        reply = clean
                    crm_store.add_message(account_id, conversation_id, "out", reply)
                except Exception as e:
                    log(f"[crm] capture out failed: {type(e).__name__}: {e}")
            return reply

        _wrapped._crm_wrapped = True
        hook_globals["get_ai_reply"] = _wrapped

    @app.route("/uadmin/crm")
    def crm_page():
        return redirect(url_for("uadmin_dashboard") + "#inbox")

    # ---------- inbox ----------
    @app.route("/uadmin/crm/api/inbox")
    @api
    def crm_inbox(user):
        ids, err = _scope(user, request.args.get("account_id"))
        if err:
            return err
        return jsonify(ok=True, items=crm_store.conversations(ids, limit=100))

    @app.route("/uadmin/crm/api/thread")
    @api
    def crm_thread(user):
        acc, conv = request.args.get("account_id", ""), request.args.get("conversation_id", "")
        ids, err = _scope(user, acc)
        if err or not acc:
            return err or (jsonify(ok=False, error="account_id required"), 400)
        return jsonify(ok=True, messages=crm_store.thread(acc, conv),
                       contact=crm_store.get_contact(acc, conv),
                       record=(record_store.get(acc, conv) or {}).get("data", {}))

    # ---------- clients ----------
    @app.route("/uadmin/crm/api/clients")
    @api
    def crm_clients(user):
        ids, err = _scope(user, request.args.get("account_id"))
        if err:
            return err
        only_won = request.args.get("all") != "1"
        rows = crm_store.contacts(ids, only_won=only_won)
        data = {}
        for aid in ids:
            for r in record_store.list_for_account(aid, limit=300):
                data[(aid, r["conversation_id"])] = r["data"]
        for r in rows:
            r["record"] = data.get((r["account_id"], r["conversation_id"]), {})
        return jsonify(ok=True, items=rows, only_won=only_won)

    @app.route("/uadmin/crm/api/client/update", methods=["POST"])
    @api
    def crm_client_update(user):
        body = request.get_json(silent=True) or {}
        acc, conv = body.get("account_id", ""), body.get("conversation_id", "")
        _, err = _scope(user, acc)
        if err or not acc or not conv:
            return err or (jsonify(ok=False, error="bad request"), 400)
        status, notes = body.get("status"), body.get("notes")
        if status is not None and status not in STATUSES:
            return jsonify(ok=False, error="bad status"), 400
        crm_store.update_contact(acc, conv, status=status, notes=notes)
        crm_store.audit(user["id"], "owner", f"Updated client …{conv[-4:]}: "
                        + ", ".join(k for k, v in (("status", status), ("notes", notes)) if v is not None))
        return jsonify(ok=True)

    @app.route("/uadmin/crm/api/client/unmark-won", methods=["POST"])
    @api
    def crm_client_unmark_won(user):
        body = request.get_json(silent=True) or {}
        acc, conv = body.get("account_id", ""), body.get("conversation_id", "")
        _, err = _scope(user, acc)
        if err or not acc or not conv:
            return err or (jsonify(ok=False, error="bad request"), 400)
        removed = crm_store.unmark_won(acc, conv)
        if removed:
            crm_store.audit(user["id"], "owner", f"Unmarked client …{conv[-4:]} as not-won")
        return jsonify(ok=True, removed=removed)

    # ---------- stats / search ----------
    @app.route("/uadmin/crm/api/stats")
    @api
    def crm_stats(user):
        ids, err = _scope(user, request.args.get("account_id"))
        if err:
            return err
        s = crm_store.stats(ids, days=_days(14, 7, 60))
        total = sum(s["statuses"].values())
        s["conversion"] = round(s["statuses"].get("won", 0) / total * 100, 1) if total else 0
        s["total_clients"] = total
        s["collected"] = sum(len(record_store.list_for_account(a, limit=1000)) for a in ids)
        return jsonify(ok=True, **s)

    @app.route("/uadmin/crm/api/search")
    @api
    def crm_search(user):
        ids, err = _scope(user, request.args.get("account_id"))
        if err:
            return err
        return jsonify(ok=True, **crm_store.search(ids, request.args.get("q", "")[:80]))

    # ---------- assistant ----------
    @app.route("/uadmin/crm/api/assistant", methods=["POST"])
    @limiter.limit("20 per minute")
    @api
    def crm_assistant_chat(user):
        body = request.get_json(silent=True) or {}
        message = str(body.get("message") or "").strip()
        if not message:
            return jsonify(ok=False, error="empty"), 400
        days = 1 if int(body.get("days") or 1) <= 1 else 3
        result = crm_assistant.run_assistant(
            groq_client, groq_model, crm_store, record_store, user, _owned(user),
            days, message, body.get("history"), log=log,
            table_store=table_store, integration_store=integration_store)
        return jsonify(ok=True, **result)

    @app.route("/uadmin/crm/api/assistant/confirm", methods=["POST"])
    @api
    def crm_assistant_confirm(user):
        body = request.get_json(silent=True) or {}
        pending = crm_store.get_pending(str(body.get("id") or ""), user["id"])
        if not pending or pending["status"] != "pending":
            return jsonify(ok=False, error="not found or already resolved"), 404
        if not body.get("approve"):
            crm_store.resolve_pending(pending["id"], user["id"], "cancelled")
            return jsonify(ok=True, status="cancelled")
        if not crm_store.resolve_pending(pending["id"], user["id"], "done"):
            return jsonify(ok=False, error="already resolved"), 409
        try:
            crm_assistant.execute_pending(crm_store, record_store, pending,
                                          [a["account_id"] for a in _owned(user)], user["id"],
                                          table_store=table_store, integration_store=integration_store)
        except Exception as e:
            log(f"[crm] confirm EXCEPTION: {type(e).__name__}: {e}")
            return jsonify(ok=False, error="failed"), 500
        return jsonify(ok=True, status="done")

    @app.route("/uadmin/crm/api/audit")
    @api
    def crm_audit(user):
        return jsonify(ok=True, items=crm_store.audit_list(user["id"]))

    # ============================================================
    # BUILT-IN TABLES
    # ============================================================

    @app.route("/uadmin/table/api/tables", methods=["GET", "POST"])
    @api
    def table_tables(user):
        if request.method == "GET":
            return jsonify(ok=True, tables=table_store.list_tables(user["id"]))
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify(ok=False, error="name required"), 400
        try:
            t = table_store.create_table(user["id"], name)
            return jsonify(ok=True, table=t)
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400

    @app.route("/uadmin/table/api/table/rename", methods=["POST"])
    @api
    def table_rename(user):
        body = request.get_json(silent=True) or {}
        tid = (body.get("table_id") or "").strip()
        name = (body.get("name") or "").strip()
        if not tid or not name:
            return jsonify(ok=False, error="table_id and name required"), 400
        ok = table_store.rename_table(user["id"], tid, name)
        return jsonify(ok=ok)

    @app.route("/uadmin/table/api/table/delete", methods=["POST"])
    @api
    def table_delete(user):
        body = request.get_json(silent=True) or {}
        tid = (body.get("table_id") or "").strip()
        if not tid:
            return jsonify(ok=False, error="table_id required"), 400
        ok = table_store.delete_table(user["id"], tid)
        if not ok:
            return jsonify(ok=False, error="cannot delete (only table or not found)"), 400
        return jsonify(ok=True)

    @app.route("/uadmin/table/api/table/default", methods=["POST"])
    @api
    def table_set_default(user):
        body = request.get_json(silent=True) or {}
        tid = (body.get("table_id") or "").strip()
        if not tid:
            return jsonify(ok=False, error="table_id required"), 400
        ok = table_store.set_default_table(user["id"], tid)
        return jsonify(ok=ok)

    # ---------- SNAPSHOT ----------
    @app.route("/uadmin/table/api/snapshot")
    @api
    def table_snapshot(user):
        tid = request.values.get("table_id", "").strip()
        if not tid:
            return jsonify(ok=False, error="table_id required"), 400
        if not table_store.get_table(user["id"], tid):
            return jsonify(ok=False, error="table not found"), 404
        snap = table_store.snapshot(user["id"], tid)
        return jsonify(ok=True, **snap)

    # ---------- CELLS ----------
    @app.route("/uadmin/table/api/cell/set", methods=["POST"])
    @api
    def table_cell_set(user):
        body = request.get_json(silent=True) or {}
        tid = (body.get("table_id") or "").strip()
        a1 = (body.get("a1") or "").strip().upper()
        value = body.get("value")
        if not tid or not a1:
            return jsonify(ok=False, error="table_id and a1 required"), 400
        try:
            info = table_store.set_cell_by_a1(user["id"], tid, a1, value)
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 400
        return jsonify(ok=True, **info)

    @app.route("/uadmin/table/api/cells/batch", methods=["POST"])
    @api
    @limiter.limit("300 per minute")
    def table_cells_batch(user):
        body = request.get_json(silent=True) or {}
        tid = (body.get("table_id") or "").strip()
        if not tid or not table_store.get_table(user["id"], tid):
            return jsonify(ok=False, error="table not found"), 404
        cells, styles = body.get("cells") or {}, body.get("styles") or {}
        if not isinstance(cells, dict) or not isinstance(styles, dict):
            return jsonify(ok=False, error="bad payload"), 400
        try:
            n = table_store.set_cells_batch(user["id"], tid, cells, styles)
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 400
        return jsonify(ok=True, written=n)

    @app.route("/uadmin/table/api/range/set", methods=["POST"])
    @api
    def table_range_set(user):
        body = request.get_json(silent=True) or {}
        tid = (body.get("table_id") or "").strip()
        a1_range = (body.get("a1_range") or "").strip().upper()
        values = body.get("values") or []
        if not tid or not a1_range:
            return jsonify(ok=False, error="table_id and a1_range required"), 400
        try:
            n = table_store.set_range_by_a1(user["id"], tid, a1_range, values)
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 400
        return jsonify(ok=True, written=n)

    # ---------- COLUMNS ----------
    @app.route("/uadmin/table/api/col/width", methods=["POST"])
    @api
    def table_col_width(user):
        body = request.get_json(silent=True) or {}
        tid = (body.get("table_id") or "").strip()
        cid = (body.get("column_id") or "").strip().upper()
        width = body.get("width")
        if not tid or not cid or width is None:
            return jsonify(ok=False, error="table_id, column_id, width required"), 400
        ok = table_store.set_column_width(user["id"], tid, cid, width)
        return jsonify(ok=ok)

    @app.route("/uadmin/table/api/col/label", methods=["POST"])
    @api
    def table_col_label(user):
        body = request.get_json(silent=True) or {}
        tid = (body.get("table_id") or "").strip()
        cid = (body.get("column_id") or "").strip().upper()
        label = body.get("label") or ""
        if not tid or not cid:
            return jsonify(ok=False, error="table_id and column_id required"), 400
        ok = table_store.set_column_label(user["id"], tid, cid, label)
        return jsonify(ok=ok)

    @app.route("/uadmin/table/api/col/add", methods=["POST"])
    @api
    def table_col_add(user):
        body = request.get_json(silent=True) or {}
        tid = (body.get("table_id") or "").strip()
        count = int(body.get("count") or 1)
        if not tid:
            return jsonify(ok=False, error="table_id required"), 400
        added = table_store.add_columns(user["id"], tid, count=count)
        return jsonify(ok=True, added=added)

    # ---------- ROWS ----------
    @app.route("/uadmin/table/api/row/height", methods=["POST"])
    @api
    def table_row_height(user):
        body = request.get_json(silent=True) or {}
        tid = (body.get("table_id") or "").strip()
        rid = (body.get("row_id") or "").strip()
        height = body.get("height")
        if not tid or not rid or height is None:
            return jsonify(ok=False, error="table_id, row_id, height required"), 400
        ok = table_store.set_row_height(user["id"], tid, rid, height)
        return jsonify(ok=ok)

    @app.route("/uadmin/table/api/row/add", methods=["POST"])
    @api
    def table_row_add(user):
        body = request.get_json(silent=True) or {}
        tid = (body.get("table_id") or "").strip()
        count = int(body.get("count") or 1)
        if not tid:
            return jsonify(ok=False, error="table_id required"), 400
        added = table_store.add_rows(user["id"], tid, count=count)
        return jsonify(ok=True, added=added)

    @app.route("/uadmin/table/api/row/delete", methods=["POST"])
    @api
    def table_row_delete(user):
        body = request.get_json(silent=True) or {}
        tid = (body.get("table_id") or "").strip()
        rid = (body.get("row_id") or "").strip()
        if not tid or not rid:
            return jsonify(ok=False, error="table_id and row_id required"), 400
        removed = table_store.delete_row(user["id"], tid, rid)
        return jsonify(ok=True, removed=removed)

    # ---------- MERGES ----------
    @app.route("/uadmin/table/api/merge/add", methods=["POST"])
    @api
    def table_merge_add(user):
        body = request.get_json(silent=True) or {}
        tid = (body.get("table_id") or "").strip()
        a1_range = (body.get("a1_range") or "").strip().upper()
        if not tid or not a1_range:
            return jsonify(ok=False, error="table_id and a1_range required"), 400
        try:
            info = table_store.add_merge(user["id"], tid, a1_range)
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 400
        return jsonify(ok=True, **info)

    @app.route("/uadmin/table/api/merge/remove", methods=["POST"])
    @api
    def table_merge_remove(user):
        body = request.get_json(silent=True) or {}
        tid = (body.get("table_id") or "").strip()
        mid = (body.get("merge_id") or "").strip()
        if not tid or not mid:
            return jsonify(ok=False, error="table_id and merge_id required"), 400
        ok = table_store.remove_merge(user["id"], tid, mid)
        return jsonify(ok=ok)

    # ---------- RULES (compile once) ----------
    def _known_fields(user):
        """Поля збору даних (integration) усіх акаунтів власника — саме під
        цими id бот передає дані в write_by_rules."""
        if not integration_store:
            return []
        seen = {}
        for a in _owned(user):
            try:
                for f in (integration_store.get(a["account_id"]).get("fields") or []):
                    fid = (f.get("id") or "").strip()
                    if fid and fid.lower() not in seen:
                        seen[fid.lower()] = {"id": fid, "label": f.get("label") or fid}
            except Exception:
                pass
        return list(seen.values())

    @app.route("/uadmin/table/api/rules/get")
    @api
    def table_rules_get(user):
        tid = request.values.get("table_id", "").strip()
        if not tid:
            return jsonify(ok=False, error="table_id required"), 400
        rules = table_store.get_rules(user["id"], tid)
        return jsonify(ok=True, rules=rules, fields=_known_fields(user))

    @app.route("/uadmin/table/api/rules/compile", methods=["POST"])
    @api
    @limiter.limit("10 per minute")
    def table_rules_compile(user):
        body = request.get_json(silent=True) or {}
        tid = (body.get("table_id") or "").strip()
        text = (body.get("source_text") or "").strip()
        if not tid or not text:
            return jsonify(ok=False, error="table_id and source_text required"), 400
        if not table_store.get_table(user["id"], tid):
            return jsonify(ok=False, error="table not found"), 404
        try:
            rules = table_rules_compiler.compile_rules(
                groq_client, groq_model, table_store,
                user["id"], tid, text,
                known_fields=_known_fields(user),
            )
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
        except Exception as e:
            log(f"[crm] rules compile EXCEPTION: {type(e).__name__}: {e}")
            return jsonify(ok=False, error=f"compile failed: {e}"), 500
        table_store.save_rules(user["id"], tid, rules, text)
        rules["_source_text"] = text
        return jsonify(ok=True, rules=rules)

    @app.route("/uadmin/table/api/rules/delete", methods=["POST"])
    @api
    def table_rules_delete(user):
        body = request.get_json(silent=True) or {}
        tid = (body.get("table_id") or "").strip()
        if not tid:
            return jsonify(ok=False, error="table_id required"), 400
        table_store.delete_rules(user["id"], tid)
        return jsonify(ok=True)

    # ---------- COLLECT RULES (як збирати поля) ----------
    def _collect_account(user, account_id):
        ids, err = _scope(user, account_id)
        if err:
            return None, err
        if not account_id or integration_store is None:
            return None, (jsonify(ok=False, error="account_id required"), 400)
        return account_id, None

    @app.route("/uadmin/collect/api/compile", methods=["POST"])
    @api
    @limiter.limit("10 per minute")
    def collect_rules_compile(user):
        body = request.get_json(silent=True) or {}
        acc, err = _collect_account(user, (body.get("account_id") or "").strip())
        if err:
            return err
        text = (body.get("source_text") or "").strip()
        if not text:
            return jsonify(ok=False, error="source_text required"), 400
        data = integration_store.get(acc)
        try:
            rules = collect_engine.compile_rules(groq_client, groq_model, data, text[:4000])
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
        except Exception as e:
            log(f"[crm] collect compile EXCEPTION: {type(e).__name__}: {e}")
            return jsonify(ok=False, error=f"compile failed: {e}"), 500
        integration_store.set_collect(acc, text, rules)
        return jsonify(ok=True, collect={"source_text": text, "rules": rules})

    @app.route("/uadmin/collect/api/delete", methods=["POST"])
    @api
    def collect_rules_delete(user):
        body = request.get_json(silent=True) or {}
        acc, err = _collect_account(user, (body.get("account_id") or "").strip())
        if err:
            return err
        integration_store.delete_collect(acc)
        return jsonify(ok=True)

    # ============================================================
    # IMPORT: .xlsx / .ods / .csv / .tsv / Google Sheets  ->  вбудована таблиця
    # ============================================================

    def _google_tokens(user):
        """refresh_token'и Google, підключені до акаунтів цього користувача (для приватних таблиць)."""
        out = []
        if sheet_store is None:
            return out
        for acc in user_store.get_user_accounts(user["id"]):
            try:
                c = sheet_store.get(acc["account_id"])
            except Exception:
                c = None
            if c and c.get("refresh_token") and c["refresh_token"] not in out:
                out.append(c["refresh_token"])
        return out

    def _import_source(user):
        """-> (sheets, label). Кидає TableImportError з текстом для користувача."""
        if request.content_length and request.content_length > table_import.MAX_FILE_BYTES + 1024 * 1024:
            raise table_import.TableImportError("The file is too large (10 MB max).")
        f = request.files.get("file")
        url = (request.form.get("url") or "").strip()
        if f and f.filename:
            data = f.read(table_import.MAX_FILE_BYTES + 1)
            import re as _re
            label = _re.sub(r"\.[^.]+$", "", f.filename)[:80] or "Import"
            return table_import.parse_file(f.filename, data), label
        if url:
            return table_import.fetch_google(url, _google_tokens(user)), "Google"
        raise table_import.TableImportError("Choose a file or paste a Google Sheets link.")

    @app.route("/uadmin/table/import")
    def table_import_page_view():
        user = _user()
        if not user:
            return redirect(url_for("uadmin_dashboard"))
        tid = (request.args.get("table_id") or "").strip()
        if tid and not (table_store and table_store.get_table(user["id"], tid)):
            tid = ""                                   # чужий або неіснуючий id ігноруємо
        mode = "append" if (tid and request.args.get("mode") == "append") else ""
        if not tid:
            default = table_store.get_default_table(user["id"]) if table_store else None
            tid = (default or {}).get("table_id", "")
        html = table_import_page.render_page(session.get("csrf_token") or "", tid, mode)
        return Response(html, mimetype="text/html")

    @app.route("/uadmin/table/api/import/preview", methods=["POST"])
    @api
    @limiter.limit("60 per hour")
    def table_import_preview(user):
        try:
            sheets, _ = _import_source(user)
        except table_import.TableImportError as e:
            return jsonify(ok=False, error=str(e)), 400
        except Exception as e:
            log(f"[table-import] preview failed: {type(e).__name__}: {e}")
            return jsonify(ok=False, error="Could not read the file. Check the format."), 400
        return jsonify(ok=True, sheets=[table_import.preview(s) for s in sheets])

    @app.route("/uadmin/table/api/import/commit", methods=["POST"])
    @api
    @limiter.limit("30 per hour")
    def table_import_commit(user):
        try:
            sheets, label = _import_source(user)
        except table_import.TableImportError as e:
            return jsonify(ok=False, error=str(e)), 400
        except Exception as e:
            log(f"[table-import] commit parse failed: {type(e).__name__}: {e}")
            return jsonify(ok=False, error="Could not read the file. Check the format."), 400

        mode = (request.form.get("mode") or "new").strip()
        selected = request.form.getlist("sheet")
        chosen = [s for s in sheets if (not selected or s["name"] in selected)]
        chosen_nonempty = [s for s in chosen if s["rows"]]
        if not chosen_nonempty:
            return jsonify(ok=False, error="The selected sheets are empty."), 400
        warnings = [w for s in chosen for w in s["warnings"]]
        results = []
        try:
            if mode == "new":
                multi = len(chosen_nonempty) > 1
                for sh in chosen_nonempty[:10]:
                    name = f"{label} — {sh['name']}" if (multi or label == "Google") else label
                    t = table_store.create_table(user["id"], name)
                    r = table_store.import_grid(user["id"], t["table_id"], sh["rows"], sh["merges"], "replace")
                    results.append({"table_id": t["table_id"], "name": t["name"], **r})
                if len(chosen_nonempty) > 10:
                    warnings.append("Only the first 10 sheets were imported.")
            elif mode in ("append", "replace"):
                tid = (request.form.get("table_id") or "").strip()
                tbl = table_store.get_table(user["id"], tid) if tid else None
                if not tbl:
                    return jsonify(ok=False, error="Choose a table."), 400
                if mode == "replace" and request.form.get("confirm_replace") != "1":
                    return jsonify(ok=False, error="Replacing data requires confirmation."), 400
                sh = chosen_nonempty[0]
                if len(chosen_nonempty) > 1:
                    warnings.append(f"Only the first sheet ({sh['name']!r}) was imported into the existing table.")
                r = table_store.import_grid(user["id"], tid, sh["rows"], sh["merges"], mode,
                                            skip_header=(request.form.get("skip_header") == "1"))
                results.append({"table_id": tid, "name": tbl["name"], **r})
            else:
                return jsonify(ok=False, error="bad mode"), 400
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
        except Exception as e:
            log(f"[table-import] commit failed: {type(e).__name__}: {e}")
            return jsonify(ok=False, error="Could not save the table."), 500
        log(f"[table-import] user={user['id']} mode={mode} -> {[(r['name'], r['rows'], r['cols']) for r in results]}")
        return jsonify(ok=True, results=results, warnings=warnings)

    # ---------- SYNC FROM RECORDS (сумісність) ----------
    @app.route("/uadmin/table/api/sync-from-records", methods=["POST"])
    @api
    def table_sync_from_records(user):
        default = table_store.get_default_table(user["id"])
        if not default:
            return jsonify(ok=False, error="no default table"), 400
        tid = default["table_id"]
        accounts = user_store.get_user_accounts(user["id"])
        account_ids = [a["account_id"] for a in accounts]
        count = 0
        for aid in account_ids:
            for rec in record_store.list_for_account(aid, limit=5000):
                data = rec.get("data") or {}
                if not data:
                    continue
                try:
                    table_store.write_by_rules(user["id"], tid, data,
                                               client_ref=rec["conversation_id"])
                    count += 1
                except Exception:
                    pass
        return jsonify(ok=True, synced=count)


def _strip_ready_marker(text: str):
    import re
    if not text:
        return text, False
    pattern = re.compile(r"<<\s*ready\s*>>", re.IGNORECASE)
    if not pattern.search(text):
        return text, False
    clean = pattern.sub("", text).rstrip()
    return clean, True
