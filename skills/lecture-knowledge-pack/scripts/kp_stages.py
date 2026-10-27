"""kp_stages.py - CLI handlers for the advancing stages (struct, integrate, merge).

Each handler does one idempotent step, prints a JSON summary and exits 0 when the stage is complete or 2 when it
left work for Claude (batches for subagents, deck digests, a merge queue). Re-running after the work is answered
continues where it stopped.
"""
from __future__ import annotations

import kp_common as kc


def cmd_struct(a, course) -> int:
    import kp_verify
    lectures = kc.select_lectures(course, a.lectures)
    try:
        res = kp_verify.advance(course, lectures, {"budget_tokens": a.budget_tokens, "kinds": a.kinds,
                                                   "batch_items": a.batch_items, "batch_tokens": a.batch_tokens,
                                                   "skip": a.skip, "force": a.force, "redecide": a.redecide})
    except FileNotFoundError as e:
        print(kc.dumps({"ok": False, "error": str(e)}))
        return kc.EXIT_PENDING
    pending = res["pending_batches"]
    nxt = ("for each pending batch launch one fresh subagent with prompts/struct_read.md "
           "(batch path + answer path), then run `struct` again") if pending else "integrate"
    if res["unreadable_reads"]:
        nxt = ("the reads in unreadable_reads are not a JSON object and were not taken in: have a fresh subagent "
               "answer those batches again; " + nxt)
    res.update(ok=True, complete=not pending, next=nxt)
    print(kc.dumps(res))
    return kc.EXIT_PENDING if pending else kc.EXIT_OK


def cmd_integrate(a, course) -> int:
    import kp_integrate
    res = kp_integrate.advance_integrate(course, kc.select_lectures(course, a.lectures),
                                         {"digest_max_tokens": a.digest_max_tokens})
    print(kc.dumps(res))
    return kc.EXIT_OK if res.get("complete") else kc.EXIT_PENDING


def cmd_merge(a, course) -> int:
    import kp_integrate
    res = kp_integrate.advance_merge(course)
    print(kc.dumps(res))
    return kc.EXIT_OK if res.get("complete") else kc.EXIT_PENDING


HANDLERS = {"struct": cmd_struct, "integrate": cmd_integrate, "merge": cmd_merge}
