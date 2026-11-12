#!/usr/bin/env python3
"""fake_agy_dr.py - deterministic offline simulator for `agy_task.py --deep-research` and `relay_step.py`.

Used by `test_deep_research.py` via `AGY_TASK=<path_to_fake_agy_dr.py>` so unit tests exercise the entire
`deep_research.py` state machine, gates, live `progress.jsonl` tailing, PID liveness checks, and error
recovery in milliseconds without network calls or a live Gemini desktop app.

Every job gets its own fake conversation (`https://gemini.google.com/app/fake_<job dir name>`), as on the real app
where two jobs never share one. Like the real relay, the fake persists the conversation it pinned in
`relay/conversation.json` and puts report provenance (`url`, `conversation_id`, `report_title`) on completed results.

Scenarios (via `FAKE_AGY_SCENARIO` env var, default `perfect`):
  perfect             standard plan, edit revision, detached worker streaming 3 progress events, >=600 char GFM report
  long_running        detached worker stays alive ~1.2s writing progress events so `--no-wait` + `status` + `collect` see live PID
  plan_timeout        `plan` step fails with `PLAN_TIMEOUT` (exit 2)
  safety              `plan` step fails with `SAFETY_BLOCKED` (exit 2)
  plan_changed        `approve` step fails immediately with `PLAN_CHANGED` (exit 2)
  confirm_missing     `approve` step fails immediately with `CONFIRM_MISSING` (exit 2)
  short_report        worker produces a 60-char report (< 400 chars) that fails Report Quality Gate
  no_heading_report   worker produces a 500-char prose report with no Markdown headings (`#`)
  collect_would_start `collect` step refuses with `COLLECT_WOULD_START` (exit 2)
  worker_crash        detached worker pins its conversation, writes 1 progress event and exits without `relay/result.json`
  foreign_conversation  approve/auto/collect report completes but its provenance names another conversation
  no_provenance       approve/auto/collect report completes without url / conversation_id provenance
  edit_foreign        `edit` returns a revised plan read from another conversation
  edit_relay_mismatch `edit` fails with the relay's `CONVERSATION_MISMATCH` before anything was typed
  edit_no_id          `edit` returns a revised plan whose url names no conversation
  approve_page_moved  `approve` refuses with `CONVERSATION_MISMATCH` before clicking anything (exit 2)
  approve_page_moved_worker  same refusal, written later by the detached worker (the `--no-wait` path)
  foreign_progress_relay_error  worker streams another research's progress, then the relay's `CONVERSATION_MISMATCH`
  auto_report_timeout worker pins its conversation, then fails with `REPORT_TIMEOUT`
  plan_owned          every job's plan lands on the same conversation (`shared_conv_777`)
  plan_no_id          `plan` returns a plan whose url names no conversation
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

VALID_REPORT_MD = """# Comprehensive Deep Research Report: Cloud-Native Vector Indexing

## 1. Executive Summary

This report evaluates distributed vector indexing architectures across latency, recall@10, memory footprint,
and operational cost under high-ingestion workloads. Empirical benchmarks confirm that hybrid HNSW + disk-backed
PQ (DiskANN) reduces DRAM consumption by 72% while keeping p99 query latency below 18ms at 50M vectors.

## 2. Architectural Comparison & Benchmarks

| Engine Architecture | Recall@10 | p99 Latency (ms) | Memory per 10M Vectors |
| --- | --- | --- | --- |
| In-Memory HNSW | 0.965 | 4.2 ms | 64 GB |
| DiskANN (Vamana + PQ) | 0.948 | 14.8 ms | 12 GB |
| IVF-PQ GPU Accelerated | 0.932 | 6.1 ms | 24 GB VRAM |

## 3. Key Findings & Recommendations

- **Ingestion Throughput**: Streaming delta indexes paired with background compaction prevent read tail-latency spikes.
- **Filtering Semantics**: Pre-filtering with roaring bitmaps outperforms post-filtering when selectivity drops below 15%.
- **Fault Recovery**: Write-ahead logging with object-store snapshot checkpoints cuts cold-start recovery time by 4x.

---

## 참고 출처 (Sources)

1. [DiskANN: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node](https://example.org/diskann-paper)
2. [High-Dimensional Vector Indexing Benchmarks 2026](https://example.org/vector-benchmarks-2026)
3. [Roaring Bitmaps in Filtered ANN Search](https://example.org/filtered-ann-roaring)
"""

FOREIGN_URL = "https://gemini.google.com/app/foreign_conv_999"
SHARED_URL = "https://gemini.google.com/app/shared_conv_777"
FOREIGN_PROGRESS = ("foreign_conversation", "foreign_progress_relay_error")
FOREIGN_REPORT_MD = VALID_REPORT_MD.replace("Cloud-Native Vector Indexing", "FOREIGN Agent Sandboxing Strategy").strip() + "\n"


def report_body(scenario: str) -> str:
    return FOREIGN_REPORT_MD if scenario == "foreign_conversation" else VALID_REPORT_MD.strip() + "\n"


def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def write_json_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def job_conversation_url(job_dir: Path) -> str:
    """This job's own fake conversation."""
    return "https://gemini.google.com/app/fake_" + re.sub(r"[^A-Za-z0-9_-]", "_", job_dir.name)


def conv_id(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].lower()


def own_url(req: dict, job_dir: Path) -> str:
    resume = req.get("resume_url")
    return resume if resume and resume != "current" else job_conversation_url(job_dir)


def report_provenance(req: dict, job_dir: Path, scenario: str) -> dict:
    """The relay's provenance fields for a completed report (the conversation the driver extracted it from)."""
    if scenario == "no_provenance":
        return {}
    url = FOREIGN_URL if scenario == "foreign_conversation" else own_url(req, job_dir)
    return {"url": url, "conversation_id": conv_id(url), "report_title": "Cloud-Native Vector Indexing"}


def pin_conversation(relay_dir: Path, req: dict, url: str) -> dict:
    """What the real relay does when the driver prints its [CONVERSATION] line: persist it at once."""
    write_json_atomic(relay_dir / "conversation.json",
                      {"seq": req.get("seq"), "step": req.get("step"), "conversation_id": conv_id(url), "url": url, "ts": now_iso()})
    return {"conversation_id": conv_id(url), "conversation_url": url}


def error_result(req: dict, code: str, message: str, **extra) -> dict:
    return {"relay_version": 2, "step": req.get("step"), "seq": req.get("seq", 1), "status": "error", "code": code,
            "message": message, "exit_code": 2, "finished_at": now_iso(), **extra}


def run_bg_worker(job_dir: Path, scenario: str) -> int:
    relay_dir = job_dir / "relay"
    req = json.loads((relay_dir / "request.json").read_text(encoding="utf-8"))
    seq = req.get("seq", 1)
    step = req.get("step", "approve")
    out_file = Path(req.get("output") or (job_dir / "report.md"))
    progress_file = relay_dir / "progress.jsonl"
    result_file = relay_dir / "result.json"
    started_file = relay_dir / "started.json"

    if started_file.is_file():
        try:
            st = json.loads(started_file.read_text(encoding="utf-8"))
            st["worker_pid"] = os.getpid()
            write_json_atomic(started_file, st)
        except Exception:
            pass

    if scenario == "approve_page_moved_worker":   # refused before any click (confirm_clicked false)
        write_json_atomic(result_file, error_result(req, "CONVERSATION_MISMATCH",
                                                    "the page did not settle on this job's conversation (it is on foreign_conv_999)",
                                                    confirm_clicked=False))
        return 0

    pinned = pin_conversation(relay_dir, req, own_url(req, job_dir))

    delay = 0.35 if scenario == "long_running" else 0.03
    steps_progress = [
        (12, "심층 리서치 진행 중 | 주요 학술 논문 및 벤치마크 데이터 수집 중"),
        (45, "작성 중 | 아키텍처별 지연 시간 및 메모리 효율 비교 분석 중"),
        (90, "작성 중 | 최종 마크다운 리포트 및 참고 출처 정리 중"),
    ]
    if scenario in FOREIGN_PROGRESS:   # the shared page showed another research's progress
        steps_progress = [(e, f"FOREIGN research | {t}") for e, t in steps_progress]

    with open(progress_file, "a", encoding="utf-8", newline="\n") as pf:
        for idx, (elapsed_s, text) in enumerate(steps_progress):
            time.sleep(delay)
            pf.write(json.dumps({"ts": now_iso(), "elapsed_s": elapsed_s, "text": text}, ensure_ascii=False) + "\n")
            pf.flush()
            if scenario == "worker_crash" and idx == 0:
                return 1

    if scenario == "auto_report_timeout":
        write_json_atomic(result_file, error_result(req, "REPORT_TIMEOUT", "Deep Research did not finish within 1200 seconds.",
                                                    hint="the research keeps running in the app; a collect step later fetches the report",
                                                    confirm_clicked=True, **pinned))
        return 0
    if scenario == "foreign_progress_relay_error":
        write_json_atomic(result_file, error_result(req, "CONVERSATION_MISMATCH",
                                                    "Conversation mismatch (after poll): expected the job's conversation, page is on foreign_conv_999",
                                                    confirm_clicked=True, **pinned))
        return 0

    if scenario == "short_report":
        report_md = "# Short\nToo brief."
    elif scenario == "no_heading_report":
        report_md = ("Plain text paragraph without any markdown section heading. " * 12).strip()
    else:
        report_md = report_body(scenario)

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(report_md, encoding="utf-8")

    res = {
        "relay_version": 2,
        "step": step,
        "seq": seq,
        "status": "completed",
        "conversation_url": pinned["conversation_url"],
        **report_provenance(req, job_dir, scenario),
        "report_file": str(out_file),
        "report_chars": len(report_md),
        "sources_count": 3,
        "elapsed_s": 92.4,
        "finished_at": now_iso(),
    }
    if step == "auto":
        res["plan"] = "1. Collect benchmarks\n2. Compare HNSW and DiskANN\n3. Synthesize findings"
        res["plan_chars"] = len(res["plan"])
    write_json_atomic(result_file, res)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 3 and argv[0] == "--bg-worker":
        return run_bg_worker(Path(argv[1]), argv[2])

    p = argparse.ArgumentParser()
    p.add_argument("--model")
    sub = p.add_subparsers(dest="cmd", required=True)
    pf = sub.add_parser("preflight")
    pf.add_argument("--workdir")

    r = sub.add_parser("run")
    r.add_argument("--deep-research", action="store_true")
    r.add_argument("--dr-skill-dir")
    r.add_argument("-w", "--workdir", required=True)
    r.add_argument("-t", "--task")
    r.add_argument("-f", "--task-file")
    r.add_argument("--effort", choices=["low", "medium", "high"])
    args, _ = p.parse_known_args(argv)

    if args.cmd == "preflight":
        print(json.dumps({"ok": True, "checks": {"agy": "fake", "version": "fake-1.3.5"}}))
        return 0

    job_dir = Path(args.workdir).resolve()
    relay_dir = job_dir / "relay"
    relay_dir.mkdir(parents=True, exist_ok=True)
    invoked_info = {
        "model": getattr(args, "model", None),
        "effort": getattr(args, "effort", None),
        "argv": argv,
    }
    write_json_atomic(relay_dir / "agy_invoked.json", invoked_info)

    req_path = relay_dir / "request.json"
    req = json.loads(req_path.read_text(encoding="utf-8"))
    step = req.get("step")
    seq = req.get("seq", 1)
    scenario = os.environ.get("FAKE_AGY_SCENARIO", "perfect")
    result_file = relay_dir / "result.json"
    started_file = relay_dir / "started.json"

    dr_payload = None

    if step == "plan":
        if scenario == "plan_timeout":
            dr_payload = error_result(req, "PLAN_TIMEOUT", "Research Plan confirmation button did not appear within 180s.")
        elif scenario == "safety":
            dr_payload = error_result(req, "SAFETY_BLOCKED", "Gemini Web safety/keyword filter blocked the prompt.")
        else:
            plan_text = (
                "1. 조사 범위 설정: 클라우드 네이티브 벡터 인덱싱(HNSW, DiskANN) 아키텍처 조사\n"
                "2. 성능 지표 수집: Recall@10, p99 Latency, 메모리 사용량 비교\n"
                "3. 운영 및 복구 분석: 스트리밍 인제스천 및 스냅샷 복구 메커니즘 정리"
            )
            url = {"plan_owned": SHARED_URL, "plan_no_id": "https://gemini.google.com/app"}.get(scenario, job_conversation_url(job_dir))
            pinned = pin_conversation(relay_dir, req, url) if scenario != "plan_no_id" else {}
            dr_payload = {
                "relay_version": 2,
                "step": "plan",
                "seq": seq,
                "status": "plan_ready",
                "event": "on_plan_generated",
                "url": url,
                **pinned,
                "plan": plan_text,
                "plan_count": 1,
                "plan_chars": len(plan_text),
                "finished_at": now_iso(),
            }
        write_json_atomic(result_file, dr_payload)

    elif step == "edit":
        if scenario in ("edit_relay_mismatch", "edit_relay_mismatch_after_click", "edit_relay_mismatch_after_typing"):
            dr_payload = error_result(req, "CONVERSATION_MISMATCH",
                                      "Conversation mismatch (after navigation): expected the job's conversation, page is on foreign_conv_999",
                                      prompt_sent=scenario == "edit_relay_mismatch_after_typing",
                                      edit_button_clicked=scenario != "edit_relay_mismatch")
        else:
            fb_file = Path(req.get("feedback_file", ""))
            fb_text = fb_file.read_text(encoding="utf-8").strip() if fb_file.is_file() else ""
            plan_text = (
                "1. 조사 범위 설정: 클라우드 네이티브 벡터 인덱싱(HNSW, DiskANN, IVF-PQ) 아키텍처 조사\n"
                "2. 성능 지표 수집: Recall@10, p99 Latency, 10M 벡터당 메모리 사용량 비교\n"
                "3. 운영 및 복구 분석: 스트리밍 인제스천 및 스냅샷 복구 메커니즘 정리\n"
                f"4. 추가 반영 사항: {fb_text}"
            )
            url = {"edit_foreign": FOREIGN_URL, "edit_no_id": "https://gemini.google.com/app"}.get(scenario, own_url(req, job_dir))
            dr_payload = {
                "relay_version": 2,
                "step": "edit",
                "seq": seq,
                "status": "plan_revised",
                "event": "on_plan_revised",
                "url": url,
                "plan": plan_text,
                "plan_count": 2,
                "plan_chars": len(plan_text),
                "finished_at": now_iso(),
            }
        write_json_atomic(result_file, dr_payload)

    elif step in ("approve", "auto"):
        if step == "approve" and scenario == "plan_changed":
            dr_payload = error_result(req, "PLAN_CHANGED", "the conversation does not match what the caller reviewed")
            write_json_atomic(result_file, dr_payload)
        elif step == "approve" and scenario == "approve_page_moved":
            dr_payload = error_result(req, "CONVERSATION_MISMATCH",
                                      "the page did not settle on this job's conversation (it is on foreign_conv_999)")
            write_json_atomic(result_file, dr_payload)
        elif step == "approve" and scenario == "confirm_missing":
            dr_payload = error_result(req, "CONFIRM_MISSING", "no active '연구 시작' button on the conversation")
            write_json_atomic(result_file, dr_payload)
        else:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--bg-worker", str(job_dir), scenario],
                cwd=str(job_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            dr_payload = {
                "relay_version": 2,
                "step": step,
                "seq": seq,
                "status": "started",
                "pid": proc.pid,
                "worker_pid": proc.pid,
                "launcher": "fake_bg",
                "started_at": now_iso(),
                "message": "worker started; relay/result.json appears when the research is done",
            }
            write_json_atomic(started_file, dr_payload)

    elif step == "collect":
        if scenario == "collect_would_start":
            dr_payload = error_result(req, "COLLECT_WOULD_START",
                                      "the plan card still has an active '연구 시작' button: the research has not started")
            write_json_atomic(result_file, dr_payload)
        else:
            out_file = Path(req.get("output") or (job_dir / "report.md"))
            report_md = report_body(scenario)
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(report_md, encoding="utf-8")
            pinned = pin_conversation(relay_dir, req, own_url(req, job_dir))
            dr_payload = {
                "relay_version": 2,
                "step": "collect",
                "seq": seq,
                "status": "completed",
                "conversation_url": pinned["conversation_url"],
                **report_provenance(req, job_dir, scenario),
                "report_file": str(out_file),
                "report_chars": len(report_md),
                "sources_count": 3,
                "elapsed_s": 65.0,
                "finished_at": now_iso(),
            }
            write_json_atomic(result_file, dr_payload)

    summary = {
        "status": "ok",
        "agy_exit_code": 0,
        "agent_status": "SUCCESS",
        "conversation_id": "fake-conv-123",
        "workdir": str(job_dir),
        "changes": {"created": [], "modified": [], "deleted": [], "sizes": {}},
        "structured": {
            "status": (dr_payload or {}).get("status", "ok"),
            "step": step,
            "summary": f"Completed relay step {step}",
        },
        "confinement": {
            "enabled": True,
            "installed": True,
            "mode": "deep-research",
            "root": str(job_dir),
            "denied_count": 0,
            "denied": [],
        },
        "deep_research": dr_payload,
        "agy_invoked": invoked_info,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
