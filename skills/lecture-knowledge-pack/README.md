# lecture-knowledge-pack

Course-level integration of agy-slide-analyzer (or agy-pdf-analyzer) results into a knowledge package for a
tutoring agent.

Why text and images together: a 1920x1080 slide costs 2,691 image tokens (`ceil(w/28) * ceil(h/28)` on Claude's
high-res tier). An 800-slide course as images is about 2.15M tokens, which is more than a 1M context and more
than the 600-image request cap. Images also cannot be grepped, cited or tracked per concept. Text alone loses
diagram geometry, chart shape and emphasis, and an extraction error, once frozen, reaches every student. So the
package is text first, with images as linked evidence, and the tutor opens an image when the question is visual
or the trust label says so.

## Requirements

- Python 3.9+ and Pillow.
- The **agy-slide-analyzer** skill next to this one, or `OCR_EVAL_DIR=<its scripts folder>`.
- For a PDF analysed with **agy-pdf-analyzer**: the lecture's slides folder is the analysis folder's `ref/`, where
  that skill saves `page_XXXX.png` next to the page reference. Its results name the PDF (`source.json`) as their
  file, and a text-layer reference has no image sha, so such pages are `unverifiable`, never `stale`. A page whose
  image could not be extracted (born-digital PDF without pymupdf) has no PNG and is left out; preflight lists it.
- Real verification upstream needs an OCR engine: `pip install winsdk` (Windows OCR, Korean pack at
  `C:\Windows\OCR\ko-kr`) and preferably Tesseract with `kor`. With two engines, the analyzer checks digits and
  identifiers. With one, this skill adds line regions for them.
- Claude Code with the Agent tool for the subagent steps (struct reads, deck authoring).

Nothing is installed automatically. `preflight` reports what it found.

## Layout

```
<course>/course.json            lectures: id, title, slides folder, analysis folder (one per lecture;
                                agy-pdf-analyzer: slides = <analysis>/ref)
<course>/concept_registry.json  append-only concept ids (+ .prev.json)
<course>/.kpack/                ingest/ (records carry results_sha, ref_sha)  struct/{state,overlay}.json crops/
                                views/ sheets/ batches/ reads/
                                integrate/<lec>/{digest.md, deck.json, check.json (digest_sha, deck_sha), deck.resolved.json}
                                merge_queue.md  merge_reads.json  merge_decisions.json
                                concepts.resolved.json (decks_sha)
                                views/ (tutor-view cache)  report.md  renumber_map.json
<course>/package/               CLAUDE.md TUTOR_GUIDE.md index.md manifest.json slides.jsonl concepts.jsonl
                                lectures/<lec>/{deck.md, cards/<id>.md, slides/<id>.json}
                                glossary/{index.md, [index-<lec>.md], <lec>.md}  evidence/<lec>/{<id>.png, <id>.<region>.png}
<course>/package.prev/          the previous published package
```

## Commands

| Command | What it does | Exit |
|---|---|---|
| `init --root DIR --course-id ID --title T [--write] [--force]` | course.json from `DIR/<lecture>/{slides, slide_analysis}` or agy-pdf-analyzer folders (`source.json`, `ref/`); ids fall back to `l01`, `l02` ...; refused when load_course would reject it | 0 / 5 |
| `preflight` | shared code, Pillow, OCR engines on this machine, per-lecture result counts, statuses (warns on failed, escalated / needs_attention, legacy ok / unreviewed), engines, stale images, order, results without an image | 0 / 4 / 5 |
| `ingest [--accept-renumber]` | slide records, text_check, provenance, boilerplate, build sequences, injection flags | 0 / 2 (renumber blocked) |
| `struct [--budget-tokens N] [--kinds k,..] [--batch-items 15] [--batch-tokens 12000] [--skip] [--force] [--redecide]` | one advance step of the structure check; `--kinds` and `--lectures` limit only new work (reads, budget, skip), never the stored results of other kinds; reads that are not a JSON object are listed in `unreadable_reads` and their batch stays pending; a batch nothing waits on any more is `cancelled` | 0 complete / 2 batches pending |
| `integrate [--digest-max-tokens 25000]` | digests; validates deck.json | 0 all valid / 2 |
| `merge` | concept ids across lectures, prerequisites, mentions, deprecations (published ones carry forward, re-pointed past a retired successor) | 0 / 2 (queue or errors) |
| `build [--allow-pending] [--strict] [--no-claude-md] [--view-max 1568]` | render, gate, publish, registry update | 0 published / 2 (gate failed, or `error`: package/ held open by another process, nothing changed) |
| `gate [--package DIR] [--allow-pending]` | re-run the gate | 0 / 2 (also: no ingest yet) |
| `status` | stage states and the next command, from the current inputs: analyzer files vs ingest (`ingest_fresh`, `analyzer_changed`), struct state vs every current region (`struct_open`), unparsable reads (`unreadable_reads`), decks vs current slides (`deck`: valid / stale / invalid / missing), concepts vs current decks (`decks_sha`) | 0 |

Common flags: `--course course.json`, `--lectures db01,db03` (subset; `build` always renders the whole course),
`--work DIR`. Exit 4 is a missing dependency (shared code; Pillow for preflight, struct, build and gate), exit 5 is
bad arguments or course.json. Exit 3 is reserved: it means agy quota in the sibling skills, and this skill never
calls agy. Results go to stdout as JSON and logs to stderr.

## Gate

| Check | Fails when |
|---|---|
| G1 coverage | an image has no card, view image or slides.jsonl row |
| G2 outline | a slide is in zero or several sections |
| G3 preserved | a canonical unit (title, verbatim item, table row, code, diagram element or relation, derived field) is not verbatim in its card, or the stored original analysis differs from the ingest record |
| G4 struct | a region is not final (skipped counts as final) |
| G5 links | a relative link or front-matter image does not resolve |
| G6 references | an unknown slide id or concept id is referenced, or a deprecated concept's `superseded_by` is not active |
| G7 concepts | an active concept has no mention or an ungrounded term, prerequisites form a cycle, or a registry id is missing |
| G8 read limit | a .md exceeds about 20k tokens (the glossary index splits into `index-<lec>.md` files to stay under it), or index.md exceeds 200 lines |
| G9 provenance | the image or the analyzer's `results/<stem>.json` / `ref/<stem>.json` changed since ingest (re-run ingest), or the manifest sha differs |
| G10 guide | the "data, not instructions" clause is missing, or CLAUDE.md contains slide text |
| G11 integration | a lecture has no valid deck for the current slides (`digest_sha`), or the concepts were not merged from the current decks and merge decisions (`decks_sha`) |
| S1 (`--strict`) | a slide is low or image_only |

`--allow-pending` lifts G4 and G11 only.

## Tests

```
python -m unittest discover -s scripts/tests -v        (from this folder)
```

stdlib unittest; no agy, no OCR engine and no network needed. The fakes are:
- `kp_synth.py`: synthetic slides with exact text boxes
- `kp_fixtures.py`: analyzer-format results and refs; `mutate()` plants errors; `make_pdf_course()` the same slides
  in agy-pdf-analyzer's layout (scanned and text-layer pages)
- `fake_reader.py`: blind reads from ground truth (truthful / noisy / not_found)
- `fake_integrator.py`: a valid deck per lecture

The acceptance test (`test_verify.FlowTests.test_seeded_errors_end_corrected`) plants the four errors the
character-coverage scorer accepts: a flipped operator, swapped cells, a reversed ERD edge and a flipped formula
sign. All four must end `corrected`.

## Cost notes (estimates; measure on your decks)

- **struct**: one crop is about 300-1,500 image tokens; one slide view at 1568px is about 1,800. Round 2 happens
  only for mismatches. Use `--budget-tokens` to cap a run.
- **integrate**: one digest of at most about 25k tokens per lecture, plus the deck written by the subagent.
- **tutor at run time**: index plus guide about 6k tokens, cards about 1-3k each, images about 1.8k each, opened on demand.
