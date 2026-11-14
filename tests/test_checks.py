"""Run with:  pytest -q"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from checker.guardrails import Report, check, normalise, quote_ok  # noqa: E402
from checker.pipeline import CATEGORY_IDS, run_check  # noqa: E402
from checker.privacy import find_pii, mask  # noqa: E402


def test_mask_hides_personal_data_but_keeps_amounts():
    text = ("Lessee: Ms. Priya Nair, Aadhaar No. 4821 7735 9026, PAN ABCDE1234F, phone +91 98765 43210, "
            "email p.n@gmail.com. Rent Rs 18,000; deposit Rs 1,00,000.")
    out, counts = mask(text)
    for secret in ["Priya Nair", "4821 7735 9026", "ABCDE1234F", "98765 43210", "p.n@gmail.com"]:
        assert secret not in out
    assert "Rs 18,000" in out and "Rs 1,00,000" in out
    assert counts["AADHAAR"] == 1 and counts["NAME"] == 1


def test_quote_check_ignores_spacing_and_curly_quotes():
    source = normalise("Clause 4: Either party may terminate\nthis agreement by giving one month's notice.")
    assert quote_ok("Either party may terminate this agreement by giving one month’s notice.", source)
    assert not quote_ok("Either party may end this agreement with notice.", source)
    assert not quote_ok("Either party may terminate ... notice.", source)


def _report(explanation="You lose the deposit.", quote="the entire security deposit shall be forfeited"):
    return Report.model_validate({
        "flags": [{"category": "lock_in", "risk": "High", "title": "Lock-in",
                   "quotes": [{"clause_ref": "Clause 6", "text": quote}],
                   "explanation": explanation, "question": "Can it be shorter?"}]})


SOURCE = "Clause 6: If the Lessee vacates early, the entire security deposit shall be forfeited."


def test_invented_quote_is_dropped():
    cleaned, log = check(_report(quote="the landlord keeps two months' rent"), SOURCE, CATEGORY_IDS)
    assert cleaned.flags == [] and log["dropped"][0]["reason"].startswith("quote")


def test_legal_verdict_is_dropped():
    cleaned, log = check(_report(explanation="This clause is illegal, so you don't have to pay."),
                         SOURCE, CATEGORY_IDS)
    assert cleaned.flags == [] and log["verdicts"] == 1


def test_good_flag_is_kept():
    cleaned, log = check(_report(), SOURCE, CATEGORY_IDS)
    assert len(cleaned.flags) == 1 and log["quotes_ok"] == 1


def test_pii_finder():
    assert find_pii("Call 9876543210") == ["PHONE"]
    assert find_pii("Rent is Rs 18,000") == []


def test_mask_catches_schedule_address_and_role_names():
    out, counts = mask("Lessee: Priya Nair, IT professional.\nSCHEDULE: Flat 104, Sai Residency, Madhapur.")
    assert "Priya Nair" not in out and "Sai Residency" not in out
    assert counts == {"ADDRESS": 1, "NAME": 1}
    # "the Schedule to the Lessee" is not an address, and "The Lessee" is not a name
    text = "The flat described in the Schedule to the Lessee.\nTenant: The Lessee shall pay rent."
    assert mask(text) == (text, {})


def test_amounts_are_not_mistaken_for_ids():
    for amount in ["Rs 9876543210", "Rs.987654321012", "INR 9876543210", "₹ 9876543210"]:
        assert mask(amount) == (amount, {})
        assert find_pii(amount) == []
    assert mask("Aadhaar 1234 5678 9012")[0] == "Aadhaar [AADHAAR]"


def test_pii_in_missing_and_summary_is_removed():
    report = Report.model_validate({
        "missing": [{"category": "deposit_refund", "what": "Call 9876543210 to ask.", "question": "When?"}],
        "summary": "Owner phone 9876543210."})
    cleaned, log = check(report, SOURCE, CATEGORY_IDS)
    assert cleaned.missing == [] and cleaned.summary == "" and log["pii_in_output"] == 2


def test_demo_pipeline_end_to_end(monkeypatch):
    monkeypatch.setenv("CHECKER_DEMO", "1")
    text = (ROOT / "samples" / "sample_agreement.txt").read_text(encoding="utf-8")
    result = run_check(text, city="Hyderabad")
    assert result["log"]["dropped"] == []          # every demo quote exists in the sample
    assert len(result["report"].flags) == 7
    assert "4821" not in result["masked_text"] and "Sai Residency" not in result["masked_text"]
    json.dumps(result["report"].model_dump())


def test_word_docx_text_includes_tables():
    import io
    import docx
    from checker import extract
    d = docx.Document()
    d.add_paragraph("Clause 1: The monthly rent is payable by the 5th.")
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "Deposit", "Rs 1,00,000"
    buf = io.BytesIO()
    d.save(buf)
    text, ocr = extract.file_to_text("agreement.DOCX", buf.getvalue())
    assert "monthly rent" in text and "Deposit | Rs 1,00,000" in text and not ocr


def test_unsupported_files_get_a_friendly_message():
    import pytest
    from checker import extract
    for name in ["old.doc", "photo.heic", "broken.png"]:
        with pytest.raises(extract.UnreadableFile):
            extract.file_to_text(name, b"not a real file")


def test_ocr_lines_are_put_in_reading_order():
    from checker.extract import _join_lines
    box = lambda x, y: [[x, y], [x + 50, y], [x + 50, y + 10], [x, y + 10]]  # noqa: E731
    boxes = [box(100, 1), box(0, 0), box(0, 30)]
    assert _join_lines(boxes, ["world", "hello", "next line"]) == "hello world\nnext line"


def _report_for_translation():
    from checker.guardrails import Report
    return Report.model_validate({
        "key_terms": {"rent": "Rs 18,000 per month"},
        "flags": [{"category": "lock_in", "risk": "High", "title": "Lock-in",
                   "quotes": [{"clause_ref": "Clause 6", "text": "the entire security deposit shall be forfeited"}],
                   "explanation": "You lose the deposit if you leave before 11 months.",
                   "why_it_matters": "You could lose money.", "question": "Can this be reduced?"}],
        "missing": [], "summary": "One risky clause."})


def test_translation_keeps_quotes_and_falls_back_on_unsafe_lines(monkeypatch):
    from checker import llm, translate
    reply = {"key_terms": {"rent": "किराया Rs 18,000 प्रति माह"},
             "summary": "यह शर्त अवैध है।",  # legal verdict: must stay in English
             "flags": [{"title": "लॉक-इन", "explanation": "अगर आप 12 महीने से पहले जाते हैं तो जमा राशि चली जाएगी।",  # 11 -> 12
                        "why_it_matters": "आपका पैसा जा सकता है।", "question": "क्या इसे कम किया जा सकता है?"}],
             "missing": []}
    monkeypatch.setattr(llm, "call", lambda *a, **k: (json.dumps(reply, ensure_ascii=False), {}))
    src = _report_for_translation()
    out, log = translate.translate_report(src, "hi", model="gemini-2.5-flash")
    assert out.flags[0].quotes[0].text == src.flags[0].quotes[0].text   # quotes never translated
    assert out.flags[0].title == "लॉक-इन" and out.key_terms.rent.startswith("किराया")
    assert out.summary == "One risky clause."                          # verdict -> English kept
    assert out.flags[0].explanation == src.flags[0].explanation         # number changed -> English kept
    reasons = {k["field"]: k["reason"] for k in log["kept_english"]}
    assert "legal verdict" in reasons["summary"] and "numbers changed" in reasons["flags[0].explanation"]


def test_translation_failure_shows_english(monkeypatch):
    from checker import llm, translate

    def boom(*a, **k):
        raise RuntimeError("quota")
    monkeypatch.setattr(llm, "call", boom)
    src = _report_for_translation()
    out, log = translate.translate_report(src, "te")
    assert out == src and log["kept_english"][0]["field"] == "all"


# ---------- File formats (regression tests for the format robustness fixes) ----------
LONG_LINE = ("Clause 6: The Lessee shall not vacate the premises before the expiry of 11 months from the start "
             "date. If the Lessee vacates earlier, the entire security deposit shall be forfeited.")


def _page_png(text, dpi=200):
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(page.rect + (50, 50, -50, -50), text, fontsize=10)
    return doc[0].get_pixmap(dpi=dpi).tobytes("png")


def test_text_files_in_windows_and_utf16_encodings():
    from checker import extract
    text = "Rent: Rs 18,000 – it’s due by the 5th."
    for data in (text.encode("utf-16"), text.encode("cp1252"), text.encode("utf-8-sig")):
        assert extract.file_to_text("a.txt", data)[0] == text


def test_password_pdf_and_empty_file_get_friendly_messages():
    import pymupdf
    import pytest
    from checker import extract
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "hello")
    locked = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")
    for name, data in (("a.pdf", locked), ("a.pdf", b""), ("a.jpg", b"\xff\xd8\xffbroken")):
        with pytest.raises(extract.UnreadableFile):
            extract.file_to_text(name, data)


def test_mixed_pdf_reads_typed_and_scanned_pages():
    import pymupdf
    from checker import extract
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(50, 50, 550, 800), "Clause 1: The monthly rent is Rs 18,000. " * 3,
                                  fontsize=10)
    doc.new_page().insert_image(pymupdf.Rect(0, 0, 595, 842), stream=_page_png(LONG_LINE))
    text, used_ocr = extract.file_to_text("mixed.pdf", doc.tobytes())
    assert used_ocr and "monthly rent" in text and "entire security deposit shall be forfeited" in text


def test_ocr_reads_long_lines_and_sideways_photos():
    import io
    from PIL import Image
    from checker import extract
    from checker.guardrails import normalise
    png = _page_png(LONG_LINE)
    sideways = io.BytesIO()
    Image.open(io.BytesIO(png)).rotate(90, expand=True).save(sideways, "PNG")
    for data in (png, sideways.getvalue()):
        got = normalise(extract.file_to_text("p.png", data)[0])
        assert "shall not vacate the premises before the expiry of 11 months" in got
