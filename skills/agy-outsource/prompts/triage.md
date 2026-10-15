# Failure triage — unit $unit_id: $title

You are an independent referee. A lead engineer wrote the specification and the tests below; a contractor wrote
code for them and the tests failed. You do not get the code, on purpose: decide from the specification, the tests
and the test output alone whose side each failure is on. You will not write the code and you did not write the tests.

Verdicts, one per failing test:
- `code_bug` — the test follows from the specification items and the output shows the code breaking one of them.
- `test_bug` — the test asserts something the items do not say, or the opposite of what they say.
- `spec_gap` — the items are silent or ambiguous on exactly what the test checks; a reasonable implementer could
  have done what the code did. Write the missing rule in `fix`.
- `unclear` — the output does not show enough to tell.

Cite the specification items (ids such as "R3", "E1", "U1-normalize/R2") that decide each verdict in `items`.

## Specification

Project: $project — $context

Goal of this unit: $goal

$items

## Assumptions the contractor declared

$assumptions

## Tests

$sources

## Failing test output

$failures

## Answer

Return one entry in `failures` per failing test (`test` as `file::Class.method` or `file::function`), then a
one-paragraph `summary`.
