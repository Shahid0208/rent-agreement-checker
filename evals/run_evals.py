"""Run the golden set through the real pipeline and score it.

Usage (from the project folder):
    python evals/run_evals.py                      # default model
    python evals/run_evals.py --model gemini-2.5-flash
    python evals/run_evals.py --model claude-sonnet-5-5   # needs ANTHROPIC_API_KEY
    python evals/run_evals.py --only G02 G09       # a few items
    python evals/run_evals.py --pause 15           # free tier: stay under 5 requests/minute
    python evals/run_evals.py --pause 15 --resume evals/results/<run>   # finish a run that hit a quota
    CHECKER_DEMO=1 python evals/run_evals.py       # dry run, no API key

Optional LLM judge (a different provider, so it doesn't grade its own model):
    set JUDGE_MODEL to a GPT model (pip install openai; needs OPENAI_API_KEY)
    or a Claude model such as claude-sonnet-5-5 (needs ANTHROPIC_API_KEY).
    To judge a saved run without calling Gemini again: python evals/rate.py judge

Writes evals/results/<timestamp>_<model>.csv (one row per item, with empty
columns for the two human raters, see evals/human_rubric.md) and a .json file with full outputs.
"""
import argparse
import csv
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from checker import llm  # noqa: E402
from checker.pipeline import PROMPT_PATH, run_check  # noqa: E402

# Human rubric v1 (evals/human_rubric.md): 3 criteria x 2 raters, PASS/FAIL each.
CRITERIA = ["accuracy", "grounded", "safe_useful"]
HUMAN_COLUMNS = [f"h{r}_{c}" for r in (1, 2) for c in CRITERIA] + ["disagreement_note"]
GATE = {"invented_clauses": 0, "exact_quote_pct": 100.0, "high_recall_pct": 90.0, "legal_verdicts": 0}


# Free judges with OpenAI-compatible APIs. Use as --model "<prefix>:<model name>".
FREE_JUDGES = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),              # console.groq.com
    "github": ("https://models.github.ai/inference", "GITHUB_TOKEN"),        # github.com/marketplace/models
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),    # openrouter.ai, models ending ":free"
    "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),     # build.nvidia.com
}


def judge(item: dict, output: dict, model: str | None = None) -> dict:
    """Grade one report with an LLM from a different provider than the checker (Gemini).

    model: a GPT model (needs OPENAI_API_KEY), a Claude model (needs ANTHROPIC_API_KEY),
    or a free one: "groq:<model>", "github:<model>", "openrouter:<model>" or "nvidia:<model>" (see FREE_JUDGES).
    Defaults to the JUDGE_MODEL environment variable. Keys can also go in .streamlit/secrets.toml.
    """
    model = model or os.environ.get("JUDGE_MODEL")
    if not model:
        return {"flag_correctness": "", "grounded_explanation": "", "judge_note": "judge skipped"}
    if model.startswith("gemini"):
        raise SystemExit("The judge must be a different provider from the checker (Gemini). "
                         "Use a GPT or Claude model.")
    prompt = (ROOT / "prompts" / "judge_prompt.md").read_text(encoding="utf-8")
    expected = {k: item[k] for k in ("expect_high", "expect_flag", "expect_missing", "must_not_high")}
    msg = (f"AGREEMENT:\n<agreement>\n{item['text']}\n</agreement>\n\nEXPECTED:\n{json.dumps(expected)}\n\n"
           f"OUTPUT:\n{json.dumps(output, ensure_ascii=False)}")
    if model.startswith("claude"):
        text, _ = llm.call(prompt, msg, model=model, max_tokens=1000)
    else:
        from openai import OpenAI  # optional dependency: pip install openai

        prefix, _, name = model.partition(":") if model.split(":")[0] in FREE_JUDGES else ("openai", "", model)
        base_url, key_name = FREE_JUDGES.get(prefix, (None, "OPENAI_API_KEY"))
        key = os.environ.get(key_name) or llm._key_from_secrets_file(key_name)
        if not key:
            return {"flag_correctness": "", "grounded_explanation": "", "judge_note": f"judge skipped: no {key_name}"}
        resp = OpenAI(api_key=key, base_url=base_url, max_retries=5).chat.completions.create(
            model=name, messages=[{"role": "system", "content": prompt}, {"role": "user", "content": msg}])
        text = resp.choices[0].message.content or ""
    try:
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except ValueError:
        return {"flag_correctness": "", "grounded_explanation": "", "judge_note": "judge reply unreadable"}
    data["judge_note"] = f"{data.pop('flag_reason', '')} | {data.pop('grounded_reason', '')}"
    return data


def score(item: dict, result: dict) -> dict:
    raw = result["raw_report"]
    log = result["log"]
    shown = result["report"]
    high = {f.category for f in raw.flags if f.risk == "High"}
    flagged = {f.category for f in raw.flags}
    missing = {m.category for m in raw.missing}

    hit_high = [c for c in item["expect_high"] if c in high]
    hit_flag = [c for c in item["expect_flag"] if c in flagged]
    hit_missing = [c for c in item["expect_missing"] if c in missing]  # use expect_mention for "flag or missing"
    hit_mention = [c for c in item["expect_mention"] if c in flagged or c in missing]
    false_high = [c for c in item["must_not_high"] if c in high]
    everything = json.dumps(raw.model_dump(), ensure_ascii=False)
    leaked = [s for s in item["must_not_contain"] if s.lower() in everything.lower()]
    invented = sum(1 for d in log["dropped"] if d["reason"].startswith("quote"))
    too_many = item.get("max_flags") is not None and len(raw.flags) > item["max_flags"]
    injection_ok = (raw.injection_detected or not item["expect_injection"])

    passed = (len(hit_high) == len(item["expect_high"]) and len(hit_flag) == len(item["expect_flag"])
              and len(hit_missing) == len(item["expect_missing"])
              and len(hit_mention) == len(item["expect_mention"]) and not false_high and not leaked
              and invented == 0 and log["verdicts"] == 0 and not too_many and injection_ok)
    return {
        "id": item["id"], "type": item["type"], "city": item["city"], "title": item["title"],
        "auto_pass": "PASS" if passed else "FAIL",
        "high_expected": len(item["expect_high"]), "high_found": len(hit_high),
        "flags_expected": len(item["expect_flag"]), "flags_found": len(hit_flag),
        "missing_expected": len(item["expect_missing"]), "missing_found": len(hit_missing),
        "false_high": ";".join(false_high), "too_many_flags": too_many,
        "quotes_total": log["quotes_total"], "quotes_ok": log["quotes_ok"], "invented_clauses": invented,
        "legal_verdicts": log["verdicts"], "pii_leaked": ";".join(leaked) or ("" if not log["pii_in_output"] else "pattern"),
        "injection_ok": injection_ok,
        "flags_shown": len(shown.flags), "flags_dropped": len(log["dropped"]),
        "input_tokens": result["usage"]["input_tokens"], "output_tokens": result["usage"]["output_tokens"],
        "seconds": result["usage"]["seconds"],
    }


def load_previous(base: str):
    """Rows (numbers restored) and full outputs of a saved run, keyed by item id."""
    with open(f"{base}.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for k, v in r.items():
            if v in ("True", "False"):
                r[k] = v == "True"
            else:
                try:
                    r[k] = int(v)
                except ValueError:
                    try:
                        r[k] = float(v)
                    except ValueError:
                        pass
    full = json.loads(Path(f"{base}.json").read_text(encoding="utf-8"))["items"]
    return {r["id"]: r for r in rows}, {f["id"]: f for f in full}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=llm.DEFAULT_MODEL)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--pause", type=float, default=0,
                    help="seconds to wait between items (free tier: use 15 to stay under 5 requests/minute)")
    ap.add_argument("--lang", default="en", choices=["en", "hi", "te", "kn"],
                    help="also translate each report and record how many lines passed the translation checks")
    ap.add_argument("--resume", help="a saved run (path without .csv/.json): re-run only its ERROR items "
                                     "and update that run in place, e.g. after hitting a daily quota")
    args = ap.parse_args()
    prev_rows, prev_full = load_previous(args.resume) if args.resume else ({}, {})

    gold = json.loads((ROOT / "evals" / "golden_set.json").read_text(encoding="utf-8"))
    items = [i for i in gold["items"] if not args.only or i["id"] in args.only]
    if args.resume:
        items = [i for i in items if prev_rows.get(i["id"], {}).get("auto_pass", "ERROR") == "ERROR"]
        print(f"Resuming {args.resume}: re-running {', '.join(i['id'] for i in items) or 'nothing'}")
    rows, full = [], []
    for n, item in enumerate(items):
        if n and args.pause:
            time.sleep(args.pause)
        print(f"{item['id']} {item['title']} ...", end=" ", flush=True)
        try:
            result = run_check(item["text"], city=item["city"], model=args.model)
        except Exception as e:  # keep going; a crash counts as a fail
            print(f"ERROR {e}")
            rows.append({"id": item["id"], "type": item["type"], "title": item["title"], "auto_pass": "ERROR"})
            continue
        row = score(item, result)
        # The judge grades the report the user actually sees (after code checks);
        # the auto-scores above use the raw answer, so the ship gate reflects the model itself.
        row.update(judge(item, result["report"].model_dump()))
        if args.lang != "en":
            from checker.translate import translate_report
            _, tlog = translate_report(result["report"], args.lang, model=args.model)
            row.update({"lang": args.lang, "translated_lines": tlog["translated"], "total_lines": tlog["lines"],
                        "kept_english": "; ".join(f"{k['field']}: {k['reason']}" for k in tlog["kept_english"])})
        row.update({c: "" for c in HUMAN_COLUMNS})
        rows.append(row)
        full.append({"id": item["id"], "raw_report": result["raw_report"].model_dump(),
                     "shown_report": result["report"].model_dump(), "log": result["log"]})
        print(row["auto_pass"])

    if args.resume:  # merge new results into the saved run, in golden-set order
        new_rows, new_full = {r["id"]: r for r in rows}, {f["id"]: f for f in full}
        order = [i["id"] for i in gold["items"] if i["id"] in prev_rows or i["id"] in new_rows]
        rows = [new_rows.get(i) or prev_rows[i] for i in order]
        full = [new_full.get(i) or prev_full[i] for i in order if i in new_full or i in prev_full]
    ok = [r for r in rows if r["auto_pass"] != "ERROR"]
    q_total = sum(r["quotes_total"] for r in ok)
    q_ok = sum(r["quotes_ok"] for r in ok)
    h_exp = sum(r["high_expected"] for r in ok)
    h_found = sum(r["high_found"] for r in ok)
    summary = {
        "items": len(rows),
        "auto_pass": sum(r["auto_pass"] == "PASS" for r in rows),
        "invented_clauses": sum(r["invented_clauses"] for r in ok),
        "exact_quote_pct": round(100 * q_ok / q_total, 1) if q_total else 100.0,
        "high_recall_pct": round(100 * h_found / h_exp, 1) if h_exp else 100.0,
        "legal_verdicts": sum(r["legal_verdicts"] for r in ok),
        "false_high": sum(bool(r["false_high"]) for r in ok),
        "errors": len(rows) - len(ok),
        "tokens_in": sum(r["input_tokens"] for r in ok),
        "tokens_out": sum(r["output_tokens"] for r in ok),
        "avg_seconds": round(sum(r["seconds"] for r in ok) / len(ok), 1) if ok else 0,
        "golden_set": gold["version"],
        "checker_prompt": f"{PROMPT_PATH.relative_to(ROOT)} ({hashlib.sha1(PROMPT_PATH.read_bytes()).hexdigest()[:7]})",
    }
    gate_pass = (summary["invented_clauses"] <= GATE["invented_clauses"]
                 and summary["exact_quote_pct"] >= GATE["exact_quote_pct"]
                 and summary["high_recall_pct"] >= GATE["high_recall_pct"]
                 and summary["legal_verdicts"] <= GATE["legal_verdicts"] and summary["errors"] == 0)

    out_dir = ROOT / "evals" / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    model_tag = "demo" if llm.demo_mode() else args.model.replace("groq:", "groq_").replace("/", "_")
    if args.lang != "en":
        model_tag += f"_{args.lang}"
    base = Path(args.resume).resolve() if args.resume else out_dir / f"{stamp}_{model_tag}"
    fields = []
    for r in rows:
        fields += [k for k in r if k not in fields]
    with open(f"{base}.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, restval="")
        w.writeheader()
        w.writerows(rows)
    Path(f"{base}.json").write_text(json.dumps({"summary": summary, "items": full}, indent=1, ensure_ascii=False),
                                    encoding="utf-8")

    print("\n=== Summary ===")
    for k, v in summary.items():
        print(f"{k:>18}: {v}")
    print(f"\nShip gate: {'PASS' if gate_pass else 'FAIL'}  "
          f"(0 invented, 100% exact quotes, >=90% high recall, 0 verdicts)")
    print(f"Saved {base}.csv and .json")


if __name__ == "__main__":
    main()
