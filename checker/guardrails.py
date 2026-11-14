"""Code checks that run on every AI answer before the user sees it.

- The answer must match our schema.
- Every quote must exist word for word in the (masked) agreement.
- No legal verdicts ("illegal", "you don't have to pay", ...).
- No personal data in the output.

Anything that fails is dropped and recorded, never shown.
"""
import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .privacy import find_pii


class Quote(BaseModel):
    clause_ref: str = "unnumbered"
    text: str


class Flag(BaseModel):
    category: str
    risk: Literal["High", "Medium"]
    title: str
    quotes: list[Quote] = Field(min_length=1)
    explanation: str
    why_it_matters: str = ""
    basis: str = ""
    question: str


class Missing(BaseModel):
    category: str
    what: str
    question: str


class KeyTerms(BaseModel):
    rent: str = "not mentioned"
    deposit: str = "not mentioned"
    lock_in: str = "not mentioned"
    notice_period: str = "not mentioned"
    rent_increase: str = "not mentioned"
    maintenance: str = "not mentioned"
    term: str = "not mentioned"


class Report(BaseModel):
    key_terms: KeyTerms = KeyTerms()
    flags: list[Flag] = []
    missing: list[Missing] = []
    injection_detected: bool = False
    summary: str = ""


BANNED = [
    r"\billegal\b", r"\bunlawful\b", r"\bunenforceable\b", r"\bnot enforceable\b",
    r"\binvalid\b", r"\bnot (?:legally )?valid\b", r"\bnull and void\b", r"\bis void\b",
    r"\bnot (?:legally )?binding\b", r"\bagainst the law\b", r"\bviolat\w* (?:the )?(?:law|act)\b",
    r"\byou (?:do not|don't|dont) (?:have|need) to pay\b", r"\byou can refuse\b",
    r"\byou (?:do not|don't|dont) have to (?:follow|obey|comply)\b",
]
BANNED_RE = re.compile("|".join(BANNED), re.IGNORECASE)

_QUOTE_MAP = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                            "\u2013": "-", "\u2014": "-", "\u00a0": " "})


def normalise(s: str) -> str:
    """Ignore differences in spacing, line breaks, curly quotes and case."""
    return re.sub(r"\s+", " ", s.translate(_QUOTE_MAP)).strip().lower()


def quote_ok(quote: str, source_norm: str) -> bool:
    q = normalise(quote)
    return len(q) >= 8 and "..." not in q and "…" not in q and q in source_norm


def parse_json(raw: str) -> dict:
    """Pull the JSON object out of the model's reply."""
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in reply")
    return json.loads(s[start:end + 1])


def validate(raw: str) -> Report:
    try:
        return Report.model_validate(parse_json(raw))
    except (ValueError, ValidationError) as e:
        raise ValueError(f"Invalid reply: {e}") from e


def _verdict(*texts: str) -> str | None:
    for t in texts:
        m = BANNED_RE.search(t or "")
        if m:
            return m.group(0)
    return None


def check(report: Report, masked_text: str, valid_categories: set[str]) -> tuple[Report, dict]:
    """Apply all code checks. Returns the cleaned report and a log for evals."""
    source = normalise(masked_text)
    log = {"quotes_total": 0, "quotes_ok": 0, "dropped": [], "verdicts": 0, "pii_in_output": 0}

    kept: list[Flag] = []
    for f in report.flags:
        if f.category not in valid_categories:
            f.category = "other"
        bad_quotes = []
        for q in f.quotes:
            log["quotes_total"] += 1
            if quote_ok(q.text, source):
                log["quotes_ok"] += 1
            else:
                bad_quotes.append(q.text)
        verdict = _verdict(f.title, f.explanation, f.why_it_matters, f.question)
        pii = find_pii(" ".join([f.title, f.explanation, f.why_it_matters, f.question]))
        if verdict:
            log["verdicts"] += 1
        if pii:
            log["pii_in_output"] += 1
        if bad_quotes:
            log["dropped"].append({"title": f.title, "reason": "quote not found in agreement", "detail": bad_quotes})
        elif verdict:
            log["dropped"].append({"title": f.title, "reason": "legal verdict wording", "detail": verdict})
        elif pii:
            log["dropped"].append({"title": f.title, "reason": "personal data in output", "detail": pii})
        else:
            kept.append(f)

    kept_missing: list[Missing] = []
    for m in report.missing:
        if m.category not in valid_categories:
            m.category = "other"
        verdict = _verdict(m.what, m.question)
        pii = find_pii(" ".join([m.what, m.question]))
        if verdict:
            log["verdicts"] += 1
            log["dropped"].append({"title": m.what, "reason": "legal verdict wording", "detail": verdict})
            continue
        if pii:
            log["pii_in_output"] += 1
            log["dropped"].append({"title": m.what, "reason": "personal data in output", "detail": pii})
            continue
        kept_missing.append(m)

    if _verdict(report.summary):
        log["verdicts"] += 1
        report.summary = ""
    elif find_pii(report.summary):
        log["pii_in_output"] += 1
        report.summary = ""
    for field, value in report.key_terms.model_dump().items():
        if find_pii(value):
            log["pii_in_output"] += 1
            setattr(report.key_terms, field, "[hidden]")

    order = {"High": 0, "Medium": 1}
    report.flags = sorted(kept, key=lambda f: order[f.risk])
    report.missing = kept_missing
    return report, log
