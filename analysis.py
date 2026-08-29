"""
Analysis layer for the Legal Document Summarizer (HYBRID version, Gemini).

Design:
  - Text extraction, summarization, and clause FINDING run locally (no cost).
  - A single Gemini API call per document does the risk REASONING: given the
    clauses already found locally, it judges severity and explains why in plain
    English. This is the one job local rule-based logic cannot do well.

The four Analyzer dropdowns drive real behavior here:
  - summary_depth   -> summary length
  - extraction_mode -> abstractive (model) vs extractive (local, instant)
  - risk_level      -> filters which clauses are returned
  - jurisdiction    -> passed only as a labeling hint, NOT as a claim of
                       jurisdiction-specific legal expertise (see note below)

Honest scoping for the writeup:
  Summarization is genuine local NLP. Clause finding is rule-based. Risk
  reasoning is a Gemini call. The system does NOT possess jurisdiction-specific
  legal knowledge; the jurisdiction field is a contextual hint passed to the
  model, and its output is informational, not legal advice.
"""

import os
import re
import json

# ---------------------------------------------------------------------------
# LOCAL: Summarization
# ---------------------------------------------------------------------------
_summarizer = None


def _get_summarizer():
    global _summarizer
    if _summarizer is None:
        from transformers import pipeline
        _summarizer = pipeline("summarization", model="sshleifer/distilbart-cnn-12-6")
    return _summarizer


def summarize_abstractive(document_text, summary_depth):
    """Model-based summary. Length depends on the Summary Depth dropdown."""
    if not document_text or len(document_text.split()) < 40:
        return document_text

    max_len = 90 if summary_depth == "Executive Summary" else 180
    min_len = 25 if summary_depth == "Executive Summary" else 60

    summarizer = _get_summarizer()
    words = document_text.split()
    chunk_size = 700
    chunks = [" ".join(words[i:i + chunk_size]) for i in range(0, len(words), chunk_size)]

    pieces = []
    for chunk in chunks:
        result = summarizer(chunk, max_length=max_len, min_length=min_len, do_sample=False)
        pieces.append(result[0]["summary_text"])
    return " ".join(pieces)


def summarize_extractive(document_text, summary_depth):
    """
    Local, instant, no model download. Picks the most important existing
    sentences. Used when the user selects 'Extractive Only'.
    Requires: pip install sumy
    """
    from sumy.parsers.plaintext import PlaintextParser
    from sumy.nlp.tokenizers import Tokenizer
    from sumy.summarizers.lex_rank import LexRankSummarizer

    sentence_count = 3 if summary_depth == "Executive Summary" else 7
    parser = PlaintextParser.from_string(document_text, Tokenizer("english"))
    summarizer = LexRankSummarizer()
    sentences = summarizer(parser.document, sentence_count)
    return " ".join(str(s) for s in sentences)


# ---------------------------------------------------------------------------
# LOCAL: Clause finding (rule-based)
# ---------------------------------------------------------------------------
CLAUSE_RULES = [
    {"category": "Indemnification", "keywords": ["indemnif", "hold harmless", "liable for all"]},
    {"category": "Termination", "keywords": ["terminate", "termination", "forfeit", "notice period"]},
    {"category": "Late Payment / Penalty", "keywords": ["penalty", "late payment", "default interest", "overdue"]},
    {"category": "Auto-Renewal", "keywords": ["automatically renew", "auto-renew", "renewed for"]},
    {"category": "Governing Law", "keywords": ["governed by", "governing law", "jurisdiction"]},
    {"category": "Confidentiality", "keywords": ["confidential", "non-disclosure", "proprietary information"]},
]


def split_into_sentences(text):
    sentences = re.split(r"(?<=[.;])\s+", text)
    return [s.strip() for s in sentences if len(s.strip()) > 15]


def find_clauses(document_text):
    """Locate candidate clauses. Severity is NOT decided here, Gemini does that."""
    found = []
    seen = set()
    for sentence in split_into_sentences(document_text):
        lower = sentence.lower()
        for rule in CLAUSE_RULES:
            if any(k in lower for k in rule["keywords"]):
                if rule["category"] not in seen:
                    snippet = sentence if len(sentence) <= 300 else sentence[:297] + "..."
                    found.append({"category": rule["category"], "text": snippet})
                    seen.add(rule["category"])
                break
    return found


# ---------------------------------------------------------------------------
# API: Risk reasoning (the ONE external call, via Gemini)
# ---------------------------------------------------------------------------
def assess_risk_with_llm(clauses, jurisdiction):
    """
    Send the locally-found clauses to Gemini to assign severity and explain why.
    ONE call per document. Returns clauses enriched with severity + explanation.

    Uses Google Gemini. Get a free API key at https://aistudio.google.com/apikey
    and set it as the GEMINI_API_KEY environment variable.

    Falls back to a safe default if no API key is set or the call fails, so the
    app never crashes just because the network is down.
    """
    api_key = os.environ.get("GEMINI_API_KEY")

    if not api_key or not clauses:
        for clause in clauses:
            clause["severity"] = "Review"
            clause["explanation"] = "Automated risk assessment unavailable (no API key set)."
        return clauses

    clause_list = "\n".join(
        f"{i+1}. [{c['category']}] {c['text']}" for i, c in enumerate(clauses)
    )

    prompt = (
        "You are assisting with a preliminary contract review. This is "
        "informational only and not legal advice. For each numbered clause "
        f"below, considering a {jurisdiction} context as general background "
        "only, assign a risk severity of exactly 'High', 'Medium', or 'Low', "
        "and give a one-sentence plain-English reason.\n\n"
        f"Clauses:\n{clause_list}\n\n"
        "Respond ONLY with a JSON array, no other text. Each element: "
        '{"index": <number>, "severity": "<High|Medium|Low>", "reason": "<one sentence>"}'
    )

    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")
        response = model.generate_content(prompt)
        raw = response.text.strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        assessments = json.loads(raw)

        by_index = {a["index"]: a for a in assessments}
        for i, clause in enumerate(clauses):
            assessment = by_index.get(i + 1, {})
            clause["severity"] = assessment.get("severity", "Review")
            clause["explanation"] = assessment.get("reason", "No assessment returned.")

    except Exception as error:
        for clause in clauses:
            clause["severity"] = "Review"
            clause["explanation"] = f"Risk assessment could not be completed: {error}"

    return clauses


# ---------------------------------------------------------------------------
# Public entry point used by app.py
# ---------------------------------------------------------------------------
def analyze_legal_text(document_text, options=None):
    """
    Run the full hybrid pipeline.

    options is a dict from the Analyzer dropdowns.
    Returns (summary_string, list_of_clause_dicts).
    """
    if options is None:
        options = {}
    summary_depth = options.get("summary_depth", "Executive Summary")
    extraction_mode = options.get("extraction_mode", "Abstractive & Extractive")
    risk_level = options.get("risk_level", "High & Medium Risk")
    jurisdiction = options.get("jurisdiction", "General Commercial")

    if not document_text:
        return "No readable text could be extracted from this document.", []

    if extraction_mode == "Extractive Only":
        summary = summarize_extractive(document_text, summary_depth)
    else:
        summary = summarize_abstractive(document_text, summary_depth)

    clauses = find_clauses(document_text)
    clauses = assess_risk_with_llm(clauses, jurisdiction)

    if risk_level == "High Risk Only":
        clauses = [c for c in clauses if c.get("severity") == "High"]

    return summary, clauses


# ---------------------------------------------------------------------------
# Test without the web server:
#   GEMINI_API_KEY=xxx python analysis.py sample_contract.txt
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python analysis.py <path-to-text-file>")
        sys.exit(1)

    with open(sys.argv[1], "r", encoding="utf-8", errors="ignore") as f:
        sample = f.read()

    test_options = {
        "summary_depth": "Executive Summary",
        "extraction_mode": "Extractive Only",
        "risk_level": "High & Medium Risk",
        "jurisdiction": "General Commercial",
    }
    summary, clauses = analyze_legal_text(sample, test_options)

    print("\n=== SUMMARY ===")
    print(summary)
    print("\n=== CLAUSES ===")
    for c in clauses:
        print(f"[{c.get('severity')}] {c['category']}: {c['text']}")
        print(f"    -> {c.get('explanation')}")
