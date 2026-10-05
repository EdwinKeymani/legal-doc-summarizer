"""
Key provisions and key terms, found with transparent keyword/pattern rules.

Covers proposal requirement 4.2: "identify obligations, liabilities, rights,
and critical dates within the text".

  extract_provisions(text) -> who must / must not / may do what, and who is liable
  extract_key_terms(text)  -> dates, deadlines and periods, money amounts, rates

Everything here is local and deterministic: no AI, no network, milliseconds
per document. That makes it cheap to run on demand from the stored text, and
easy to explain in a viva: every result can be traced to the rule that found
it. The trade-off is that unusual wording can be missed or misclassified;
the report says so.
"""

import re
from datetime import date

# ---------------------------------------------------------------------------
# Sentence splitting
# ---------------------------------------------------------------------------
# Abbreviations that end with a full stop but do not end a sentence
_ABBREVIATIONS = r"(?<!\bLtd\.)(?<!\bCo\.)(?<!\bInc\.)(?<!\bNo\.)(?<!\bNos\.)(?<!\bArt\.)(?<!\bSec\.)(?<!\bCl\.)(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\bvs\.)(?<!\bMr\.)(?<!\bMrs\.)(?<!\bMs\.)(?<!\bDr\.)(?<!\bSt\.)(?<!\bPara\.)(?<!\bp\.)(?<!\bRef\.)(?<!\bReg\.)(?<!\bcf\.)"
_SENTENCE_BOUNDARY = re.compile(_ABBREVIATIONS + r"(?<=[.;!?])\s+(?=[\"'(\[]?[A-Z0-9])|\n\s*\n|\n(?=\s*(?:\d+(?:\.\d+)*[.)]?|\([a-z0-9]+\)|[A-Z]{3,})\s)")


_SMALL_WORDS = {"of", "and", "the", "to", "for", "in", "on", "a", "an", "&", "or", "by", "with"}


def _is_heading_line(line):
    """Short line, no closing punctuation, mostly Capitalised Words:
    '1.2 Order of Precedence', 'TERMINATION', 'Direct Liability Cap'."""
    stripped = re.sub(r"^\s*(?:\d+(?:\.\d+)*\.?|\([a-z0-9]+\)|[A-Z]\.)\s*", "", line).strip()
    if not stripped or len(stripped) > 70 or stripped[-1] in ".;:,":
        return False
    words = [word for word in re.findall(r"[A-Za-z][\w'&-]*", stripped) if word.lower() not in _SMALL_WORDS]
    return bool(words) and sum(word[0].isupper() for word in words) / len(words) >= 0.75


def _separate_headings(text):
    """Put a paragraph break after heading lines so a heading never fuses
    with the sentence below it. Ordinary line breaks are left alone, because
    PDF text wraps mid-sentence."""
    output_lines = []
    for line in (text or "").splitlines():
        output_lines.append(line)
        if _is_heading_line(line):
            output_lines.append("")
    return "\n".join(output_lines)


def split_sentences(text):
    """Split contract text into sentences. Also breaks on blank lines, after
    headings, and before new numbered clauses ('12.1 ', '(a) '), which
    DOCX/PDF text often contains without a full stop before them."""
    pieces = _SENTENCE_BOUNDARY.split(_separate_headings(text))
    sentences = []
    for piece in pieces:
        cleaned = re.sub(r"\s+", " ", piece or "").strip()
        if len(cleaned) >= 20:
            sentences.append(cleaned)
    return sentences


def _shorten(text, limit=320):
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


# ---------------------------------------------------------------------------
# Provisions: obligations, prohibitions, rights, liabilities
# ---------------------------------------------------------------------------
PROVISION_TYPES = ["Obligation", "Prohibition", "Right", "Liability"]

PROVISION_PLURALS = {
    "Obligation": "Obligations",
    "Prohibition": "Prohibitions",
    "Right": "Rights",
    "Liability": "Liabilities",
}

PROVISION_DESCRIPTIONS = {
    "Obligation": "Something a party must do",
    "Prohibition": "Something a party must not do",
    "Right": "Something a party may do or is entitled to",
    "Liability": "Who is responsible for losses, damages or indemnities",
}

# Checked in this order; the first match decides the type. Liability goes
# first because "shall indemnify" is better read as a liability than a plain
# obligation; strong rights go before obligations because "shall be entitled
# to" is a right despite the "shall".
_LIABILITY_PATTERN = re.compile(r"\b(liable|liability|liabilities|indemnif\w*|hold\s+harmless|damages|losses)\b", re.I)
_PROHIBITION_PATTERN = re.compile(
    r"\b(shall\s+not|must\s+not|may\s+not|will\s+not|cannot|can\s+not|shall\s+refrain|agrees?\s+not\s+to|"
    r"(?:is|are)\s+(?:not\s+permitted|prohibited|forbidden)|"
    r"neither\s+part(?:y|ies)\s+(?:may|shall|will|must))\b", re.I)  # "Neither party may X" = nobody may X
_STRONG_RIGHT_PATTERN = re.compile(
    r"\b((?:is|are|shall\s+be)\s+entitled\s+to|(?:has|have|shall\s+have)\s+the\s+right\s+to|reserves?\s+the\s+right)\b", re.I)
_OBLIGATION_PATTERN = re.compile(
    r"\b(shall|must|agrees?\s+to|(?:is|are)\s+required\s+to|undertakes?\s+to|(?:is|are)\s+responsible\s+for|"
    r"(?:is|are)\s+obliged\s+to|covenants?\s+to)\b", re.I)
_WEAK_RIGHT_PATTERN = re.compile(
    r"\b(may|(?:is|are)\s+permitted\s+to|at\s+(?:its|their|his|her)\s+(?:sole\s+)?(?:option|discretion))\b", re.I)

# Definitions ("'Services' means...") are not provisions
_DEFINITION_PATTERN = re.compile(
    r"\b(shall\s+mean|means|shall\s+have\s+the\s+meaning|is\s+defined\s+as|(?:be\s+)?referred\s+to\s+as)\b", re.I)

# The grammatical subject is usually the party: "The Client shall ...",
# "Either party may ...", "Kamau & Associates agrees to ...".
_PARTY_AT_END = re.compile(
    r"((?:[Tt]he|[Ee]ach|[Ee]ither|[Nn]either|[Bb]oth|[Aa]ny)\s+(?:[Pp]art(?:y|ies)|[A-Z][\w&'.-]*(?:\s+(?:&\s+)?[A-Z][\w&'.-]*){0,3})"
    r"|[A-Z][\w&'.-]*(?:\s+(?:&\s+)?[A-Z][\w&'.-]*){0,4})\s*,?\s*$")

# Subjects that are the document itself, not a party
_NOT_A_PARTY = {
    "agreement", "the agreement", "this agreement", "contract", "the contract", "this contract",
    "clause", "this clause", "section", "this section", "schedule", "lease", "this lease", "deed", "this deed",
    "it", "they", "such", "any", "the", "notice", "payment",
}


# Returned by _find_party when the subject is the contract itself
# ("This Agreement shall commence..."): a term, not a party's duty.
DOCUMENT_ITSELF = "__document__"

_POSSESSIVE_PARTY = re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+)?((?:[Tt]he\s+|[Ee]ach\s+)?[A-Z][\w&-]*(?:\s+[A-Z][\w&-]*){0,2})['\u2019]s\b")


def _find_party(sentence, cue_match):
    """Return the party named just before the modal verb, DOCUMENT_ITSELF,
    or None when no party can be identified."""
    text_before_cue = sentence[: cue_match.start()]
    text_before_cue = re.sub(r"\([^)]*\)\s*$", "", text_before_cue).rstrip(" ,")  # drop "(the 'Client')"
    search_window = text_before_cue[-70:]
    party_match = _PARTY_AT_END.search(search_window)
    party = None
    if party_match:
        words_before_party = search_window[: party_match.start()]
        # "performance of Services shall": Services is an object, not the subject
        if not re.search(r"\b(of|to|for|by|with|under|in|on|from|between|against|than)\s*$", words_before_party, re.I):
            party = re.sub(r"\s+", " ", party_match.group(1)).strip(" ,'\"")
            party = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", party)  # leading clause number
            # Heading words fused in front: "SOW Requirements Each SOW" -> "Each SOW"
            determiner_positions = [match.start() for match in re.finditer(r"\b(?:The|Each|Either|Neither|Both|Any)\s", party)]
            if determiner_positions:
                party = party[determiner_positions[-1]:]
            party = re.sub(r"^(?:[A-Z]{2,}\s+)+(?=[A-Z][a-z])", "", party)  # "TERMINATION Either Party"
    if party and party.lower() in _NOT_A_PARTY:
        party = DOCUMENT_ITSELF
    if party in (None, DOCUMENT_ITSELF):
        # "The Provider's total liability ... shall" -> The Provider
        possessive_match = _POSSESSIVE_PARTY.search(sentence)
        if possessive_match:
            return possessive_match.group(1)
        return party
    if len(party) < 2:
        return None
    if party.split()[0].lower() in ("the", "each", "either", "neither", "both", "any"):
        party = party[0].upper() + party[1:]
    return party


def _looks_like_heading(sentence):
    """'6. LIABILITY AND INDEMNITY', '8.2 Direct Liability Cap' and similar."""
    letters = [character for character in sentence if character.isalpha()]
    if letters and sum(character.isupper() for character in letters) / len(letters) > 0.8:
        return True
    return _is_heading_line(sentence)


def _classify_sentence(sentence):
    """Return (provision_type, cue_match) or (None, None)."""
    if _DEFINITION_PATTERN.search(sentence):
        return None, None
    for provision_type, pattern in (
        ("Liability", _LIABILITY_PATTERN),
        ("Prohibition", _PROHIBITION_PATTERN),
        ("Right", _STRONG_RIGHT_PATTERN),
        ("Obligation", _OBLIGATION_PATTERN),
        ("Right", _WEAK_RIGHT_PATTERN),
    ):
        cue_match = pattern.search(sentence)
        if cue_match:
            return provision_type, cue_match
    return None, None


def extract_provisions(text, maximum_per_type=40):
    """
    Returns a dict: {provision_type: [{"party": str|None, "text": str}, ...]}
    in document order, de-duplicated, capped per type.
    """
    found = {provision_type: [] for provision_type in PROVISION_TYPES}
    seen_sentences = set()
    for sentence in split_sentences(text):
        if len(sentence) > 900 or _looks_like_heading(sentence):
            continue  # run-on blocks (tables) and section headings are not provisions
        normalized = sentence.lower()
        if normalized in seen_sentences:
            continue
        provision_type, cue_match = _classify_sentence(sentence)
        if not provision_type or len(found[provision_type]) >= maximum_per_type:
            continue
        # For liability sentences the party is usually before the main verb,
        # which may be a "shall" rather than the "liable" keyword itself.
        party_cue = cue_match
        if provision_type == "Liability":
            # The subject sits before the main verb, wherever "liable"/"losses" appears
            modal_match = re.search(r"\b(shall|must|will|agrees?|undertakes?|(?:is|are)\s+(?:liable|responsible))\b", sentence, re.I)
            party_cue = modal_match or cue_match
        party = _find_party(sentence, party_cue)
        if party is None and re.match(r"neither\s+part", cue_match.group(0), re.I):
            party = "Neither party"
        if party == DOCUMENT_ITSELF:
            if provision_type != "Liability":
                continue  # a term of the contract; its dates and periods are in key terms
            party = None
        found[provision_type].append({"party": party, "text": _shorten(sentence)})
        seen_sentences.add(normalized)
    return found


UNIDENTIFIED_PARTY_LABEL = "Party not identified"


def _party_key(label):
    """'The Provider', 'Provider' and 'the provider' are one party."""
    return re.sub(r"^the\s+", "", label.strip().lower())


def group_by_party(provisions):
    """[{"party", "text"}] -> [(party_label, [texts])], biggest named group
    first, unidentified last. The label shown is the form used most often."""
    groups = {}
    for provision in provisions:
        label = provision["party"] or UNIDENTIFIED_PARTY_LABEL
        group = groups.setdefault(_party_key(label), {"labels": {}, "texts": []})
        group["labels"][label] = group["labels"].get(label, 0) + 1
        group["texts"].append(provision["text"])
    result = [(max(group["labels"], key=group["labels"].get), group["texts"]) for group in groups.values()]
    return sorted(result, key=lambda group: (group[0] == UNIDENTIFIED_PARTY_LABEL, -len(group[1])))


# ---------------------------------------------------------------------------
# Key terms: dates, deadlines/periods, amounts, rates
# ---------------------------------------------------------------------------
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_NAME = r"(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"

_DATE_PATTERNS = [
    # 3rd day of March 2026 / 3 day of March, 2026
    (re.compile(_DAY + r"\s+day\s+of\s+(" + _MONTH_NAME + r"),?\s+(\d{4})", re.I), "dmy"),
    # 1st April 2026 / 1 April, 2026
    (re.compile(r"\b" + _DAY + r"\s+(" + _MONTH_NAME + r"),?\s+(\d{4})\b", re.I), "dmy"),
    # April 1, 2026 / April 1st 2026
    (re.compile(r"\b(" + _MONTH_NAME + r")\s+" + _DAY + r",?\s+(\d{4})\b", re.I), "mdy"),
    # 2026-04-01
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "iso"),
    # 01/04/2026 or 1.4.2026 (Kenyan usage: day first)
    (re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b"), "numeric"),
]

_NUMBER_WORDS = (r"(?:(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:-(?:one|two|three|four|five|six|seven|eight|nine))?|"
                 r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
                 r"seventeen|eighteen|nineteen|(?:one\s+)?hundred(?:\s+and\s+eighty)?)")
_PERIOD_PATTERN = re.compile(
    r"\b(?:(?:within|not\s+later\s+than|no\s+later\s+than|at\s+least|for\s+a\s+(?:period|term)\s+of|"
    r"prior\s+to|after|before|of|for)\s+)?"
    r"(?:" + _NUMBER_WORDS + r"\s*\(\d{1,4}\)|\(?\d{1,4}\)?|" + _NUMBER_WORDS + r")\s+"
    r"(?:business\s+|working\s+|calendar\s+|clear\s+|consecutive\s+)?"
    r"(?:hours?|days?|weeks?|months?|years?)"
    r"(?:['\u2019]?\s*(?:prior\s+)?(?:written\s+)?notice)?", re.I)

_AMOUNT_PATTERN = re.compile(
    r"(?:\b(?:KES|KShs?\.?|Kshs?\.?|USD|US\$|EUR|GBP)\s?|[$\u20ac\u00a3]\s?)\d[\d,]*(?:\.\d{1,2})?(?:\s?(?:million|billion|m|bn)\b)?"
    r"|\b\d[\d,]*(?:\.\d{1,2})?\s?(?:Kenya(?:n)?\s+Shillings|shillings|US\s+dollars|dollars)\b", re.I)

_RATE_PATTERN = re.compile(
    r"\b\d{1,3}(?:\.\d+)?\s?(?:%|per\s?cent\b|percent\b)"
    r"|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|twenty-five|fifty)\s+(?:per\s?cent|percent)\b", re.I)

KEY_TERM_TYPES = ["Date", "Deadline / Period", "Amount", "Rate"]


def _to_date(match, layout):
    """Turn a regex match into a datetime.date, or None if it is not real."""
    try:
        if layout == "dmy":
            day, month_name, year = match.group(1), match.group(2), match.group(3)
            month = _MONTHS[month_name.lower().rstrip(".")]
        elif layout == "mdy":
            month_name, day, year = match.group(1), match.group(2), match.group(3)
            month = _MONTHS[month_name.lower().rstrip(".")]
        elif layout == "iso":
            year, month, day = match.group(1), match.group(2), match.group(3)
        else:  # numeric, day first
            day, month, year = match.group(1), match.group(2), match.group(3)
        return date(int(year), int(month), int(day))
    except (ValueError, KeyError):
        return None


def extract_key_terms(text, maximum_per_type=40):
    """
    Returns {term_type: [{"value": str, "context": str, "date": date|None}]}.
    Dates are sorted chronologically; everything else stays in document order.
    """
    found = {term_type: [] for term_type in KEY_TERM_TYPES}
    seen = {term_type: set() for term_type in KEY_TERM_TYPES}

    def add(term_type, value, context, parsed_date=None):
        key = (value.lower(), context.lower()[:80])
        if key in seen[term_type] or len(found[term_type]) >= maximum_per_type:
            return
        seen[term_type].add(key)
        found[term_type].append({"value": value, "context": _shorten(context, 240), "date": parsed_date})

    for sentence in split_sentences(text):
        if len(sentence) > 900:
            continue
        date_spans = []
        for pattern, layout in _DATE_PATTERNS:
            for match in pattern.finditer(sentence):
                if any(start <= match.start() < end for start, end in date_spans):
                    continue  # already captured by a more specific pattern
                parsed_date = _to_date(match, layout)
                if parsed_date:
                    date_spans.append(match.span())
                    add("Date", match.group(0).strip(" ,"), sentence, parsed_date)
        for match in _PERIOD_PATTERN.finditer(sentence):
            value = re.sub(r"\s+", " ", match.group(0)).strip()
            if re.match(r"^(?:of|for|after|before)\s", value, re.I) and not re.search(r"notice|period|term", value, re.I):
                value = value.split(" ", 1)[1]  # "of 30 days" -> "30 days"
            add("Deadline / Period", value, sentence)
        for match in _AMOUNT_PATTERN.finditer(sentence):
            add("Amount", re.sub(r"\s+", " ", match.group(0)).strip(), sentence)
        for match in _RATE_PATTERN.finditer(sentence):
            add("Rate", re.sub(r"\s+", " ", match.group(0)).strip(), sentence)

    found["Date"].sort(key=lambda item: item["date"])
    return found


def summarize_counts(provisions, key_terms):
    """Small dict of totals, used for headings and the export."""
    return {
        "provisions": {provision_type: len(items) for provision_type, items in provisions.items()},
        "key_terms": {term_type: len(items) for term_type, items in key_terms.items()},
    }


# ---------------------------------------------------------------------------
# Does this look like a contract?
# ---------------------------------------------------------------------------
# The clause rules, risk prompt and provision finder are designed for
# contracts. Any document can be uploaded, so the report warns when the text
# shows too few contract signals.
#
# Thresholds were measured, not guessed. On three real contracts versus six
# other documents (lecture notes, a project proposal, reports, exam papers):
#   party references per 1,000 words   contracts 38-59   others  0-5
#   "shall"/"must" per 1,000 words     contracts 18-37   others  0-7
#   different contract clause terms    contracts 5-8     others  0-4
#   calls itself an agreement early    contracts always  others  never
# Each threshold sits in the gap between the two groups; 3 of 4 must be met.
# (Numbered clauses were tested and dropped: proposals and reports have them too.)
CONTRACT_SIGNALS_REQUIRED = 3

_AGREEMENT_TITLE = re.compile(
    r"\b(agreement|contract|lease|tenancy|deed|memorandum\s+of\s+understanding|terms\s+(?:and|&)\s+conditions|"
    r"letter\s+of\s+(?:offer|appointment)|non-disclosure)\b", re.I)
_PARTY_REFERENCE = re.compile(
    r"\b(the\s+parties|either\s+party|each\s+party|neither\s+party|both\s+parties|party\s+[A-B]|"
    r"(?:the\s+)?(?:client|provider|supplier|landlord|tenant|lessor|lessee|employer|employee|licensor|licensee|"
    r"buyer|seller|contractor|service\s+provider))\b", re.I)
_CLAUSE_TERMS = {
    "governing law": re.compile(r"\bgoverned\s+by|governing\s+law\b", re.I),
    "termination": re.compile(r"\bterminat\w+", re.I),
    "indemnity": re.compile(r"\bindemnif\w+|hold\s+harmless", re.I),
    "confidentiality": re.compile(r"\bconfidential\w*", re.I),
    "breach": re.compile(r"\bbreach\w*", re.I),
    "liability": re.compile(r"\bliab(?:le|ility)\b", re.I),
    "payment": re.compile(r"\b(?:invoice|payable|fees?)\b", re.I),
    "boilerplate": re.compile(r"\bin\s+witness\s+whereof|hereinafter|force\s+majeure|entire\s+agreement", re.I),
}


def contract_signals(text):
    """Return (looks_like_contract, signals_met). Pure counting, no AI."""
    text = text or ""
    words = max(1, len(text.split()))
    per_thousand_words = lambda count: count / words * 1000
    clause_terms_found = [name for name, pattern in _CLAUSE_TERMS.items() if pattern.search(text)]

    signals_met = []
    if _AGREEMENT_TITLE.search(text[:1500]):
        signals_met.append("calls itself an agreement near the start")
    if per_thousand_words(len(_PARTY_REFERENCE.findall(text))) >= 10:
        signals_met.append("refers to the parties throughout")
    if per_thousand_words(len(re.findall(r"\b(?:shall|must)\b", text, re.I))) >= 10:
        signals_met.append("written as obligations (shall / must)")
    if len(clause_terms_found) >= 5:
        signals_met.append("contains typical contract clauses")
    return len(signals_met) >= CONTRACT_SIGNALS_REQUIRED, signals_met
