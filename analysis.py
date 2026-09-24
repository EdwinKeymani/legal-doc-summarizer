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


# ---------------------------------------------------------------------------
# LOCAL: Extractive summarization (fast, no model download, no API cost)
# ---------------------------------------------------------------------------
def summarize_extractive_fast(document_text, summary_depth):
    """LexRank algorithm via sumy. Instant, local, free."""
    sentence_count = 3 if summary_depth == "Executive Summary" else 7

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
    except Exception:
        pass

    return _fallback_summary(document_text, sentence_count)


def summarize_extractive_premium(document_text, summary_depth):
    """LSA algorithm via sumy. Still instant and local, but uses a different
    (latent semantic analysis) approach that often picks more representative
    sentences than LexRank, especially on longer/denser documents."""
    sentence_count = 3 if summary_depth == "Executive Summary" else 7

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
    except Exception:
        pass

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

    target_length = "3-4 sentences" if summary_depth == "Executive Summary" else "8-10 sentences"

    trimmed_text = document_text[:15000]

    prompt = (
        "Summarize the following legal document in your own words, in "
        f"{target_length}. Focus on the key obligations, parties involved, "
        "and any notable terms. This is informational only, not legal advice.\n\n"
        f"Document:\n{trimmed_text}"
    )

    try:
        from google import genai

        client = genai.Client()
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
        )
        result = response.text.strip()
        if result:
            return result
    except Exception:
        pass

    return summarize_extractive_fast(document_text, summary_depth)


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
        import time
        from google import genai
        from google.genai import types

        client = genai.Client()

        response = None
        last_error = None
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model="gemini-3.6-flash",
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                    ),
                )
                break
            except Exception as retry_error:
                last_error = retry_error
                if attempt == 0:
                    time.sleep(1)

        if response is None:
            raise last_error

        assessments = json.loads(response.text)

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
