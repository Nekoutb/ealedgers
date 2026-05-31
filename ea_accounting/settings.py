"""Django settings for the EA Accounting Application.

Reads production-sensitive values from env vars so the same settings.py
works in dev (no env vars set → safe defaults) and production
(env vars set by systemd EnvironmentFile).
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ('true', '1', 'yes', 'on')


# --- Core / security ---------------------------------------------------------

SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-)lo1&_pydt8=4c#4)2ljjo8g7kq8i&xokq-faf8n(2y220psc=',  # dev fallback
)

DEBUG = _env_bool('DJANGO_DEBUG', True)

ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get('DJANGO_ALLOWED_HOSTS', '*').split(',') if h.strip()
]

# CSRF: trust the HTTPS origins of any allowed hostnames (non-IPs)
CSRF_TRUSTED_ORIGINS = [
    f'https://{h}' for h in ALLOWED_HOSTS
    if h and not h.replace('.', '').isdigit() and h != '*'
]

# --- Apps / middleware -------------------------------------------------------

INSTALLED_APPS = [
    # `accounting` MUST come before `django.contrib.admin` so our
    # accounting/templates/admin/base_site.html override wins over Django's default.
    'accounting',
    # v2 apps (Step 11 scaffold — empty until later phases populate them).
    # L3 knowledge, L4 agents, L2 ingest, L6 connectors (see EXECUTION_PLAN §2).
    'knowledge',
    'agents',
    'ingest',
    'connectors',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',  # provides {% load humanize %} (intcomma, etc.)
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Resolves request.tenant from the authenticated user's session/membership.
    # MUST come after AuthenticationMiddleware (needs request.user).
    'accounting.middleware.TenantContextMiddleware',
]

ROOT_URLCONF = 'ea_accounting.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'accounting.context_processors.accounting_nav',
            ],
        },
    },
]

WSGI_APPLICATION = 'ea_accounting.wsgi.application'

# --- Database ---------------------------------------------------------------
#
# Step 2 (Phase 0.3) — the app speaks both SQLite (dev default) and
# PostgreSQL (production target). Driven by the ``DATABASE_URL`` env var:
#
#   - unset / blank → SQLite at ``BASE_DIR / db.sqlite3`` (unchanged dev path)
#   - postgres:// or postgresql:// URL → Postgres via psycopg
#
# When ``DATABASE_URL`` points at Postgres, the ``TenantContextMiddleware``
# additionally sets the session-local ``app.current_tenant_id`` variable so
# the Row-Level Security policies installed in migration 0008 can filter
# rows in the database layer as a defence-in-depth backstop on top of the
# ORM-level ``for_tenant`` calls.

def _parse_database_url(url):
    """Tiny URL parser to avoid the dj-database-url dependency."""
    from urllib.parse import urlparse, unquote, parse_qsl
    p = urlparse(url)
    if p.scheme not in ('postgres', 'postgresql'):
        raise ValueError(
            f'Unsupported DATABASE_URL scheme {p.scheme!r}; '
            f'expected postgres:// or postgresql://'
        )
    options = dict(parse_qsl(p.query))
    return {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': (p.path or '/').lstrip('/'),
        'USER': unquote(p.username or ''),
        'PASSWORD': unquote(p.password or ''),
        'HOST': p.hostname or '',
        'PORT': str(p.port) if p.port else '',
        'OPTIONS': options,
        'CONN_MAX_AGE': int(os.environ.get('DJANGO_DB_CONN_MAX_AGE', '60')),
    }


_DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
if _DATABASE_URL:
    DATABASES = {'default': _parse_database_url(_DATABASE_URL)}
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
]

# --- Locale ----------------------------------------------------------------

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Douala'
USE_I18N = True
USE_TZ = True
USE_THOUSAND_SEPARATOR = True
THOUSAND_SEPARATOR = ' '
DECIMAL_SEPARATOR = ','

# --- Static files ----------------------------------------------------------

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- Authentication flow ---------------------------------------------------

# Unauthenticated users hitting protected pages are bounced to the landing
# page (which IS the login form). After successful login they go to the
# workspace (module launcher), and from there into individual modules.
LOGIN_URL = '/'
LOGIN_REDIRECT_URL = '/workspace/'
LOGOUT_REDIRECT_URL = '/'


# --- Production hardening (only when DEBUG is off) -------------------------

if not DEBUG:
    # nginx terminates TLS and sets X-Forwarded-Proto
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30  # 30 days, expand to 1y once happy
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = False
    X_FRAME_OPTIONS = 'DENY'
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = 'same-origin'
