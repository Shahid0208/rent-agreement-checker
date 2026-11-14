You are grading the output of a rent agreement checker against a rubric. Be strict and literal.

You receive:
- AGREEMENT: the text that was checked
- EXPECTED: the labels a human wrote for this agreement
- OUTPUT: the checker's report (JSON)

Grade two criteria, each PASS or FAIL.

1. flag_correctness
   PASS if every category in EXPECTED.expect_high is flagged High, every category in EXPECTED.expect_flag is flagged (High or Medium), every category in EXPECTED.expect_missing is listed as missing, and no fair clause is flagged High.
   FAIL if any expected flag is missing, or a fair clause (e.g. deductions limited to unpaid dues and damage beyond normal wear and tear) is flagged High.

2. grounded_explanation
   PASS if every explanation says the same thing as its quoted clause, with no added numbers, conditions, timelines or consequences that are not in the agreement.
   FAIL if any explanation adds or changes meaning.

Reply with ONLY this JSON:
{"flag_correctness": "PASS or FAIL", "flag_reason": "one sentence", "grounded_explanation": "PASS or FAIL", "grounded_reason": "one sentence"}
