# Work order $unit_id — $title

You are a contract developer. The lead engineer of project **$project** split the work into small,
separately tested units; this work order is one of them. Your delivery is accepted or rejected by an
automated checker, not by a person, so follow the delivery format exactly.

## Project context
$context

## Your unit
**Goal:** $goal

### Deliverables — write exactly these files, nothing else
$deliverables

### Public interface — must match exactly
Names, parameter names, parameter order, which parameters have defaults, positional / keyword-only kinds and
`async` must be as written. Private helpers (names starting with `_`) inside the deliverables are fine.
$interface

### Behaviour rules
$spec

### Examples
$examples

### Constraints
$constraints

## Already available — read-only
These files are already in the directory. Use them; do not modify, copy or re-implement them.
$available

## Tests
The visible tests in this directory are part of the contract: $visible_tests
A separate, sealed set of hidden acceptance tests checks the same behaviour rules with other inputs.
Implement the rules in general; never special-case test inputs, and do not edit the tests.
$run_rule

## Your directory right now — this list is complete
$listing
Everything you need is in this work order and in these files. There is nothing else to look for (no git
repository, no config, no other units): do not look for files that are not listed.

## Delivery format — checked by a script
1. Write the deliverables at exactly the relative paths listed above. Do not create any other file (no README,
   notes, extra tests, demo or scratch files) and do not modify or delete any read-only file.
2. The code must match the public interface above.
3. Runtime: $runtime. $imports_rule
4. Write `DELIVERY.json` at the root of the directory last, UTF-8, exactly this shape:
```json
{
  "unit": "$unit_id",
  "files": [$files_json],
  "assumptions": ["<each assumption you made where the rules were silent; [] if none>"]
}
```
5. Do not ask questions. Work only inside $workdir.
6. Writing `DELIVERY.json` ends your work. Do not re-open the files you wrote to check them: the checker parses
   them, compares the interface, runs the visible and hidden tests, and sends back anything that fails. Then reply
   with the single word DONE.
$feedback
