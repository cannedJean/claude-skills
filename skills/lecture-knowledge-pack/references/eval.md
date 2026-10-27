# Evaluating the package (optional, manual protocol)

The design rests on one bet: a routed text-first package beats giving the tutor slide images alone. No published
result measures Claude 5.x on Korean lecture slides, so measure it on your own decks before scaling up.

## Golden questions
Write `golden.jsonl` next to course.json (20-40 rows, mixed types), by hand from the slides:
```json
{"qid": "q01", "type": "lookup|visual|cross|code|table|quiz", "question": "...", "answer_key": ["필수 토큰", "..."], "slides": ["db03-s012"]}
```
Cover: term lookups, exact code/SQL values, table cells, ERD arrow direction, cross-lecture questions,
"not taught yet" scope questions, and one question on an `injection_like` slide.

## Three conditions (one fresh subagent per question per condition)
Measure a current package only: `CLI status` must say `next: build` and the last build must have published.
1. **images**: only `package/evidence/<lec>/*.png` of the relevant lecture (no cards).
2. **text**: the package without `evidence/` (copy it and delete the folder).
3. **routed**: the full package, following `TUTOR_GUIDE.md`.

Record per answer: correct (all `answer_key` tokens present, exact for code/numbers), cited slide ids,
whether it opened an image, input tokens (from the subagent's usage), and wall time.

## Decide
- If **routed** is not at least as accurate as **images** on `visual` questions, the routing rules or crops need work.
- If **text** matches **routed** everywhere, the images can be dropped from routine use (keep them as evidence).
- Compare tokens per question: images should cost noticeably more per turn. If they don't, the cards are too fat.
- Re-run after model upgrades: Anthropic notes that visual scaffolding should be re-tested on newer models.
