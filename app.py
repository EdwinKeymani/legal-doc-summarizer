"""
Legal Document Summarizer - Backend POC
Single-file Flask application. Local only: SQLite + local file storage.

Run with:  python app.py
Then open: http://127.0.0.1:5000
"""

import os
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, send_from_directory, abort
)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# Document text extraction
import pdfplumber
import docx

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)

# Clean and normalize DATABASE_URL from environment
raw_db_url = os.environ.get("DATABASE_URL", "sqlite:///instance/app.db").strip().strip("'").strip(""")
if raw_db_url.startswith("postgres://"):
    raw_db_url = raw_db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = raw_db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "connect_args": {"connect_timeout": 10}
}

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-insecure-key-change-me")


app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "connect_args": {"connect_timeout": 10}
}

app.config["UPLOAD_FOLDER"] = "/tmp/uploads" if os.environ.get("VERCEL") else os.path.join(BASE_DIR, "uploads")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FORCE_SECURE_COOKIES") == "1"

ALLOWED_EXTENSIONS = {"pdf", "docx", "txt"}
database = SQLAlchemy(app)
csrf = CSRFProtect(app)

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)


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


class ExtractedClause(database.Model):
    id = database.Column(database.Integer, primary_key=True)
    document_id = database.Column(database.Integer, database.ForeignKey("document.id"), nullable=False)
    clause_category = database.Column(database.String(80))
    extracted_text = database.Column(database.Text)
    risk_severity = database.Column(database.String(20))
    explanation = database.Column(database.Text)


def is_allowed_file(file_name):
    return "." in file_name and file_name.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def extract_text_from_document(file_path, file_format):
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
            extracted_text += paragraph.text + "\n"

    elif file_format == "txt":
        with open(file_path, "r", encoding="utf-8", errors="ignore") as text_file:
            extracted_text = text_file.read()

    return extracted_text.strip()


@app.context_processor
def inject_user_initials():
    name = session.get("user_name", "")
    if not name:
        return {"user_initials": "U"}
    parts = name.split()
    if len(parts) >= 2:
        initials = parts[0][0] + parts[1][0]
    else:
        initials = name[:2]
    return {"user_initials": initials.upper()}


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapped


@app.route("/")
def index():
    return redirect(url_for("dashboard") if "user_id" in session else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password_hash, password):
            session.clear()
            session["user_id"] = user.id
            session["user_name"] = user.full_name
            return redirect(url_for("dashboard"))

        flash("Invalid email or password.")
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not full_name or not email or not password:
            flash("All fields are required.")
            return redirect(url_for("register"))

        if len(password) < 8:
            flash("Password must be at least 8 characters.")
            return redirect(url_for("register"))

        if User.query.filter_by(email=email).first():
            flash("An account with that email already exists.")
            return redirect(url_for("register"))

        new_user = User(
            full_name=full_name,
            email=email,
            password_hash=generate_password_hash(password),
        )
        database.session.add(new_user)
        database.session.commit()

        session.clear()
        session["user_id"] = new_user.id
        session["user_name"] = new_user.full_name
        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
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

    start_of_today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    processed_today = sum(
        1 for document in user_documents
        if document.upload_date.replace(tzinfo=timezone.utc) >= start_of_today
        and document.status == "Processed"
    )
    pending_count = sum(1 for document in user_documents if document.status == "Processing")
    failed_count = sum(1 for document in user_documents if document.status == "Failed")

    current_user = database.session.get(User, session["user_id"])

    return render_template(
        "dashboard.html",
        user_name=session.get("user_name"),
        user_email=current_user.email if current_user else "",
        documents=user_documents,
        total_documents=total_documents,
        high_risk_count=high_risk_count,
        processed_today=processed_today,
        pending_count=pending_count,
        failed_count=failed_count,
        active_view="dashboard",
    )


@app.route("/upload", methods=["POST"])
@login_required
def upload_document():
    uploaded_file = request.files.get("document")

    if not uploaded_file or uploaded_file.filename == "":
        flash("No file was selected.")
        return redirect(url_for("dashboard"))

    if not is_allowed_file(uploaded_file.filename):
        flash("Unsupported file type. Upload a PDF, DOCX, or TXT file.")
        return redirect(url_for("dashboard"))

    original_name = secure_filename(uploaded_file.filename)
    if not original_name:
        flash("Invalid file name.")
        return redirect(url_for("dashboard"))

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

    analysis_options = {
        "summary_depth": request.form.get("summary_depth", "Executive Summary"),
        "extraction_mode": request.form.get("extraction_mode", "Abstractive & Extractive"),
        "risk_level": request.form.get("risk_level", "High & Medium Risk"),
        "jurisdiction": request.form.get("jurisdiction", "General Commercial"),
    }

    try:
        document_text = extract_text_from_document(file_path, file_format)

        if not document_text:
            raise ValueError("No extractable text was found in this file.")

        summary, detected_clauses = analyze_legal_text(document_text, analysis_options)

        new_document.summary_text = summary
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
        flash(f"'{original_name}' processed successfully.")

    except Exception as exc:
        database.session.rollback()
        new_document.status = "Failed"
        new_document.error_message = str(exc)
        database.session.add(new_document)
        database.session.commit()
        flash(f"'{original_name}' could not be processed: {exc}")

    return redirect(url_for("dashboard"))


@app.route("/document/<int:document_id>")
@login_required
def view_document(document_id):
    document = database.session.get(Document, document_id)
    if document is None:
        abort(404)

    if document.user_id != session["user_id"]:
        flash("You do not have access to that document.")
        return redirect(url_for("dashboard"))

    return render_template(
        "document_detail.html",
        document=document,
        user_name=session.get("user_name"),
    )


@app.route("/settings/profile", methods=["POST"])
@login_required
def update_profile():
    full_name = request.form.get("full_name", "").strip()

    if not full_name:
        flash("Full name cannot be empty.")
        return redirect(url_for("dashboard") + "#settings")

    user = database.session.get(User, session["user_id"])
    user.full_name = full_name
    database.session.commit()

    session["user_name"] = full_name

    flash("Profile updated successfully.")
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

    if len(new_password) < 8:
        flash("New password must be at least 8 characters.")
        return redirect(url_for("dashboard") + "#settings")

    if new_password != confirm_password:
        flash("New password and confirmation do not match.")
        return redirect(url_for("dashboard") + "#settings")

    user.password_hash = generate_password_hash(new_password)
    database.session.commit()

    flash("Password changed successfully.")
    return redirect(url_for("dashboard") + "#settings")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first()

        generic_message = "If an account with that email exists, a reset link has been generated."

        if user:
            token = secrets.token_urlsafe(32)
            user.reset_token = token
            user.reset_token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
            database.session.commit()

            reset_link = url_for("reset_password", token=token, _external=True)

            print(f"\n[PASSWORD RESET] Link for {email}: {reset_link}\n")

            flash(generic_message)
            flash(f"[DEV MODE] Reset link: {reset_link}")
        else:
            flash(generic_message)

        return redirect(url_for("forgot_password"))

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    user = User.query.filter_by(reset_token=token).first()

    if not user or not user.reset_token_expiry or user.reset_token_expiry < datetime.now(timezone.utc):
        flash("This reset link is invalid or has expired.")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if len(new_password) < 8:
            flash("Password must be at least 8 characters.")
            return redirect(url_for("reset_password", token=token))

        if new_password != confirm_password:
            flash("Passwords do not match.")
            return redirect(url_for("reset_password", token=token))

        user.password_hash = generate_password_hash(new_password)
        user.reset_token = None
        user.reset_token_expiry = None
        database.session.commit()

        flash("Password reset successfully. Please log in.")
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

    flash(f"'{document.file_name}' was deleted.")
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

    return send_from_directory(
        app.config["UPLOAD_FOLDER"],
        document.stored_name,
        as_attachment=True,
        download_name=document.file_name,
    )


@app.route("/health")
def health_check():
    """Simple endpoint to confirm the server is awake and responding.
    Useful for warming up Render's free tier before a demo, and for
    Render's own uptime monitoring."""
    return "OK", 200


from analysis import analyze_legal_text  # noqa: E402


# Create tables on import so this works whether run locally (python app.py)
# or imported directly by a serverless platform like Vercel, which never
# executes the __main__ block below.
@app.before_request
def create_tables_on_first_request():
    if not getattr(app, "_got_first_request", False):
        try:
            database.create_all()
        except Exception as e:
            app.logger.error(f"Database setup error: {e}")
        app._got_first_request = True


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG") == "1"
    app.run(debug=debug_mode)
