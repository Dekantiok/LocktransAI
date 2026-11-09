import os
from pathlib import Path
from dotenv import load_dotenv
import dj_database_url

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'django-insecure-dev-fallback-key-change-in-production')

DEBUG = os.environ.get('DJANGO_DEBUG', 'False') == 'True'

if not DEBUG and SECRET_KEY == 'django-insecure-dev-fallback-key-change-in-production':
    raise RuntimeError(
        "DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is off."
    )

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost').split(',')
    if host.strip()
]

if DEBUG:
    # Dev only: allow tunnel suffixes so webhooks reach this machine; never in production.
    ALLOWED_HOSTS += [
        suffix for suffix in (
            '.trycloudflare.com', '.ngrok.io', '.ngrok-free.app', '.loca.lt',
        )
        if suffix not in ALLOWED_HOSTS
    ]

if not DEBUG and ALLOWED_HOSTS == ['127.0.0.1', 'localhost']:
    raise RuntimeError("DJANGO_ALLOWED_HOSTS must be set when DJANGO_DEBUG is off.")

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'escrow',
    'agent',
    'voice',
    'whatsapp',
]

# Plain tuples: inline styles/scripts predate nonces; nonces come with the frontend build.
CONTENT_SECURITY_POLICY = {
    'DIRECTIVES': {
        'default-src': ("'self'",),
        'connect-src': ("'self'", 'https://cdn.jsdelivr.net',
                        'https://fonts.googleapis.com'),
        'font-src': ("'self'", 'https://fonts.gstatic.com', 'data:'),
        'img-src': ("'self'", 'data:'),
        'script-src': ("'self'", 'https://cdn.jsdelivr.net'),
        'style-src': ("'self'", "'unsafe-inline'",
                      'https://cdn.jsdelivr.net', 'https://fonts.googleapis.com'),
    },
}

def _permissions_policy(get_response):
    """Permissions-Policy header (mic only, self only); Django has no setting for it."""
    def middleware(request):
        response = get_response(request)
        response['Permissions-Policy'] = 'microphone=(self), camera=()'
        return response
    return middleware


MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'csp.middleware.CSPMiddleware',
    'config.settings._permissions_policy',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'config.context_processors.demo_mode',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# SQLite locally; managed Postgres via DATABASE_URL when DEBUG is off.
if DEBUG:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': Path(os.environ.get('DJANGO_DB_PATH', BASE_DIR / 'db.sqlite3')),
        }
    }
else:
    DATABASES = {
        'default': dj_database_url.config(
            default=os.environ.get('DATABASE_URL'),
            conn_max_age=600,
            ssl_require=True,
        )
    }

CACHES = {
    'default': {
        # LocMem cache fragments OTP/voice/vault state by design; sessions stay database-backed.
        'BACKEND': os.environ.get(
            'CACHE_BACKEND', 'django.core.cache.backends.locmem.LocMemCache'),
        'LOCATION': os.environ.get('CACHE_LOCATION', 'locktrans'),
    }
}

# Passwordless OTP sessions: bound the window a stolen session stays useful.
SESSION_COOKIE_AGE = int(os.environ.get('SESSION_COOKIE_AGE', '86400'))
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'Africa/Lagos'

USE_I18N = True

USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        # Not manifest storage: missing entries 500 the admin on fresh checkouts.
        'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage',
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Console backend in DEBUG; configure SMTP for production delivery.
if DEBUG:
    EMAIL_BACKEND = os.environ.get(
        'EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')
else:
    EMAIL_BACKEND = os.environ.get(
        'EMAIL_BACKEND', 'django.core.mail.backends.smtp.EmailBackend')
EMAIL_HOST = os.environ.get('EMAIL_HOST', '')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'True') == 'True'
if not DEBUG and EMAIL_BACKEND.endswith('smtp.EmailBackend') and not EMAIL_HOST:
    # No SMTP: log invitations instead of crashing the boot.
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "EMAIL_HOST unset: counterparty invitations will log instead of sending."
    )
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
DEFAULT_FROM_EMAIL = os.environ.get(
    'DEFAULT_FROM_EMAIL', 'Locktrans.AI <no-reply@locktrans.example>')

# Demo mode: OTP delivery is stubbed and payments simulated; AI and escrow logic stay real.
DEMO_MODE = os.environ.get('DEMO_MODE', 'False') == 'True'
DEMO_OTP_CODE = os.environ.get('DEMO_OTP_CODE', '123456') or '123456'
DEMO_ALLOW_PROD = os.environ.get('DEMO_ALLOW_PROD', 'False') == 'True'
# True routes funding/payouts through the simulated checkout; False uses the real PayPal sandbox.
DEMO_SIMULATE_PAYMENTS = os.environ.get('DEMO_SIMULATE_PAYMENTS', 'True') == 'True'

if not DEBUG and DEMO_MODE and not DEMO_ALLOW_PROD:
    raise RuntimeError(
        "DEMO_MODE is on with DJANGO_DEBUG off. Demo mode accepts a "
        "universal OTP and simulates payments, so it must never run in "
        "production by accident. Set DEMO_ALLOW_PROD=True only for an "
        "intentional, isolated demo deployment."
    )

SESSION_ENGINE = os.environ.get(
    'SESSION_ENGINE', 'django.contrib.sessions.backends.db')

# PayPal Orders v2 + Payouts. Sandbox by default; PAYPAL_MODE=live with live credentials for production.
PAYPAL_CLIENT_ID = os.environ.get('PAYPAL_CLIENT_ID', '')
PAYPAL_CLIENT_SECRET = os.environ.get('PAYPAL_CLIENT_SECRET', '')
PAYPAL_MODE = os.environ.get('PAYPAL_MODE', 'sandbox')
PAYPAL_BASE_URL = os.environ.get('PAYPAL_BASE_URL', '')
PAYPAL_WEBHOOK_ID = os.environ.get('PAYPAL_WEBHOOK_ID', '')
PAYPAL_MERCHANT_EMAIL = os.environ.get('PAYPAL_MERCHANT_EMAIL', '')
if not DEBUG and not PAYPAL_CLIENT_ID:
    raise RuntimeError(
        "PAYPAL_CLIENT_ID must be set when DJANGO_DEBUG is off."
    )
TERMII_API_KEY = os.environ.get('TERMII_API_KEY', 'dummy_key')
TERMII_EMAIL_CONFIG_ID = os.environ.get('TERMII_EMAIL_CONFIG_ID', 'default_config')

# WhatsApp Business Platform Cloud API.
WHATSAPP_ACCESS_TOKEN = os.environ.get('WHATSAPP_ACCESS_TOKEN', '')
WHATSAPP_PHONE_NUMBER_ID = os.environ.get('WHATSAPP_PHONE_NUMBER_ID', '')
WHATSAPP_BUSINESS_ACCOUNT_ID = os.environ.get('WHATSAPP_BUSINESS_ACCOUNT_ID', '')
WHATSAPP_WEBHOOK_VERIFY_TOKEN = os.environ.get('WHATSAPP_WEBHOOK_VERIFY_TOKEN', '')
WHATSAPP_APP_SECRET = os.environ.get('WHATSAPP_APP_SECRET', '')
WHATSAPP_API_VERSION = os.environ.get('WHATSAPP_API_VERSION', 'v21.0')

# Separate key from the chat model; voice degrades to typed input when unset.
MODEL_API_KEY = os.environ.get('MODEL_API_KEY', '')
ASR_BASE_URL = os.environ.get('ASR_BASE_URL', 'https://api.meta.ai')
ASR_MODEL = os.environ.get('ASR_MODEL', 'muse-voice-transcribe-1.0')
VOICE_MAX_BYTES = int(os.environ.get('VOICE_MAX_BYTES', str(8 * 1024 * 1024)))
VOICE_TIMEOUT = int(os.environ.get('VOICE_TIMEOUT', '60'))

# Any OpenAI-compatible /v1 endpoint; swapping AI servers is a config change, no code needed.
INFERENCE_API_KEY = os.environ.get('INFERENCE_API_KEY', '')
INFERENCE_BASE_URL = os.environ.get('INFERENCE_BASE_URL', '')
INFERENCE_MODEL = os.environ.get('INFERENCE_MODEL', '')

# INFERENCE_MAX_TOKENS must cover chain-of-thought plus the answer on reasoning models.
AGENT_MAX_ITERATIONS = int(os.environ.get('AGENT_MAX_ITERATIONS', '8'))
AGENT_REQUEST_TIMEOUT = int(os.environ.get('AGENT_REQUEST_TIMEOUT', '60'))
INFERENCE_MAX_TOKENS = int(os.environ.get('INFERENCE_MAX_TOKENS', '16384'))

# Bound outbound gateway calls so a slow gateway never holds requests open (matters most on payout).
GATEWAY_HTTP_TIMEOUT = int(os.environ.get('GATEWAY_HTTP_TIMEOUT', '15'))

# Cap WhatsApp media downloads; the transcriber also caps the converted WAV independently.
WHATSAPP_MEDIA_MAX_BYTES = int(os.environ.get('WHATSAPP_MEDIA_MAX_BYTES', str(8 * 1024 * 1024)))

# Cooldown bounds inbox spam driven from a known escrow code.
OTP_ISSUE_COOLDOWN_SECONDS = int(os.environ.get('OTP_ISSUE_COOLDOWN_SECONDS', '60'))

# Log OTPs only while developing; off by default so credentials never leak.
LOG_OTP_TO_CONSOLE = os.environ.get('LOG_OTP_TO_CONSOLE', 'False') == 'True'

# Opt-in live Termii sends in DEBUG; every OTP then costs a real credit.
TERMII_SEND_IN_DEBUG = os.environ.get('TERMII_SEND_IN_DEBUG', 'False') == 'True'

# Trusted origins for cross-site POSTs; DEBUG still rejects HTTPS origins not on this list.
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',')
    if origin.strip()
]

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = os.environ.get('SECURE_SSL_REDIRECT', 'True') == 'True'
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    # Preloading is opt-in: browsers force HTTPS for months and it is hard to walk back.
    SECURE_HSTS_PRELOAD = os.environ.get('SECURE_HSTS_PRELOAD', 'False') == 'True'
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = 'same-origin'
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    X_FRAME_OPTIONS = 'DENY'

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'simple': {'format': '[{asctime}] {levelname} {name}: {message}', 'style': '{'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'simple'},
    },
    'root': {'handlers': ['console'], 'level': os.environ.get('LOG_LEVEL', 'INFO')},
    'loggers': {
        'escrow': {
            'handlers': ['console'],
            'level': os.environ.get('LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
    },
}
