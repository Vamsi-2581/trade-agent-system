import os
from pathlib import Path
from datetime import timedelta
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv()

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-in-production")

DEBUG = False

ALLOWED_HOSTS = []

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    # Third-party
    "rest_framework",
    "corsheaders",
    "channels",
    # Core app
    "apps.core",
    # Apps
    "apps.authentication",
    "apps.agent",
    "apps.skill",
    "apps.exchange",
    "apps.trading",
    "apps.riskguard",
    "apps.backtest",
    "apps.memory",
    "apps.channel",
    "apps.notify",
    "apps.datasource",
    "apps.signal_monitor",
    "apps.strategy_engine",
    "apps.regime",
    "apps.logging_app",
    # Celery beat
    "django_celery_beat",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "core.urls"
ASGI_APPLICATION = "core.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "trade_agent"),
        "USER": os.environ.get("DB_USER", "postgres"),
        "PASSWORD": os.environ.get("DB_PASSWORD", ""),
        "HOST": os.environ.get("DB_HOST", "localhost"),
        "PORT": os.environ.get("DB_PORT", "5432"),
        "CONN_MAX_AGE": int(os.environ.get("DB_CONN_MAX_AGE", "30")),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {"options": "-c search_path=public"},
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.environ.get("REDIS_URL", "redis://localhost:6379/1"),
    }
}

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [os.environ.get("REDIS_URL", "redis://localhost:6379")],
        },
    },
}

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
REDIS_MEMORY_URL = os.environ.get(
    "REDIS_MEMORY_URL", f"{REDIS_URL}/6"
)  # Redis DB 6 for memory system

# Redis DB allocation
# DB0: Django cache
# DB1: Channel layers
# DB2: Celery broker
# DB3: Agent task queue (Redis Stream)
# DB4: Trading order queue (Redis Stream)
# DB5: Position cache
# DB6: Agent working memory (TTL)
# DB7: RiskGuard circuit breaker
REDIS_DB_CACHE = 0
REDIS_DB_CELERY = 2
REDIS_DB_AGENT_STREAM = 3
REDIS_DB_TRADING_STREAM = 4
REDIS_DB_POSITION = 5
REDIS_DB_MEMORY = 6
REDIS_DB_RISK = 7
REDIS_DB_FRAME = 8  # 框架状态持久化
REDIS_DB_WS_PENDING = 9  # WebSocket 断线待发消息缓存

CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://localhost:6379") + "/2"
CELERY_RESULT_BACKEND = os.environ.get("REDIS_URL", "redis://localhost:6379") + "/2"
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = "UTC"

# ── Reliability: prevent task loss ──
CELERY_TASK_ACKS_LATE = True  # Ack AFTER task completes — if worker crashes, task is re-delivered
CELERY_TASK_REJECT_ON_WORKER_LOST = True  # Re-queue if worker process is killed
CELERY_RESULT_EXPIRES = 86400  # Keep results for 24h (seconds)
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "visibility_timeout": 3600,  # Re-deliver unacked messages after 1h
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "20/min",
        "user": "200/min",
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=1),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
}

CORS_ALLOWED_ORIGINS = os.environ.get(
    "CORS_ALLOWED_ORIGINS", "http://localhost:3000"
).split(",")

AUTH_USER_MODEL = "authentication.User"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True

# 业务时区：用户口语时间（"每天10点"）按此时区解释，见 apps/common/time_utils.py。
# 注意这不改变 TIME_ZONE/CELERY_TIMEZONE —— 那两个保持 UTC 以兼容已有数据。
BUSINESS_TIMEZONE = os.environ.get("BUSINESS_TIMEZONE", "Asia/Shanghai")

# LLM
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL_PRIMARY = os.environ.get("OPENAI_MODEL_PRIMARY", "gpt-4o")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL_FALLBACK = os.environ.get("ANTHROPIC_MODEL_FALLBACK", "claude-opus-4-6")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL_FALLBACK = os.environ.get("DEEPSEEK_MODEL_FALLBACK", "deepseek-chat")
DEEPSEEK_API_BASE_URL = os.environ.get(
    "DEEPSEEK_API_BASE_URL", "https://api.deepseek.com/v1"
)
OPENAI_API_BASE_URL = os.environ.get(
    "OPENAI_API_BASE_URL", "https://api.openai.com/v1/"
)
LLM_TOKEN_BUDGET = int(os.environ.get("LLM_TOKEN_BUDGET", "100000"))

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "")

# Proxies (each can be set independently; fall back to HTTPS_PROXY / HTTP_PROXY)
_default_proxy = os.environ.get("HTTPS_PROXY", "") or os.environ.get("HTTP_PROXY", "")
TELEGRAM_PROXY = os.environ.get("TELEGRAM_PROXY", "") or _default_proxy
OPENAI_PROXY = os.environ.get("OPENAI_PROXY", "") or _default_proxy
ANTHROPIC_PROXY = os.environ.get("ANTHROPIC_PROXY", "") or _default_proxy
DEEPSEEK_PROXY = os.environ.get("DEEPSEEK_PROXY", "") or _default_proxy
WEB_PROXY = os.environ.get("WEB_PROXY", "") or _default_proxy

# 数据源：后端启动时自动建立 WS 实时行情供给（默认开启；
# 非 Web 进程如 celery 可设 DATASOURCE_AUTO_WS=false 关闭）
DATASOURCE_AUTO_WS = (
    os.environ.get("DATASOURCE_AUTO_WS", "true").lower() not in ("0", "false", "no")
)

# ---------------------------------------------------------------------------
# 行情数据同步（K 线 / 资金费率）
# ---------------------------------------------------------------------------
# 标的集合单一来源：K 线增量同步与资金费率同步共用同一批永续合约。
# 历史上两者各自硬编码在各自的任务模块里（见 spec §8），容易漂移。
PERPETUAL_SYNC_SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
KLINE_SYNC_SYMBOLS = list(PERPETUAL_SYNC_SYMBOLS)
FUNDING_SYNC_SYMBOLS = list(PERPETUAL_SYNC_SYMBOLS)

KLINE_SYNC_TIMEFRAMES = ["15m", "1h", "4h"]
KLINE_BACKFILL_DAYS = 365  # 首次回填 / 无记录时的默认回溯天数
KLINE_SYNC_INTERVAL = 900  # 增量同步间隔（秒）；beat 与任务同源读这一个值

# Web tools
WEB_SEARCH_PROVIDER = os.environ.get(
    "WEB_SEARCH_PROVIDER", "duckduckgo"
)  # brave|tavily|searxng|jina|duckduckgo
WEB_SEARCH_MAX_RESULTS = int(os.environ.get("WEB_SEARCH_MAX_RESULTS", "5"))
WEB_FETCH_MAX_CHARS = int(os.environ.get("WEB_FETCH_MAX_CHARS", "8000"))
BRAVE_SEARCH_API_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
SEARXNG_BASE_URL = os.environ.get("SEARXNG_BASE_URL", "")
JINA_API_KEY = os.environ.get("JINA_API_KEY", "")

# Lark / Feishu
LARK_APP_ID = os.environ.get("LARK_APP_ID", "")
LARK_APP_SECRET = os.environ.get("LARK_APP_SECRET", "")
LARK_VERIFICATION_TOKEN = os.environ.get("LARK_VERIFICATION_TOKEN", "")
LARK_ENCRYPT_KEY = os.environ.get("LARK_ENCRYPT_KEY", "")
LARK_OAUTH_REDIRECT_URI = os.environ.get(
    "LARK_OAUTH_REDIRECT_URI",
    "http://localhost:8000/api/channel/auth/lark/callback/",
)
LARK_OAUTH_SCOPES = os.environ.get("LARK_OAUTH_SCOPES", "")

# Main channel for system notifications (lark | telegram | tui)
# Only the main channel receives notifications; others are suppressed.
MAIN_CHANNEL = os.environ.get("MAIN_CHANNEL", "lark")

# Fernet encryption key for API secrets (generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
FERNET_KEY = os.environ.get("FERNET_KEY", "")

if not FERNET_KEY:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        "FERNET_KEY environment variable is required. "
        "Generate one with: python -c "
        '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
    )

# ===================================================
# Logging Configuration — unified log collection
# ===================================================
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} [{name}] {message}",
            "style": "{",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        "system_log": {
            "class": "apps.logging_app.handler.SystemLogHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console", "system_log"],
        "level": "INFO",
    },
    "loggers": {
        "uvicorn.access": {
            "handlers": ["console"],
            "level": "WARNING",  # 设为 WARNING 即可屏蔽正常请求日志
        },
        "rest_framework": {
            "level": "WARNING",
            "handlers": ["console"],
            "propagate": False,
        },
        "httpx": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        "httpcore": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        "telegram": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        "ccxt": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        "markdown_it": {
            "level": "WARNING",
            "handlers": ["console"],
            "propagate": False,
        },
        "websockets": {
            "level": "WARNING",
            "handlers": ["console"],
            "propagate": False,
        },
        "urllib3": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        # "celery": {"level": "WARNING", "handlers": ["console"], "propagate": False},
    },
}
