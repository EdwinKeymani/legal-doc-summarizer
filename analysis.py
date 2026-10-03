"""
Analysis layer for the Legal Document Summarizer (HYBRID version, Gemini).

Design:
  - Text extraction and clause FINDING run locally (no cost, fast).
  - Summarization has three tiers:
      1. Extractive - Fast     -> sumy LexRank (instant, local, no API cost)
      2. Extractive - Premium  -> sumy LSA (instant, local, different/often
                                   better sentence selection than LexRank)
      3. Abstractive - Premium -> Gemini itself writes the summary in its
                                   own words. Fast because it's a small API
                                   call, NOT a heavy local model download
                                   (unlike the old transformers approach).
  - A single Gemini API call per document does the risk REASONING: given the
    clauses already found locally, it judges severity and explains why in
    plain English.

The Analyzer dropdowns drive real behavior here:
  - summary_depth   -> summary length
  - extraction_mode -> which summarization tier to use (see above)
  - risk_level      -> filters which clauses are returned
  - jurisdiction    -> passed only as a labeling hint, NOT as a claim of
                       jurisdiction-specific legal expertise

Honest scoping for the writeup:
  Extractive summarization is genuine local NLP (sumy). Abstractive summaries
  and risk reasoning both come from Gemini API calls. Clause finding is
  rule-based. The system does NOT possess jurisdiction-specific legal
  knowledge; the jurisdiction field is a contextual hint passed to the model,
  and its output is informational, not legal advice.
"""

import os
import re
import json
import time
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gemini access with retry + model fallback
# ---------------------------------------------------------------------------
# Models are tried in order. Each model has its own capacity, so when one is
# overloaded (503) the next one usually answers. Override on Render with e.g.
#   GEMINI_MODELS=gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite
DEFAULT_GEMINI_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]

ATTEMPTS_PER_MODEL = 2            # retries on the same model before moving on
INITIAL_BACKOFF_SECONDS = 1.5     # doubles after each retryable failure
PER_REQUEST_TIMEOUT_MS = 20000    # a single Gemini HTTP call may take at most 20s
TOTAL_TIME_BUDGET_SECONDS = 45    # never spend longer than this on one task

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

USER_FACING_ASSESSMENT_ERROR = (
    "Risk assessment is temporarily unavailable because the AI service is busy. "
    "Use 'Re-run risk assessment' on this report to try again."
)


class GeminiUnavailableError(Exception):
    """Raised when every configured model failed within the time budget."""


def get_configured_models():
    configured = os.environ.get("GEMINI_MODELS", "")
    models = [name.strip() for name in configured.split(",") if name.strip()]
    return models or DEFAULT_GEMINI_MODELS


def _create_gemini_client():
    from google import genai
    from google.genai import types
    return genai.Client(http_options=types.HttpOptions(timeout=PER_REQUEST_TIMEOUT_MS))


def _error_status_code(error):
    """google-genai APIError exposes .code; network errors have no code."""
    status_code = getattr(error, "code", None)
    return status_code if isinstance(status_code, int) else None


def _is_retryable(error):
    status_code = _error_status_code(error)
    if status_code is not None:
        return status_code in RETRYABLE_STATUS_CODES
    # No HTTP status: timeouts, dropped connections ("Server disconnected").
    return True


def generate_with_fallback(prompt, expect_json=False):
    """
    Call Gemini with exponential backoff and model fallback.
    Returns the response text. Raises GeminiUnavailableError if nothing worked.
    """
    from google.genai import types

    client = _create_gemini_client()
    config = types.GenerateContentConfig(response_mime_type="application/json") if expect_json else None
    deadline = time.monotonic() + TOTAL_TIME_BUDGET_SECONDS
    last_error = None

    for model_name in get_configured_models():
        backoff_seconds = INITIAL_BACKOFF_SECONDS
        for attempt_number in range(1, ATTEMPTS_PER_MODEL + 1):
            if time.monotonic() >= deadline:
                raise GeminiUnavailableError(f"Time budget exhausted. Last error: {last_error}")
            try:
                response = client.models.generate_content(
                    model=model_name, contents=prompt, config=config
                )
                response_text = (response.text or "").strip()
                if response_text:
                    if model_name != get_configured_models()[0]:
                        logger.warning("Gemini answered via fallback model %s", model_name)
                    return response_text
                last_error = ValueError("Empty response")
            except Exception as error:  # noqa: BLE001 - SDK raises several types
                last_error = error
                logger.warning(
                    "Gemini call failed (model=%s, attempt=%d): %s",
                    model_name, attempt_number, error,
                )
                if not _is_retryable(error):
                    break  # e.g. 404 model retired, 400 bad request: next model
            remaining_seconds = deadline - time.monotonic()
            if attempt_number < ATTEMPTS_PER_MODEL and remaining_seconds > backoff_seconds:
                time.sleep(backoff_seconds)
                backoff_seconds *= 2

    raise GeminiUnavailableError(f"All Gemini models failed. Last error: {last_error}")


# ---------------------------------------------------------------------------
# LOCAL: Extractive summarization (fast, no model download, no API cost)
# ---------------------------------------------------------------------------
_nltk_tokenizer_checked = False


def ensure_nltk_tokenizer_data():
    """sumy needs NLTK's sentence tokenizer data. A fresh server (e.g. every
    Render deploy) does not have it, and without it sumy raises LookupError,
    which used to send every extractive summary to the 'first N sentences'
    fallback without anyone noticing. Download it once if missing."""
    global _nltk_tokenizer_checked
    if _nltk_tokenizer_checked:
        return
    import nltk
    for resource_path, package_name in [("tokenizers/punkt_tab", "punkt_tab"), ("tokenizers/punkt", "punkt")]:
        try:
            nltk.data.find(resource_path)
        except LookupError:
            logger.warning("NLTK '%s' data missing, downloading it now", package_name)
            try:
                nltk.download(package_name, quiet=True)
            except Exception as download_error:  # noqa: BLE001
                logger.error("Could not download NLTK '%s': %s", package_name, download_error)
    _nltk_tokenizer_checked = True


def summarize_extractive_fast(document_text, summary_depth):
    """LexRank algorithm via sumy. Instant, local, free."""
    sentence_count = 3 if summary_depth == "Executive Summary" else 7
    ensure_nltk_tokenizer_data()

    try:
        from sumy.parsers.plaintext import PlaintextParser
        from sumy.nlp.tokenizers import Tokenizer
        from sumy.summarizers.lex_rank import LexRankSummarizer

        parser = PlaintextParser.from_string(document_text, Tokenizer("english"))
        summarizer = LexRankSummarizer()
        sentences = summarizer(parser.document, sentence_count)
        result = " ".join(str(s) for s in sentences)
        if result.strip():
            return result
    except Exception as error:  # noqa: BLE001
        logger.warning("LexRank failed, using first-sentences fallback: %s", error)

    return _fallback_summary(document_text, sentence_count)


def summarize_extractive_premium(document_text, summary_depth):
    """LSA algorithm via sumy. Still instant and local, but uses a different
    (latent semantic analysis) approach that often picks more representative
    sentences than LexRank, especially on longer/denser documents."""
    sentence_count = 3 if summary_depth == "Executive Summary" else 7
    ensure_nltk_tokenizer_data()

    try:
        from sumy.parsers.plaintext import PlaintextParser
        from sumy.nlp.tokenizers import Tokenizer
        from sumy.summarizers.lsa import LsaSummarizer

        parser = PlaintextParser.from_string(document_text, Tokenizer("english"))
        summarizer = LsaSummarizer()
        sentences = summarizer(parser.document, sentence_count)
        result = " ".join(str(s) for s in sentences)
        if result.strip():
            return result
    except Exception as error:  # noqa: BLE001
        logger.warning("LSA failed, using first-sentences fallback: %s", error)

    return _fallback_summary(document_text, sentence_count)


def _fallback_summary(document_text, sentence_count):
    """Simple 'first N sentences' fallback if a sumy algorithm fails on an
    unusual document structure (rare, but happens on some PDFs)."""
    simple_sentences = split_into_sentences(document_text)
    return " ".join(simple_sentences[:sentence_count]) if simple_sentences else document_text[:500]


# ---------------------------------------------------------------------------
# API: Abstractive summarization via Gemini (fast — one small API call,
# not a heavy local model download like the old transformers approach)
# ---------------------------------------------------------------------------
def summarize_abstractive_gemini(document_text, summary_depth):
    """
    Asks Gemini to write a real abstractive summary in its own words.
    Falls back to the fast extractive summary if no API key is set or the
    call fails, so the app never breaks just because the network is down.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return summarize_extractive_fast(document_text, summary_depth)

    trimmed_text = document_text[:30000]

    if summary_depth == "Executive Summary":
        instructions = (
            "Provide a concise Executive Summary in 3 to 5 bullet points. "
            "Focus strictly on the high-level purpose, key parties, primary liability/financial obligation, and bottom-line outcome."
        )
    elif summary_depth in ["Detailed Clauses", "Detailed Analysis", "Detailed"]:
        instructions = (
            "Perform a comprehensive, deep-dive document review. Do NOT give a brief or shallow summary.\n"
            "Go through the ENTIRE text thoroughly and break down every key section and point using the following structure:\n\n"
            "1. **Core Overview**: 2 detailed paragraphs explaining the full scope and intent of the agreement.\n"
            "2. **Key Clauses & Provisions**: Bulleted list detailing major clauses, obligations, representations, and operational requirements.\n"
            "3. **Financials & Milestones**: List all payment schedules, fee arrangements, effective dates, and renewal terms.\n"
            "4. **Risks & Termination**: Outline termination conditions, liability caps, indemnities, and governing law."
        )
    else:  # Standard / Default
        instructions = (
            "Provide a balanced 2-3 paragraph summary covering the document scope, main obligations, key provisions, and governing terms."
        )

    prompt = f"{instructions}\n\nDocument Text:\n{trimmed_text}\n\nNote: Informational only, not formal legal advice." 

    try:
        return generate_with_fallback(prompt)
    except Exception as error:  # noqa: BLE001
        logger.error("Abstractive summary failed, using extractive fallback: %s", error)

    # Be explicit that the user is NOT looking at an AI-written summary.
    fallback_summary = summarize_extractive_fast(document_text, summary_depth)
    return (
        "[Note: the AI summary service was busy, so this is a key-sentence "
        "(extractive) summary instead.]\n\n" + fallback_summary
    )


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
# API: Risk reasoning (the ONE required external call, via Gemini)
# ---------------------------------------------------------------------------
def assess_risk_with_llm(clauses, jurisdiction):
    """
    Send the locally-found clauses to Gemini to assign severity and explain why.
    ONE call per document. Returns clauses enriched with severity + explanation.
    Falls back to a safe default if no API key is set or the call fails.
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
        "Return a list where each element is an object with 'index', 'severity', and 'reason'."
    )

    try:
        response_text = generate_with_fallback(prompt, expect_json=True)
        assessments = json.loads(response_text)
        if isinstance(assessments, dict):
            # Some models wrap the list, e.g. {"assessments": [...]}
            assessments = next((value for value in assessments.values() if isinstance(value, list)), [])

        allowed_severities = {"High", "Medium", "Low"}
        assessments_by_index = {
            int(item["index"]): item for item in assessments
            if isinstance(item, dict) and str(item.get("index", "")).isdigit()
        }
        for clause_position, clause in enumerate(clauses, start=1):
            assessment = assessments_by_index.get(clause_position, {})
            severity = str(assessment.get("severity", "")).strip().capitalize()
            clause["severity"] = severity if severity in allowed_severities else "Review"
            clause["explanation"] = assessment.get("reason") or USER_FACING_ASSESSMENT_ERROR

    except Exception as error:  # noqa: BLE001
        # Full technical detail goes to the server log (visible in Render's
        # Logs tab); the user gets a plain-language message instead.
        logger.error("Risk assessment failed: %s", error)
        for clause in clauses:
            clause["severity"] = "Review"
            clause["explanation"] = USER_FACING_ASSESSMENT_ERROR

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
    extraction_mode = options.get("extraction_mode", "Extractive - Fast")
    risk_level = options.get("risk_level", "High & Medium Risk")
    jurisdiction = options.get("jurisdiction", "General Commercial")

    if not document_text:
        return "No readable text could be extracted from this document.", []

    if extraction_mode == "Abstractive - Premium (AI)":
        summary = summarize_abstractive_gemini(document_text, summary_depth)
    elif extraction_mode == "Extractive - Premium":
        summary = summarize_extractive_premium(document_text, summary_depth)
    else:
        summary = summarize_extractive_fast(document_text, summary_depth)

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
        "extraction_mode": "Extractive - Fast",
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
