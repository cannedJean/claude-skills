# Trust model

## text_check (what agy-slide-analyzer actually verified, from `results/<stem>.json`)

| analyzer status | reasons / judgments | text_check |
|---|---|---|
| accepted | judgments present, none against the analysis | `claude_verified` |
| accepted | `little text on both sides` / `no_reference_little_text` | `thin_accepted` |
| accepted | otherwise ("reference agrees") | `ocr_verified` |
| escalated | | `review_pending` |
| needs_attention | | `needs_attention` |
| unverified | | `unverified` |
| failed / no result file | | `image_only` |
| ok / unreviewed (legacy reviewer mode) | | `legacy_unverified` |

`provenance`: `ok` when `ref/<stem>.json` sha256 equals the current image; `stale` when it differs, when the
result's `file` is neither the image nor the PDF named in agy-pdf-analyzer's `source.json`, or when a PDF reference's
`source_sha` is not the current `source.json` sha; `unverifiable` when there is no reference or the reference has no
image sha (an agy-pdf-analyzer text-layer page).

Freshness: ingest stores `results_sha` and `ref_sha` (the bytes of `results/<stem>.json` and `ref/<stem>.json`). An
analyzer `resolve` or `rescore` after ingest changes them; status then says `ingest`, and G9 fails until ingest runs
again, so a corrected value never sits behind an old label.

`digits_checked`: at least one OCR word has weight `high`, i.e. two engines agreed on it (a PDF text layer is a
trusted reference, so its words are all `high`). With one engine
(winocr gives no confidences) the analyzer checks no digit or identifier at all; the struct stage then adds
`line` regions (at most 6 per slide) and sets `digits_checked_by_pack` once every exact token of the OCR lines is
accounted for; when the cap left candidate lines out, it stays false.

## Routing trust (per slide, `kp_ingest.slide_trust`)

| trust | rule |
|---|---|
| image_only | text_check `image_only` |
| low | provenance `stale`; text_check `needs_attention`, `unverified`, `review_pending`, `legacy_unverified`; any region `disputed` or `not_found`; `thin_accepted` slide with graphics and no diagram whose visual check is not ok |
| high | text_check `ocr_verified` or `claude_verified` + digits checked (analyzer or pack) + no `[?]` left + every region in `agreed`, `minor`, `corrected`, `agy_upheld`, `filled` |
| medium | any region `missed_content` (text two blind reads agree on, attached to the card); everything else (unchecked or skipped regions, digits unchecked, thin text-only slides) |

## Build sequences and boilerplate (ingest)

`build_of`: consecutive slides with the same title where everything the earlier one shows besides its title and
header/footer (verbatim text, every code block, every table row with its headers, every diagram element and
relation) is still on the next one, which shows more. A slide with only its title is not a step. The index folds a
run into its last slide; mentions count the last slide, plus a concept's own definition slide.

`boilerplate`: verbatim lines on at least half of the lecture's slides (min 3), never the title. A build run counts
as one slide, so the bullets a progressive reveal keeps on every step stay content (digest, mentions, card body).

## Regions (struct stage)

| kind | selected when | compared as |
|---|---|---|
| code | every `code_blocks[i]`; verbatim items with >= 2 SQL/Python-looking lines | operator-preserving tokens; indentation levels for Python/YAML |
| table | >= 4 cells, or any `[?]` cell | cell grid by position (shape first) |
| diagram | kind != illustration with relations or >= 3 elements | directed edge set + cardinality labels |
| formula | verbatim item with math characters / Greek / `a = b`, < 30% Hangul | math tokens (x² = x^2, α = alpha) |
| uncertain | verbatim item containing `[?]` | two blind reads must agree -> `filled` |
| line | single-engine or thin slides: OCR lines whose digits / identifiers the analysis lacks; lines the analyzer judged `agy_wrong` | exact tokens, patched into the one line of the agy item the read matches; a read the analysis already contains (no item of its own, e.g. split across items) is `agreed`; lines on a table region are left to the table check |
| visual | `thin_accepted` + graphics + no diagram declared | completeness: text or edges the analysis lacks -> `missed_content` |

States: `agreed` / `minor` (read 1 matches), `agy_upheld` (read 2 sided with the analysis), `corrected`
(two reads agree against it; the card shows the corrected value, the original stays in `slides/<id>.json`),
`filled` (`[?]` filled by two agreeing reads), `disputed` (no majority), `not_found` (asked twice, crop then
whole slide), `missed_content`, `skipped_budget`, `skipped`, and while running `new`, `queued`,
`needs_second_read`, `requeue_view`. A region restarts (`new`) when its inputs change: `region_sha` covers the image and
the analysis side it is compared with (the part; for a line region the item it patches and the OCR line; for a
visual region the whole transcription).

Crops: all regions of a slide are located in one pass over whole OCR lines (operators kept; needles of 3 characters
or fewer match exactly; each OCR line goes to one needle, identical lines to the place nearest the region's other
lines). A crop that cannot be placed unambiguously falls back to the slide view, whose questions name the object
as "N개 중 k번째" with boxes in view pixels. Code, table and formula crops are then widened to the visual block
(`kp_regions.block_box`): a box with its own background (a dark code box) is taken whole; on the slide background the
crop runs to the end of the ink and to a border line, because OCR often misses lone `})` lines and line ends.

Round 2 is always read on the whole slide view (the round-1 crop stays as evidence): two readers of one cut crop
would agree on what it cut off. A vote never deletes analysis content: a correction that only removes text is
`agy_upheld`; one that removes text and also changes something is `disputed`. Real-deck test (SSAFY Vue, 123 slides):
before these rules 29 "corrections" were mostly truncations and added 15 critical errors to 24 audited cards; after,
2 corrections, 0 critical errors.

Batches: `pending` until their reads are taken in (`done`); `cancelled` when no region waits on them any more
(`--skip`, or the slide changed), with the `.md` removed so nobody answers it.

Line regions (decision rules): read 1 is `agreed` when its text is anywhere in the analysis (code blocks included;
curly quotes, spacing and bullets ignored). Otherwise read 2 is taken on the slide view and both reads are compared:
a read in the analysis -> `agy_upheld`; reads that agree (or one is a fragment of the other, cut mid-word or not) are
patched into their unit only when the OCR line clearly is that unit (similarity >= 0.6), else attached as
`missed_content`; reads that differ only by marks the analysis leaves out (▼, ▶, bullets) -> `minor`; reads that
disagree -> `disputed`. `struct --redecide` re-applies these rules to stored reads after an upgrade (no new reads;
regions that now need a second read are batched). Real-deck test (SSAFY Vue): 13 line disputes, mostly reads matched to
the wrong unit (code skipped, URL fragments, curly quotes), became 0; the DevTools labels both readers saw are on the
cards as missed_content.
