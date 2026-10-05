# Chambua

**Contracts, broken down.** Chambua (Swahili: *to analyse, to break down*) is the product name of the diploma project *Web-Based Automated Legal Document Summary & Insights Tool*.

A Flask web application that reads contracts (PDF, Word or text) and produces:

- a summary: extractive (LexRank or LSA, on the server) or abstractive (Google Gemini)
- flagged clauses rated High, Medium or Low risk, each with a one-line reason
- obligations, prohibitions, rights and liabilities, grouped by party
- key dates, deadlines, amounts and rates
- downloadable PDF and Word reports

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

## Deployment (Render)

- **Build command:** `pip install -r requirements.txt && python -m nltk.downloader punkt punkt_tab`
- **Start command:** `gunicorn app:app --timeout 120`
- **Environment:** `SECRET_KEY`, `DATABASE_URL`, `GEMINI_API_KEY`, `FORCE_SECURE_COOKIES=1`
- **Health check path:** `/health`

Tables are created automatically on startup.

## Project layout

| Path | Contents |
|---|---|
| `app.py` | Routes, database models, upload pipeline, exports |
| `analysis.py` | Summaries, clause finding, Gemini risk rating with retries and fallbacks |
| `provisions.py` | Rule-based obligations, rights, prohibitions, liabilities, dates and amounts |
| `report_export.py` | PDF (ReportLab) and Word (python-docx) reports |
| `tools/db_backup.py` | Back up or move the database between SQLite and PostgreSQL |
| `templates/` | Jinja2 pages; `_*.html` files are shared partials (`_brand.html` holds the logo) |
| `static/` | `styles.css` (app), `auth.css` (sign-in pages), `components.css` and `ui.js` (shared), `brand/` (favicons, app icons, logo) |
| `uploads/`, `legal.db` | Created at runtime, git-ignored |

## Useful commands

```bash
python analysis.py sample_contract.txt                       # run the analysis pipeline on a text file
python tools/db_backup.py counts --database-url "<url>"      # row counts
python tools/db_backup.py export backups/backup.json --database-url "<url>"
python tools/db_backup.py import backups/backup.json --database-url "<new url>"
```

Backup files contain password hashes and document text. They are git-ignored; keep them private.

## What documents it suits

Any PDF, Word or text file can be uploaded, but the clause rules, risk ratings and provision finder are designed for **contracts in English** (service agreements, leases, employment contracts, NDAs). For other documents the report shows a "doesn't look like a contract" notice; the summary still works. Scanned PDFs need OCR first.

## Data and privacy

- **Passwords** are stored as scrypt hashes.
- **Extractive summaries and provision finding** run entirely on the server.
- **Sent to Google's Gemini API:** flagged clause sentences, for risk rating, and, in "Abstractive - Premium (AI)" mode only, up to the first 30,000 characters of the document.

## Documentation

See the project documentation set: API reference, database guide, password-reset email plan, and presentation guide.
