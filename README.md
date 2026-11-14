# Rent Agreement Explainer: Team 4 MVP

A first-time renter uploads their rent agreement and gets a plain-language summary, High/Medium flags with the exact clause quoted, a list of what's missing, and questions to ask the landlord. Not legal advice.

**Flow:** pick city and report language (English, Hindi, Telugu, Kannada) → upload a PDF, Word file or photos (or paste text) → personal data hidden → the AI model (Gemini by default) reads and flags → code double-checks → report.

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then paste your Gemini API key
streamlit run app.py
```

No key yet? Run the demo, which returns a saved answer for the built-in sample agreement:

```bash
CHECKER_DEMO=1 streamlit run app.py      # Windows PowerShell: $env:CHECKER_DEMO="1"; streamlit run app.py
```

Click **Paste the text → Use a sample agreement → Check my agreement**.

## Deploy (free) on Streamlit Community Cloud

1. Push this folder to a GitHub repo. `.gitignore` keeps your key and results out of it.
2. On share.streamlit.io, create an app from the repo with `app.py` as the entry point.
3. In the app's **Settings → Secrets**, add `GEMINI_API_KEY = "..."` (and `ANTHROPIC_API_KEY` only if you want to compare with Claude).
4. Open the app a few minutes before each customer session. Free apps sleep when idle.

## Run the evals

```bash
python evals/run_evals.py                            # Gemini 3.8 Flash (default)
python evals/run_evals.py --model gemini-2.5-flash   # older free model
python evals/run_evals.py --model claude-sonnet-5-5  # compare with Claude (paid, needs ANTHROPIC_API_KEY)
python evals/run_evals.py --only G02 G09           # a few items
```

Each run writes `evals/results/<time>_<model>.csv` (one row per golden item, with empty columns for your two human raters) and a `.json` with the full outputs. The terminal prints the summary and whether the **ship gate** passes:

| Ship gate | Target |
|---|---|
| Invented clauses | 0 |
| Exact quotes | 100% |
| Recall on high-risk clauses | ≥ 90% |
| Legal verdicts | 0 |

Scores are measured on the model's raw answer, before the code checks remove anything, so the gate reflects the model's real behaviour.

**Free second model:** `python evals/run_evals.py --model groq:openai/gpt-oss-20b --pause 20` (needs `GROQ_API_KEY`). **Old prompt versions:** prefix any run with `CHECKER_PROMPT=prompts/history/system_prompt_v0.md` to compare prompts on the same model. **Finish a run that hit a quota:** add `--resume evals/results/<run>`.

**Translations:** add `--lang hi` (or `te`, `kn`) to also translate every report and record how many lines passed the translation checks.

**Free tier:** add `--pause 15` to stay under Gemini's 5 requests per minute. Each model has its own daily limit (about 20 requests for `gemini-3.8-flash`).

**Human ratings and the LLM judge** work on a saved run, so they don't use Gemini quota:

```bash
python evals/rate.py sheet                              # rating sheet for two human raters
python evals/rate.py judge --model claude-sonnet-5-5    # or a GPT model (pip install openai)
python evals/rate.py summary                            # rater disagreements + judge agreement
```

The judge must be a different provider from the checker (Gemini), so use Claude (`ANTHROPIC_API_KEY`), GPT (`OPENAI_API_KEY`), or a free one: `--model groq:llama-3.3-70b-versatile` (`GROQ_API_KEY`), `--model github:openai/gpt-4o-mini` (`GITHUB_TOKEN`) an OpenRouter `:free` model (`--model openrouter:<name>`, `OPENROUTER_API_KEY`) or NVIDIA (`--model nvidia:meta/llama-3.3-70b-instruct`, `NVIDIA_API_KEY`). Put keys in `.streamlit/secrets.toml` or as environment variables. Two of you rate the same 10 items by hand (Y/N in `human1_actionable_safe` / `human2_actionable_safe` in the CSV, without discussing first); if the judge matches you on fewer than 8 of 10, fix `prompts/judge_prompt.md` before trusting it.

**Unit tests** for masking and the code checks: `pytest -q`

## Project structure

| Path | What it is |
|---|---|
| `app.py` | Streamlit web app |
| `checker/extract.py` | Text from PDFs, Word `.docx`, photos (JPG/PNG/WebP) and `.txt`. Photos and scanned PDFs are read with on-device OCR (RapidOCR), so nothing unmasked leaves the machine. Old `.doc` files get a "save as .docx" message |
| `checker/privacy.py` | Hides Aadhaar, PAN, phone, email, PIN, addresses (incl. the Schedule) and names (after Mr/Smt/S/o or "Lessee:") before any AI call. Numbers after Rs/INR/₹ are kept as amounts |
| `checker/llm.py` | Gemini and Claude API calls, model list, and the demo mode |
| `checker/translate.py` | Translates a checked report into Hindi, Telugu or Kannada; quotes stay in the original; unsafe or changed lines fall back to English |
| `checker/ui_text.py` | Report headings and labels in each language (to verify with native speakers) |
| `checker/guardrails.py` | Output schema, exact-quote check, banned verdict words, personal data check |
| `checker/pipeline.py` | Runs all steps; used by both the app and the evals |
| `config/checklist.json` | What to look for, and when it's High, Medium or missing |
| `config/cities.json` | City profiles (Hyderabad, Bengaluru, Mumbai / Pune, Chennai, Delhi, Noida/UP, Other) with sources |
| `prompts/system_prompt.md` | The checker's instructions |
| `prompts/judge_prompt.md` | The LLM judge's instructions |
| `evals/golden_set.json` | Golden set v1.1 (35 items: 18 happy, 10 edge, 7 adversarial; each with correct output, why and labeller). Older versions in `evals/history/` |
| `evals/human_rubric.md` | Human rubric v1: 3 criteria with PASS/FAIL anchors, two raters |
| `evals/rate.py` | Rating sheet, LLM judge on a saved run, and the human/judge agreement summary |
| `evals/ITERATION_LOG.md` | Every run, failure class, fix and delta (a graded deliverable) |
| `prompts/history/` | Earlier versions of the checker and judge prompts |
| `samples/sample_agreement.txt` | Sample agreement for demos |
| `data/opt_in/` | Anonymised copies from users who tick the opt-in box |

## How to improve it (your iteration loop)

1. Run the evals and read the failures in the CSV and JSON.
2. Group failures into a class (for example, "misses one-sided notice periods").
3. Fix the worst class: usually `prompts/system_prompt.md` or `config/checklist.json`.
4. Re-run, and log the change and the before/after scores in your iteration log.
5. Add new real agreements to `evals/golden_set.json` as you collect them. Never edit an item after a run just to raise a score.

## Things to know

- **Model names.** Set in `checker/llm.py`: `gemini-3.8-flash` (default) and `gemini-2.5-flash` (both on Gemini's free tier), plus `claude-sonnet-5-5` and `claude-opus-5-5` for comparison. If an API rejects a name, check https://ai.google.dev/gemini-api/docs/models or https://docs.claude.com/en/docs/about-claude/models and update it there.
- **Gemini free tier.** It has per-minute and per-day request limits (the app retries when it hits one), and Google may use free-tier inputs to improve its products. Agreements are masked first, but masking isn't perfect, so for real customer agreements consider enabling billing (paid-tier data isn't used that way). Check https://ai.google.dev/gemini-api/docs/pricing for current terms.
- **City profiles.** Rules marked `TO VERIFY` in `config/cities.json` come from property portals and news. Confirm them with the official act or a lawyer before your final demo. The Bengaluru 2026 law change is deliberately switched off until verified.
- **Opt-in copies** are saved to `data/opt_in/`. On Streamlit Community Cloud this folder is temporary and is wiped when the app restarts, so download copies regularly or switch to a Google Sheet later.
- **Name detection** uses patterns like "Mr.", "Smt." and "S/o". Installing `presidio-analyzer` (plus a spaCy English model) adds broader name detection, but it's a heavy install for the free tier.
- **Cost and speed** for every check appear in the sidebar (tokens, seconds, attempts). Note them in your PRD.
