---
name: agy-slide-analyzer
description: Analyse a folder of lecture slide images (PNG/JPG, one per page) by delegating every image to the Antigravity CLI (`agy`) — an analyst session per slide (persona + deck context + chain-of-thought + few-shot) writes structured JSON (verbatim text, tables, code, diagrams, key concepts, summary) — and verify each result WITHOUT a second LLM look at the image. A sealed local-OCR reference (Windows OCR / Tesseract + layout statistics), built before the analysis exists, is compared with the JSON by a deterministic scorer; clear omissions are retried with OCR-derived feedback; only disputed lines are cropped for Claude to read blind, plus a small audit sample. Use when the user wants lecture slides / page images transcribed, summarised or turned into study notes and says things like "슬라이드 분석해줘", "강의자료 이미지 agy로 분석", "analyse these slides with agy". Claude opens only the crops and thumbnails listed in review_queue.md, never the slides themselves.
---

# agy-slide-analyzer

Delegates slide-image understanding to `agy` and gives Claude text it can trust, with the verification done
by a **different vendor** than the analyst (no self-preference) and **without re-analysing the image**:

```
every slide ──> [0] ocr_ref: local OCR + layout, sealed (all slides, before the first agy call)
                    deck template lines (same text, same place in the header/footer band on 5+ slides) are set aside
slide.png   ──> [1] agy analyst ──> slide_analysis.json ──> [2] validator + [3] scorer
                     ^                                          │ accept   → done (0 reviewer tokens)
                     └──── retry once, feedback = missing OCR lines ┤ retry
                                                                │ escalate → crops / thumbnail → Claude (blind) → resolve
                                         [4] audit: OCR-accepted slides → thumbnail → Claude (blind)
                                             + shadow mode: 30% of the slides only the new scorer rules accept
```

Builds on **agy-delegate** (`../agy-delegate/scripts/agy_task.py`, or set `AGY_TASK`). Commands are relative
to this skill's directory. Use `python3` on macOS/Linux, `python` on Windows.

## Workflow

1. **Preflight** (agy sign-in via agy-delegate, OCR engines):
   ```
   python scripts/slide_analyze.py preflight
   ```
   At least one OCR engine should be `available` (Tesseract with `kor`, or Windows OCR via `pip install winsdk`).
   Without one, slides end as `unverified` and the run is not a verification. With exactly one engine, the run
   adds an upscaled second pass of it (`winocr_x2`; `OCR_SCALES`, default `1,2`, `OCR_SCALES=1` turns it off)
   so numbers and identifiers still get checked; it costs about +0.35 s per slide.
2. **Pilot on 2-3 slides**, read the results, then run the whole deck. Give the personas the subject and a
   one-paragraph deck context — it measurably improves transcription.
   ```
   python scripts/slide_analyze.py run --slides <dir> --pages 1-3 --subject 데이터베이스 --context "SSAFY 데이터베이스 강의 슬라이드. SQL DDL/DML, 모델링, 정규화 포함."
   python scripts/slide_analyze.py run --slides <dir> -j 4 --subject ... --context ...
   ```
   `run` seals the reference of **every** slide of the deck first (also on a `--pages` pilot), so the deck
   template is known. One agy session per slide (two when a retry fires). Resumable: only `failed` slides and
   slides whose image changed are redone unless `--force`. One `--out` belongs to one deck
   (`slides_source.json`); another deck's folder is refused with exit 5. On agy `RESOURCE_EXHAUSTED` the run
   stops at once (exit 3); tell the user the reset time from the log and re-run the same command later.
   Header/footer items whose words recur in the header/footer items of 3+ slides (the deck's logo, slogan,
   section band) do not count as unsupported text. A slide is first scored against the analyses on disk at that
   moment; once every slide is in, `run` re-scores every slide of the deck that has no judgments yet against the
   whole deck without agy (`settle:` lines in the log; also the slides of an earlier, quota-stopped run and those
   outside `--pages`), so `run` and `rescore` end with the same decisions and queue. Results of stems that are not
   in the slides folder (left over from a longer deck) never count.
3. **Read `report.md`**: status counts (and, after `--pages`, the counts of that selection), the pending
   review queue with its image-token estimate and the measured ≈2k total tokens per item, the audit lines
   (shadow mode adds two, see below).
   Then `all_slides.json` (always the whole deck, one object per slide with a `_qa` block).
   **Do not open the slide images.**
4. **Work the review queue (blind protocol)** when `review_queue.md` exists:
   - Open **only** the files it lists (`crops/*.png`): line crops and 640px thumbnails. Do not open the
     original slide and do not read the slide's analysis JSON before answering.
   - Write `crop_reads.json` in the output folder: `{"<id>": <answer>}` — for `line` items the text you read
     (`"[?]"` if unreadable); for `thumb_read` / `thumb_audit` a list of the main text lines you can read
     (no guessing); for `thumb_struct` `{"verdict": "ok"|"issue", "note": "..."}` after comparing what you
     see with the 분석가 선언 shown in the queue.
   - Several reviewers: `python scripts/slide_analyze.py queue --slides <dir> --shards N` writes disjoint
     `review_queue_<k>.md` files; each answers into `crop_reads_<k>.json`. `resolve` merges them and stops
     (exit 5) when one id has two different current answers (an answer written for an older version of the
     item drops out first).
   - Run `python scripts/slide_analyze.py resolve --slides <dir>`. The script compares your reading with
     the analysis (numbers and identifiers must match exactly, and comparison operators too, in a line crop
     and in every line of a full-slide read, against every copy of the line in the analysis) and sets
     `accepted` / `needs_attention`. The crops of a `line_order_mismatch` are judged together once all are
     answered: readings that anchor to the analysis in another order than they stand on the slide make both
     `agy_wrong`. An answer written for an older queue entry (a re-run or `rescore` replaced the record, and
     the item's fingerprint changed) is ignored and the item stays queued; look at it again and answer it as
     `{"answer": <answer>, "fp": "<fp>"}` with the `fp` shown for it in `review_queue.md` (the same plain
     answer stays ignored).
   - If `grounding_queue.md` exists, judge (text only) whether `summary` / `key_concepts` / `exam_points`
     follow from the transcription, write `grounding_reads.json` (`{"<page>": {"unsupported": [...]}}`)
     and run `resolve` again.
5. **Report to the user**: counts from `report.md`, every non-`accepted` slide with its reason, and the audit
   lines (OCR-accepted slides with independent audits; Claude-accepted slides, which are not audited; the
   new-rule-only stratum and the shadow count).

## Shadow mode (rollout check for the next decks)

The scorer's thresholds and rules (the code-free graphics threshold, the deck-template and header/footer
boilerplate rules, the thin-reference rule) were fitted on one deck. Until they have held on other decks, every
scored record also carries `score_legacy` — `{decision, reasons}` of the first-release rules (every scorer v2
switch off), never used for its status — and `report.md` shows:

- `Audit (new-rule-only accepts): audited N of M accepted, K with issues → estimated residual error X% …` —
  the slides the scorer accepts but the first-release rules would not; a stem-hash share of them
  (`--shadow-audit-rate`, default 0.3) gets the usual `<stem>__thumb_audit` item (`audit_stratum: new_only`
  in the record; a slide is audited once, and the OCR-accepted stratum keeps its own minimum and estimate);
- `Shadow mode (score_legacy): the scorer accepts A of S scored slide(s), its first-release rules L; …` —
  how many accepts are new-only, and how many only the old rules would give.

Answer these audit items like any other. Quote both lines to the user. Once two decks show no issue in the
new-only stratum, turn the shadow audits off with `--shadow-audit-rate 0` (`score_legacy` is still written;
it costs no reviewer tokens).

## Trusting the output

| Signal | Meaning |
|---|---|
| `status: accepted`, `decision: accept` | the sealed OCR reference agrees with the transcription (recall, digits, identifiers, operators, line order, structure); no reviewer needed |
| reasons `little text on both sides`, `thin reference, content supported` | little text on the slide, and everything the analysis transcribes (header/footer items and diagram labels included) is in the reference, numbers and identifiers too (lecture-knowledge-pack labels it `thin_accepted`); `thin reference, content not confirmed` escalates instead |
| `status: accepted` after `resolve` | Claude's blind reading of the disputed crops matched the analysis (for a line-order dispute: in the same order) |
| `status: escalated` | waiting for the review queue (step 4) |
| `status: needs_attention` | retry exhausted, or Claude's reading contradicted the analysis — report it, do not paper over it; `--force --pages N` re-runs it |
| `status: unverified` | no OCR reference could be built (no engine) and nothing to show — unverified, say so |
| `status: failed` | agy wrote nothing usable; re-run picks it up |
| `recall` / `unsupported` / `digits` columns | share of OCR text found in the analysis (deck template lines left out), share of analysis text the OCR never saw, share of OCR numbers present |
| `operator_or_digit_mismatch`, `line_order_mismatch`, `copy_mismatch` | an OCR line has another comparison operator or number than its analysis line, code lines are out of order, or a line written twice disagrees with itself (→ crops) |
| `ref_thin`, `ref_unreliable` | the OCR could read little / badly; the decision leaned on Claude |
| `table_candidate_undeclared`, `graphics_undeclared` | the layout suggests a table / diagram the analysis does not declare (dark panels whose light ink OCR reads as text are code and do not count as graphics; a dark diagram and marks drawn on a panel do; → thumbnail question) |
| `concept_ungrounded`, `title_ungrounded`, `interpretation_on_title_only`, `grounding_issue` | an interpreted field is not backed by the transcription (these go to the text-only grounding queue) |
| `visual_undeclared`, `code_duplicated`, `illustration_with_relations`, `divider_with_body`, `series_type_mixed` | the analyst broke a prompt rule: a capture / highlight mark not declared as a `screenshot` / `annotation` diagram, code repeated in `verbatim_text`, relations on an `illustration`, a divider with body text, a `(k/n)` series typed inconsistently (warnings / info) |
| `fewshot_leak`, `low_confidence`, `[?]`, `printed_page_diff`, `duplicate_content` | leak (error, retried), self-reported uncertainty, page order / duplicate checks |
| audit lines in `report.md` | `Audit (OCR-accepted): audited N of M accepted, K with issues → estimated residual error X% (upper ≈ Y%)`, `Claude-accepted: …`, and in shadow mode `Audit (new-rule-only accepts): …` and `Shadow mode (score_legacy): …` — quote them all |
| `score_legacy`, `audit_stratum` in a record | shadow mode: the first-release rules' decision (not the status), and which audit stratum (`ocr` / `new_only`) took the slide |
| `metrics.boilerplate_chars` | characters of deck header/footer boilerplate left out of `unsupported` on this slide |

## Calibration and benchmarking (optional, recommended once per kind of material)

- Copy 10-30 result analyses to a `gold/` folder as `<stem>.json`, correct them by hand (the user does this).
- `python scripts/slide_analyze.py calibrate --slides <dir> --gold gold/` fits `recall_accept` /
  `unsupported_accept` to a 5% false-accept target, reports how much of the gold text each OCR engine read,
  and writes `<out>/calibration.json` with `scorer_version` (picked up by later runs; `--thresholds`
  overrides; a file fitted under an older scorer is used with a warning — refit it).
- `python scripts/slide_analyze.py bench --slides <dir> --gold gold/ --label <variant>` records true
  recall / precision / char similarity / digit and cell accuracy for prompt or model A/B in `bench.json`.
- `python scripts/slide_analyze.py rescore --slides <dir>` re-scores the stored analyses with the current
  thresholds / scorer without calling agy (previous queue answers are discarded unless the re-created item is
  identical; references are rebuilt when the reference format changed). `reference` rebuilds outdated
  references too, except those of slides whose judgments or queued line crops name their line ids, and prints
  built / kept / unchanged counts; `run` rebuilds only the references of slides it re-analyses. Every check added in scorer v2 has an off-switch in the thresholds JSON
  (`template_exclusion`, `close_thin_shortcut`, `line_check`, `latin_mid`, `copy_check`, `fold_match`,
  `table_v2`, `glued_digit_skip`, `hf_boilerplate`, `graphics_trigger_nocode: null`) for leave-one-out
  comparisons; with all of them off the scorer is the first-release one (`score_legacy`).

## Rules

- All image reading goes through agy and local OCR. Claude reads JSON/report files and the queue's crops only.
- Answer the queue **before** looking at the analysis JSON of that slide (blind), and never copy OCR text
  into `crop_reads.json` — type what the crop shows.
- Read files agy writes (UTF-8); agy's stdout is mangled by the console code page on Windows. This skill's
  own stdout is UTF-8 (JSON summary, exit code intact on a cp949 console).
- Prompts live in `prompts/analyst.md` (`str.format` template: literal braces are doubled). Keep the few-shot
  examples fictional and their marker strings in `slide_validate.FEWSHOT_LEAKS` in sync (a test checks it), and
  keep the enums (`slide_type`, regions, the diagram `kind` list with `syntax_breakdown` / `screenshot` /
  `annotation`) identical in agy-pdf-analyzer's prompt and validator. `prompts/reviewer.md` is only used by
  the legacy `--reviewer agy` mode (same-vendor review; comparison runs only).
- agy runs without `--allow-all`; it only needs to view the image and write one JSON file.
- `scripts/ocr_ref.py`, `ocr_score.py`, `crops.py`, `textnorm.py`, `slide_analyze.py` are shared with
  agy-pdf-analyzer and lecture-knowledge-pack; keep them here, change them only additively and run
  `scripts/tests/test_contract.py` (it pins what the siblings use) before changing them.
