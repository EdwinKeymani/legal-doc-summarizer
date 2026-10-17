
"""
Chambua - contract summaries, risky clauses and key terms.
Web-Based Automated Legal Document Summary & Insights Tool (diploma project).
Single-file Flask application.

Run with:  python app.py
Then open: http://127.0.0.1:5000
"""

import io
import os
import re
import json
import time
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps
import click
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, send_from_directory, send_file, abort, jsonify, g, has_request_context
)
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate, upgrade as upgrade_database_schema
import sqlalchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.middleware.proxy_fix import ProxyFix
from markupsafe import Markup
import markdown as markdown_renderer
import nh3
from flask_wtf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# Document text extraction
import pdfplumber
import docx

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv()

app = Flask(__name__)

# Render runs the app behind one proxy (it sets RENDER=true itself). Trusting
# that single hop gives the audit log the visitor's real IP address instead of
# the proxy's, and makes generated links use https.
if os.environ.get("RENDER"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Minutes after which a document still marked "Processing" is treated as
# interrupted (worker restart, timeout, closed tab) and marked Failed.
STALE_PROCESSING_MINUTES = 10

# Only show password-reset links on screen during local development.
# On Render this stays off, so the link is only written to the server log.
SHOW_RESET_LINK_ON_SCREEN = os.environ.get("SHOW_RESET_LINK") == "1"

# Brand: change the name here and it changes on every page, title and report
BRAND_NAME = "Chambua"
BRAND_TAGLINE = "Contracts, broken down."

# Single source of truth for every choice the UI offers. Templates build their
# dropdowns from these lists, and submitted values are checked against them.
THEME_OPTIONS = ["system", "light", "dark"]
SUMMARY_DEPTH_OPTIONS = ["Executive Summary", "Detailed Clauses"]
EXTRACTION_MODE_OPTIONS = ["Extractive - Fast", "Extractive - Premium", "Abstractive - Premium (AI)"]
RISK_LEVEL_OPTIONS = ["All Clauses", "High & Medium Risk", "High Risk Only"]

# Which clause severities the report shows for each Risk Detection Level.
# "Review" (not yet assessed) is always shown so a pending clause is never hidden.
VISIBLE_SEVERITIES_BY_RISK_LEVEL = {
    "All Clauses": {"High", "Medium", "Low", "Review"},
    "High & Medium Risk": {"High", "Medium", "Review"},
    "High Risk Only": {"High", "Review"},
}

# Proposal requirement 4.3: summary within 15 seconds for files under 50 pages
PERFORMANCE_TARGET_SECONDS = 15

# Roles (increment 7). Their permissions live in the database (role,
# permission and role_permission tables), seeded by the migration.
ROLE_MEMBER = "member"
ROLE_REVIEWER = "reviewer"
ROLE_ADMIN = "admin"
ROLE_ORDER = [ROLE_MEMBER, ROLE_REVIEWER, ROLE_ADMIN]

# Set this to an existing account's email to make it the first Administrator.
# It only acts while no Administrator exists, so it cannot be used later to
# take over the system. Remove it once the first Administrator is set up.
INITIAL_ADMIN_EMAIL = os.environ.get("INITIAL_ADMIN_EMAIL", "").strip().lower()

USERS_PER_PAGE = 25
AUDIT_ENTRIES_PER_PAGE = 50

# How long "Remember me" keeps someone signed in
REMEMBER_ME_DAYS = 30
CONTEXT_HINT_OPTIONS = ["General Commercial", "Employment", "Property / Lease"]

MAX_UPLOAD_MEGABYTES = 25

# HTML tags allowed in rendered AI summaries. Anything else (scripts, iframes,
# event handlers) is stripped, because the summary text comes from an AI model
# that was fed user-uploaded content.
ALLOWED_SUMMARY_TAGS = {
    "p", "br", "strong", "em", "b", "i", "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "blockquote", "code", "hr",
}

# Pull the secret from the environment; fall back to a clearly-labeled dev
# value so the app still runs out of the box for local testing. Never rely
# on the fallback outside of localhost.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-insecure-key-change-me")

# Database: reads DATABASE_URL when set (e.g. Render's Postgres), otherwise
# falls back to a local SQLite file for local development. Render (and some
# other providers) hand out a URL starting with postgres:// but SQLAlchemy
# 1.4+ requires postgresql://, so normalize it here.
database_url = os.environ.get("DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "legal.db"))
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url.replace("postgresql://", "postgresql+psycopg2://", 1) if database_url else database_url

# Fix SSL SYSCALL EOF errors on Render PostgreSQL
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,       # Checks connection health before running queries; reconnects if dropped
    "pool_recycle": 280,          # Recycles connections every ~4.5 minutes before Render drops them
    "pool_timeout": 30,           # Prevents long-hanging connection attempts
}

# Uploads: use /tmp on Vercel (its only writable location), otherwise a
# local uploads/ folder next to app.py.
app.config["UPLOAD_FOLDER"] = "/tmp/uploads" if os.environ.get("VERCEL") else os.path.join(BASE_DIR, "uploads")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MEGABYTES * 1024 * 1024  # matches the UI

# Harden session cookies. SECURE requires HTTPS, so it's disabled for local
# http://127.0.0.1 testing by default — set FORCE_SECURE_COOKIES=1 once
# this sits behind TLS.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FORCE_SECURE_COOKIES") == "1"
# Applies only when "Remember me" is ticked (session.permanent = True).
# Otherwise the login cookie is deleted when the browser is closed.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=REMEMBER_ME_DAYS)


@event.listens_for(Engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    """SQLite ignores foreign keys unless asked. Turning them on makes local
    testing behave like Postgres on Render, so delete-order bugs show up on
    your machine instead of in production."""
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

ALLOWED_EXTENSIONS = {"pdf", "docx", "txt"}
database = SQLAlchemy(app)

# Schema changes are versioned scripts in migrations/versions (Flask-Migrate,
# built on Alembic). They run automatically at start-up; see prepare_database().
migrate = Migrate(app, database, directory=os.path.join(BASE_DIR, "migrations"))

# Clean up database sessions after every request to prevent stale connections
@app.teardown_appcontext
def shutdown_session(exception=None):
    database.session.remove()

csrf = CSRFProtect(app)

# Ensure the upload directory exists on startup
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

# ---------------------------------------------------------------------------
# Database models
# ---------------------------------------------------------------------------
# Links each role to the permissions it holds (one row per tick in the
# role-permission matrix).
role_permission = database.Table(
    "role_permission",
    database.Column("role_id", database.Integer, database.ForeignKey("role.id"), primary_key=True),
    database.Column("permission_id", database.Integer, database.ForeignKey("permission.id"), primary_key=True),
)


class Role(database.Model):
    id = database.Column(database.Integer, primary_key=True)
    name = database.Column(database.String(20), unique=True, nullable=False)   # member / reviewer / admin
    label = database.Column(database.String(40), nullable=False)               # shown on screen
    description = database.Column(database.String(255), nullable=False)
    permissions = database.relationship(
        "Permission", secondary=role_permission, lazy="selectin", order_by="Permission.id"
    )

    @property
    def permission_names(self):
        return {permission.code_name for permission in self.permissions}


class Permission(database.Model):
    id = database.Column(database.Integer, primary_key=True)
    code_name = database.Column(database.String(60), unique=True, nullable=False)  # e.g. review.assign
    description = database.Column(database.String(255), nullable=False)


class User(database.Model):
    id = database.Column(database.Integer, primary_key=True)
    full_name = database.Column(database.String(120), nullable=False)
    email = database.Column(database.String(120), unique=True, nullable=False)
    password_hash = database.Column(database.String(255), nullable=False)
    reset_token = database.Column(database.String(64))
    reset_token_expiry = database.Column(database.DateTime)
    role_id = database.Column(
        database.Integer, database.ForeignKey("role.id", name="fk_user_role_id_role"), nullable=False, index=True
    )
    # Deactivated accounts cannot sign in; their documents and history are kept
    is_active = database.Column(
        database.Boolean, nullable=False, default=True, server_default=sqlalchemy.true()
    )
    role = database.relationship("Role", lazy="joined")
    documents = database.relationship("Document", backref="owner", lazy=True)


class Document(database.Model):
    id = database.Column(database.Integer, primary_key=True)
    user_id = database.Column(database.Integer, database.ForeignKey("user.id"), nullable=False)
    file_name = database.Column(database.String(255), nullable=False)
    stored_name = database.Column(database.String(255), nullable=False)
    file_format = database.Column(database.String(10))
    upload_date = database.Column(database.DateTime, default=lambda: datetime.now(timezone.utc))
    summary_text = database.Column(database.Text)
    status = database.Column(database.String(20), default="Processing")
    error_message = database.Column(database.Text)
    clauses = database.relationship("ExtractedClause", backref="document", lazy=True)
    analysis = database.relationship(
        "DocumentAnalysis", backref="document", uselist=False, cascade="all, delete-orphan"
    )


class ExtractedClause(database.Model):
    id = database.Column(database.Integer, primary_key=True)
    document_id = database.Column(database.Integer, database.ForeignKey("document.id"), nullable=False)
    clause_category = database.Column(database.String(80))
    extracted_text = database.Column(database.Text)
    risk_severity = database.Column(database.String(20))
    explanation = database.Column(database.Text)


class DocumentAnalysis(database.Model):
    """How each document was analysed: the options chosen, what actually ran,
    how long it took, and the extracted text. Its own table (one row per
    document) so no migration is needed. Documents uploaded before this
    existed simply have no row; the app treats that as 'unknown'."""
    id = database.Column(database.Integer, primary_key=True)
    document_id = database.Column(database.Integer, database.ForeignKey("document.id"), unique=True, nullable=False)
    summary_depth = database.Column(database.String(40))
    extraction_mode = database.Column(database.String(40))
    risk_level = database.Column(database.String(40))
    context_hint = database.Column(database.String(40))
    summary_method = database.Column(database.String(120))  # what really ran, incl. fallbacks
    truncated_for_ai = database.Column(database.Boolean, default=False)
    processing_seconds = database.Column(database.Float)
    extracted_character_count = database.Column(database.Integer)
    extracted_text = database.Column(database.Text)  # kept so restarts that wipe uploads/ lose nothing


class UserPreference(database.Model):
    """Per-user settings, one row per user. A user without a row gets the
    defaults (see get_user_preferences); the row is created on first save."""
    id = database.Column(database.Integer, primary_key=True)
    user_id = database.Column(database.Integer, database.ForeignKey("user.id"), unique=True, nullable=False)
    theme = database.Column(database.String(10), nullable=False, default="system")
    default_summary_depth = database.Column(database.String(40), nullable=False, default=SUMMARY_DEPTH_OPTIONS[0])
    default_extraction_mode = database.Column(database.String(40), nullable=False, default=EXTRACTION_MODE_OPTIONS[0])
    default_risk_level = database.Column(database.String(40), nullable=False, default=RISK_LEVEL_OPTIONS[0])
    default_context_hint = database.Column(database.String(40), nullable=False, default=CONTEXT_HINT_OPTIONS[0])
    sidebar_collapsed = database.Column(
        database.Boolean, nullable=False, default=False, server_default=sqlalchemy.false()
    )


class AuditLog(database.Model):
    """Append-only record of who did what, to which record, and when. The app
    has no route that edits or deletes entries. Never holds passwords, tokens
    or contract text: only IDs and short facts."""
    id = database.Column(database.Integer, primary_key=True)
    occurred_at = database.Column(
        database.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True
    )
    actor_user_id = database.Column(database.Integer, database.ForeignKey("user.id"), nullable=True, index=True)
    actor_role = database.Column(database.String(20))   # copied, so later role changes don't rewrite history
    action = database.Column(database.String(60), nullable=False, index=True)
    target_type = database.Column(database.String(40))
    target_id = database.Column(database.Integer)
    details = database.Column(database.Text)             # small JSON object
    ip_address = database.Column(database.String(45))
    actor = database.relationship("User", lazy="joined")

    @property
    def details_dict(self):
        try:
            return json.loads(self.details) if self.details else {}
        except ValueError:
            return {}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def is_allowed_file(file_name):
    """Return True only for extensions we can actually process."""
    return "." in file_name and file_name.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def extract_text_from_document(file_path, file_format):
    """
    Pull raw text out of a PDF, DOCX, or TXT file.
    Returns a single string of the document's text content.
    Raises on malformed/unreadable files so the caller can record a
    proper failure state instead of silently producing an empty summary.
    """
    extracted_text = ""

    if file_format == "pdf":
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    extracted_text += page_text + "\n"

    elif file_format == "docx":
        document_object = docx.Document(file_path)
        for paragraph in document_object.paragraphs:
            # Blank line between paragraphs: a Word paragraph is a separate
            # block, so headings and form fields never fuse with the next one.
            extracted_text += paragraph.text + "\n\n"

    elif file_format == "txt":
        with open(file_path, "r", encoding="utf-8", errors="ignore") as text_file:
            extracted_text = text_file.read()

    return extracted_text.strip()


@app.context_processor
def inject_brand():
    return {"brand_name": BRAND_NAME, "brand_tagline": BRAND_TAGLINE}


@app.context_processor
def inject_theme():
    """Signed-in pages get the theme saved in the database, so a change on one
    device applies on the others. Signed-out pages get an empty value, and the
    browser falls back to the last theme it saw."""
    preferences = g.get("preferences")
    return {"current_theme": preferences.theme if preferences is not None else ""}


def initials_for(full_name):
    parts = (full_name or "").split()
    if not parts:
        return "U"
    initials = parts[0][0] + parts[1][0] if len(parts) >= 2 else parts[0][:2]
    return initials.upper()


def avatar_hue_for(full_name):
    """A stable colour per person for the initials circle (same name, same
    colour). Only the hue varies; saturation and lightness are fixed in CSS so
    white text stays readable on every colour."""
    return sum(ord(character) for character in (full_name or "")) * 47 % 360


# Sidebar entries. Each appears only for roles holding its permission.
# (permission, key, label, Font Awesome icon, endpoint, URL fragment)
NAVIGATION_ITEMS = [
    ("document.view_own", "dashboard", "Dashboard", "fa-house", "dashboard", "dashboard"),
    ("document.upload", "analyzer", "Document Analyzer", "fa-file-contract", "dashboard", "analyzer"),
    ("document.view_own", "documents", "My Documents", "fa-folder-open", "dashboard", "documents"),
    ("review.view_queue", "review_queue", "Review Queue", "fa-list-check", "dashboard", None),
    ("stats.view", "overview", "Overview", "fa-chart-line", "dashboard", None),
    ("user.view_all", "users", "Users", "fa-users", "admin_users", None),
    ("user.change_role", "roles", "Roles & Permissions", "fa-shield-halved", "admin_roles", None),
    ("audit.view", "audit", "Audit Log", "fa-clipboard-list", "admin_audit", None),
]


@app.context_processor
def inject_account():
    """Everything the shared layout (sidebar, account menu, settings window)
    needs about the signed-in person. Empty on signed-out pages."""
    user = g.get("current_user")
    if user is None:
        return {"current_user": None, "navigation_items": [], "user_initials": "U"}
    navigation_items = [
        {
            "key": key, "label": label, "icon": icon,
            "url": url_for(endpoint) + (f"#{fragment}" if fragment else ""),
            # Dashboard tabs switch without reloading; ui needs to know which
            "tab_view": f"{fragment}-view" if fragment else None,
        }
        for permission_name, key, label, icon, endpoint, fragment in NAVIGATION_ITEMS
        if permission_name in g.permissions
    ]
    return {
        "current_user": user,
        "user_permissions": g.permissions,
        "navigation_items": navigation_items,
        "user_initials": initials_for(user.full_name),
        "avatar_hue": avatar_hue_for(user.full_name),
        "preferences": g.preferences,
        "sidebar_collapsed": bool(g.preferences and g.preferences.sidebar_collapsed),
        "settings_return_path": request.path,
        "theme_options": THEME_OPTIONS,
        "summary_depth_options": SUMMARY_DEPTH_OPTIONS,
        "extraction_mode_options": EXTRACTION_MODE_OPTIONS,
        "risk_level_options": RISK_LEVEL_OPTIONS,
        "context_hint_options": CONTEXT_HINT_OPTIONS,
    }


# ---------------------------------------------------------------------------
# Input validation (server side; the browser checks are only a convenience)
# ---------------------------------------------------------------------------
EMAIL_MAX_LENGTH = 254          # RFC 5321 limit for a full address
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128       # long enough for passphrases; stops multi-megabyte inputs
FULL_NAME_MAX_LENGTH = 100

# Pragmatic shape check: something@domain.tld, no spaces. (Fully validating an
# address is impossible without emailing it; this catches typos.)
EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}$")


def normalize_email(raw_email):
    return (raw_email or "").strip().lower()


def email_error(email):
    """Return an error message for an already-normalized email, or None."""
    if not email:
        return "Enter your email address."
    if len(email) > EMAIL_MAX_LENGTH or not EMAIL_PATTERN.match(email) or ".." in email:
        return "Enter a valid email address, like name@example.com."
    return None


def new_password_error(password, email=None):
    """Rules for any NEW password (register, reset, change). Existing
    passwords are never re-checked, so older accounts can still sign in."""
    if not password:
        return "Enter a password."
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters."
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"Password must be at most {PASSWORD_MAX_LENGTH} characters."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "Password must include at least one letter and one number."
    if email and password.lower() in (email, email.split("@")[0]):
        return "Password must not be your email address."
    return None


def full_name_error(full_name):
    if not full_name:
        return "Enter your full name."
    if len(full_name) < 2 or not re.search(r"[^\W\d_]", full_name):
        return "Enter your name using letters."
    if len(full_name) > FULL_NAME_MAX_LENGTH:
        return f"Name must be at most {FULL_NAME_MAX_LENGTH} characters."
    return None


def pick_option(submitted_value, allowed_options, fallback=None):
    """Return the submitted value only if it is one the UI actually offers."""
    if submitted_value in allowed_options:
        return submitted_value
    return fallback if fallback is not None else allowed_options[0]


def get_user_preferences(user_id, create_if_missing=False):
    """Return the user's saved preferences, or an unsaved object holding the
    defaults. Only writes a row when create_if_missing=True (on save)."""
    preferences = UserPreference.query.filter_by(user_id=user_id).first()
    if preferences is None:
        preferences = UserPreference(
            user_id=user_id,
            theme="system",
            default_summary_depth=SUMMARY_DEPTH_OPTIONS[0],
            default_extraction_mode=EXTRACTION_MODE_OPTIONS[0],
            default_risk_level=RISK_LEVEL_OPTIONS[0],
            default_context_hint=CONTEXT_HINT_OPTIONS[0],
        )
        if create_if_missing:
            database.session.add(preferences)
    return preferences


@app.template_filter("render_markdown")
def render_markdown_filter(markdown_text):
    """Turn the AI's Markdown (**bold**, bullet lists, headings) into HTML,
    then strip anything that is not on the safe-tag list."""
    if not markdown_text:
        return ""
    unsafe_html = markdown_renderer.markdown(markdown_text, extensions=["sane_lists"])
    safe_html = nh3.clean(unsafe_html, tags=ALLOWED_SUMMARY_TAGS, attributes={})
    return Markup(safe_html)


@app.template_filter("iso_utc")
def iso_utc_filter(datetime_value):
    """ISO-8601 timestamp with an explicit UTC marker, for <time datetime=...>.
    The browser converts it to the viewer's local time."""
    if datetime_value is None:
        return ""
    return as_utc(datetime_value).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_provision_view(document):
    """Obligations, prohibitions, rights, liabilities and key terms for one
    document, computed from the text stored at upload. Rule-based and fast
    (milliseconds), so it runs on demand and improvements to the rules apply
    to older documents too. Returns (sections, key_terms, is_available)."""
    stored_text = document.analysis.extracted_text if document.analysis else None
    if not stored_text:
        return [], {}, False
    found_provisions = extract_provisions(stored_text)
    sections = [
        # (plural label for headings, description, [(party, [sentences])])
        (PROVISION_PLURALS[provision_type], PROVISION_DESCRIPTIONS[provision_type], group_by_party(found_provisions[provision_type]))
        for provision_type in PROVISION_TYPES
    ]
    return sections, extract_key_terms(stored_text), True


DOWNLOAD_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9]{8,40}$")


def attach_download_token(response):
    """Downloads never leave the page, so the browser cannot tell when the file
    has arrived. The button sends ?download_token=<random>; echoing it back as a
    short-lived cookie with the file lets ui.js stop the spinner at that moment."""
    download_token = request.args.get("download_token", "")
    if DOWNLOAD_TOKEN_PATTERN.match(download_token):
        response.set_cookie("download_token", download_token, max_age=60, samesite="Lax",
                            secure=app.config["SESSION_COOKIE_SECURE"])
    return response


def original_file_exists(document):
    """Render's free tier wipes uploads/ on every restart, so the original
    file can be gone even though its analysis (in the database) is fine."""
    return bool(document.stored_name) and os.path.exists(
        os.path.join(app.config["UPLOAD_FOLDER"], document.stored_name)
    )


def as_utc(datetime_value):
    """Database drivers return naive datetimes; treat them as UTC so they can
    be compared with timezone-aware values without raising TypeError."""
    if datetime_value is None:
        return None
    if datetime_value.tzinfo is None:
        return datetime_value.replace(tzinfo=timezone.utc)
    return datetime_value


def mark_stale_documents_failed(user_id):
    """A document stays 'Processing' forever if the request that was analysing
    it died mid-way. Flip those to Failed so the dashboard counts stay honest
    and the user can see what happened."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_PROCESSING_MINUTES)
    stale_documents = Document.query.filter_by(user_id=user_id, status="Processing").all()
    changed = False
    for stale_document in stale_documents:
        if as_utc(stale_document.upload_date) < cutoff:
            stale_document.status = "Failed"
            stale_document.error_message = (
                "Processing was interrupted before it finished. Please upload the document again."
            )
            changed = True
    if changed:
        database.session.commit()


def login_required(view_func):
    """Redirect anonymous visitors to the login page before running a view.
    Used only for pages every role may open (settings, sign out); everything
    else uses permission_required."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if g.get("current_user") is None:
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# Access control (increment 7): who is signed in, and what they may do
# ---------------------------------------------------------------------------
@app.before_request
def load_signed_in_user():
    """Re-read the signed-in user, their role and its permissions from the
    database on every request, instead of trusting the session cookie. A role
    change or a deactivation therefore takes effect on the person's next click."""
    g.current_user = None
    g.permissions = frozenset()
    g.preferences = None
    if request.endpoint == "static" or "user_id" not in session:
        return None

    user = database.session.get(User, session["user_id"])
    if user is None:
        session.clear()
        return None
    if not user.is_active:
        session.clear()
        flash("This account has been deactivated. Contact the clinic administrator if you think this is a mistake.")
        return redirect(url_for("login"))

    g.current_user = user
    g.permissions = frozenset(user.role.permission_names)
    g.preferences = get_user_preferences(user.id)
    return None


def has_permission(user, permission_name):
    """True if the user's role holds this permission. Anything not granted is
    refused (deny by default): an unknown or misspelt name is never granted."""
    if user is None or not user.is_active:
        return False
    if user is g.get("current_user"):
        return permission_name in g.permissions
    return permission_name in user.role.permission_names


def deny_access(reason_details):
    """Refuse without revealing anything: the same 'not found' page as for a
    record that does not exist, so IDs and admin pages cannot be probed."""
    record_audit_event("access.denied", details={**reason_details, "path": request.path}, commit=True)
    abort(404)


def permission_required(permission_name):
    """Route decorator: the signed-in user's role must hold this permission."""
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            if g.get("current_user") is None:
                return redirect(url_for("login"))
            if not has_permission(g.current_user, permission_name):
                deny_access({"permission": permission_name})
            return view_func(*args, **kwargs)
        return wrapped
    return decorator


# What each kind of document action needs when the user owns the document
OWN_DOCUMENT_PERMISSION_BY_PURPOSE = {
    "view": "document.view_own",        # read the report, re-run the risk assessment
    "export": "document.export_own",    # PDF / Word report, original file
    "delete": "document.delete_own",
}


def can_access_document(user, document, purpose):
    """The one place that decides who may touch a document.

    Owners need the matching permission for the purpose. Reviewers will reach
    documents assigned to them through document.view_assigned (increment 8).
    Administrators never read contract text, so they have no path here."""
    if user is None or document is None:
        return False
    return document.user_id == user.id and has_permission(user, OWN_DOCUMENT_PERMISSION_BY_PURPOSE[purpose])


def get_document_or_404(document_id, purpose):
    document = database.session.get(Document, document_id)
    if not can_access_document(g.get("current_user"), document, purpose):
        deny_access({"document_id": document_id, "purpose": purpose})
    return document


def record_audit_event(action, target_type=None, target_id=None, details=None, actor=None, commit=False):
    """Add an audit entry.

    With commit=False (the default) it is saved by the caller's own commit, so
    the entry and the action are stored together or not at all. commit=True is
    for events with nothing else to save (sign-in, export); if that write fails
    the user's action still goes ahead and the failure goes to the server log."""
    actor = actor if actor is not None else g.get("current_user")
    entry = AuditLog(
        occurred_at=datetime.now(timezone.utc),
        actor_user_id=actor.id if actor is not None else None,
        actor_role=actor.role.name if actor is not None and actor.role is not None else None,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=json.dumps(details, ensure_ascii=False, default=str) if details else None,
        ip_address=(request.remote_addr or "")[:45] or None if has_request_context() else None,
    )
    database.session.add(entry)
    if commit:
        try:
            database.session.commit()
        except Exception:  # noqa: BLE001
            database.session.rollback()
            app.logger.exception("Could not write audit entry %s", action)
    return entry


def role_by_name(role_name):
    return Role.query.filter_by(name=role_name).one()


def count_active_admins(lock_rows=False):
    query = User.query.join(Role).filter(Role.name == ROLE_ADMIN, User.is_active.is_(True))
    if lock_rows:
        query = query.with_for_update(of=User)  # PostgreSQL: two admins can't demote each other at once
    return len(query.all())


# ---------------------------------------------------------------------------
# Authentication routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return redirect(url_for("dashboard") if "user_id" in session else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = normalize_email(request.form.get("email"))
        password = request.form.get("password", "")

        if email_error(email) or not password or len(password) > PASSWORD_MAX_LENGTH:
            # Same wording as a wrong password: never reveal which part failed
            record_audit_event("auth.login_failed", details={"email": email[:EMAIL_MAX_LENGTH]}, commit=True)
            flash("Invalid email or password.")
            return render_template("login.html", form_values={"email": email}), 400

        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password_hash, password):
            if not user.is_active:
                # Only said after a correct password, so it reveals nothing
                record_audit_event("auth.login_blocked", "user", user.id, commit=True)
                flash("This account has been deactivated. Contact the clinic administrator if you think this is a mistake.")
                return render_template("login.html", form_values={"email": email}), 403
            session.clear()
            # Ticked: stay signed in for REMEMBER_ME_DAYS. Unticked: signed out
            # when the browser closes. (Previously always permanent, so the
            # checkbox did nothing.)
            session.permanent = request.form.get("remember") == "on"
            session["user_id"] = user.id
            session["user_name"] = user.full_name
            record_audit_event("auth.login_succeeded", "user", user.id, actor=user, commit=True)
            return redirect(url_for("dashboard"))

        # The email typed is kept (never the password), to spot guessing
        record_audit_event("auth.login_failed", "user" if user else None, user.id if user else None,
                           details={"email": email}, commit=True)
        flash("Invalid email or password.")
        # Re-show the form with the email kept, so only the password is retyped
        return render_template("login.html", form_values={"email": email}), 401

    return render_template("login.html", form_values={})



@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = re.sub(r"\s+", " ", request.form.get("full_name", "")).strip()
        email = normalize_email(request.form.get("email"))
        password = request.form.get("password", "")

        field_errors = {
            "full_name": full_name_error(full_name),
            "email": email_error(email),
            "password": new_password_error(password, email),
        }
        if not field_errors["email"] and User.query.filter_by(email=email).first():
            field_errors["email"] = "An account with that email already exists. Sign in instead?"
        field_errors = {field: message for field, message in field_errors.items() if message}

        if field_errors:
            # Re-show the form with the name and email kept and each problem
            # shown under its field (the password is never echoed back)
            return render_template(
                "register.html",
                form_values={"full_name": full_name, "email": email},
                field_errors=field_errors,
            ), 400

        try:
            new_user = User(
                full_name=full_name,
                email=email,
                password_hash=generate_password_hash(password),
                # Always a Member: the role is never read from the form, so
                # nobody can register themselves as a Reviewer or Administrator
                role=role_by_name(ROLE_MEMBER),
                is_active=True,
            )
            database.session.add(new_user)
            database.session.flush()  # gives new_user an id for the audit entry
            record_audit_event("user.registered", "user", new_user.id, actor=new_user)
            database.session.commit()  # account and audit entry saved together

            session.clear()
            session.permanent = False  # new accounts get a browser-session login
            session["user_id"] = new_user.id
            session["user_name"] = new_user.full_name
            return redirect(url_for("dashboard"))

        except Exception as e:
            database.session.rollback()  # Prevents thread locking or stale transactions
            flash("An error occurred while creating your account. Please try again.")
            print(f"Registration DB Error: {e}")  # Logs error directly to Render console
            return render_template("register.html", form_values={"full_name": full_name, "email": email},
                                   field_errors={}), 500

    return render_template("register.html", form_values={}, field_errors={})
@app.route("/logout")
def logout():
    if g.get("current_user") is not None:
        record_audit_event("auth.logout", "user", g.current_user.id, commit=True)
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    """Everyone lands on /dashboard after signing in; what they see depends on
    their role's permissions."""
    if has_permission(g.current_user, "document.view_own"):
        return render_member_dashboard()
    if has_permission(g.current_user, "review.view_queue"):
        return render_template("reviewer_home.html", active_view="review_queue")
    if has_permission(g.current_user, "stats.view"):
        return render_admin_overview()
    deny_access({"permission": "any home page"})


def render_member_dashboard():
    mark_stale_documents_failed(session["user_id"])

    user_documents = (
        Document.query.filter_by(user_id=session["user_id"])
        .order_by(Document.upload_date.desc())
        .all()
    )

    total_documents = len(user_documents)
    high_risk_count = (
        ExtractedClause.query.join(Document)
        .filter(Document.user_id == session["user_id"])
        .filter(ExtractedClause.risk_severity == "High")
        .count()
    )

    # Risk profile chart: clause counts per severity, and per category
    severity_rows = (
        database.session.query(ExtractedClause.risk_severity, database.func.count(ExtractedClause.id))
        .join(Document).filter(Document.user_id == session["user_id"])
        .group_by(ExtractedClause.risk_severity).all()
    )
    severity_counts = {severity: 0 for severity in ("High", "Medium", "Low", "Review")}
    for severity, clause_count in severity_rows:
        severity_counts[severity if severity in severity_counts else "Review"] += clause_count
    category_rows = (
        database.session.query(ExtractedClause.clause_category, database.func.count(ExtractedClause.id))
        .join(Document).filter(Document.user_id == session["user_id"])
        .filter(ExtractedClause.risk_severity == "High")
        .group_by(ExtractedClause.clause_category)
        .order_by(database.func.count(ExtractedClause.id).desc()).limit(5).all()
    )

    start_of_today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    processed_today = sum(
        1 for document in user_documents
        if as_utc(document.upload_date) >= start_of_today
        and document.status == "Processed"
    )
    pending_count = sum(1 for document in user_documents if document.status == "Processing")
    failed_count = sum(1 for document in user_documents if document.status == "Failed")

    # Evidence for the 15-second performance requirement
    measured_seconds = [
        document.analysis.processing_seconds for document in user_documents
        if document.analysis and document.analysis.processing_seconds is not None
        and document.status == "Processed"
    ]
    average_processing_seconds = (sum(measured_seconds) / len(measured_seconds)) if measured_seconds else None
    within_target_percent = (
        round(100 * sum(1 for seconds in measured_seconds if seconds <= PERFORMANCE_TARGET_SECONDS) / len(measured_seconds))
        if measured_seconds else None
    )

    current_user = database.session.get(User, session["user_id"])

    return render_template(
        "dashboard.html",
        user_name=session.get("user_name"),
        user_email=current_user.email if current_user else "",
        documents=user_documents,
        total_documents=total_documents,
        high_risk_count=high_risk_count,
        severity_counts=severity_counts,
        total_clause_count=sum(severity_counts.values()),
        top_high_risk_categories=category_rows,
        processed_today=processed_today,
        pending_count=pending_count,
        failed_count=failed_count,
        average_processing_seconds=average_processing_seconds,
        within_target_percent=within_target_percent,
        measured_document_count=len(measured_seconds),
        performance_target_seconds=PERFORMANCE_TARGET_SECONDS,
        active_view="dashboard",
        preferences=get_user_preferences(session["user_id"]),
        theme_options=THEME_OPTIONS,
        summary_depth_options=SUMMARY_DEPTH_OPTIONS,
        extraction_mode_options=EXTRACTION_MODE_OPTIONS,
        risk_level_options=RISK_LEVEL_OPTIONS,
        context_hint_options=CONTEXT_HINT_OPTIONS,
        max_upload_megabytes=MAX_UPLOAD_MEGABYTES,
    )


@app.route("/upload", methods=["POST"])
@permission_required("document.upload")
def upload_document():
    uploaded_file = request.files.get("document")

    if not uploaded_file or uploaded_file.filename == "":
        flash("No file was selected.")
        return redirect(url_for("dashboard") + "#analyzer")

    if not is_allowed_file(uploaded_file.filename):
        flash("Unsupported file type. Upload a PDF, DOCX, or TXT file.")
        return redirect(url_for("dashboard") + "#analyzer")

    original_name = secure_filename(uploaded_file.filename)
    if not original_name:
        flash("Invalid file name.")
        return redirect(url_for("dashboard") + "#analyzer")

    file_format = original_name.rsplit(".", 1)[1].lower()
    stored_name = f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{original_name}"
    file_path = os.path.join(app.config["UPLOAD_FOLDER"], stored_name)
    uploaded_file.save(file_path)

    new_document = Document(
        user_id=session["user_id"],
        file_name=original_name,
        stored_name=stored_name,
        file_format=file_format.upper(),
        status="Processing",
    )
    database.session.add(new_document)
    database.session.commit()

    # Unknown values (tampered form, stale browser tab) fall back to defaults
    # instead of being passed straight into the AI prompt.
    analysis_options = {
        "summary_depth": pick_option(request.form.get("summary_depth"), SUMMARY_DEPTH_OPTIONS),
        "extraction_mode": pick_option(request.form.get("extraction_mode"), EXTRACTION_MODE_OPTIONS),
        "risk_level": pick_option(request.form.get("risk_level"), RISK_LEVEL_OPTIONS),
        "jurisdiction": pick_option(request.form.get("jurisdiction"), CONTEXT_HINT_OPTIONS),
    }

    analysis_record = DocumentAnalysis(
        summary_depth=analysis_options["summary_depth"],
        extraction_mode=analysis_options["extraction_mode"],
        risk_level=analysis_options["risk_level"],
        context_hint=analysis_options["jurisdiction"],
    )
    processing_started_at = time.perf_counter()

    try:
        document_text = extract_text_from_document(file_path, file_format)

        if not document_text:
            raise ValueError(
                "No extractable text was found in this file. If it is a scanned PDF "
                "(a photo of pages), it needs OCR first."
            )

        analysis_result = run_analysis(document_text, analysis_options)
        detected_clauses = analysis_result["clauses"]

        analysis_record.summary_method = analysis_result["summary_method"]
        analysis_record.truncated_for_ai = analysis_result["truncated_for_ai"]
        analysis_record.extracted_character_count = len(document_text)
        analysis_record.extracted_text = document_text
        analysis_record.processing_seconds = round(time.perf_counter() - processing_started_at, 2)
        new_document.analysis = analysis_record

        new_document.summary_text = analysis_result["summary"]
        new_document.status = "Processed"

        for clause in detected_clauses:
            database.session.add(
                ExtractedClause(
                    document_id=new_document.id,
                    clause_category=clause["category"],
                    extracted_text=clause["text"],
                    risk_severity=clause.get("severity", "Review"),
                    explanation=clause.get("explanation", ""),
                )
            )
        record_audit_event("document.uploaded", "document", new_document.id, details={
            "file_name": original_name, "status": "Processed", "mode": analysis_options["extraction_mode"]})
        database.session.commit()
        assessment_failed = any(
            clause.get("severity") == "Review" for clause in detected_clauses
        )
        if assessment_failed:
            flash(
                f"'{original_name}' was summarized, but the AI risk assessment is temporarily "
                "unavailable. Use 'Re-run risk assessment' below in a moment."
            )
        else:
            flash(f"'{original_name}' processed successfully.", "success")

    except Exception as exc:
        database.session.rollback()
        new_document.status = "Failed"
        new_document.error_message = str(exc)
        analysis_record.processing_seconds = round(time.perf_counter() - processing_started_at, 2)
        new_document.analysis = analysis_record
        database.session.add(new_document)
        record_audit_event("document.uploaded", "document", new_document.id, details={
            "file_name": original_name, "status": "Failed", "mode": analysis_options["extraction_mode"]})
        database.session.commit()
        flash(f"'{original_name}' could not be processed. The reason is shown below.")

    # Go straight to this document's results (or its failure reason), instead
    # of the dashboard where the user had to find it in Recent Activity.
    return redirect(url_for("view_document", document_id=new_document.id))


@app.route("/document/<int:document_id>")
@permission_required("document.view_own")
def view_document(document_id):
    document = get_document_or_404(document_id, "view")

    # Apply this document's Risk Detection Level when displaying. Every clause
    # is stored; rows outside the level are hidden behind "Show all".
    chosen_risk_level = document.analysis.risk_level if document.analysis else "All Clauses"
    visible_severities = VISIBLE_SEVERITIES_BY_RISK_LEVEL.get(
        chosen_risk_level, VISIBLE_SEVERITIES_BY_RISK_LEVEL["All Clauses"]
    )
    hidden_clause_count = sum(
        1 for clause in document.clauses if clause.risk_severity not in visible_severities
    )

    provision_sections, key_terms, provisions_available = build_provision_view(document)

    # Any file can be uploaded, but the clause and provision rules are built for
    # contracts; warn when the text does not look like one.
    stored_text = document.analysis.extracted_text if document.analysis else None
    looks_like_contract, contract_signals_met = contract_signals(stored_text) if stored_text else (True, [])

    return render_template(
        "document_detail.html",
        document=document,
        user_name=session.get("user_name"),
        active_view="documents",
        chosen_risk_level=chosen_risk_level,
        visible_severities=visible_severities,
        hidden_clause_count=hidden_clause_count,
        performance_target_seconds=PERFORMANCE_TARGET_SECONDS,
        provision_sections=provision_sections,
        key_terms=key_terms,
        provisions_available=provisions_available,
        original_file_available=original_file_exists(document),
        looks_like_contract=looks_like_contract,
        contract_signals_met=contract_signals_met,
    )


EXPORT_FORMATS = {
    "pdf": ("application/pdf", "pdf"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
}


@app.route("/document/<int:document_id>/export/<export_format>")
@permission_required("document.export_own")
def export_report(document_id, export_format):
    """Download the analysis as a PDF or Word report (proposal requirement 4.2)."""
    if export_format not in EXPORT_FORMATS:
        abort(404)
    document = get_document_or_404(document_id, "export")
    if document.status != "Processed":
        flash("Only successfully processed documents can be exported.")
        return redirect(url_for("view_document", document_id=document.id))

    provision_sections, key_terms, _ = build_provision_view(document)
    chosen_risk_level = document.analysis.risk_level if document.analysis else None
    risk_level_note = (
        f"All {len(document.clauses)} flagged clauses are included, although '{chosen_risk_level}' was chosen for on-screen display."
        if chosen_risk_level and chosen_risk_level != "All Clauses" else None
    )
    content = build_report_content(
        document, render_markdown_filter(document.summary_text), provision_sections, key_terms, risk_level_note,
        brand_name=BRAND_NAME,
    )

    try:
        file_bytes = export_pdf(content) if export_format == "pdf" else export_docx(content)
    except Exception as error:  # noqa: BLE001
        app.logger.exception("Report export failed for document %s: %s", document.id, error)
        flash("The report could not be generated. Please try again, or try the other format.")
        return redirect(url_for("view_document", document_id=document.id))

    mime_type, extension = EXPORT_FORMATS[export_format]
    record_audit_event("document.exported", "document", document.id,
                       details={"file_name": document.file_name, "format": export_format}, commit=True)
    return attach_download_token(send_file(
        io.BytesIO(file_bytes),
        mimetype=mime_type,
        as_attachment=True,
        download_name=report_filename(document.file_name, extension),
    ))


def settings_return_url(tab=None):
    """Settings are a window that opens over any page, so after saving go back
    to that page. Only a path on this site is accepted (never another site),
    and on an error the window reopens on the same tab."""
    return_path = request.form.get("return_to", "").split("#")[0]
    if not return_path.startswith("/") or return_path.startswith("//") or "\\" in return_path:
        return_path = url_for("dashboard")
    return return_path + (f"#settings-{tab}" if tab else "")


@app.route("/settings/profile", methods=["POST"])
@login_required
def update_profile():
    full_name = re.sub(r"\s+", " ", request.form.get("full_name", "")).strip()

    name_problem = full_name_error(full_name)
    if name_problem:
        flash(name_problem)
        return redirect(settings_return_url("profile"))

    user = g.current_user
    if user.full_name != full_name:
        user.full_name = full_name
        record_audit_event("user.profile_updated", "user", user.id)
        database.session.commit()
    session["user_name"] = full_name

    flash("Profile updated successfully.", "success")
    return redirect(settings_return_url())


@app.route("/settings/password", methods=["POST"])
@login_required
def change_password():
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    user = g.current_user

    if not check_password_hash(user.password_hash, current_password):
        flash("Current password is incorrect.")
        return redirect(settings_return_url("security"))

    password_problem = new_password_error(new_password, user.email)
    if password_problem:
        flash(password_problem)
        return redirect(settings_return_url("security"))

    if new_password == current_password:
        flash("The new password must be different from the current one.")
        return redirect(settings_return_url("security"))

    if new_password != confirm_password:
        flash("New password and confirmation do not match.")
        return redirect(settings_return_url("security"))

    user.password_hash = generate_password_hash(new_password)
    record_audit_event("auth.password_changed", "user", user.id)
    database.session.commit()

    flash("Password changed successfully.", "success")
    return redirect(settings_return_url())


@app.route("/settings/preferences", methods=["POST"])
@permission_required("document.upload")
def update_preferences():
    """Save the default analysis options (the Analysis defaults tab). Only
    roles that upload documents have analysis defaults."""
    preferences = get_user_preferences(g.current_user.id, create_if_missing=True)
    preferences.theme = pick_option(request.form.get("theme"), THEME_OPTIONS, preferences.theme)
    preferences.default_summary_depth = pick_option(
        request.form.get("default_summary_depth"), SUMMARY_DEPTH_OPTIONS, preferences.default_summary_depth)
    preferences.default_extraction_mode = pick_option(
        request.form.get("default_extraction_mode"), EXTRACTION_MODE_OPTIONS, preferences.default_extraction_mode)
    preferences.default_risk_level = pick_option(
        request.form.get("default_risk_level"), RISK_LEVEL_OPTIONS, preferences.default_risk_level)
    preferences.default_context_hint = pick_option(
        request.form.get("default_context_hint"), CONTEXT_HINT_OPTIONS, preferences.default_context_hint)
    database.session.commit()

    flash("Preferences saved.", "success")
    return redirect(settings_return_url())


@app.route("/settings/theme", methods=["POST"])
@login_required
def update_theme():
    """Used by the sun/moon button and the Appearance tab. Called with fetch(),
    so it returns JSON instead of redirecting. CSRF token arrives in X-CSRFToken."""
    request_data = request.get_json(silent=True) or {}
    requested_theme = request_data.get("theme")
    if requested_theme not in THEME_OPTIONS:
        return jsonify({"error": "Unknown theme"}), 400

    preferences = get_user_preferences(g.current_user.id, create_if_missing=True)
    preferences.theme = requested_theme
    database.session.commit()
    return jsonify({"theme": requested_theme})


@app.route("/settings/sidebar", methods=["POST"])
@login_required
def update_sidebar_state():
    """Remembers whether the sidebar is collapsed, per person, so it follows
    them across devices. Called with fetch(); returns JSON."""
    request_data = request.get_json(silent=True) or {}
    collapsed = request_data.get("collapsed")
    if not isinstance(collapsed, bool):
        return jsonify({"error": "collapsed must be true or false"}), 400

    preferences = get_user_preferences(g.current_user.id, create_if_missing=True)
    preferences.sidebar_collapsed = collapsed
    database.session.commit()
    return jsonify({"collapsed": collapsed})


@app.errorhandler(413)
def upload_too_large(error):
    """Flask's default for an oversized upload is a bare error page. Send the
    user back to the Analyzer with a readable message instead."""
    flash(f"That file is larger than {MAX_UPLOAD_MEGABYTES} MB. Please upload a smaller file.")
    return redirect(url_for("dashboard") + "#analyzer")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = normalize_email(request.form.get("email"))

        format_problem = email_error(email)
        if format_problem:
            # A format problem says nothing about whether an account exists
            flash(format_problem)
            return render_template("forgot_password.html", form_values={"email": email}), 400

        user = User.query.filter_by(email=email).first()

        generic_message = "If an account with that email exists, a reset link has been generated."

        if user:
            token = secrets.token_urlsafe(32)
            user.reset_token = token
            user.reset_token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
            record_audit_event("auth.reset_requested", "user", user.id)
            database.session.commit()

            reset_link = url_for("reset_password", token=token, _external=True)

            # Without an email service the link goes to the server log only.
            # Showing it on screen would let anyone reset anyone's password,
            # so that is restricted to local development (SHOW_RESET_LINK=1).
            print(f"\n[PASSWORD RESET] Link for {email}: {reset_link}\n", flush=True)

            flash(generic_message)
            if SHOW_RESET_LINK_ON_SCREEN:
                flash(f"[DEV MODE] Reset link: {reset_link}", "persistent")
        else:
            flash(generic_message)

        return redirect(url_for("forgot_password"))

    return render_template("forgot_password.html", form_values={})


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    user = User.query.filter_by(reset_token=token).first()

    if not user or not user.reset_token_expiry or as_utc(user.reset_token_expiry) < datetime.now(timezone.utc):
        flash("This reset link is invalid or has expired.")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        password_problem = new_password_error(new_password, user.email)
        if password_problem:
            flash(password_problem)
            return redirect(url_for("reset_password", token=token))

        if new_password != confirm_password:
            flash("Passwords do not match.")
            return redirect(url_for("reset_password", token=token))

        user.password_hash = generate_password_hash(new_password)
        user.reset_token = None
        user.reset_token_expiry = None
        record_audit_event("auth.reset_completed", "user", user.id, actor=user)
        database.session.commit()

        flash("Password reset successfully. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)


@app.route("/document/<int:document_id>/delete", methods=["POST"])
@permission_required("document.delete_own")
def delete_document(document_id):
    document = get_document_or_404(document_id, "delete")

    file_path = os.path.join(app.config["UPLOAD_FOLDER"], document.stored_name)
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except OSError:
        pass

    record_audit_event("document.deleted", "document", document.id, details={"file_name": document.file_name})
    ExtractedClause.query.filter_by(document_id=document.id).delete()
    database.session.delete(document)
    database.session.commit()

    flash(f"'{document.file_name}' was deleted.", "success")
    return redirect(url_for("dashboard") + "#documents")


@app.route("/document/<int:document_id>/download")
@permission_required("document.export_own")
def download_document(document_id):
    document = get_document_or_404(document_id, "export")

    if not original_file_exists(document):
        flash(
            "The original file is no longer on the server (the free hosting tier clears "
            "uploaded files when it restarts). The analysis is unaffected; you can export it as a report."
        )
        return redirect(url_for("view_document", document_id=document.id))

    record_audit_event("document.downloaded", "document", document.id,
                       details={"file_name": document.file_name}, commit=True)
    return attach_download_token(send_from_directory(
        app.config["UPLOAD_FOLDER"],
        document.stored_name,
        as_attachment=True,
        download_name=document.file_name,
    ))


@app.route("/document/<int:document_id>/reassess", methods=["POST"])
@permission_required("document.view_own")
def reassess_document(document_id):
    """Re-run only the Gemini risk assessment for clauses that came back as
    'Review' (API busy, timeout, no key at the time). The summary and the
    clauses themselves are kept; no re-upload needed."""
    document = get_document_or_404(document_id, "view")

    pending_clauses = [clause for clause in document.clauses if clause.risk_severity == "Review"]
    if not pending_clauses:
        flash("All clauses already have a risk assessment.", "success")
        return redirect(url_for("view_document", document_id=document.id))

    clause_payload = [
        {"category": clause.clause_category, "text": clause.extracted_text}
        for clause in pending_clauses
    ]
    stored_context_hint = (
        document.analysis.context_hint if document.analysis and document.analysis.context_hint
        else CONTEXT_HINT_OPTIONS[0]
    )
    assessed_payload = assess_risk_with_llm(clause_payload, stored_context_hint)

    newly_assessed_count = 0
    for clause, assessed in zip(pending_clauses, assessed_payload):
        clause.risk_severity = assessed.get("severity", "Review")
        clause.explanation = assessed.get("explanation", "")
        if clause.risk_severity != "Review":
            newly_assessed_count += 1
    record_audit_event("document.reassessed", "document", document.id, details={
        "file_name": document.file_name, "clauses_rated": newly_assessed_count, "clauses_pending": len(pending_clauses)})
    database.session.commit()

    if newly_assessed_count == len(pending_clauses):
        flash("Risk assessment completed.", "success")
    else:
        flash("The AI service is still busy. Please try again in a minute.")
    return redirect(url_for("view_document", document_id=document.id))


# ---------------------------------------------------------------------------
# Administrator pages (increment 7)
# ---------------------------------------------------------------------------
# Plain-language names for audit actions, used on the Audit Log page and its filter
AUDIT_ACTION_LABELS = {
    "auth.login_succeeded": "Signed in",
    "auth.login_failed": "Sign-in failed",
    "auth.login_blocked": "Sign-in refused (account deactivated)",
    "auth.logout": "Signed out",
    "auth.password_changed": "Password changed",
    "auth.reset_requested": "Password reset requested",
    "auth.reset_completed": "Password reset completed",
    "user.registered": "Account registered",
    "user.profile_updated": "Profile updated",
    "user.role_changed": "Role changed",
    "user.deactivated": "Account deactivated",
    "user.reactivated": "Account restored",
    "document.uploaded": "Document uploaded",
    "document.exported": "Report exported",
    "document.downloaded": "Original file downloaded",
    "document.deleted": "Document deleted",
    "document.reassessed": "Risk assessment re-run",
    "access.denied": "Access refused",
    "audit.pruned": "Old audit entries removed",
}


@app.template_filter("audit_action_label")
def audit_action_label_filter(action):
    return AUDIT_ACTION_LABELS.get(action, action)


def roles_in_display_order():
    roles_by_name = {role.name: role for role in Role.query.all()}
    return [roles_by_name[name] for name in ROLE_ORDER if name in roles_by_name]


def like_pattern(search_text):
    """Case-insensitive 'contains' pattern, with % and _ typed by the user
    treated as ordinary characters."""
    escaped_text = search_text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped_text}%"


def naive_utc(datetime_value):
    """Dates are stored as UTC without a timezone marker; compare like with like."""
    return as_utc(datetime_value).replace(tzinfo=None)


def admin_users_return_url():
    """Back to the Users list the admin was on (same search and page)."""
    return_path = request.form.get("return_to", "")
    if not return_path.startswith(url_for("admin_users")):
        return_path = url_for("admin_users")
    return return_path


def render_admin_overview():
    active_counts_by_role = dict(
        database.session.query(Role.name, database.func.count(User.id))
        .outerjoin(User, (User.role_id == Role.id) & User.is_active.is_(True))
        .group_by(Role.name).all()
    )
    week_start = naive_utc(datetime.now(timezone.utc) - timedelta(days=7))
    recent_events = (
        AuditLog.query.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc()).limit(8).all()
        if has_permission(g.current_user, "audit.view") else []
    )
    return render_template(
        "admin_overview.html",
        active_view="overview",
        roles=roles_in_display_order(),
        active_counts_by_role=active_counts_by_role,
        deactivated_count=User.query.filter(User.is_active.is_(False)).count(),
        # Counts only: Administrators never see document names or contents here
        documents_total=Document.query.count(),
        documents_this_week=Document.query.filter(Document.upload_date >= week_start).count(),
        recent_events=recent_events,
    )


@app.route("/admin/users")
@permission_required("user.view_all")
def admin_users():
    search_text = request.args.get("q", "").strip()[:120]
    role_filter = request.args.get("role", "")
    page_number = request.args.get("page", 1, type=int)

    users_query = User.query.join(Role)
    if search_text:
        pattern = like_pattern(search_text)
        users_query = users_query.filter(database.or_(
            database.func.lower(User.full_name).like(pattern, escape="\\"),
            User.email.like(pattern, escape="\\"),
        ))
    if role_filter in ROLE_ORDER:
        users_query = users_query.filter(Role.name == role_filter)
    users_page = users_query.order_by(database.func.lower(User.full_name), User.id).paginate(
        page=page_number, per_page=USERS_PER_PAGE, error_out=False)

    return render_template(
        "admin_users.html",
        active_view="users",
        users_page=users_page,
        roles=roles_in_display_order(),
        search_text=search_text,
        role_filter=role_filter if role_filter in ROLE_ORDER else "",
        can_change_roles=has_permission(g.current_user, "user.change_role"),
        can_deactivate=has_permission(g.current_user, "user.deactivate"),
        return_to=request.full_path,
    )


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@permission_required("user.change_role")
def admin_change_role(user_id):
    target_user = database.session.get(User, user_id)
    if target_user is None:
        abort(404)
    new_role_name = request.form.get("role", "")

    if new_role_name not in ROLE_ORDER:
        flash("Choose a valid role.")
        return redirect(admin_users_return_url())
    if target_user.id == g.current_user.id:
        flash("You cannot change your own role. Ask another Administrator to do it.")
        return redirect(admin_users_return_url())

    old_role_name = target_user.role.name
    if new_role_name == old_role_name:
        flash(f"{target_user.full_name} is already a {target_user.role.label}.", "success")
        return redirect(admin_users_return_url())
    if old_role_name == ROLE_ADMIN and target_user.is_active and count_active_admins(lock_rows=True) <= 1:
        database.session.rollback()
        flash("This is the last active Administrator. Make someone else an Administrator first.")
        return redirect(admin_users_return_url())

    new_role = role_by_name(new_role_name)
    target_user.role = new_role
    record_audit_event("user.role_changed", "user", target_user.id, details={
        "name": target_user.full_name, "email": target_user.email, "from": old_role_name, "to": new_role_name})
    database.session.commit()
    flash(f"{target_user.full_name} is now a {new_role.label}. It applies from their next click.", "success")
    return redirect(admin_users_return_url())


@app.route("/admin/users/<int:user_id>/status", methods=["POST"])
@permission_required("user.deactivate")
def admin_change_status(user_id):
    """Deactivate or restore an account. Accounts are never deleted, so their
    documents and audit history stay intact."""
    target_user = database.session.get(User, user_id)
    if target_user is None:
        abort(404)
    requested_action = request.form.get("action", "")

    if requested_action == "deactivate":
        if target_user.id == g.current_user.id:
            flash("You cannot deactivate your own account.")
            return redirect(admin_users_return_url())
        if not target_user.is_active:
            return redirect(admin_users_return_url())
        if target_user.role.name == ROLE_ADMIN and count_active_admins(lock_rows=True) <= 1:
            database.session.rollback()
            flash("This is the last active Administrator and cannot be deactivated.")
            return redirect(admin_users_return_url())
        target_user.is_active = False
        record_audit_event("user.deactivated", "user", target_user.id,
                           details={"name": target_user.full_name, "email": target_user.email})
        database.session.commit()
        flash(f"{target_user.full_name}'s account is deactivated. They are signed out at their next click.", "success")
    elif requested_action == "restore":
        if target_user.is_active:
            return redirect(admin_users_return_url())
        target_user.is_active = True
        record_audit_event("user.reactivated", "user", target_user.id,
                           details={"name": target_user.full_name, "email": target_user.email})
        database.session.commit()
        flash(f"{target_user.full_name}'s account is restored.", "success")
    else:
        flash("Unknown action.")
    return redirect(admin_users_return_url())


@app.route("/admin/roles")
@permission_required("user.change_role")
def admin_roles():
    """The live role-permission matrix, read from the database. Read-only."""
    roles = roles_in_display_order()
    active_counts_by_role = dict(
        database.session.query(User.role_id, database.func.count(User.id))
        .filter(User.is_active.is_(True)).group_by(User.role_id).all()
    )
    return render_template(
        "admin_roles.html",
        active_view="roles",
        roles=roles,
        permissions=Permission.query.order_by(Permission.id).all(),
        permission_names_by_role={role.id: role.permission_names for role in roles},
        active_counts_by_role=active_counts_by_role,
    )


@app.route("/admin/audit")
@permission_required("audit.view")
def admin_audit():
    person_filter = request.args.get("person", "").strip()[:120]
    action_filter = request.args.get("action", "")
    date_from_text = request.args.get("from", "")
    date_to_text = request.args.get("to", "")
    page_number = request.args.get("page", 1, type=int)

    entries_query = AuditLog.query
    if person_filter:
        pattern = like_pattern(person_filter)
        matching_user_ids = [user_id for (user_id,) in database.session.query(User.id).filter(database.or_(
            database.func.lower(User.full_name).like(pattern, escape="\\"),
            User.email.like(pattern, escape="\\"),
        ))]
        # Also finds failed sign-ins, which record the email typed but no person
        entries_query = entries_query.filter(database.or_(
            AuditLog.actor_user_id.in_(matching_user_ids),
            database.and_(AuditLog.target_type == "user", AuditLog.target_id.in_(matching_user_ids)),
            database.func.lower(AuditLog.details).like(pattern, escape="\\"),
        ))
    if action_filter in AUDIT_ACTION_LABELS:
        entries_query = entries_query.filter(AuditLog.action == action_filter)

    filter_problems = []
    try:
        if date_from_text:
            entries_query = entries_query.filter(
                AuditLog.occurred_at >= datetime.strptime(date_from_text, "%Y-%m-%d"))
        if date_to_text:
            entries_query = entries_query.filter(
                AuditLog.occurred_at < datetime.strptime(date_to_text, "%Y-%m-%d") + timedelta(days=1))
    except ValueError:
        filter_problems.append("Dates must look like 2026-10-14.")

    entries_page = entries_query.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc()).paginate(
        page=page_number, per_page=AUDIT_ENTRIES_PER_PAGE, error_out=False)
    return render_template(
        "admin_audit.html",
        active_view="audit",
        entries_page=entries_page,
        action_labels=AUDIT_ACTION_LABELS,
        person_filter=person_filter,
        action_filter=action_filter if action_filter in AUDIT_ACTION_LABELS else "",
        date_from_text=date_from_text,
        date_to_text=date_to_text,
        filter_problems=filter_problems,
    )


@app.errorhandler(404)
def page_not_found(error):
    """One plain page for anything missing or not allowed (see deny_access)."""
    return render_template("not_found.html"), 404


@app.route("/favicon.ico")
def favicon():
    """Browsers request /favicon.ico on their own; serving it stops a 404
    on every visit (previously visible in the Render logs)."""
    return send_from_directory(
        os.path.join(app.static_folder, "brand"), "favicon.ico",
        mimetype="image/vnd.microsoft.icon", max_age=86400,
    )


@app.route("/health")
def health_check():
    """Simple endpoint to confirm the server is awake and responding.
    Useful for warming up Render's free tier before a demo, and for
    Render's own uptime monitoring."""
    return "OK", 200


from analysis import run_analysis, assess_risk_with_llm  # noqa: E402
from provisions import (  # noqa: E402
    PROVISION_TYPES, PROVISION_PLURALS, PROVISION_DESCRIPTIONS, extract_provisions, extract_key_terms, group_by_party,
    contract_signals,
)
from report_export import build_report_content, export_docx, export_pdf, report_filename  # noqa: E402


# ---------------------------------------------------------------------------
# Start-up: database schema and the first Administrator
# ---------------------------------------------------------------------------
# Arbitrary number that names Chambua's migration lock in PostgreSQL
MIGRATION_LOCK_KEY = 72310007


def prepare_database():
    """Bring the database up to the latest migration (replaces the old
    database.create_all()). Safe on every start-up: already-applied
    migrations are skipped. A database from before increment 7 has no
    migration history; the baseline migration recognises its existing tables
    and leaves them as they are, then the roles migration runs.

    On PostgreSQL a lock makes sure only one process migrates at a time, if
    the server ever runs several workers."""
    with app.app_context():
        engine = database.engine
        if engine.dialect.name != "postgresql":
            upgrade_database_schema()
            return
        with engine.connect() as lock_connection:
            lock_connection.execute(sqlalchemy.text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
            try:
                upgrade_database_schema()
            finally:
                lock_connection.execute(sqlalchemy.text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY})
                lock_connection.commit()


def promote_to_admin(user, source):
    old_role_name = user.role.name
    user.role = role_by_name(ROLE_ADMIN)
    record_audit_event("user.role_changed", "user", user.id, details={
        "name": user.full_name, "email": user.email, "from": old_role_name, "to": ROLE_ADMIN, "source": source})
    database.session.commit()


def promote_initial_admin_from_environment():
    """INITIAL_ADMIN_EMAIL: on a server with no shell (Render's free tier), this
    is how the first Administrator is made. It does nothing once any
    Administrator exists, so leaving it set cannot be abused later."""
    if not INITIAL_ADMIN_EMAIL:
        return
    with app.app_context():
        admin_role = role_by_name(ROLE_ADMIN)
        if User.query.filter_by(role_id=admin_role.id).first() is not None:
            return
        user = User.query.filter_by(email=INITIAL_ADMIN_EMAIL).first()
        if user is None or not user.is_active:
            app.logger.warning("INITIAL_ADMIN_EMAIL is set, but no active account uses %s. "
                               "Register that account, then restart.", INITIAL_ADMIN_EMAIL)
            return
        promote_to_admin(user, "INITIAL_ADMIN_EMAIL")
        app.logger.warning("%s is now the first Administrator. You can remove INITIAL_ADMIN_EMAIL.", user.email)


@app.cli.command("create-admin")
@click.argument("email")
def create_admin_command(email):
    """Make an existing account an Administrator: flask --app app create-admin EMAIL"""
    user = User.query.filter_by(email=normalize_email(email)).first()
    if user is None:
        raise click.ClickException(f"No account uses {email}. Register it first.")
    if user.role.name == ROLE_ADMIN:
        click.echo(f"{user.email} is already an Administrator.")
        return
    promote_to_admin(user, "flask create-admin")
    click.echo(f"{user.email} is now an Administrator.")


@app.cli.command("prune-audit-log")
@click.option("--months", default=24, show_default=True, help="Keep entries newer than this many months.")
def prune_audit_log_command(months):
    """Delete audit entries older than the retention period (24 months by
    default), as the Data Protection Act expects. Run it on a schedule."""
    if months < 1:
        raise click.ClickException("--months must be at least 1.")
    cutoff = naive_utc(datetime.now(timezone.utc) - timedelta(days=30 * months))
    deleted_count = AuditLog.query.filter(AuditLog.occurred_at < cutoff).delete(synchronize_session=False)
    record_audit_event("audit.pruned", details={"entries_removed": deleted_count, "older_than_months": months})
    database.session.commit()
    click.echo(f"Removed {deleted_count} audit entries older than {months} months.")


# AUTO_MIGRATE=0 skips the start-up migration, for running a single
# "flask db ..." command by hand. Leave it unset on Render.
if os.environ.get("AUTO_MIGRATE", "1") != "0":
    prepare_database()
    promote_initial_admin_from_environment()

if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG") == "1"
    app.run(debug=debug_mode)