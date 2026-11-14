"""Rent Agreement Explainer: Team 4 MVP.

Run locally:   streamlit run app.py
Demo (no key): CHECKER_DEMO=1 streamlit run app.py
"""
import html
import json
import os
import uuid
from datetime import datetime

import streamlit as st

from checker import extract, llm
from checker.translate import LANGUAGES, translate_report
from checker.ui_text import TEXT
from checker.pipeline import CATEGORY_NAMES, CITY_NAMES, ROOT, run_check

st.set_page_config(page_title="Rent Agreement Explainer", page_icon="📄", layout="centered")

st.markdown(
    """
    <style>
      .block-container {max-width: 760px; padding-top: 2.5rem;}
      .risk {display:inline-block; padding:2px 10px; border-radius:999px; font-size:0.8rem;
             font-family: system-ui, sans-serif; font-weight:600; margin-right:8px;}
      .risk-high {background:#F6D9D3; color:#7A1E12;}
      .risk-medium {background:#F7E7C4; color:#6B4A07;}
      .flag-title {font-size:1.15rem; font-weight:600;}
      .clause {border-left:3px solid #C9BFA8; padding:6px 12px; margin:8px 0; background:#FFFDF8;
               font-style:italic;}
      .clause-ref {font-size:0.8rem; color:#6B6355; font-style:normal; display:block; margin-bottom:2px;}
      .term-label {font-size:0.85rem; color:#6B6355;}
      .term-value {font-size:1rem; margin-bottom:0.6rem;}
    </style>
    """,
    unsafe_allow_html=True,
)


def get_api_key(model):
    name = llm.key_name(model)  # GEMINI_API_KEY or ANTHROPIC_API_KEY
    try:
        return st.secrets[name]
    except Exception:
        return os.environ.get(name)


# Errors that mean "this model can't answer right now" (quota, billing, overload), not "bad input".
BUSY_MARKERS = ("429", "402", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "rate limit", "quota", "overloaded")


def is_busy_error(e: Exception) -> bool:
    msg = str(e)
    return any(m.lower() in msg.lower() for m in BUSY_MARKERS)


def check_with_backup(text, city, model):
    """Run the check; if the chosen model is out of quota or busy, try the other models that have a key.

    Returns (result, model_used, failed_models). Raises the last error if every model fails.
    """
    candidates = [model] + [m for m in llm.MODELS.values() if m != model and get_api_key(m)]
    failed, last_error = [], None
    for m in candidates:
        try:
            return run_check(text, city=city, model=m, api_key=get_api_key(m)), m, failed
        except Exception as e:
            last_error = e
            if not is_busy_error(e):
                raise
            failed.append(m)
    raise last_error


def save_opt_in(masked_text, city, result):
    folder = ROOT / "data" / "opt_in"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.json"
    payload = {"city": city, "masked_text": masked_text, "report": result["report"].model_dump()}
    (folder / name).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def load_sample():
    st.session_state["pasted"] = (ROOT / "samples" / "sample_agreement.txt").read_text(encoding="utf-8")


# ---------- Sidebar: for the team, not for users ----------
with st.sidebar:
    st.subheader("Team settings")
    model_label = st.selectbox("Model", list(llm.MODELS.keys()))
    model = llm.MODELS[model_label]
    if llm.demo_mode():
        st.info("Demo mode: answers come from a saved reply for the sample agreement.")
    st.caption("Usage and dropped items for the last check appear here.")

# ---------- Header ----------
st.title("Know what you're signing")
st.write(
    "Upload your rent agreement. We'll explain it in plain words, point out clauses worth asking "
    "about, list what's missing, and give you questions for your landlord."
)
st.caption("🤖 Your agreement is read by an AI model, which can make mistakes: always check the quoted "
           "clause in your own copy. This is not legal advice. We explain and flag; you decide. "
           "Names, Aadhaar, PAN and phone numbers are hidden before anything is sent to the AI.")

# ---------- Inputs ----------
col_city, col_lang = st.columns(2)
with col_city:
    city = st.selectbox("Which city is the flat in?", CITY_NAMES, index=CITY_NAMES.index("Other"))
with col_lang:
    # Can be changed after a check too: the report is translated on demand, not re-checked.
    lang_label = st.selectbox("Report language · भाषा · భాష · ಭಾಷೆ", list(LANGUAGES.keys()), key="lang")
lang = LANGUAGES[lang_label]
# A radio, not tabs: Streamlit can't tell which tab is open, so with tabs an old
# upload would silently win over freshly pasted text.
source = st.radio("How would you like to share it?", ["Upload a file", "Paste the text"],
                  horizontal=True, label_visibility="collapsed")
uploaded, pasted = [], ""
if source == "Upload a file":
    uploaded = st.file_uploader(
        "Rent agreement: PDF, Word (.docx), photos (JPG/PNG) or .txt",
        type=extract.UPLOAD_TYPES, accept_multiple_files=True,
        help="Took a photo of each page? Select all the photos together, in page order.")
else:
    pasted = st.text_area("Paste the agreement", height=200, key="pasted",
                          placeholder="Paste the full agreement here")
    st.button("Use a sample agreement", on_click=load_sample)

keep_copy = st.checkbox("Keep an anonymised copy to help us improve the tool (optional)", value=False)

if st.button("Check my agreement", type="primary", width="stretch"):
    text = ""
    if uploaded:
        parts, used_ocr, notes = [], False, []
        with st.spinner("Reading your file... (photos and scans take a few seconds per page)"):
            for f in uploaded:
                try:
                    part, ocr, file_notes = extract.file_to_text_with_notes(f.name, f.getvalue())
                except extract.UnreadableFile as e:
                    st.error(f"{f.name}: {e}")
                    st.stop()
                except Exception as e:  # unexpected: calm message for the user, details for the team
                    st.error(f"{f.name}: we couldn't read this file. Try a PDF, or paste the text instead.")
                    with st.sidebar:
                        st.exception(e)
                    st.stop()
                if not part.strip():
                    st.warning(f"{f.name}: no readable text found, so it was skipped.")
                parts.append(part)
                used_ocr = used_ocr or ocr
                notes += [f"{f.name}: {n}" for n in file_notes]
        for n in notes:
            st.info(n)
        text = "\n\n".join(p for p in parts if p.strip())
        if used_ocr and text.strip():
            st.info("We read the text from a photo or scan. If anything looks wrong in the report, "
                    "a clearer photo or a typed copy will work better.")
        elif not text.strip():
            st.error("We couldn't find any text in this file. If it's a photo, try a clearer, "
                     "well-lit picture, or paste the text instead.")
            st.stop()
    elif pasted.strip():
        text = pasted
    if not text.strip():
        st.warning("Upload a file first." if source == "Upload a file" else "Paste the agreement first.")
        st.stop()
    text, clipped = extract.clip(text)
    if clipped:
        st.info("This agreement is very long, so we checked the first part only.")
    if not llm.demo_mode() and not any(get_api_key(m) for m in llm.MODELS.values()):
        st.error("No API key found. Add GEMINI_API_KEY or GROQ_API_KEY to .streamlit/secrets.toml.")
        st.stop()
    with st.spinner("Reading your agreement. This usually takes under a minute..."):
        try:
            result, used_model, failed = check_with_backup(text, city, model)
        except Exception as e:  # show a calm message, keep details for the team
            if is_busy_error(e):
                st.error("The AI service is busy or has reached its limit right now. Please try again in a few minutes.")
            else:
                st.error("Something went wrong while checking. Please try again in a moment.")
            with st.sidebar:
                st.exception(e)
            st.stop()
    if failed:
        with st.sidebar:
            label = {v: k for k, v in llm.MODELS.items()}
            names = ", ".join(label.get(m, m) for m in failed)
            st.warning(f"Busy or out of quota: {names}. This check used {label.get(used_model, used_model)}.")
    st.session_state["result"] = result
    st.session_state["translations"] = {}  # cached per language for this result
    st.session_state["city_checked"] = city
    if keep_copy:
        save_opt_in(result["masked_text"], city, result)

# ---------- Results ----------
result = st.session_state.get("result")
if result:
    T = TEXT[lang]
    report, tlog = result["report"], None
    if lang != "en":
        cache = st.session_state.setdefault("translations", {})
        if lang not in cache:
            with st.spinner(T["translating"]):
                used = result["usage"]["model"] if result["usage"]["model"] in llm.MODELS.values() else model
                cache[lang] = translate_report(result["report"], lang, model=used, api_key=get_api_key(used))
        report, tlog = cache[lang]
    st.divider()
    if tlog and any(k["field"] == "all" for k in tlog["kept_english"]):
        st.info(T["failed"])
    elif tlog and tlog["kept_english"]:
        st.caption(T["partial"])

    if result["masked"]:
        names = {"NAME": "name", "AADHAAR": "Aadhaar number", "PAN": "PAN", "PHONE": "phone number",
                 "EMAIL": "email", "ADDRESS": "address", "PINCODE": "PIN code"}
        plural = {"ADDRESS": "addresses", "PAN": "PANs"}
        hidden = ", ".join(f"{n} {plural.get(tag, names.get(tag, tag.lower()) + 's') if n > 1 else names.get(tag, tag.lower())}"
                           for tag, n in result["masked"].items())
        st.caption(f"Hidden before checking: {hidden}.")
    if report.injection_detected:
        st.warning("This document contains text aimed at AI tools (for example, telling them not to "
                   "flag anything). We ignored it and checked the agreement normally.")

    st.subheader(T["deal"])
    terms = report.key_terms.model_dump()
    labels = {k: T[k] for k in ("rent", "deposit", "lock_in", "notice_period", "rent_increase", "maintenance", "term")}
    cols = st.columns(2)
    for i, (key, label) in enumerate(labels.items()):
        with cols[i % 2]:
            st.markdown(f"<div class='term-label'>{label}</div>"
                        f"<div class='term-value'>{html.escape(terms[key])}</div>", unsafe_allow_html=True)
    if report.summary:
        st.write(report.summary)

    highs = sum(f.risk == "High" for f in report.flags)
    st.subheader(T["worth"].format(n=len(report.flags)))
    if not report.flags:
        st.success(T["none_found"])
    else:
        st.caption(T["counts"].format(h=highs, m=len(report.flags) - highs))
        if T["quote_note"]:
            st.caption(T["quote_note"])
    for f in report.flags:
        with st.container(border=True):
            cls = "risk-high" if f.risk == "High" else "risk-medium"
            st.markdown(f"<span class='risk {cls}'>{T[f.risk]}</span>"
                        f"<span class='flag-title'>{html.escape(f.title)}</span>", unsafe_allow_html=True)
            for q in f.quotes:
                st.markdown(f"<div class='clause'><span class='clause-ref'>{html.escape(q.clause_ref)}</span>"
                            f"“{html.escape(q.text)}”</div>", unsafe_allow_html=True)
            st.write(f.explanation)
            if f.why_it_matters:
                st.write(f"**{T['why']}** {f.why_it_matters}")
            st.write(f"**{T['ask']}** {f.question}")
            if lang == "en":
                st.caption(CATEGORY_NAMES.get(f.category, "Other"))

    if report.missing:
        st.subheader(T["missing"].format(n=len(report.missing)))
        for m in report.missing:
            with st.container(border=True):
                st.write(f"**{CATEGORY_NAMES.get(m.category, 'Other')}:** {m.what}" if lang == "en" else m.what)
                st.write(f"**{T['ask']}** {m.question}")

    questions = [f.question for f in report.flags] + [m.question for m in report.missing]
    if questions:
        st.subheader(T["questions"])
        text_list = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
        st.code(text_list, language=None, wrap_lines=True)
        st.download_button(T["download"], text_list, file_name=f"questions_for_landlord_{lang}.txt")

    st.caption(T["footer"])

    with st.sidebar:
        u = result["usage"]
        st.metric("Time (s)", u["seconds"])
        st.caption(f"Model: {u['model']} · tokens in {u['input_tokens']}, out {u['output_tokens']} · "
                   f"attempts {u['attempts']}")
        log = result["log"]
        st.caption(f"Quotes checked: {log['quotes_ok']}/{log['quotes_total']} exact")
        if log["dropped"]:
            with st.expander(f"Dropped by code checks ({len(log['dropped'])})"):
                st.json(log["dropped"])
        if tlog:
            tu = tlog.get("usage") or {}
            st.caption(f"Translation ({lang}): {tlog['translated']}/{tlog['lines']} lines translated · "
                       f"tokens in {tu.get('input_tokens', 0)}, out {tu.get('output_tokens', 0)} · "
                       f"{tu.get('seconds', 0)} s")
            if tlog["kept_english"]:
                with st.expander(f"Kept in English ({len(tlog['kept_english'])})"):
                    st.json(tlog["kept_english"])
