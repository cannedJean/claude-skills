# Research Plan Review Guide (Caller Verification Rubric)

Before approving a Gemini Deep Research plan (`deep_research.py approve`), compare the generated plan (`latest_plan` in `<job>/plan_v*.md`) against `<job>/goal.md` recorded before the plan step ran:

1. **Subtopic Coverage**: Does the plan explicitly cover every required subtopic, technical dimension, and comparison axis listed in `goal.md`?
2. **Scope & Exclusions**: Does the plan respect the scope boundaries and avoid excluded topics or time periods stated in `goal.md`?
3. **Methodology & Evidence**: Does the plan gather primary sources, benchmarks, specifications, or empirical data required by `goal.md` rather than superficial summaries?
4. **Action Decision**:
   - **Missing or drifted items** → Run `deep_research.py edit --job <id> --feedback "<specific instructions on what steps to add, remove, or refine>"`.
   - **All criteria in `goal.md` satisfied** → Run `deep_research.py approve --job <id> --reason "<20+ char explanation mapping plan items to goal.md criteria>" --out <report.md>`.
