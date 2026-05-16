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
