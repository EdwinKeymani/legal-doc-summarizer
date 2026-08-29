# Web-Based Automated Legal Document Summary & Insights Tool

A Flask application that summarizes legal documents and flags risk-relevant
clauses. Summarization runs locally; risk assessment uses the Google Gemini API.

## Setup

1. Create and activate a virtual environment:
   ```
   python -m venv venv
   source venv/Scripts/activate     # Windows (Git Bash)
   # source venv/bin/activate       # macOS / Linux
   ```

2. Install dependencies:
   ```
   pip install -r requirements.txt
   python -c "import nltk; nltk.download('punkt'); nltk.download('punkt_tab')"
   ```

3. Set your Gemini API key (get one free at https://aistudio.google.com/apikey):
   ```
   export GEMINI_API_KEY="your_key_here"
   ```
   The app runs without a key too; clauses are then marked "Review" instead of
   receiving an AI risk assessment.

4. Run:
   ```
   python app.py
   ```
   Open http://127.0.0.1:5000

## Testing the analysis without the web server

```
GEMINI_API_KEY="your_key" python analysis.py sample_contract.txt
```

## Project layout

- `app.py`            Flask routes, database models, request handling
- `analysis.py`       NLP pipeline: summarization, clause finding, risk assessment
- `templates/`        Jinja2 HTML templates
- `static/`           CSS
- `uploads/`          Uploaded documents (git-ignored, created at runtime)
- `legal.db`          SQLite database (git-ignored, created at runtime)
