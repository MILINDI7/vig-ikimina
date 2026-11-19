import os
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "local-development-only-change-before-deployment")
if not DEBUG and SECRET_KEY.startswith("local-development"):
    raise RuntimeError("Set DJANGO_SECRET_KEY for production")
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
INSTALLED_APPS = ["django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles", "accounts", "groups", "contributions", "ratings"]
MIDDLEWARE = ["django.middleware.security.SecurityMiddleware", "django.contrib.sessions.middleware.SessionMiddleware", "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware", "django.contrib.auth.middleware.AuthenticationMiddleware", "accounts.middleware.PasswordChangeMiddleware", "django.contrib.messages.middleware.MessageMiddleware", "django.middleware.clickjacking.XFrameOptionsMiddleware"]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [BASE_DIR / "templates"], "APP_DIRS": True, "OPTIONS": {"context_processors": ["django.template.context_processors.request", "django.contrib.auth.context_processors.auth", "django.contrib.messages.context_processors.messages", "contributions.context_processors.navigation"]}}]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}
if os.environ.get("POSTGRES_DB"):
    DATABASES["default"] = {"ENGINE": "django.db.backends.postgresql", "NAME": os.environ["POSTGRES_DB"], "USER": os.environ.get("POSTGRES_USER", "postgres"), "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""), "HOST": os.environ.get("POSTGRES_HOST", "localhost"), "PORT": os.environ.get("POSTGRES_PORT", "5432")}
AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [{"NAME": "django.contrib.auth.password_validation." + name} for name in ["UserAttributeSimilarityValidator", "MinimumLengthValidator", "CommonPasswordValidator", "NumericPasswordValidator"]]
LANGUAGE_CODE = "en"
TIME_ZONE = "Africa/Kigali"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
X_FRAME_OPTIONS = "DENY"

# No media URL route: every proof is streamed through an authorized view.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {"location": BASE_DIR / "private_media"}},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    "proofs": {"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {"location": BASE_DIR / "private_media", "base_url": None}},
}
if os.environ.get("PROOF_S3_BUCKET"):
    STORAGES["proofs"] = {"BACKEND": "storages.backends.s3.S3Storage", "OPTIONS": {
        "bucket_name": os.environ["PROOF_S3_BUCKET"],
        "endpoint_url": os.environ.get("PROOF_S3_ENDPOINT") or None,
        "region_name": os.environ.get("AWS_DEFAULT_REGION") or None,
        "default_acl": "private", "querystring_auth": True, "file_overwrite": False,
    }}
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 6 * 1024 * 1024
