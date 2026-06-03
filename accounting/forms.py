"""Forms for self-serve sign-up.

The ``SignupForm`` creates a User, a Tenant, and a Membership in one
atomic transaction so the new account always lands in a usable state
(authenticated user with a tenant to log into).
"""

import re

from django import forms
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils.text import slugify

from accounting.models import ERPConnection, Membership, Tenant


User = get_user_model()


# Reserved slugs we never want to assign — they'd collide with our URL
# routes or look weird as a subdomain.
RESERVED_SLUGS = {
    "admin", "api", "app", "apps", "accounting", "billing", "static",
    "media", "workspace", "signup", "login", "logout", "tenant", "www",
    "mail", "smtp", "support", "help", "blog", "docs", "status",
}


def _unique_tenant_slug(base):
    """Generate a unique tenant slug from a company name.

    Falls back to suffixes (``-2``, ``-3``, …) if the base is taken or
    reserved.
    """
    base = slugify(base) or "company"
    if base in RESERVED_SLUGS:
        base = f"{base}-co"
    slug = base
    n = 2
    while Tenant.objects.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1
        if n > 9999:  # defensive — should never happen
            raise ValueError("Could not generate a unique tenant slug")
    return slug


class SignupForm(forms.Form):
    """One-page sign-up: username, email, password (×2), company name,
    country. Creates User + Tenant + Membership(role=owner) atomically."""

    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "username", "autofocus": True}),
        help_text="Letters, digits and @/./+/-/_ only.",
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )
    password1 = forms.CharField(
        label="Password",
        min_length=8,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="At least 8 characters.",
    )
    password2 = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    company_name = forms.CharField(
        max_length=128,
        label="Company name",
        widget=forms.TextInput(attrs={"autocomplete": "organization"}),
    )
    country = forms.CharField(
        max_length=64,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "country-name"}),
        help_text="Optional. e.g. Cameroon, Senegal.",
    )

    def clean_username(self):
        value = self.cleaned_data["username"].strip()
        if not re.match(r"^[\w.@+-]+$", value):
            raise forms.ValidationError(
                "Username may only contain letters, digits, and @/./+/-/_."
            )
        if User.objects.filter(username__iexact=value).exists():
            raise forms.ValidationError("That username is already taken.")
        return value

    def clean_email(self):
        value = self.cleaned_data["email"].strip().lower()
        # Email collisions are a soft signal — we still let them sign up,
        # but log if they reuse. (Some users do legitimately want a second
        # account.) If we ever switch to email-as-login, tighten this.
        return value

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("password1")
        p2 = cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "Passwords don't match.")
        return cleaned

    @transaction.atomic
    def save(self):
        """Create User → Tenant → Membership atomically. Returns the new
        User (already saved, with `tenant` attached on the instance for
        callers who want it without a second query)."""
        data = self.cleaned_data

        user = User.objects.create_user(
            username=data["username"],
            email=data["email"],
            password=data["password1"],
        )

        tenant = Tenant.objects.create(
            slug=_unique_tenant_slug(data["company_name"]),
            name=data["company_name"].strip(),
            country=data.get("country", "").strip(),
            owner=user,
        )

        Membership.objects.create(
            user=user,
            tenant=tenant,
            role="owner",
            active=True,
        )

        # Convenience for the view — avoids a second roundtrip.
        user._signup_tenant = tenant  # noqa: SLF001
        return user


class ERPConnectionForm(forms.Form):
    """Create / edit a tenant's ERP connection. The API key is write-only:
    on edit it's left blank to keep the stored (encrypted) value. Connection
    is tested on save by the view."""

    vendor = forms.ChoiceField(choices=ERPConnection.VENDORS)
    name = forms.CharField(
        max_length=64,
        widget=forms.TextInput(attrs={"placeholder": "e.g. Production Odoo"}),
    )
    base_url = forms.URLField(
        required=False, label="Base URL",
        widget=forms.URLInput(attrs={"placeholder": "https://yourco.odoo.com"}),
    )
    database = forms.CharField(
        max_length=128, required=False, label="Database name",
        widget=forms.TextInput(attrs={"placeholder": "yourco"}),
    )
    api_user = forms.CharField(
        max_length=128, required=False, label="API user",
        widget=forms.TextInput(attrs={"placeholder": "agent@yourco.com"}),
    )
    api_key = forms.CharField(
        required=False, label="API key",
        widget=forms.PasswordInput(render_value=False,
                                   attrs={"autocomplete": "new-password"}),
    )
    is_primary = forms.BooleanField(
        required=False, label="Primary connection")

    @staticmethod
    def initial_from(connection):
        """Build the initial dict for editing an existing connection (the API
        key is never pre-filled — it's encrypted and write-only)."""
        cfg = connection.config or {}
        return {
            "vendor": connection.vendor,
            "name": connection.name,
            "base_url": cfg.get("url", ""),
            "database": cfg.get("db", ""),
            "api_user": cfg.get("username", ""),
            "is_primary": connection.is_primary,
        }

    def apply_to(self, connection):
        """Write the cleaned non-secret fields onto an ERPConnection
        instance. The API key is handled separately by the view (so a blank
        value keeps the existing key on edit)."""
        cd = self.cleaned_data
        connection.vendor = cd["vendor"]
        connection.name = cd["name"]
        connection.is_primary = cd["is_primary"]
        config = dict(connection.config or {})
        config["url"] = cd.get("base_url", "")
        config["db"] = cd.get("database", "")
        config["username"] = cd.get("api_user", "")
        connection.config = config
