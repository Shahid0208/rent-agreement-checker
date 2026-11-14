"""Hide personal details before anything is sent to the AI.

Regex handles Aadhaar, PAN, phone, email, PIN codes, addresses (after
"residing at", "situated at", "Schedule:" ...), names after honorifics
(Mr, Smt, S/o ...) and names after role labels ("Lessee: Priya Nair").
If Microsoft Presidio is installed, it is also used to catch other person names.
Amounts like "Rs 1,00,000" or "18000" are deliberately left alone, and a
number right after Rs / INR / ₹ / Rupees is never treated as Aadhaar or phone.

In each pattern, the named group "v" is the part that gets replaced.
If a pattern has no "v" group, the whole match is replaced.
"""
import re

ROLE = r"(?:Lessee|Lessor|Tenant|Landlord|Owner|Licensee|Licensor)"
# Capitalised words that follow a role label but are not names
NOT_NAME = r"(?!(?:The|This|That|Rs|Rupees|INR|Mr|Mrs|Ms|Miss|Smt|Shri|Sri|Kum|Dr|Name|Address|Aadhaar|PAN)\b)"
# Text up to the end of the line or sentence (a full stop followed by a space or line end)
TO_END = r"(?P<v>[^\n;]{5,200}?)(?=\.?\s*$|\.\s|;)"
CITY = r"(?:Hyderabad|Secunderabad|Bengaluru|Bangalore|Mumbai|Pune|Chennai|Delhi|New Delhi|Gurugram|Noida|Kolkata)"

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("ADDRESS", re.compile(r"(?i)residing at\s+(?P<v>[^;]{5,200}?)(?=,?\s*(?:hereinafter|\(|;|\.\s|\.$|\n))")),
    ("ADDRESS", re.compile(rf"(?im)\b(?:situated at|located at|schedule\s*(?:of (?:the )?property)?\s*[:\-]|"
                           rf"(?:property|premises) address\s*[:\-]|address\s*[:\-])\s*{TO_END}")),
    ("AADHAAR", re.compile(r"(?<!\d)\d{4}[\s-]?\d{4}[\s-]?\d{4}(?!\d)")),
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("PINCODE", re.compile(r"(?i)\b(?:pin\s*code|pincode|pin)\s*[:\-]?\s*(?P<v>\d{6})\b")),
    ("PINCODE", re.compile(rf"(?i)\b{CITY}\s*[-–,]?\s*(?P<v>[1-9]\d{{5}})\b")),
    ("NAME", re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Smt|Shri|Sri|Kum|Dr)\.?\s+(?P<v>[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+\.?){0,3})")),
    ("NAME", re.compile(rf"\b{ROLE}(?:'s)?(?:\s+name)?\s*[:\-]\s*{NOT_NAME}(?P<v>[A-Z][a-z]+(?:\s+{NOT_NAME}[A-Z][a-z]+){{0,3}})")),
    ("NAME", re.compile(r"\b(?:S/o|D/o|W/o|C/o)\.?\s+(?P<v>[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+\.?){0,3})")),
]

# A number straight after a currency marker is an amount, not an ID or phone
CURRENCY_BEFORE = re.compile(r"(?i)(?:\brs\.?|\binr|₹|\brupees)\s*$")
NUMBER_TAGS = {"AADHAAR", "PHONE"}

# Tags checked on the AI's output (address and PIN are context-dependent, so skipped there)
OUTPUT_CHECK = {"AADHAAR", "PAN", "PHONE", "EMAIL"}

_presidio = None
try:  # optional extra
    from presidio_analyzer import AnalyzerEngine  # type: ignore
    _presidio = AnalyzerEngine()
except Exception:
    _presidio = None


def _spans(tag, regex, text):
    """Yield (start, end) of each part to hide, skipping amounts and already-masked text."""
    for m in regex.finditer(text):
        if "v" in regex.groupindex and m.group("v"):
            start, end = m.span("v")
        else:
            start, end = m.span()
        if "[" in text[start:end]:  # already masked
            continue
        if tag in NUMBER_TAGS and CURRENCY_BEFORE.search(text[max(0, start - 8):start]):
            continue
        yield start, end


def _apply(tag, regex, text, counts):
    out, last = [], 0
    for start, end in _spans(tag, regex, text):
        out.append(text[last:start])
        out.append(f"[{tag}]")
        last = end
        counts[tag] = counts.get(tag, 0) + 1
    out.append(text[last:])
    return "".join(out)


def mask(text: str) -> tuple[str, dict]:
    """Return (masked_text, counts per tag)."""
    counts: dict[str, int] = {}
    out = text
    for tag, regex in PATTERNS:
        out = _apply(tag, regex, out, counts)
    if _presidio is not None:
        results = _presidio.analyze(text=out, entities=["PERSON"], language="en")
        for r in sorted(results, key=lambda r: r.start, reverse=True):
            chunk = out[r.start:r.end]
            if "[" in chunk or r.score < 0.6:
                continue
            out = out[: r.start] + "[NAME]" + out[r.end:]
            counts["NAME"] = counts.get("NAME", 0) + 1
    return out, counts


def find_pii(text: str) -> list[str]:
    """List personal-data patterns still present in a piece of output text."""
    found = []
    for tag, regex in PATTERNS:
        if tag in OUTPUT_CHECK and tag not in found and next(_spans(tag, regex, text), None):
            found.append(tag)
    return found
