"""Human ratings and the LLM judge for a saved eval run (no new checker calls).

Usage (from the project folder), each works on the newest run in evals/results/ unless you pass --run:
    python evals/rate.py sheet                                    # rating sheet for the two human raters
    python evals/rate.py judge --model groq:openai/gpt-oss-120b   # fills the judge columns (free on Groq)
    python evals/rate.py summary                                  # rater disagreements + judge agreement

Human rubric v1 (evals/human_rubric.md): two raters, three criteria, PASS or FAIL each:
    h1_accuracy  h1_grounded  h1_safe_useful   (rater 1)
    h2_accuracy  h2_grounded  h2_safe_useful   (rater 2)
Rate independently, then write reasons for FAILs and differences in disagreement_note.
"""
import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

from run_evals import CRITERIA, HUMAN_COLUMNS, judge  # noqa: E402

RESULTS = ROOT / "evals" / "results"
PASS_WORDS, FAIL_WORDS = {"pass", "p", "y", "yes", "1", "true"}, {"fail", "f", "n", "no", "0", "false"}
# Which judge column answers the same question as each human criterion.
JUDGE_FOR = {"accuracy": "flag_correctness", "grounded": "grounded_explanation"}
OLD_HUMAN_COLUMNS = ["human1_actionable_safe", "human2_actionable_safe"]


def ext(base: Path, suffix: str) -> Path:
    """base + suffix (model names like gemini-2.5-flash contain dots, so with_suffix breaks)."""
    return base.parent / (base.name + suffix)


def latest_run() -> Path:
    runs = sorted(p.parent / p.name[: -len(".json")] for p in RESULTS.glob("*.json"))
    runs = [r for r in runs if "_demo" not in r.name]
    if not runs:
        raise SystemExit("No saved runs yet. Run: python evals/run_evals.py --pause 15")
    return runs[-1]


def load(base: Path):
    full = json.loads(ext(base, ".json").read_text(encoding="utf-8"))
    with open(ext(base, ".csv"), newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    # Older runs used one Y/N column per rater; move them to rubric v1 columns (empty ones only).
    if rows and any(c in rows[0] for c in OLD_HUMAN_COLUMNS) and all(
            not r.get(c) for r in rows for c in OLD_HUMAN_COLUMNS):
        note = {r["id"]: r.get("disagreement_note", "") for r in rows}
        for r in rows:
            for c in OLD_HUMAN_COLUMNS + ["disagreement_note"]:
                r.pop(c, None)
            r.update({c: "" for c in HUMAN_COLUMNS})
            r["disagreement_note"] = note[r["id"]]
        save_rows(base, rows)
    return full, rows


def save_rows(base: Path, rows: list[dict]):
    fields = []
    for r in rows:
        fields += [k for k in r if k not in fields]
    # Keep the human columns last, where raters expect them.
    fields = [f for f in fields if f not in HUMAN_COLUMNS] + [c for c in HUMAN_COLUMNS if c in fields]
    with open(ext(base, ".csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, restval="")
        w.writeheader()
        w.writerows(rows)


def gold_items() -> dict:
    gold = json.loads((ROOT / "evals" / "golden_set.json").read_text(encoding="utf-8"))
    return {i["id"]: i for i in gold["items"]}


def cmd_sheet(base: Path):
    full, _ = load(base)
    gold = gold_items()
    out = [f"# Rating sheet: {base.name}", "",
           "Rubric: [evals/human_rubric.md](../human_rubric.md). Rate each item on your own, without "
           "discussing it first. For each of the three criteria write **PASS** or **FAIL** in your columns of",
           f"`evals/results/{base.name}.csv` (rater 1: `h1_accuracy`, `h1_grounded`, `h1_safe_useful`; "
           "rater 2: `h2_...`). Give a reason for every FAIL in `disagreement_note`.", "",
           "- **Accuracy**: risky clauses flagged at the right level, fair clauses not flagged High, "
           "absences under Missing.",
           "- **Grounded**: quotes word for word; explanations add no numbers, timelines, conditions or "
           "consequences the clause doesn't state.",
           "- **Safe & useful**: no legal verdict, no personal data, hidden instructions ignored, "
           "plain words, questions you could actually ask.", ""]
    for entry in full["items"]:
        item, rep = gold[entry["id"]], entry["shown_report"]
        out += ["---", "", f"## {item['id']}: {item['title']} ({item['type']})", "", "**Agreement**", "",
                *[f"> {line}" if line.strip() else ">" for line in item["text"].splitlines()], "",
                f"**Summary:** {rep.get('summary', '')}", "", f"**Flags ({len(rep['flags'])})**", ""]
        for f in rep["flags"]:
            out.append(f"- **{f['risk']}: {f['title']}** ({f['category']})")
            for q in f["quotes"]:
                out.append(f"  - {q['clause_ref']}: “{q['text']}”")
            out += [f"  - Explanation: {f['explanation']}",
                    *([f"  - Why it matters: {f['why_it_matters']}"] if f.get("why_it_matters") else []),
                    f"  - Ask: {f['question']}"]
        if not rep["flags"]:
            out.append("- none")
        out += ["", f"**Missing ({len(rep['missing'])})**", ""]
        out += [f"- {m['category']}: {m['what']} Ask: {m['question']}" for m in rep["missing"]] or ["- none"]
        if rep.get("injection_detected"):
            out += ["", "**Injection detected:** yes"]
        out += ["", "| | Accuracy | Grounded | Safe & useful |", "|---|---|---|---|",
                "| Rater 1 | | | |", "| Rater 2 | | | |", ""]
    path = base.parent / f"{base.name}_rating_sheet.md"
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"Wrote {path}")


def cmd_judge(base: Path, model: str):
    full, rows = load(base)
    gold = gold_items()
    reports = {e["id"]: e["shown_report"] for e in full["items"]}
    prompt_id = hashlib.sha1((ROOT / "prompts" / "judge_prompt.md").read_bytes()).hexdigest()[:7]
    for row in rows:
        if row["id"] not in reports:
            continue
        print(f"{row['id']} ...", end=" ", flush=True)
        verdict = judge(gold[row["id"]], reports[row["id"]], model=model)
        row.update(verdict)
        row["judge_model"] = model
        row["judge_prompt"] = prompt_id
        print(verdict.get("flag_correctness"), verdict.get("grounded_explanation"))
    save_rows(base, rows)
    print(f"Saved judge columns to {ext(base, '.csv')} (judge prompt {prompt_id})")


def pf(value: str) -> str:
    v = (value or "").strip().lower()
    return "PASS" if v in PASS_WORDS else "FAIL" if v in FAIL_WORDS else ""


def cmd_summary(base: Path):
    _, rows = load(base)
    ids = [r["id"] for r in rows]
    h = {(r["id"], n, c): pf(r.get(f"h{n}_{c}")) for r in rows for n in (1, 2) for c in CRITERIA}
    judged = {r["id"]: r for r in rows if r.get("flag_correctness") in ("PASS", "FAIL")}
    print(f"Run: {base.name}\n")

    print("Human ratings (rubric v1)")
    any_rated = False
    for c in CRITERIA:
        both = [i for i in ids if h[(i, 1, c)] and h[(i, 2, c)]]
        if not both:
            continue
        any_rated = True
        diff = [i for i in both if h[(i, 1, c)] != h[(i, 2, c)]]
        passed = sum(h[(i, 1, c)] == h[(i, 2, c)] == "PASS" for i in both)
        print(f"  {c:<12} rated by both on {len(both)}: PASS by both {passed}, "
              f"disagreements {len(diff)}" + (f" ({', '.join(diff)})" if diff else ""))
    if not any_rated:
        print("  none filled in yet (h1_* and h2_* columns; see evals/human_rubric.md)")

    print()
    if not judged:
        print("LLM judge: not run yet (python evals/rate.py judge --model ...)")
        return
    model = next(iter(judged.values())).get("judge_model", "?")
    both_pass = sum(r["flag_correctness"] == r["grounded_explanation"] == "PASS" for r in judged.values())
    print(f"LLM judge ({model}): {both_pass} of {len(judged)} items PASS on both criteria")
    for c, jc in JUDGE_FOR.items():
        print(f"  {jc}: {sum(r[jc] == 'PASS' for r in judged.values())} of {len(judged)} PASS")
    print()
    print("Judge vs humans (items where both raters gave the same verdict)")
    compared = False
    for c, jc in JUDGE_FOR.items():
        agreed = [i for i in judged if h[(i, 1, c)] and h[(i, 1, c)] == h[(i, 2, c)]]
        if not agreed:
            continue
        compared = True
        match = [i for i in agreed if judged[i][jc] == h[(i, 1, c)]]
        miss = [i for i in agreed if i not in match]
        print(f"  {c:<12} judge matches on {len(match)} of {len(agreed)} "
              f"({round(100 * len(match) / len(agreed))}%)" + (f"; differs on {', '.join(miss)}" if miss else ""))
    if compared:
        print("  Trust the judge only at 8 of 10 or better on each criterion.")
    else:
        print("  available once both raters have filled in accuracy and grounded")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["sheet", "judge", "summary"])
    ap.add_argument("--run", help="path to a run without extension, e.g. evals/results/20261114_0024_gemini-2.5-flash")
    ap.add_argument("--model", help="judge model, e.g. groq:openai/gpt-oss-120b, a GPT or a Claude model")
    args = ap.parse_args()
    base = Path(args.run).resolve() if args.run else latest_run()
    if args.command == "sheet":
        cmd_sheet(base)
    elif args.command == "judge":
        if not args.model:
            raise SystemExit("Pass --model, e.g. --model groq:openai/gpt-oss-120b")
        cmd_judge(base, args.model)
    else:
        cmd_summary(base)


if __name__ == "__main__":
    main()
