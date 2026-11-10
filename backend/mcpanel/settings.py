"""Django settings for the MCPanel management API.

All deployment-specific configuration comes from environment variables so the
same image runs in development, CI and production (Unraid). See
deploy/.env.example for the documented variables.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def env_bool(name: str, default: bool = False) -> bool:
    value = env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    raw = env(name, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def read_secret(name: str, default: str | None = None) -> str | None:
    """Read NAME or NAME_FILE (Docker secrets style)."""
    file_path = env(f"{name}_FILE")
    if file_path:
        return Path(file_path).read_text().strip()
    return env(name, default)


DEBUG = env_bool("MCPANEL_DEBUG", False)
TESTING = env_bool("MCPANEL_TESTING", False)

SECRET_KEY = read_secret("MCPANEL_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG or TESTING:
        SECRET_KEY = "insecure-development-key-do-not-use-in-production"
    else:
        raise ImproperlyConfigured("MCPANEL_SECRET_KEY (or MCPANEL_SECRET_KEY_FILE) must be set")
# Previous secret keys, comma- or whitespace-separated, while rotating MCPANEL_SECRET_KEY:
# signatures (sessions, password reset links) and encrypted secrets (apps/core/crypto.py)
# made with them stay valid. Remove them after `manage.py reencrypt_secrets`.
SECRET_KEY_FALLBACKS = (read_secret("MCPANEL_SECRET_KEY_FALLBACKS", "") or "").replace(",", " ").split()

ALLOWED_HOSTS = env_list("MCPANEL_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("MCPANEL_CSRF_TRUSTED_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "channels",
    "apps.core",
    "apps.accounts",
    "apps.audit",
    "apps.nodes",
    "apps.catalog",
    "apps.instances",
    "apps.jobs",
    "apps.modpacks",
    "apps.content",
    "apps.backups",
    "apps.players",
    "apps.console",
    "apps.notifications",
    "apps.metrics",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # before WhiteNoise, so the SPA files it serves (/index.html, assets) get the headers too
    "apps.core.middleware.SecurityHeadersMiddleware",
    "apps.core.middleware.UploadLimitMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.RequestContextMiddleware",
]

ROOT_URLCONF = "mcpanel.urls"

FRONTEND_DIST = Path(env("MCPANEL_FRONTEND_DIST", str(BASE_DIR / "frontend_dist")) or "")

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = None
ASGI_APPLICATION = "mcpanel.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "mcpanel"),
        "USER": env("POSTGRES_USER", "mcpanel"),
        "PASSWORD": read_secret("POSTGRES_PASSWORD", "mcpanel"),
        "HOST": env("POSTGRES_HOST", "localhost"),
        "PORT": env("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": int(env("POSTGRES_CONN_MAX_AGE", "60") or 60),
        "CONN_HEALTH_CHECKS": True,
        # Separate test databases for parallel test runs (e.g. several checkouts on one host):
        # MCPANEL_TEST_DB_NAME=test_mcpanel_<something>. Default: Django's test_<NAME>.
        **({"TEST": {"NAME": env("MCPANEL_TEST_DB_NAME", "")}} if env("MCPANEL_TEST_DB_NAME", "") else {}),
    }
}

REDIS_URL = env("MCPANEL_REDIS_URL", "redis://localhost:6379/0") or ""

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        # redis-py >= 8 defaults socket_timeout to 5 s, the same as channels_redis' 5 s
        # BZPOPMIN wait, so idle consumers die with "Timeout reading from redis" and every
        # WebSocket (console, live events) drops every 5-10 s. Keep the read timeout above it.
        "CONFIG": {"hosts": [{"address": REDIS_URL, "socket_timeout": 30}]},
    }
}
if TESTING:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "mcpanel",
    }
}
if TESTING:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.ScryptPasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
if TESTING:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # fast tests only

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TZ", "UTC") or "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
WHITENOISE_ROOT = FRONTEND_DIST if FRONTEND_DIST.exists() else None
WHITENOISE_INDEX_FILE = False

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Sessions / cookies -----------------------------------------------------
SESSION_COOKIE_NAME = "mcpanel_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = int(env("MCPANEL_SESSION_AGE_SECONDS", str(60 * 60 * 24 * 7)) or 0)
SESSION_COOKIE_SECURE = env_bool("MCPANEL_SECURE_COOKIES", False)
CSRF_COOKIE_NAME = "mcpanel_csrftoken"
CSRF_COOKIE_HTTPONLY = False  # SPA reads it to send X-CSRFToken
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_FAILURE_VIEW = "apps.core.views.csrf_failure"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
if env_bool("MCPANEL_BEHIND_TLS_PROXY", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True

# --- REST framework ------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.core.auth.CsrfEnforcedSessionAuthentication"],
    # PasswordChangeEnforced: accounts flagged must_change_password get 403 must_change_password
    # everywhere except the views that pin their own permission_classes (auth state/setup/login/
    # logout/me/password/reset and the health probe).
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
        "apps.core.permissions.PasswordChangeEnforced",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": None,
    "EXCEPTION_HANDLER": "apps.core.errors.exception_handler",
    "DEFAULT_THROTTLE_RATES": {"login": "10/min", "setup": "10/min"},
    # Identify clients by REMOTE_ADDR only. Without this DRF trusts any client-supplied
    # X-Forwarded-For, which makes the login throttle trivially bypassable. Behind a
    # reverse proxy, uvicorn --proxy-headers (MCPANEL_BEHIND_TLS_PROXY) already sets
    # REMOTE_ADDR to the real client for trusted proxies only.
    "NUM_PROXIES": 0,
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "MCPanel API",
    "DESCRIPTION": "Management API for the MCPanel self-hosted Minecraft server platform.",
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAuthenticated", "apps.core.permissions.PasswordChangeEnforced"],
}


# --- Celery ----------------------------------------------------------------------
def celery_broker_url(url: str) -> str:
    """The URL as Celery's Redis transport (kombu) reads it.

    Everything else here (redis-py, Django's cache, channels_redis) writes a Redis server on a
    Unix socket as ``unix:///path/redis.sock?db=0``. kombu has no "unix" transport ("No such
    transport: unix"): it wants ``redis+socket:///path/redis.sock`` with the database as
    ``?virtual_host=N``. So MCPANEL_REDIS_URL alone is enough in .env, whatever its form.
    """
    parts = urlsplit(url or "")
    if parts.scheme != "unix":
        return url
    query = parse_qs(parts.query)
    database = (query.get("db") or ["0"])[0].strip() or "0"
    password = parts.password or (query.get("password") or [""])[0]
    credentials = f"{quote(parts.username or '', safe='')}:{quote(password, safe='')}@" if (parts.username or password) else ""
    return f"redis+socket://{credentials}{parts.path}" + (f"?virtual_host={database}" if database != "0" else "")


CELERY_BROKER_URL = celery_broker_url(env("MCPANEL_CELERY_BROKER_URL", REDIS_URL) or "")
CELERY_RESULT_BACKEND = None
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
# With Redis as broker, a message that is still unacknowledged (acks_late: until the task
# returns) after the visibility timeout (kombu default: 1 h) is handed to another worker. Jobs
# (modpack installs, big backups/restores, migrations) can run for hours, so keep it far above
# the longest job. A redelivered job message is harmless anyway: run_job ignores it while the
# job's heartbeat is fresh (apps/jobs/runner.py).
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "visibility_timeout": int(env("MCPANEL_CELERY_VISIBILITY_TIMEOUT", "86400") or 86400),
}
CELERY_TASK_ALWAYS_EAGER = env_bool("MCPANEL_CELERY_EAGER", TESTING)
CELERY_TASK_EAGER_PROPAGATES = False
CELERY_TIMEZONE = TIME_ZONE
# Two queues with separate bindings: "celery" (default) carries jobs (apps.jobs.runner.run_job,
# long and blocking), "periodic" the beat ticks and other short tasks. A worker started without
# -Q consumes every queue listed here, so the stock `worker` command keeps working; a second,
# dedicated `worker -Q periodic` keeps reconcile, schedules and the watchdog running even while
# long jobs occupy every slot of the main worker.
CELERY_TASK_DEFAULT_QUEUE = "celery"
CELERY_TASK_QUEUES = {
    "celery": {"exchange": "celery", "routing_key": "celery"},
    "periodic": {"exchange": "periodic", "routing_key": "periodic"},
}
CELERY_TASK_ROUTES = {
    "apps.instances.tasks.*": {"queue": "periodic"},
    "apps.jobs.tasks.*": {"queue": "periodic"},
    "apps.metrics.tasks.*": {"queue": "periodic"},
    "apps.modpacks.tasks.*": {"queue": "periodic"},
    "apps.notifications.*": {"queue": "periodic"},
}
# "expires" drops ticks that waited too long in the queue (no pile-up behind busy workers);
# overlapping runs of the same tick are skipped by mcpanel.celery.SINGLE_FLIGHT.
CELERY_BEAT_SCHEDULE = {
    "reconcile-instances": {"task": "apps.instances.tasks.reconcile_all", "schedule": 10.0, "options": {"expires": 30}},
    "scheduler-tick": {"task": "apps.instances.tasks.scheduler_tick", "schedule": 60.0, "options": {"expires": 55}},
    "jobs-watchdog": {"task": "apps.jobs.tasks.watchdog", "schedule": 120.0, "options": {"expires": 110}},
    "metrics-sample": {"task": "apps.metrics.tasks.sample_all", "schedule": 60.0, "options": {"expires": 50}},
    "metrics-cleanup": {"task": "apps.metrics.tasks.cleanup", "schedule": 3600.0, "options": {"expires": 3000}},
    # hourly tick; each modpack server is checked at most every 6 h (apps/modpacks/updates.py)
    "modpack-update-check": {
        "task": "apps.modpacks.tasks.check_updates_due",
        "schedule": 3600.0,
        "options": {"expires": 1800},
    },
}
# Job liveness (apps/jobs/runner.py): while a handler runs, its worker touches Job.heartbeat_at
# every MCPANEL_JOB_HEARTBEAT_SECONDS. A RUNNING job without a heartbeat for
# MCPANEL_JOB_STALE_SECONDS was interrupted (worker crashed/restarted) and is failed by the
# watchdog or by a redelivered message; jobs with a fresh heartbeat are left alone. A job still
# heartbeating after MCPANEL_JOB_MAX_RUNTIME_HOURS (0 = no limit) is failed as hung.
MCPANEL_JOB_HEARTBEAT_SECONDS = float(env("MCPANEL_JOB_HEARTBEAT_SECONDS", "30") or 30)
MCPANEL_JOB_STALE_SECONDS = float(env("MCPANEL_JOB_STALE_SECONDS", "300") or 300)
MCPANEL_JOB_MAX_RUNTIME_HOURS = float(env("MCPANEL_JOB_MAX_RUNTIME_HOURS", "48") or 0)
if MCPANEL_JOB_HEARTBEAT_SECONDS <= 0 or MCPANEL_JOB_STALE_SECONDS < 3 * MCPANEL_JOB_HEARTBEAT_SECONDS:
    raise ImproperlyConfigured("MCPANEL_JOB_STALE_SECONDS must be at least 3x MCPANEL_JOB_HEARTBEAT_SECONDS (> 0)")

# --- First-run setup ------------------------------------------------------------
# MCPANEL_SETUP_TOKEN (or MCPANEL_SETUP_TOKEN_FILE) is read per request by
# apps.accounts.views.setup_token(), not here: a token file that cannot be read keeps the setup
# page locked instead of preventing the panel from starting.

# --- Agent / node defaults ---------------------------------------------------
MCPANEL_AGENT_URL = env("MCPANEL_AGENT_URL", "http://localhost:8700") or ""
MCPANEL_AGENT_TOKEN = read_secret("MCPANEL_AGENT_TOKEN", "dev-agent-token" if (DEBUG or TESTING) else None)
if not MCPANEL_AGENT_TOKEN:
    raise ImproperlyConfigured("MCPANEL_AGENT_TOKEN (or MCPANEL_AGENT_TOKEN_FILE) must be set")
MCPANEL_AGENT_TIMEOUT = float(env("MCPANEL_AGENT_TIMEOUT", "30") or 30)
# TLS for agents reached over https:// (see apps/nodes/tls.py). The fingerprint pins the
# default node's certificate (python -m mcagent fingerprint); the CA bundle adds a private CA
# for agents with CA-issued certificates. Neither is needed for the single-host stack.
MCPANEL_AGENT_TLS_FINGERPRINT = re.sub(r"[\s:]", "", env("MCPANEL_AGENT_TLS_FINGERPRINT", "") or "").lower()
if MCPANEL_AGENT_TLS_FINGERPRINT and not re.fullmatch(r"[0-9a-f]{64}", MCPANEL_AGENT_TLS_FINGERPRINT):
    raise ImproperlyConfigured("MCPANEL_AGENT_TLS_FINGERPRINT must be a SHA-256 fingerprint (64 hex digits)")
MCPANEL_AGENT_CA_BUNDLE = env("MCPANEL_AGENT_CA_BUNDLE", "") or ""
# A stopped server has no container: the reconciler asks the agent to remove ("retire") the
# container of a server that is meant to be stopped, after the agent saved the record of its last
# run (apps/instances/reconcile.py, maybe_retire). false = keep them, as releases before did.
MCPANEL_RETIRE_STOPPED_CONTAINERS = env_bool("MCPANEL_RETIRE_STOPPED_CONTAINERS", True)

# Host addresses shown to users for connecting (purely informational).
MCPANEL_PUBLIC_HOSTNAME = env("MCPANEL_PUBLIC_HOSTNAME", "")
MCPANEL_LAN_ADDRESS = env("MCPANEL_LAN_ADDRESS", "")

# Port allocation policy for Minecraft instances.
MCPANEL_PORT_RANGE_START = int(env("MCPANEL_PORT_RANGE_START", "25565") or 25565)
MCPANEL_PORT_RANGE_END = int(env("MCPANEL_PORT_RANGE_END", "25665") or 25665)

# Optional provider credentials (can also be set in System Settings).
CURSEFORGE_API_KEY = read_secret("CURSEFORGE_API_KEY", "")
MCPANEL_USER_AGENT = env(
    "MCPANEL_USER_AGENT", "mcpanel/0.1.0 (self-hosted Minecraft server panel; +https://github.com/cj7s1/mcserver)"
)
# Generic webhooks may target private/LAN addresses (Home Assistant, ntfy, Gotify...). Loopback,
# link-local/metadata and MCPanel's own services are always refused (apps/notifications/service.py).
MCPANEL_WEBHOOK_ALLOW_LAN = env_bool("MCPANEL_WEBHOOK_ALLOW_LAN", True)

DATA_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
MCPANEL_MAX_UPLOAD_BYTES = int(env("MCPANEL_MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)) or 0)

# --- Logging -------------------------------------------------------------------
LOG_LEVEL = env("MCPANEL_LOG_LEVEL", "INFO") or "INFO"
LOG_JSON = env_bool("MCPANEL_LOG_JSON", not DEBUG)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"request_context": {"()": "apps.core.logging.RequestContextFilter"}},
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.json.JsonFormatter",
            "fmt": "%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s",
        },
        "plain": {"format": "%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json" if LOG_JSON else "plain",
            "filters": ["request_context"],
        }
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.db.backends": {"level": "WARNING"},
        "httpx": {"level": "WARNING"},
        "httpcore": {"level": "WARNING"},
    },
}
