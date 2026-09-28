---
name: agy-slide-analyzer
description: Analyse a folder of lecture slide images (PNG/JPG, one per page) by delegating every image to the Antigravity CLI (`agy`) — an analyst session per slide (persona + deck context + chain-of-thought + few-shot) writes structured JSON (verbatim text, tables, code, diagrams, key concepts, summary), a deterministic validator and an independent agy reviewer session check it against the image, and failures are retried with feedback. Use when the user wants lecture slides / PDF page images transcribed, summarised or turned into study notes and says things like "슬라이드 분석해줘", "강의자료 이미지 agy로 분석", "analyse these slides with agy". Claude must not open the slide images itself; it consumes the JSON.
---

# agy-slide-analyzer

Delegates slide-image understanding to `agy` and gives Claude text it can trust: every slide gets a
structured analysis, validator flags and a second-opinion review. Builds on the **agy-delegate** skill
(`../agy-delegate/scripts/agy_task.py`, or set `AGY_TASK`).

Commands are relative to this skill's directory. Use `python3` on macOS/Linux, `python` on Windows.

## Workflow

1. **Preflight** (checks `agy` sign-in via agy-delegate):
   ```
   python scripts/slide_analyze.py preflight
   ```
2. **Pilot on 2-3 slides**, read the results, then run the whole deck. Give the personas the subject
   and a one-paragraph deck context (course, audience, topics) — it measurably improves transcription.
   ```
   python scripts/slide_analyze.py run --slides <dir> --pages 1-3 --subject 데이터베이스 --context "SSAFY 데이터베이스 강의 슬라이드. SQL DDL/DML, 모델링, 정규화 포함."
   python scripts/slide_analyze.py run --slides <dir> -j 4 --subject ... --context ...
   ```
   Full runs take ~1 min per slide / jobs; run them in the background. Runs are resumable: `ok` /
   `needs_attention` slides are skipped unless `--force`; `failed` slides are re-analysed and
   `unreviewed` slides only get the reviewer pass again. `--work` moves agy scratch dirs (default `<out>/.work`).
   A full deck costs ~2 agy sessions per slide (more with retries) and can exhaust the agy quota.
   When agy reports `RESOURCE_EXHAUSTED`, the run stops at once (queued slides are left pending, a slide
   whose review hit the limit keeps its analysis as `unreviewed`) and exits with code 3; tell the user
   the reset time from the log and re-run the same command afterwards.
3. **Read `report.md` first**, then `all_slides.json` (one object per slide with a `_qa` block).
   Do not open the images yourself — if a slide looks wrong, re-run it:
   `run --slides <dir> --pages N --force`.

## Trusting the output

| Signal | Meaning |
|---|---|
| `status: ok` + `review_verdict: pass` | validator clean and the reviewer found no meaningful errors |
| `review_verdict: minor_issues` | usable; see `_qa.review_errors` / `review_missing` for what to patch |
| `status: needs_attention` | still failing after the retry — report it to the user, do not paper over it |
| `concept_ungrounded`, `title_ungrounded` | a term is not in the transcribed text → possible hallucination |
| `fewshot_leak` | the few-shot example was copied (error, triggers retry) |
| `low_confidence`, `[?]` in text, `uncertain` | agy could not read part of the slide |
| `printed_page_diff`, `duplicate_content` | page order / duplicate-slide sanity checks |

When summarising for the user, quote counts from `report.md` and list every non-ok slide.

## Rules

- All image reading goes through agy. Claude reads only the JSON / report files.
- Read files agy writes (UTF-8); agy's stdout is mangled by the console code page on Windows.
- Prompts live in `prompts/analyst.md` and `prompts/reviewer.md` (`str.format` templates: literal
  braces are doubled). Keep the few-shot examples fictional and keep their marker strings in
  `slide_validate.FEWSHOT_LEAKS` in sync if you edit them.
- agy runs without `--allow-all`; it only needs to view the image and write one JSON file.
