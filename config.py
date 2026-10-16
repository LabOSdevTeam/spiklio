import os
import bcrypt

class Config:
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY") or os.environ.get("groq")
    ZERNIO_API_KEY = os.environ.get("ZERNIO_API_KEY")
    ZERNIO_SECRET = os.environ.get("ZERNIO_SECRET")
    # Was missing entirely - render_admin() reads this and used to crash /admin with
    # AttributeError the moment you logged in. None is fine: get_connected_accounts()
    # just lists all accounts instead of filtering to one profile.
    CLIENT_PROFILE_ID = os.environ.get("ZERNIO_CLIENT_PROFILE_ID")

    # Google OAuth - дозволяє клієнту підключити СВОЮ Google Таблицю через
    # власний логін (Picker), без сервісних акаунтів. Client ID/Secret і
    # API key для Picker беруться з Google Cloud Console (OAuth Client ID
    # типу "Web application" + окремий API key, обмежений на Picker API і
    # на домен сайту). REDIRECT_URI має ЗБІГАТИСЯ символ-в-символ з тим,
    # що прописано в Authorized redirect URIs в Google Cloud Console.
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
    GOOGLE_PICKER_API_KEY = os.environ.get("GOOGLE_PICKER_API_KEY")
    GOOGLE_REDIRECT_URI = os.environ.get(
        "GOOGLE_REDIRECT_URI", "https://chatwave.duckdns.org/admin/google/callback"
    )

    # Замість хешу passlib тепер буде сирий хеш bcrypt
    # Він виглядає як рядок типу: $2b$12$...
    ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", b"$2b$12$8BgX57pB0fdY7vBJnwr0peSi682sedOdfwYmYs/fqMnW7XQlmZ.La")
    
    MAX_LOGIN_ATTEMPTS = 3
    LOGIN_WINDOW_HOURS = 24
    SESSION_LIFETIME = 14400  # 4 години в секундах
