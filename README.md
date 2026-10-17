# Web-Based Automated Legal Document Summary & Insights Tool

A Flask web application that reads contracts (PDF, Word or text) and produces:

- a summary: extractive (LexRank or LSA, on the server) or abstractive (Google Gemini)
- flagged clauses rated High, Medium or Low risk, each with a one-line reason
- obligations, prohibitions, rights and liabilities, grouped by party
- key dates, deadlines, amounts and rates
- downloadable PDF and Word reports
- three roles for a legal-aid clinic (Member, Reviewer, Administrator), with an audit log of who did what

Built as a Diploma in Computer Science project at Mount Kenya University. Results are informational only and are not legal advice.

## Quick start (local)

```bash
python -m venv venv
source venv/Scripts/activate        # Windows Git Bash   (macOS/Linux: source venv/bin/activate)
pip install -r requirements.txt
python -m nltk.downloader punkt punkt_tab
```

Create a `.env` file in the project folder (it is git-ignored):

```
SECRET_KEY=<long random string: python -c "import secrets; print(secrets.token_hex(32))">
GEMINI_API_KEY=<optional: free key from https://aistudio.google.com/apikey>
SHOW_RESET_LINK=1
FLASK_DEBUG=1
```

Run `python app.py` and open http://127.0.0.1:5000.

Without `GEMINI_API_KEY` everything still works: summaries use the local algorithms, and clauses are marked "Review" until a key is added.

## Configuration

| Variable | Where | Purpose |
|---|---|---|
| `SECRET_KEY` | Always | Signs login cookies. Never commit it. |
| `DATABASE_URL` | Production | PostgreSQL connection string. Without it, the app uses local `legal.db` (SQLite). |
| `GEMINI_API_KEY` | Recommended | Enables AI summaries and risk ratings |
| `GEMINI_MODELS` | Optional | Model order, e.g. `gemini-3.6-flash,gemini-3.5-flash` |
| `FORCE_SECURE_COOKIES` | Production: `1` | HTTPS-only cookies |
| `SHOW_RESET_LINK` | Local only | Show password-reset links on screen |
| `FLASK_DEBUG` | Local only | Debug mode for `python app.py` |
| `INITIAL_ADMIN_EMAIL` | Once | Makes that already-registered account an Administrator at startup. Remove it afterwards. |
| `AUTO_MIGRATE` | Optional | `0` stops the app from applying database migrations at startup (default: on) |

## Deployment (Render)

- **Build command:** `pip install -r requirements.txt && python -m nltk.downloader punkt punkt_tab`
- **Start command:** `gunicorn app:app --timeout 120`
- **Environment:** `SECRET_KEY`, `DATABASE_URL`, `GEMINI_API_KEY`, `FORCE_SECURE_COOKIES=1`
- **Health check path:** `/health`

Database migrations (`migrations/`) are applied automatically at startup, so a deploy needs no extra step. An increment 6 database upgrades in place: every existing account becomes a Member. Take a backup first (`tools/db_backup.py export`).

## Project layout

| Path | Contents |
|---|---|
| `app.py` | Routes, database models, upload pipeline, exports |
| `analysis.py` | Summaries, clause finding, Gemini risk rating with retries and fallbacks |
| `provisions.py` | Rule-based obligations, rights, prohibitions, liabilities, dates and amounts |
| `report_export.py` | PDF (ReportLab) and Word (python-docx) reports |
| `tools/db_backup.py` | Back up or move the database between SQLite and PostgreSQL |
| `migrations/` | Versioned database changes (Flask-Migrate / Alembic) |
| `templates/` | Jinja2 pages; `_*.html` files are shared partials |
| `static/` | `styles.css` (app), `auth.css` (sign-in pages), `components.css` and `ui.js` (shared) |
| `uploads/`, `legal.db` | Created at runtime, git-ignored |

## Useful commands

```bash
python analysis.py sample_contract.txt                       # run the analysis pipeline on a text file
python tools/db_backup.py counts --database-url "<url>"      # row counts
python tools/db_backup.py export backups/backup.json --database-url "<url>"
python tools/db_backup.py import backups/backup.json --database-url "<new url>"
```

```bash
flask --app app create-admin edwin@example.com              # make a registered account an Administrator
flask --app app prune-audit-log --months 24                 # delete audit entries older than 24 months
flask --app app db current                                  # which migration the database is on
```

Backup files contain password hashes and document text. They are git-ignored; keep them private.

## Roles

| Role | Who | Can |
|---|---|---|
| Member | Clinic clients (everyone who registers) | Upload contracts, read, export and delete their own reports |
| Reviewer | Law students, paralegals, advocates | Work through the review queue (review workflow arrives in a later increment) |
| Administrator | Clinic coordinator | Manage people and roles, view the permission matrix and the audit log |

Permissions live in the database (`role`, `permission`, `role_permission`) and are checked on every request. Anything not granted is refused, and a refused page looks exactly like a missing one (404). Administrators don't upload documents: use a separate Member account for that.

## Data and privacy

- **Passwords** are stored as scrypt hashes.
- **Audit log** entries record actions, never passwords or contract text.
- **Extractive summaries and provision finding** run entirely on the server.
- **Sent to Google's Gemini API:** flagged clause sentences, for risk rating, and, in "Abstractive - Premium (AI)" mode only, up to the first 30,000 characters of the document.

## Documentation

See the project documentation set: API reference, database guide, password-reset email plan, and presentation guide.
