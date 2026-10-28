# agy-slide-analyzer

Turn a folder of lecture slide images into verified, structured JSON by delegating each image to the
Antigravity CLI (`agy`), and verify every result **without a second LLM look at the image** and **without
the analyst's vendor reviewing itself**. Built on [agy-delegate](../agy-delegate/). Design notes:
[docs/ocr-evaluation-design.md](../../docs/ocr-evaluation-design.md).

```
all slides ──> [0] ocr_ref.py  local OCR + layout stats, sealed with each image's sha256 before any agy call;
                               header/footer-band lines at the same place on 5+ slides = the deck template
slide.png  ──> [1] agy analyst ──> slide_analysis.json
           ──> [2] slide_validate.py (schema, grounding, leaks, prompt-rule warnings)
           ──> [3] ocr_score.py  analysis vs. reference ──> accept | retry (feedback = missing OCR lines) | escalate
           ──> [4] crops.py     escalate: crops of disputed lines / 640px thumbnail ──> review_queue.md ──> Claude (blind)
                                audit: a deterministic sample of OCR-accepted slides gets an independent thumbnail read
                                (shadow mode: plus 30% of the slides only the new scorer rules accept)
           ──> [5] resolve      Claude's crop_reads*.json vs. the analysis ──> accepted | needs_attention
```

- **Reference** (`scripts/ocr_ref.py`, `REF_VERSION` 3): Tesseract (`tesseract` binary, `kor+eng`, word
  confidences) and/or Windows OCR (`winsdk`, no confidences). Word weights: `high` when two different engines
  agree (or Tesseract is confident), `variant` when only two passes of the same recognizer agree (base + 2x
  upscale, exact match after look-alike folding; never `high`), `mid` / `low` otherwise. Layout statistics:
  ink density, non-text ink (`graphics_ratio`), the same with dark code / terminal panels painted out
  (`graphics_ratio_nocode`, `code_boxes`; a dark region is a panel only when OCR words cover most of its light
  ink, so a dark-theme diagram stays graphics, and marks drawn on a panel count as graphics), ruling lines,
  table-like word grids (`table_candidate`, and the stricter `table_grid2`, whose columns are aligned starts,
  ends or centres, / `ruled_table2`), title band, dHash, `text_masked`.
- **Scorer** (`scripts/ocr_score.py`, scorer v2): weighted OCR recall (character-bigram matching, so
  per-syllable OCR splits do not matter, with an OCR look-alike fallback: `vaIue`, `c0unt`, `v—if`), unsupported
  share of the analysis text, digit and Latin-identifier recall on `high` / `variant` words, comparison
  operators, numbers and code-line order on the analysis line each OCR line anchors to (one-to-one, so a line
  the slide shows twice is compared with both copies; the order check also runs when the code is copied into
  `verbatim_text`), lines the analyst wrote twice that disagree, structure triggers. Deck-template lines do not
  count for recall; header/footer items do not count towards the "text-rich" size but are checked for
  hallucination like any other text, except the deck's own boilerplate: words that recur in the header/footer
  items of 3+ slides (`hf_boilerplate`: a logo the OCR never reads, a slogan, a section band) leave the
  unsupported share (they still count for recall). `run` scores a slide against the analyses on disk at that
  moment and, once the analyst pool ends, re-scores every slide of the deck without judgments against the whole
  deck without agy (settle; also after a quota stop or a partial `--force` run), so `run` and `rescore` decide
  and queue alike. Only the deck's own stems count: leftover results of a longer deck in the same `--out` do not. A thin reference accepts only content it supports, with the digit /
  identifier checks and the diagram labels included. A text-layer (PDF) reference is exact: an operator or
  number that differs from it is a retry, not a question for the reviewer. A decision policy, Korean retry
  feedback, gold-set metrics and threshold fitting.
- **Reviewer material** (`scripts/crops.py`): what Claude is allowed to see and how its blind answers are
  judged (numbers and identifiers must match exactly, comparison operators and numbers too, on a line crop and
  on every line of a full-slide read, against every copy of the line; a full-slide read also flags analysis text
  neither the OCR nor Claude saw, ignoring the deck's boilerplate; the crops of a line-order dispute are judged
  together, and readings in the right order on the slide but another order in the analysis are `agy_wrong`).
  Every queue item carries a fingerprint (`fp`, shown in review_queue.md) of what Claude was shown, and answers
  already on disk when `run` or `rescore` replaces a record are recorded as written for the outgoing item, so an
  answer to an older item is never applied to a new one. Such an item is answered again as
  `{"answer": ..., "fp": "<fp>"}`; the same plain answer stays ignored.
- **Validator** (`scripts/slide_validate.py`): schema / grounding / leak errors (malformed fields become
  `bad_type` / `bad_table` errors, never a crash) plus warnings for the prompt's rules:
  `interpretation_on_title_only`, `visual_undeclared`, `code_duplicated`, `illustration_with_relations`,
  `divider_with_body`, and `series_type_mixed` (info).
- **Analyst prompt** (`prompts/analyst.md`): instructional-designer persona, deck context, 5-step chain of
  thought, three fictional few-shot examples (table, SQL, code + capture + highlight), strict JSON schema.
  Captures are declared as `screenshot` diagrams with their UI labels, highlight marks as `annotation`
  diagrams with relations, labelled code breakdowns as `syntax_breakdown`; text moved into code / tables /
  diagrams is not repeated in `verbatim_text`; title-only slides get no exam points. `prompts/reviewer.md` is
  only used by the legacy `--reviewer agy` mode.

## Measured on a real 123-slide deck

One real lecture deck (123 slides, many of them HTML/JavaScript code on dark panels; Windows OCR only), the
stored agy analyses re-scored without new agy calls:

| | Stock scorer | Scorer v2 + variant OCR (defaults, `rescore` of a copy) |
|---|---|---|
| accept / escalate | 22 / 101 | 100 / 23 |
| review queue | 58 line crops + 101 thumbnails, ≈32.6k image tokens | 29 line crops + 17 thumbnails, ≈6.8k image tokens (58 items, ≈10.5k, with the default audit sample of 12 slides; 68 items, ≈13.5k, with shadow mode's 10 extra audits) |
| slides Claude's review had flagged (5) that get accepted | – | 0 (each still has a contradicting item queued) |
| graphics triggers (real ones kept) | 39 (37 were dark code panels) | 5 |
| table escalations | 21 (none was a real table) | 3 |

The stock run's real review cost 322,948 tokens for 159 items (≈2,030 tokens per item, whole reviewer-agent
contexts, about 10x the image tokens alone); at that rate the v2 queue with audits costs an estimated ≈116k
(≈136k with the shadow audits). With the 2x OCR pass off (`OCR_SCALES=1`) the same rescore gives 97 / 26.
(Six of the line crops are a centre line of the section-divider slides, which is no longer taken for deck template
because template lines are now looked for in the header/footer bands only.)
Escalation precision of the stock scorer was 5 of 101. Seeded mutations caught on accepted slides, measured
with the prototype of these rules (stock → v2): omitted
line 34/60 → 137/232, text from another slide 43/44 → 190/198, single-digit change 0/24 → 133/134, identifier
changes 5 → 115 of 474, operator swaps 0 → 4 of 6, code-line swaps 0 → 88 of 110 (71 before the order check
also ran on code copied into `verbatim_text`); 0 false alarms on the 123 stored analyses. Known blind spots:
`==`→`=`, `+`→`-`, `*`→`/`, `++`→`--`, a dropped `!`, a number glued to a unit (`50px`→`5px`) on a line no
second pass or engine confirmed. The thresholds were tuned on this one deck with one OCR engine, and the code-free
graphics threshold has a thin margin (a real case at 0.0165, the next false positive at 0.0142), so the
slides only the new rules accept are audited on the next decks (shadow mode, below).

**Shadow mode (rollout check).** Every scored record carries `score_legacy`, the decision and reasons of the
first-release rules (every scorer v2 switch off; on that deck they accept 16 of 123 slides with the variant
references, 22 with one OCR pass, as the stock scorer did), never used for the status. A stem-hash share of the
slides the scorer accepts but `score_legacy` does not (`--shadow-audit-rate`, default 0.3) gets the usual
`<stem>__thumb_audit` item (`audit_stratum: new_only`); `report.md` reports that stratum on its own line
(`Audit (new-rule-only accepts): audited N of M accepted, K with issues → estimated residual error X% …`) next
to a count of new-only accepts against the legacy ones. On the deck above that is 88 new-only accepts and 10
extra audit items (≈3.1k image tokens). Once two decks show no issue in that stratum, pass
`--shadow-audit-rate 0`.

## Requirements

- The [agy-delegate](../agy-delegate/) skill installed next to this one (or `AGY_TASK=/path/to/agy_task.py`)
  and `agy` signed in (see its README).
- Python 3.9+, Pillow (`pip install pillow`); numpy recommended (layout statistics).
- One OCR engine (two different ones are better):
  - **Tesseract** with Korean: Windows installer from the UB-Mannheim build (tick *Korean* under additional
    language data), `brew install tesseract tesseract-lang`, or `apt install tesseract-ocr tesseract-ocr-kor`.
    Found on PATH, via `TESSERACT_BIN`, or in the default install folder. No Python package needed.
  - **Windows OCR** (Windows 10/11): `pip install winsdk` and the Korean OCR language pack
    (Settings → Language → 한국어 → optional features → OCR; present when `C:\Windows\OCR\ko-kr` exists).
  - With Windows OCR alone, a 2x upscaled second pass (`winocr_x2`) is added automatically: OCR time goes from
    ≈0.15 to ≈0.5 s per slide, and digits / identifiers get checked on the words both passes read alike.
    `OCR_SCALES=1` turns it off, `OCR_SCALES=1,2,3` adds a 3x pass. It is never a substitute for a second
    engine: lecture-knowledge-pack still treats such slides as single-engine.
- Nothing is installed automatically; `preflight` reports what it found.

## Use

```bash
python scripts/slide_analyze.py preflight
python scripts/slide_analyze.py run --slides ./lm/slides --pages 1-3 \
  --subject 데이터베이스 --context "SSAFY 데이터베이스 강의. SQL DDL/DML, 모델링, 정규화."
python scripts/slide_analyze.py run --slides ./lm/slides -j 4 --subject 데이터베이스 --context "..."
# Claude answers review_queue.md into crop_reads.json (or, split: queue --shards 3, crop_reads_<k>.json), then:
python scripts/slide_analyze.py resolve --slides ./lm/slides
python scripts/slide_analyze.py report --slides ./lm/slides
```

Images are sorted by file name; that order is the page number. Output (default `<slides>/../slide_analysis`):

```
results/<stem>.json   per-slide record: status, decision, attempts, analysis, validation, score, score_legacy, queue, judgments
ref/<stem>.json       sealed OCR reference (lines, word boxes, weights, agreement, layout stats, image sha256)
crops/*.png           line crops and thumbnails for the reviewer
review_queue.md/json  the reviewer's blind work list; crop_reads.json / grounding_reads.json are its answers
review_queue_<k>.md   `queue --shards N`: disjoint parts, answered in crop_reads_<k>.json
applied_reads.json    which queue entry (fingerprint) each answer was judged against
all_slides.json       all analyses of the deck, each with a `_qa` block (status, decision, metrics, judgments, flags)
report.md             QA table, one row per slide, plus queue size / token estimates / audit lines (shadow stratum too)
slides_source.json    the deck manifest (folder, sha256 per image): this --out belongs to this deck
calibration.json      written by `calibrate` (with scorer_version); thresholds picked up by later runs
.work/                agy work directories and rendered prompts (safe to delete)
```

Results and report files are written atomically; an unreadable result is redone by `run` and reported as
missing by `report`, never fatal. The deck files always cover every slide that has a result; with `--pages`
only the returned counts and the exit code are limited to the selection.

## Commands and options

| Command | Purpose |
|---|---|
| `run --slides DIR` | references (whole deck) → analyst → validate → score → (retry) → queue; resumable |
| `reference --slides DIR` | build the sealed references only (no agy); useful before a long run; prints how many were built, kept (outdated, see below) and unchanged |
| `resolve --slides DIR` | apply `crop_reads*.json` / `grounding_reads.json`, set final statuses, rebuild the report |
| `queue --slides DIR --shards N` | rebuild the report and split the review queue into N disjoint files for parallel reviewers |
| `report --slides DIR` | rebuild `all_slides.json`, `report.md` and the review queue from `results/` |
| `rescore --slides DIR` | re-run validator + scorer on the stored analyses without agy (after `calibrate`, a scorer change or a reference rebuild); re-queues escalations from scratch and may rebuild references |
| `calibrate --slides DIR --gold DIR` | fit thresholds on human-corrected analyses (`<stem>.json`), compare OCR engines |
| `bench --slides DIR --gold DIR --label X` | true recall / precision / similarity / digit / cell accuracy per run |

| Flag (`run`) | Meaning |
|---|---|
| `--out DIR`, `--pages 1-10,15` | output folder (one deck per folder), subset (1-based) |
| `--subject`, `--context` | fills the analyst persona and deck context |
| `--engine auto\|winocr,tesseract` | OCR engines for the reference (default: every available one; `winocr` first) |
| `--audit-rate 0.1` | share of OCR-accepted slides queued for an independent blind thumbnail read (at least 2; 10 once 50+ are accepted) |
| `--shadow-audit-rate 0.3` | share of the slides only the new scorer rules accept (shadow mode) that also get a blind thumbnail read; `0` turns it off (also on `report`, `resolve`, `rescore`, `queue`) |
| `--grounding-check flagged\|all\|none` | which slides go to the text-only grounding queue |
| `--thresholds FILE` | thresholds JSON (default `<out>/calibration.json`, then the skill's `calibration.json`, then built-in) |
| `-j/--jobs N`, `--timeout MIN`, `--model` | parallel sessions (default 4), minutes per agy call (10), agy model slug |
| `--force` | redo slides that already have results (and take over an `--out` of another deck) |
| `--reviewer agy` | legacy mode: an agy reviewer session per slide instead of the reference + Claude flow |
| `--work DIR` | where agy work directories go |
| env `OCR_SCALES` | passes of a single OCR engine (default `1,2`) |

Thresholds JSON keys added in scorer v2 (all on by default; each can be switched off for a leave-one-out
comparison): `graphics_trigger_nocode` (0.015 on the code-free graphics share; `null` = the old 0.06 rule),
`table_v2`, `fold_match`, `template_exclusion`, `close_thin_shortcut`, `line_check`, `latin_mid`,
`copy_check`, `glued_digit_skip`, `hf_boilerplate`, `weights.variant` (1.0). With every switch off
(`ocr_score.legacy_thresholds`) the scorer is the first-release one: that is `score_legacy`. Older calibration
files keep loading; a file fitted under scorer v1 triggers a warning to refit.

**Reference rebuilds:** a reference older than `REF_VERSION` 3 (or built with another engine set, e.g. before
the variant pass) is rebuilt (OCR only, no agy) when its slide is re-analysed, by `reference`, and by `rescore`.
`run` alone does not touch the references of finished slides. A slide whose record holds Claude's judgments or
line crops still waiting in its queue keeps its old reference under `run` and `reference`, because those ids
name its line ids (a rebuild renumbers them); `rescore` (which drops judgments and queue) rebuilds it.

Exit codes: 0 every slide accepted, 2 some slides escalated / need attention / failed, 3 agy quota exhausted
(resumable), 4 agy-delegate missing or no OCR engine for `reference`, 5 bad arguments, an `--out` holding
another deck, or two different answers for one queue id.

**Quota:** one agy session per slide, two when a retry fires (the legacy mode costs two to four). On
`RESOURCE_EXHAUSTED` the run stops immediately, the log shows agy's "Resets in …", exit code 3; an analysis
already scored before the cut-off is kept. Re-run later.

## Tests

```bash
python -m unittest discover -s scripts/tests -v
```

`test_contract.py` pins what agy-pdf-analyzer and lecture-knowledge-pack use from the shared code
(signatures, reason strings, vocabularies, id formats, `norm()` golden values, the v3 additions) and runs both
siblings on fake-agy output when they sit next to this skill. The other files cover the reference builder, the
scorer, the judge and the pipeline (cp949 console, truncated results, deck collisions, stale answers, quota
during a retry); `test_regressions.py` holds one test per false accept or stale-answer path found in review
(a code walkthrough read as deck template, a dark diagram read as a code panel, header/footer-tagged
hallucinations next to the deck's header/footer boilerplate, `run` settling on the `rescore` decisions without
agy, thin references, swapped or duplicated code lines, number+unit digits, line-order disputes,
answers to replaced items, references renumbered under pending crops). Pure tests always run; engine and pipeline tests run when Pillow and an OCR engine are
present, with expectations that hold for one engine as well as two. The pipeline tests use
`scripts/tests/fake_agy_task.py` (set `AGY_TASK` to it to exercise the flow without agy); every fixture is
synthetic (`scripts/tests/synth.py`).
