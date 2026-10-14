---
name: agy-slide-analyzer
description: Analyse a folder of lecture slide images (PNG/JPG, one per page) by delegating every image to the Antigravity CLI (`agy`) — an analyst session per slide (persona + deck context + chain-of-thought + few-shot) writes structured JSON (verbatim text, tables, code, diagrams, key concepts, summary) — and verify each result WITHOUT a second LLM look at the image. A sealed local-OCR reference (Windows OCR / Tesseract + layout statistics), built before the analysis exists, is compared with the JSON by a deterministic scorer; clear omissions are retried with OCR-derived feedback; only disputed lines are cropped for Claude to read blind, plus a small audit sample. Use when the user wants lecture slides / page images transcribed, summarised or turned into study notes and says things like "슬라이드 분석해줘", "강의자료 이미지 agy로 분석", "analyse these slides with agy". Claude opens only the crops and thumbnails listed in review_queue.md, never the slides themselves.
---

# agy-slide-analyzer

Delegates slide-image understanding to `agy` and gives Claude text it can trust, with the verification done
by a **different vendor** than the analyst (no self-preference) and **without re-analysing the image**:

```
slide.png ──> [0] ocr_ref: local OCR + layout, sealed ──┐
          ──> [1] agy analyst ──> slide_analysis.json ──┴──> [2] validator + [3] scorer
                   ^                                               │ accept   → done (0 reviewer tokens)
                   └──── retry once, feedback = missing OCR lines ──┤ retry
                                                                   │ escalate → crops / thumbnail → Claude (blind) → resolve
                                                       [4] audit: random accepted slides → thumbnail → Claude (blind)
```

Builds on **agy-delegate** (`../agy-delegate/scripts/agy_task.py`, or set `AGY_TASK`). Commands are relative
to this skill's directory. Use `python3` on macOS/Linux, `python` on Windows.

## Workflow

1. **Preflight** (agy sign-in via agy-delegate, OCR engines):
   ```
   python scripts/slide_analyze.py preflight
   ```
   At least one OCR engine should be `available` (Tesseract with `kor`, or Windows OCR via `pip install winsdk`).
   Without one, slides end as `unverified` and the run is not a verification.
2. **Pilot on 2-3 slides**, read the results, then run the whole deck. Give the personas the subject and a
   one-paragraph deck context — it measurably improves transcription.
   ```
   python scripts/slide_analyze.py run --slides <dir> --pages 1-3 --subject 데이터베이스 --context "SSAFY 데이터베이스 강의 슬라이드. SQL DDL/DML, 모델링, 정규화 포함."
   python scripts/slide_analyze.py run --slides <dir> -j 4 --subject ... --context ...
   ```
   One agy session per slide (two when a retry fires). Runs are resumable: only `failed` slides are redone
   unless `--force`. On agy `RESOURCE_EXHAUSTED` the run stops at once (exit 3), tell the user the reset time
   from the log and re-run the same command later.
3. **Read `report.md`**: status counts, the pending review queue and its estimated image tokens, the audit
   line. Then `all_slides.json` (one object per slide with a `_qa` block). **Do not open the slide images.**
4. **Work the review queue (blind protocol)** when `review_queue.md` exists:
   - Open **only** the files it lists (`crops/*.png`): line crops and 640px thumbnails. Do not open the
     original slide and do not read the slide's analysis JSON before answering.
   - Write `crop_reads.json` in the output folder: `{"<id>": <answer>}` — for `line` items the text you read
     (`"[?]"` if unreadable); for `thumb_read` / `thumb_audit` a list of the main text lines you can read
     (no guessing); for `thumb_struct` `{"verdict": "ok"|"issue", "note": "..."}` after comparing what you
     see with the 분석가 선언 shown in the queue.
   - Run `python scripts/slide_analyze.py resolve --slides <dir>`. The script compares your reading with
     the analysis (numbers and identifiers must match exactly) and sets `accepted` / `needs_attention`.
   - If `grounding_queue.md` exists, judge (text only) whether `summary` / `key_concepts` / `exam_points`
     follow from the transcription, write `grounding_reads.json` (`{"<page>": {"unsupported": [...]}}`)
     and run `resolve` again.
5. **Report to the user**: counts from `report.md`, every non-`accepted` slide with its reason, and the audit
   line (how many accepted slides were spot-checked and the estimated residual error).

## Trusting the output

| Signal | Meaning |
|---|---|
| `status: accepted`, `decision: accept` | the sealed OCR reference agrees with the transcription (recall, digits, identifiers, structure); no reviewer needed |
| `status: accepted` after `resolve` | Claude's blind reading of the disputed crops matched the analysis |
| `status: escalated` | waiting for the review queue (step 4) |
| `status: needs_attention` | retry exhausted, or Claude's reading contradicted the analysis — report it, do not paper over it; `--force --pages N` re-runs it |
| `status: unverified` | no OCR reference could be built (no engine) and nothing to show — unverified, say so |
| `status: failed` | agy wrote nothing usable; re-run picks it up |
| `recall` / `unsupported` / `digits` columns | share of OCR text found in the analysis, share of analysis text the OCR never saw, share of OCR numbers present |
| `ref_thin`, `ref_unreliable` | the OCR could read little / badly; the decision leaned on Claude |
| `table_candidate_undeclared`, `graphics_undeclared` | the layout suggests a table / diagram the analysis does not declare (heuristic → thumbnail question) |
| `concept_ungrounded`, `title_ungrounded`, `grounding_issue` | an interpreted field is not backed by the transcription |
| `fewshot_leak`, `low_confidence`, `[?]`, `printed_page_diff`, `duplicate_content` | as before: leak (error, retried), self-reported uncertainty, page order / duplicate checks |
| audit line in `report.md` | `audited N of M accepted, K with issues → residual error X% (upper ≈ Y%)` — quote it |

## Calibration and benchmarking (optional, recommended once per kind of material)

- Copy 10-30 result analyses to a `gold/` folder as `<stem>.json`, correct them by hand (the user does this).
- `python scripts/slide_analyze.py calibrate --slides <dir> --gold gold/` fits `recall_accept` /
  `unsupported_accept` to a 5% false-accept target, reports how much of the gold text each OCR engine read,
  and writes `<out>/calibration.json` (picked up automatically by later runs; `--thresholds` overrides).
- `python scripts/slide_analyze.py bench --slides <dir> --gold gold/ --label <variant>` records true
  recall / precision / char similarity / digit and cell accuracy for prompt or model A/B in `bench.json`.
- `python scripts/slide_analyze.py rescore --slides <dir>` re-scores the stored analyses with the current
  thresholds / scorer without calling agy (previous queue answers are discarded).

## Rules

- All image reading goes through agy and local OCR. Claude reads JSON/report files and the queue's crops only.
- Answer the queue **before** looking at the analysis JSON of that slide (blind), and never copy OCR text
  into `crop_reads.json` — type what the crop shows.
- Read files agy writes (UTF-8); agy's stdout is mangled by the console code page on Windows.
- Prompts live in `prompts/analyst.md` (`str.format` template: literal braces are doubled). Keep the few-shot
  examples fictional and their marker strings in `slide_validate.FEWSHOT_LEAKS` in sync. `prompts/reviewer.md`
  is only used by the legacy `--reviewer agy` mode (same-vendor review; comparison runs only).
- agy runs without `--allow-all`; it only needs to view the image and write one JSON file.
- `scripts/ocr_ref.py`, `ocr_score.py`, `crops.py`, `textnorm.py` are shared with agy-pdf-analyzer; keep
  them here.
