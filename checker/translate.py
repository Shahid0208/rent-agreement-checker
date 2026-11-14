"""Translate a checked report into Hindi, Telugu or Kannada.

Why a separate step: the report is produced and checked in English (the version
our evals measure), then translated. Translation gets its own checks, and any
part that fails them stays in English, so a user never sees an unchecked line.

What is never translated:
- quotes from the agreement (they must stay word for word, and are checked against it)
- category ids, risk levels (the app shows translated labels for those)

Checks on every translated line:
- no legal-verdict wording in the target language (BANNED_INDIC) or in English
- every number in the English line (rent, days, %) still appears in the translation
- no personal data
"""
import json
import re

from . import guardrails, llm
from .privacy import find_pii

LANGUAGES = {"English": "en", "हिन्दी (Hindi)": "hi", "తెలుగు (Telugu)": "te", "ಕನ್ನಡ (Kannada)": "kn"}
LANGUAGE_NAMES = {"hi": "Hindi", "te": "Telugu", "kn": "Kannada"}

# Legal-verdict wording, matched as substrings (\b does not work for Indic scripts).
# Drafted by the team with AI help. TO VERIFY with a native speaker of each language.
BANNED_INDIC = {
    "hi": ["अवैध", "गैरकानूनी", "ग़ैरकानूनी", "गैर-कानूनी", "ग़ैर-क़ानूनी", "गैर कानूनी", "अमान्य",
           "कानून के खिलाफ", "क़ानून के ख़िलाफ़", "कानून का उल्लंघन", "लागू नहीं किया जा सकता",
           "बाध्यकारी नहीं", "भुगतान नहीं करना है", "भुगतान करने की जरूरत नहीं", "भुगतान करने की ज़रूरत नहीं",
           "भुगतान करने से मना कर सकते", "पैसे देने की जरूरत नहीं", "पैसे देने की ज़रूरत नहीं"],
    "te": ["చట్టవిరుద్ధ", "చట్ట విరుద్ధ", "అక్రమ", "చెల్లుబాటు కాదు", "చెల్లుబాటు కాని", "చట్టానికి వ్యతిరేక",
           "అమలు చేయలేరు", "అమలు చేయబడదు", "కట్టుబడి ఉండనవసరం లేదు", "చెల్లించనవసరం లేదు",
           "చెల్లించాల్సిన అవసరం లేదు", "చెల్లించడానికి నిరాకరించవచ్చు"],
    "kn": ["ಕಾನೂನುಬಾಹಿರ", "ಕಾನೂನು ಬಾಹಿರ", "ಅಕ್ರಮ", "ಅಮಾನ್ಯ", "ಕಾನೂನಿಗೆ ವಿರುದ್ಧ", "ಜಾರಿಗೊಳಿಸಲಾಗುವುದಿಲ್ಲ",
           "ಬದ್ಧವಾಗಿಲ್ಲ", "ಪಾವತಿಸಬೇಕಾಗಿಲ್ಲ", "ಪಾವತಿಸುವ ಅಗತ್ಯವಿಲ್ಲ", "ಪಾವತಿಸಲು ನಿರಾಕರಿಸಬಹುದು"],
}

SYSTEM = """You translate a rent agreement report for a first-time renter in India into {language}.

Rules:
1. Translate every string value in the JSON you receive into simple, everyday {language} that a person with no legal background understands. Prefer common words over formal or Sanskritised ones; keep common English words people actually use (rent, deposit, lock-in, notice, maintenance) in brackets after the {language} word the first time if that helps.
2. Keep the exact same JSON structure: same keys, same number of items, same order. Do not add, remove or merge items.
3. Copy every number exactly as written, in Western digits (18,000 stays 18,000; 5% stays 5%; 30 days → 30 + the {language} word for days). Keep "Rs" or "₹" with amounts.
4. Keep tags such as [NAME], [ADDRESS], [AADHAAR] exactly as they are.
5. Do not add anything: no new advice, no opinions, and never say a clause is illegal, invalid, unenforceable or that the tenant does not have to pay.
6. Questions must stay polite questions the tenant can ask the landlord.

Reply with ONLY the translated JSON object."""

NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(s: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUM_RE.findall(s or "")}


def _verdict(lang: str, text: str) -> str | None:
    for w in BANNED_INDIC.get(lang, []):
        if w in (text or ""):
            return w
    return guardrails._verdict(text)


def _line_ok(lang: str, english: str, translated) -> tuple[bool, str]:
    if not isinstance(translated, str) or not translated.strip():
        return False, "empty"
    if english.strip() and translated.strip() == english.strip() and re.search(r"[A-Za-z]{4}", english):
        return False, "not translated"
    missing = _numbers(english) - _numbers(translated)
    if missing:
        return False, f"numbers changed: {sorted(missing)}"
    v = _verdict(lang, translated)
    if v:
        return False, f"legal verdict wording: {v}"
    if find_pii(translated):
        return False, "personal data"
    return True, ""


def _payload(report: guardrails.Report) -> dict:
    return {
        "key_terms": report.key_terms.model_dump(),
        "summary": report.summary,
        "flags": [{"title": f.title, "explanation": f.explanation, "why_it_matters": f.why_it_matters,
                   "question": f.question} for f in report.flags],
        "missing": [{"what": m.what, "question": m.question} for m in report.missing],
    }


def translate_report(report: guardrails.Report, lang: str, model: str = llm.DEFAULT_MODEL,
                     api_key: str | None = None) -> tuple[guardrails.Report, dict]:
    """Return (translated report, log). Lines that fail a check stay in English.

    log: {"lang", "lines", "translated", "kept_english": [{"field", "reason"}], "usage"}
    """
    out = report.model_copy(deep=True)
    log = {"lang": lang, "lines": 0, "translated": 0, "kept_english": [], "usage": {}}
    if lang == "en":
        return out, log
    source = _payload(report)
    system = SYSTEM.format(language=LANGUAGE_NAMES[lang])
    try:
        raw, usage = llm.call(system, json.dumps(source, ensure_ascii=False), model=model, api_key=api_key)
        log["usage"] = usage
        data = guardrails.parse_json(raw)
    except Exception as e:  # whole translation failed: show English, say so
        log["error"] = str(e)[:300]
        log["kept_english"].append({"field": "all", "reason": "translation failed"})
        return out, log

    def apply(obj, field, english, translated, label):
        log["lines"] += 1
        ok, why = _line_ok(lang, english, translated)
        if ok:
            setattr(obj, field, translated)
            log["translated"] += 1
        elif english.strip():
            log["kept_english"].append({"field": label, "reason": why})

    for k, v in source["key_terms"].items():
        apply(out.key_terms, k, v, (data.get("key_terms") or {}).get(k), f"key_terms.{k}")
    apply(out, "summary", source["summary"], data.get("summary"), "summary")
    for name, items, keys in (("flags", out.flags, ("title", "explanation", "why_it_matters", "question")),
                              ("missing", out.missing, ("what", "question"))):
        got = data.get(name) or []
        if len(got) != len(items):
            log["kept_english"].append({"field": name, "reason": f"item count changed ({len(items)} → {len(got)})"})
            continue
        for i, (obj, tr) in enumerate(zip(items, got)):
            for k in keys:
                apply(obj, k, getattr(report, name)[i].__getattribute__(k), (tr or {}).get(k), f"{name}[{i}].{k}")
    return out, log
