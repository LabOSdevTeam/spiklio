"""
google_sheets.py — OAuth-логін клієнта в Google + запис у ОБРАНУ ним таблицю.

Ідея: клієнт логіниться у власний Google-акаунт (наш сервер ніколи не бачить
його пароль), обирає таблицю через Google Picker, ми отримуємо refresh_token
і spreadsheet_id, і далі при кожному save_client_info (worker.py) апсертимо
рядок у цю таблицю - так само, як апсертимо в client_records (storage.py).

Тут навмисно НЕ використовується google-api-python-client / google-auth -
все на голому requests, в тому ж стилі, що й Zernio-виклики в worker.py:
менше залежностей, і весь HTTP-обмін видно в одному місці для дебагу.
"""
import time
import requests

from config import Config

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"

# spreadsheets - читати/писати в таблицю; drive.file - бачити ЛИШЕ ті файли,
# які клієнт сам обрав через Picker (не весь його Drive).
SCOPES = "https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/drive.file"


def build_auth_url(state: str, redirect_uri: str = None) -> str:
    """
    Будує URL для OAuth-логіна.
    redirect_uri обов'язково має бути в списку Authorized redirect URIs
    у Google Cloud Console, інакше Google поверне redirect_uri_mismatch.
    Якщо не переданий — використовує Config.GOOGLE_REDIRECT_URI (фолбек для /admin).
    """
    params = {
        "client_id": Config.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri or Config.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",   # потрібно, щоб отримати refresh_token
        "prompt": "consent",        # без цього Google повертає refresh_token лише ПЕРШОГО разу
        "state": state,
        "include_granted_scopes": "true",
    }
    query = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items())
    return f"{AUTH_URL}?{query}"


def exchange_code(code: str, redirect_uri: str = None) -> dict:
    """
    Обмінює одноразовий code (з ?code=...) на access_token + refresh_token.
    redirect_uri МАЄ бути ТИМ САМИМ, що і при build_auth_url — інакше Google
    поверне помилку "redirect_uri_mismatch".
    Кидає requests.HTTPError при невдачі.
    """
    resp = requests.post(TOKEN_URL, data={
        "code": code,
        "client_id": Config.GOOGLE_CLIENT_ID,
        "client_secret": Config.GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri or Config.GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()


def refresh_access_token(refresh_token: str) -> str:
    """Обмінює довгоживучий refresh_token на короткоживучий access_token
    (~1 година). Викликається перед КОЖНИМ записом - жодного кешування,
    щоб не думати про інвалідацію."""
    resp = requests.post(TOKEN_URL, data={
        "refresh_token": refresh_token,
        "client_id": Config.GOOGLE_CLIENT_ID,
        "client_secret": Config.GOOGLE_CLIENT_SECRET,
        "grant_type": "refresh_token",
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_first_sheet_title(access_token: str, spreadsheet_id: str) -> str:
    """Picker віддає лише ID файлу-таблиці, не конкретного аркуша всередині
    неї - беремо перший аркуш (properties.title) і працюємо з ним."""
    resp = requests.get(
        f"{SHEETS_API}/{spreadsheet_id}",
        params={"fields": "sheets.properties.title"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    resp.raise_for_status()
    sheets = resp.json().get("sheets") or []
    if not sheets:
        raise ValueError("spreadsheet has no sheets")
    return sheets[0]["properties"]["title"]


def _values_get(access_token: str, spreadsheet_id: str, a1_range: str):
    resp = requests.get(
        f"{SHEETS_API}/{spreadsheet_id}/values/{requests.utils.quote(a1_range)}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("values") or []


def _values_update(access_token: str, spreadsheet_id: str, a1_range: str, row: list):
    resp = requests.put(
        f"{SHEETS_API}/{spreadsheet_id}/values/{requests.utils.quote(a1_range)}",
        params={"valueInputOption": "USER_ENTERED"},
        headers={"Authorization": f"Bearer {access_token}"},
        json={"values": [row]},
        timeout=10,
    )
    resp.raise_for_status()


def _values_append(access_token: str, spreadsheet_id: str, sheet_title: str, row: list):
    resp = requests.post(
        f"{SHEETS_API}/{spreadsheet_id}/values/{requests.utils.quote(sheet_title)}!A1:append",
        params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
        headers={"Authorization": f"Bearer {access_token}"},
        json={"values": [row]},
        timeout=10,
    )
    resp.raise_for_status()


def _header_layout(fields: list):
    """Однакова колонка-порядок і для заголовку, і для рядків з даними.
    Перша колонка - службовий conversation_id (по ньому шукаємо рядок для
    апдейту), остання - час останнього оновлення."""
    field_ids = [f["id"] for f in fields if f.get("id")]
    header = ["conversation_id"] + [
        (next((f.get("label") for f in fields if f["id"] == fid), fid)) for fid in field_ids
    ] + ["updated_at"]
    return field_ids, header


def sync_client_record(refresh_token: str, spreadsheet_id: str, sheet_title: str,
                        conversation_id: str, data: dict, fields: list):
    """Апсертить ОДИН рядок (по conversation_id) у клієнтську Google Таблицю.
    Best-effort: викликається з фонового потоку у worker.py, тому сам ловить
    усі винятки і просто повертає False - помилка тут ніколи не повинна
    зіпсувати відповідь користувачу в Instagram."""
    if not refresh_token or not spreadsheet_id or not sheet_title:
        return False
    try:
        access_token = refresh_access_token(refresh_token)
        field_ids, header = _header_layout(fields)

        current_header = _values_get(access_token, spreadsheet_id, f"'{sheet_title}'!A1:Z1")
        if not current_header or current_header[0] != header:
            _values_update(access_token, spreadsheet_id, f"'{sheet_title}'!A1", header)

        row = [conversation_id] + [str(data.get(fid, "")) for fid in field_ids] + [
            time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        ]

        column_a = _values_get(access_token, spreadsheet_id, f"'{sheet_title}'!A:A")
        row_index = None
        for i, existing_row in enumerate(column_a, start=1):
            if existing_row and existing_row[0] == conversation_id:
                row_index = i
                break

        if row_index:
            _values_update(access_token, spreadsheet_id, f"'{sheet_title}'!A{row_index}", row)
        else:
            _values_append(access_token, spreadsheet_id, sheet_title, row)
        return True
    except Exception:
        import traceback
        print("[google_sheets] sync_client_record() EXCEPTION:\n" + traceback.format_exc(), flush=True)
        return False
