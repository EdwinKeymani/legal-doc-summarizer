
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
import time
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, send_from_directory, send_file, abort, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine
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
class User(database.Model):
    id = database.Column(database.Integer, primary_key=True)
    full_name = database.Column(database.String(120), nullable=False)
    email = database.Column(database.String(120), unique=True, nullable=False)
    password_hash = database.Column(database.String(255), nullable=False)
    reset_token = database.Column(database.String(64))
    reset_token_expiry = database.Column(database.DateTime)
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
    """Per-user settings. Kept in its own table (one row per user) so adding it
    needs no migration: database.create_all() creates new tables on startup,
    on both local SQLite and Render Postgres, without touching existing ones."""
    id = database.Column(database.Integer, primary_key=True)
    user_id = database.Column(database.Integer, database.ForeignKey("user.id"), unique=True, nullable=False)
    theme = database.Column(database.String(10), nullable=False, default="system")
    default_summary_depth = database.Column(database.String(40), nullable=False, default=SUMMARY_DEPTH_OPTIONS[0])
    default_extraction_mode = database.Column(database.String(40), nullable=False, default=EXTRACTION_MODE_OPTIONS[0])
    default_risk_level = database.Column(database.String(40), nullable=False, default=RISK_LEVEL_OPTIONS[0])
    default_context_hint = database.Column(database.String(40), nullable=False, default=CONTEXT_HINT_OPTIONS[0])


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
    """Logged-in pages get the saved theme from the session. Logged-out pages
    get an empty value, and the browser falls back to the last theme it saw."""
    return {"current_theme": session.get("theme", "system") if "user_id" in session else ""}


@app.context_processor
def inject_user_initials():
    """Make user_initials available in every template without passing it per route."""
    name = session.get("user_name", "")
    if not name:
        return {"user_initials": "U"}
    parts = name.split()
    if len(parts) >= 2:
        initials = parts[0][0] + parts[1][0]
    else:
        initials = name[:2]
    return {"user_initials": initials.upper()}


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
    """Redirect anonymous visitors to the login page before running a view."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapped


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
            flash("Invalid email or password.")
            return render_template("login.html", form_values={"email": email}), 400

        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password_hash, password):
            session.clear()
            # Ticked: stay signed in for REMEMBER_ME_DAYS. Unticked: signed out
            # when the browser closes. (Previously always permanent, so the
            # checkbox did nothing.)
            session.permanent = request.form.get("remember") == "on"
            session["user_id"] = user.id
            session["user_name"] = user.full_name
            session["theme"] = get_user_preferences(user.id).theme
            return redirect(url_for("dashboard"))

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
            )
            database.session.add(new_user)
            database.session.commit()  # Saves cleanly to PostgreSQL

            session.clear()
            session.permanent = False  # new accounts get a browser-session login
            session["user_id"] = new_user.id
            session["user_name"] = new_user.full_name
            session["theme"] = "system"
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
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
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
@login_required
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
        database.session.commit()
        flash(f"'{original_name}' could not be processed. The reason is shown below.")

    # Go straight to this document's results (or its failure reason), instead
    # of the dashboard where the user had to find it in Recent Activity.
    return redirect(url_for("view_document", document_id=new_document.id))


@app.route("/document/<int:document_id>")
@login_required
def view_document(document_id):
    document = database.session.get(Document, document_id)
    if document is None:
        abort(404)

    if document.user_id != session["user_id"]:
        flash("You do not have access to that document.")
        return redirect(url_for("dashboard"))

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
@login_required
def export_report(document_id, export_format):
    """Download the analysis as a PDF or Word report (proposal requirement 4.2)."""
    if export_format not in EXPORT_FORMATS:
        abort(404)
    document = database.session.get(Document, document_id)
    if document is None:
        abort(404)
    if document.user_id != session["user_id"]:
        flash("You do not have access to that document.")
        return redirect(url_for("dashboard"))
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
    return attach_download_token(send_file(
        io.BytesIO(file_bytes),
        mimetype=mime_type,
        as_attachment=True,
        download_name=report_filename(document.file_name, extension),
    ))


@app.route("/settings/profile", methods=["POST"])
@login_required
def update_profile():
    full_name = re.sub(r"\s+", " ", request.form.get("full_name", "")).strip()

    name_problem = full_name_error(full_name)
    if name_problem:
        flash(name_problem)
        return redirect(url_for("dashboard") + "#settings")

    user = database.session.get(User, session["user_id"])
    user.full_name = full_name
    database.session.commit()

    session["user_name"] = full_name

    flash("Profile updated successfully.", "success")
    return redirect(url_for("dashboard") + "#settings")


@app.route("/settings/password", methods=["POST"])
@login_required
def change_password():
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    user = database.session.get(User, session["user_id"])

    if not check_password_hash(user.password_hash, current_password):
        flash("Current password is incorrect.")
        return redirect(url_for("dashboard") + "#settings")

    password_problem = new_password_error(new_password, user.email)
    if password_problem:
        flash(password_problem)
        return redirect(url_for("dashboard") + "#settings")

    if new_password == current_password:
        flash("The new password must be different from the current one.")
        return redirect(url_for("dashboard") + "#settings")

    if new_password != confirm_password:
        flash("New password and confirmation do not match.")
        return redirect(url_for("dashboard") + "#settings")

    user.password_hash = generate_password_hash(new_password)
    database.session.commit()

    flash("Password changed successfully.", "success")
    return redirect(url_for("dashboard") + "#settings")


@app.route("/settings/preferences", methods=["POST"])
@login_required
def update_preferences():
    """Save appearance and default analysis options from the Settings tab."""
    preferences = get_user_preferences(session["user_id"], create_if_missing=True)
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

    session["theme"] = preferences.theme
    flash("Preferences saved.", "success")
    return redirect(url_for("dashboard") + "#settings")


@app.route("/settings/theme", methods=["POST"])
@login_required
def update_theme():
    """Used by the sun/moon button in the sidebar. Called with fetch(), so it
    returns JSON instead of redirecting. CSRF token arrives in X-CSRFToken."""
    request_data = request.get_json(silent=True) or {}
    requested_theme = request_data.get("theme")
    if requested_theme not in THEME_OPTIONS:
        return jsonify({"error": "Unknown theme"}), 400

    preferences = get_user_preferences(session["user_id"], create_if_missing=True)
    preferences.theme = requested_theme
    database.session.commit()
    session["theme"] = requested_theme
    return jsonify({"theme": requested_theme})


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
        database.session.commit()

        flash("Password reset successfully. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)


@app.route("/document/<int:document_id>/delete", methods=["POST"])
@login_required
def delete_document(document_id):
    document = database.session.get(Document, document_id)
    if document is None:
        abort(404)

    if document.user_id != session["user_id"]:
        flash("You do not have access to that document.")
        return redirect(url_for("dashboard"))

    file_path = os.path.join(app.config["UPLOAD_FOLDER"], document.stored_name)
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except OSError:
        pass

    ExtractedClause.query.filter_by(document_id=document.id).delete()
    database.session.delete(document)
    database.session.commit()

    flash(f"'{document.file_name}' was deleted.", "success")
    return redirect(url_for("dashboard") + "#documents")


@app.route("/document/<int:document_id>/download")
@login_required
def download_document(document_id):
    document = database.session.get(Document, document_id)
    if document is None:
        abort(404)

    if document.user_id != session["user_id"]:
        flash("You do not have access to that document.")
        return redirect(url_for("dashboard"))

    if not original_file_exists(document):
        flash(
            "The original file is no longer on the server (the free hosting tier clears "
            "uploaded files when it restarts). The analysis is unaffected; you can export it as a report."
        )
        return redirect(url_for("view_document", document_id=document.id))

    return attach_download_token(send_from_directory(
        app.config["UPLOAD_FOLDER"],
        document.stored_name,
        as_attachment=True,
        download_name=document.file_name,
    ))


@app.route("/document/<int:document_id>/reassess", methods=["POST"])
@login_required
def reassess_document(document_id):
    """Re-run only the Gemini risk assessment for clauses that came back as
    'Review' (API busy, timeout, no key at the time). The summary and the
    clauses themselves are kept; no re-upload needed."""
    document = database.session.get(Document, document_id)
    if document is None:
        abort(404)
    if document.user_id != session["user_id"]:
        flash("You do not have access to that document.")
        return redirect(url_for("dashboard"))

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
    database.session.commit()

    if newly_assessed_count == len(pending_clauses):
        flash("Risk assessment completed.", "success")
    else:
        flash("The AI service is still busy. Please try again in a minute.")
    return redirect(url_for("view_document", document_id=document.id))


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


# Create any missing tables on startup (does not alter existing tables).
with app.app_context():
    database.create_all()

if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG") == "1"
    app.run(debug=debug_mode)