#!/usr/bin/env python3
"""fake_agy_dr.py - deterministic offline simulator for `agy_task.py --deep-research` and `relay_step.py`.

Used by `test_deep_research.py` via `AGY_TASK=<path_to_fake_agy_dr.py>` so unit tests exercise the entire
`deep_research.py` state machine, gates, live `progress.jsonl` tailing, PID liveness checks, and error
recovery in milliseconds without network calls or a live Gemini desktop app.

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
  worker_crash        detached worker writes 1 progress event and exits without writing `relay/result.json`
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
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


def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def write_json_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


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

    delay = 0.35 if scenario == "long_running" else 0.03
    steps_progress = [
        (12, "심층 리서치 진행 중 | 주요 학술 논문 및 벤치마크 데이터 수집 중"),
        (45, "작성 중 | 아키텍처별 지연 시간 및 메모리 효율 비교 분석 중"),
        (90, "작성 중 | 최종 마크다운 리포트 및 참고 출처 정리 중"),
    ]

    with open(progress_file, "a", encoding="utf-8", newline="\n") as pf:
        for idx, (elapsed_s, text) in enumerate(steps_progress):
            time.sleep(delay)
            pf.write(json.dumps({"ts": now_iso(), "elapsed_s": elapsed_s, "text": text}, ensure_ascii=False) + "\n")
            pf.flush()
            if scenario == "worker_crash" and idx == 0:
                return 1

    if scenario == "short_report":
        report_md = "# Short\nToo brief."
    elif scenario == "no_heading_report":
        report_md = ("Plain text paragraph without any markdown section heading. " * 12).strip()
    else:
        report_md = VALID_REPORT_MD.strip() + "\n"

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(report_md, encoding="utf-8")

    res = {
        "relay_version": 2,
        "step": step,
        "seq": seq,
        "status": "completed",
        "url": req.get("resume_url") or "https://gemini.google.com/app/fake_conv_001",
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
    sub = p.add_subparsers(dest="cmd", required=True)
    pf = sub.add_parser("preflight")
    pf.add_argument("--workdir")

    r = sub.add_parser("run")
    r.add_argument("--deep-research", action="store_true")
    r.add_argument("--dr-skill-dir")
    r.add_argument("-w", "--workdir", required=True)
    r.add_argument("-t", "--task")
    r.add_argument("-f", "--task-file")
    args, _ = p.parse_known_args(argv)

    if args.cmd == "preflight":
        print(json.dumps({"ok": True, "checks": {"agy": "fake", "version": "fake-1.3.5"}}))
        return 0

    job_dir = Path(args.workdir).resolve()
    relay_dir = job_dir / "relay"
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
            dr_payload = {
                "relay_version": 2,
                "step": "plan",
                "seq": seq,
                "status": "error",
                "code": "PLAN_TIMEOUT",
                "message": "Research Plan confirmation button did not appear within 180s.",
                "exit_code": 2,
                "finished_at": now_iso(),
            }
            write_json_atomic(result_file, dr_payload)
        elif scenario == "safety":
            dr_payload = {
                "relay_version": 2,
                "step": "plan",
                "seq": seq,
                "status": "error",
                "code": "SAFETY_BLOCKED",
                "message": "Gemini Web safety/keyword filter blocked the prompt.",
                "exit_code": 2,
                "finished_at": now_iso(),
            }
            write_json_atomic(result_file, dr_payload)
        else:
            plan_text = (
                "1. 조사 범위 설정: 클라우드 네이티브 벡터 인덱싱(HNSW, DiskANN) 아키텍처 조사\n"
                "2. 성능 지표 수집: Recall@10, p99 Latency, 메모리 사용량 비교\n"
                "3. 운영 및 복구 분석: 스트리밍 인제스천 및 스냅샷 복구 메커니즘 정리"
            )
            dr_payload = {
                "relay_version": 2,
                "step": "plan",
                "seq": seq,
                "status": "plan_ready",
                "event": "on_plan_generated",
                "url": "https://gemini.google.com/app/fake_conv_001",
                "plan": plan_text,
                "plan_count": 1,
                "plan_chars": len(plan_text),
                "finished_at": now_iso(),
            }
            write_json_atomic(result_file, dr_payload)

    elif step == "edit":
        fb_file = Path(req.get("feedback_file", ""))
        fb_text = fb_file.read_text(encoding="utf-8").strip() if fb_file.is_file() else ""
        plan_text = (
            "1. 조사 범위 설정: 클라우드 네이티브 벡터 인덱싱(HNSW, DiskANN, IVF-PQ) 아키텍처 조사\n"
            "2. 성능 지표 수집: Recall@10, p99 Latency, 10M 벡터당 메모리 사용량 비교\n"
            "3. 운영 및 복구 분석: 스트리밍 인제스천 및 스냅샷 복구 메커니즘 정리\n"
            f"4. 추가 반영 사항: {fb_text}"
        )
        dr_payload = {
            "relay_version": 2,
            "step": "edit",
            "seq": seq,
            "status": "plan_revised",
            "event": "on_plan_revised",
            "url": req.get("resume_url") or "https://gemini.google.com/app/fake_conv_001",
            "plan": plan_text,
            "plan_count": 2,
            "plan_chars": len(plan_text),
            "finished_at": now_iso(),
        }
        write_json_atomic(result_file, dr_payload)

    elif step in ("approve", "auto"):
        if step == "approve" and scenario == "plan_changed":
            dr_payload = {
                "relay_version": 2,
                "step": "approve",
                "seq": seq,
                "status": "error",
                "code": "PLAN_CHANGED",
                "message": "the conversation does not match what the caller reviewed",
                "exit_code": 2,
                "finished_at": now_iso(),
            }
            write_json_atomic(result_file, dr_payload)
        elif step == "approve" and scenario == "confirm_missing":
            dr_payload = {
                "relay_version": 2,
                "step": "approve",
                "seq": seq,
                "status": "error",
                "code": "CONFIRM_MISSING",
                "message": "no active '연구 시작' button on the conversation",
                "exit_code": 2,
                "finished_at": now_iso(),
            }
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
            dr_payload = {
                "relay_version": 2,
                "step": "collect",
                "seq": seq,
                "status": "error",
                "code": "COLLECT_WOULD_START",
                "message": "the plan card still has an active '연구 시작' button: the research has not started",
                "exit_code": 2,
                "finished_at": now_iso(),
            }
            write_json_atomic(result_file, dr_payload)
        else:
            out_file = Path(req.get("output") or (job_dir / "report.md"))
            report_md = VALID_REPORT_MD.strip() + "\n"
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(report_md, encoding="utf-8")
            dr_payload = {
                "relay_version": 2,
                "step": "collect",
                "seq": seq,
                "status": "completed",
                "url": req.get("resume_url") or "https://gemini.google.com/app/fake_conv_001",
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
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
