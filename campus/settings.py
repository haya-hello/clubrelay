import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("QINGLIAN_DATA_DIR", BASE_DIR / "var"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
secret_path = DATA_DIR / "secret.key"
# 本机随机密钥持久化，不进入版本库。 / Persist a local random key outside version control.
try:
    with secret_path.open("x", encoding="utf-8") as stream:
        stream.write(secrets.token_urlsafe(48))
except FileExistsError:
    pass
SECRET_KEY = secret_path.read_text(encoding="utf-8").strip()
DEBUG = False
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "[::1]"]
INSTALLED_APPS = ["django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles", "knowledge", "activities", "operations", "collector"]
MIDDLEWARE = ["django.middleware.security.SecurityMiddleware", "django.contrib.sessions.middleware.SessionMiddleware", "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware", "django.contrib.auth.middleware.AuthenticationMiddleware", "operations.security.ManagerOnlyMiddleware", "django.contrib.messages.middleware.MessageMiddleware", "django.middleware.clickjacking.XFrameOptionsMiddleware"]
ROOT_URLCONF = "campus.urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [BASE_DIR / "templates"], "APP_DIRS": True, "OPTIONS": {"context_processors": ["django.template.context_processors.request", "django.contrib.auth.context_processors.auth", "django.contrib.messages.context_processors.messages", "knowledge.context.navigation"]}}]
WSGI_APPLICATION = "campus.wsgi.application"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": DATA_DIR / "campus.sqlite3", "OPTIONS": {"timeout": 10}}}
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "var" / "static"
MEDIA_ROOT = DATA_DIR / "uploads"
MEDIA_URL = "/private-uploads-not-served/"
FILE_UPLOAD_HANDLERS = ["operations.uploads.BatchUploadHandler"]
FILE_UPLOAD_TEMP_DIR = DATA_DIR / "upload-temp"
FILE_UPLOAD_TEMP_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "ops_home"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_NAME = "qinglian_campus_session"
CSRF_COOKIE_NAME = "qinglian_campus_csrf"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
DATA_UPLOAD_MAX_MEMORY_SIZE = 400000
AUTH_PASSWORD_VALIDATORS = [{"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}}]
