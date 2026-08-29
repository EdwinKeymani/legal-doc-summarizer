"""
Legal Document Summarizer - Backend POC
Single-file Flask application. Local only: SQLite + local file storage.

Run with:  python app.py
Then open: `http://127.0.0.1:5000
"""

import os
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, send_from_directory
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# Document text extraction
import pdfplumber
import docx

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------
app = Flask(__name__)

# In a real deployment this would come from an environment variable, not source.
# For a local POC a fixed string is acceptable, but never commit a real secret.
app.config["SECRET_KEY"] = "dev-secret-change-me"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///legal.db"
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB cap, matches the UI

ALLOWED_EXTENSIONS = {"pdf", "docx", "txt"}

database = SQLAlchemy(app)

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
    documents = database.relationship("Document", backref="owner", lazy=True)


class Document(database.Model):
    id = database.Column(database.Integer, primary_key=True)
    user_id = database.Column(database.Integer, database.ForeignKey("user.id"), nullable=False)
    file_name = database.Column(database.String(255), nullable=False)
    stored_name = database.Column(database.String(255), nullable=False)
    file_format = database.Column(database.String(10))
    upload_date = database.Column(database.DateTime, default=datetime.utcnow)
    summary_text = database.Column(database.Text)
    status = database.Column(database.String(20), default="Processing")
    clauses = database.relationship("ExtractedClause", backref="document", lazy=True)


class ExtractedClause(database.Model):
    id = database.Column(database.Integer, primary_key=True)
    document_id = database.Column(database.Integer, database.ForeignKey("document.id"), nullable=False)
    clause_category = database.Column(database.String(80))
    extracted_text = database.Column(database.Text)
    risk_severity = database.Column(database.String(20))  # High / Medium / Low / Review
    explanation = database.Column(database.Text)          # LLM's one-line reason


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
            extracted_text += paragraph.text + "\n"

    elif file_format == "txt":
        with open(file_path, "r", encoding="utf-8", errors="ignore") as text_file:
            extracted_text = text_file.read()

    return extracted_text.strip()


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


def login_required_redirect():
    """Return a redirect to login if no user is in the session, else None."""
    if "user_id" not in session:
        return redirect(url_for("login"))
    return None


# ---------------------------------------------------------------------------
# Authentication routes
# ---------------------------------------------------------------------------
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

        session["user_id"] = new_user.id
        session["user_name"] = new_user.full_name
        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Application routes
# ---------------------------------------------------------------------------
@app.route("/dashboard")
def dashboard():
    guard = login_required_redirect()
    if guard:
        return guard

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

    # Documents uploaded since midnight today
    start_of_today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    processed_today = sum(
        1 for document in user_documents
        if document.upload_date >= start_of_today and document.status == "Processed"
    )
    pending_count = sum(1 for document in user_documents if document.status != "Processed")

    current_user = User.query.get(session["user_id"]) 

    return render_template(
        "dashboard.html",
        user_name=session.get("user_name"),
        user_email=current_user.email if current_user else "",
        documents=user_documents,
        total_documents=total_documents,
        high_risk_count=high_risk_count,
        processed_today=processed_today,
        pending_count=pending_count,
        active_view="dashboard",
    )


@app.route("/upload", methods=["POST"])
def upload_document():
    guard = login_required_redirect()
    if guard:
        return guard

    uploaded_file = request.files.get("document")

    if not uploaded_file or uploaded_file.filename == "":
        flash("No file was selected.")
        return redirect(url_for("dashboard"))

    if not is_allowed_file(uploaded_file.filename):
        flash("Unsupported file type. Upload a PDF, DOCX, or TXT file.")
        return redirect(url_for("dashboard"))

    # Save the file with a safe, unique name
    original_name = secure_filename(uploaded_file.filename)
    file_format = original_name.rsplit(".", 1)[1].lower()
    stored_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{original_name}"
    file_path = os.path.join(app.config["UPLOAD_FOLDER"], stored_name)
    uploaded_file.save(file_path)

    # Create the database record
    new_document = Document(
        user_id=session["user_id"],
        file_name=original_name,
        stored_name=stored_name,
        file_format=file_format.upper(),
        status="Processing",
    )
    database.session.add(new_document)
    database.session.commit()

    # Read the four Analyzer dropdown selections from the form.
    # Defaults match the first <option> in each dropdown.
    analysis_options = {
        "summary_depth": request.form.get("summary_depth", "Executive Summary"),
        "extraction_mode": request.form.get("extraction_mode", "Abstractive & Extractive"),
        "risk_level": request.form.get("risk_level", "High & Medium Risk"),
        "jurisdiction": request.form.get("jurisdiction", "General Commercial"),
    }

    # Extract text and run analysis
    document_text = extract_text_from_document(file_path, file_format)
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
    return redirect(url_for("dashboard"))


@app.route("/document/<int:document_id>")
def view_document(document_id):
    guard = login_required_redirect()
    if guard:
        return guard

    document = Document.query.get_or_404(document_id)

    # Ownership check: users can only see their own documents
    if document.user_id != session["user_id"]:
        flash("You do not have access to that document.")
        return redirect(url_for("dashboard"))

    return render_template(
        "document_detail.html",
        document=document,
        user_name=session.get("user_name"),
    )


# ---------------------------------------------------------------------------
# The analysis layer  (see analysis.py for the real logic)
# ---------------------------------------------------------------------------
from analysis import analyze_legal_text  # noqa: E402  (imported here for clarity)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    with app.app_context():
        database.create_all()   # Creates legal.db and all tables on first run
    app.run(debug=True)
