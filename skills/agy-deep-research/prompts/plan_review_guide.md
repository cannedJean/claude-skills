# Reviewing `plan_vN.md` against `goal.md`

Use at SKILL.md step 4, and again after every `edit`. How to write `prompt.md` is in `references/prompt-spec.md`; how to write `goal.md` is in SKILL.md step 2.

Go through `goal.md` one item at a time and find the plan step that satisfies it. An item with no step is missing, however thorough the rest of the plan looks.

1. **Coverage**: every entity, algorithm and metric in "Must cover" / "Must report" is named in a plan step. "Compare leading vector databases" does not cover an item that names four of them.
2. **Scope**: nothing from "Out of scope" appears, and the time range matches.
3. **Evidence**: the plan gathers what the "Source standard" asks for (primary papers, official documentation, benchmarks, named Workspace documents), not summaries of summaries.
4. **Drift**: steps that answer a different question than the prompt asked. Remove them; they use research time.

Decide:

- **Any item missing, contradicted, or drifted**: `edit` with imperative changes only (Add, Remove, Reorder, Pin), one sentence per change, naming the goal item's terms. No questions: a question makes Gemini leave plan editing and reply in prose (`FEEDBACK_INTERROGATIVE_DENIED`).
- **Every item covered**: `approve` with a `--reason` that maps plan steps to goal items.
- **Still missing items after two edits**: stop and show the user (SKILL.md step 5).
