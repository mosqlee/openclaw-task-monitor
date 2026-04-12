#!/usr/bin/env python3
"""
Task Tracker - 任务追踪管理器
Phase 1: 解决超时不可追踪 + 任务无输出问题
Phase 2: 增强追踪能力（工具调用、Prompt快照、文件变化、决策记录）

用法:
  python3 task_tracker.py init <task_id> <goal> <agent> [--steps step1,step2,...]
  python3 task_tracker.py checkpoint <task_id> <step> <status> [--note "备注"]
  python3 task_tracker.py complete <task_id> [--output "结果"] [--duration 12345]
  python3 task_tracker.py fail <task_id> <reason> [--last-step "步骤"] [--duration 12345]
  python3 task_tracker.py timeout <task_id> [--last-step "步骤"] [--duration 12345]
  python3 task_tracker.py status <task_id>
  python3 task_tracker.py list [--status running|completed|failed|timeout]
  python3 task_tracker.py cleanup [--max-age-hours 72]
  python3 task_tracker.py watchdog [--max-age-minutes 30]

  # Phase 2: 增强追踪命令
  python3 task_tracker.py tool-call <task_id> <tool_name> --args '{...}' --result "..." [--context "..."]
  python3 task_tracker.py prompt-snapshot <task_id> --prompt "..." [--metadata '{...}']
  python3 task_tracker.py file-change <task_id> <file_path> <change_type> [--description "..."]
  python3 task_tracker.py decision <task_id> <decision> [--rationale "..."] [--alternatives "..."]
  python3 task_tracker.py trace-summary <task_id>
"""

import argparse
import json
import os
import subprocess
import sys
import time
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path

TRACE_DIR = Path(os.path.expanduser(os.environ.get("TASK_TRACE_DIR", "~/.openclaw/workspace/data/task-traces")))
DEFAULT_NOTIFY_USER = os.environ.get("DEFAULT_NOTIFY_USER", "")

# 尝试从 USER.md 读取默认通知人
def _load_default_notify_user() -> str:
    global DEFAULT_NOTIFY_USER
    if DEFAULT_NOTIFY_USER:
        return DEFAULT_NOTIFY_USER
    for candidate in [
        Path.home() / ".openclaw" / "workspace" / "USER.md",
        Path(os.environ.get("OPENCLAW_WORKSPACE", "")) / "USER.md",
    ]:
        if candidate.exists():
            text = candidate.read_text()
            for line in text.splitlines():
                if "open_id" in line.lower() and "ou_" in line:
                    import re
                    m = re.search(r"ou_[a-f0-9]+", line)
                    if m:
                        DEFAULT_NOTIFY_USER = m.group()
                        return DEFAULT_NOTIFY_USER
    return ""

DEFAULT_NOTIFY_USER = _load_default_notify_user()


def _resolve_notify_target(requester_session_key: str = "") -> str:
    """从 sessionKey 解析通知目标

    格式示例:
      agent:main:feishu:direct:ou_xxx  → user:ou_xxx
      agent:main:feishu:group:oc_xxx   → chat:oc_xxx
      agent:main:web:xxx               → (无法解析)
    """
    if not requester_session_key:
        return ""
    parts = requester_session_key.split(":")
    # 寻找 channel 和 target_type
    try:
        # 格式: agent:{agent_id}:{channel}:{type}:{target}
        # 或:   agent:{agent_id}:{channel}:{type}
        if len(parts) >= 5 and parts[2] == "feishu":
            target_type = parts[3]  # direct / group
            target_id = parts[4]   # ou_xxx / oc_xxx
            if target_type == "direct" and target_id.startswith("ou_"):
                return f"user:{target_id}"
            elif target_type == "group" and target_id.startswith("oc_"):
                return f"chat:{target_id}"
    except (IndexError, ValueError):
        pass
    return ""
CST = timezone(timedelta(hours=8))

# 结果截断长度
MAX_RESULT_LENGTH = 500
MAX_PROMPT_LENGTH = 5000  # Prompt快照截断长度


def now_str():
    return datetime.now(CST).isoformat()


def task_dir(task_id: str) -> Path:
    return TRACE_DIR / task_id


def load_file(task_id: str, name: str) -> dict:
    p = task_dir(task_id) / name
    if p.exists():
        return json.loads(p.read_text())
    return {}


def save_file(task_id: str, name: str, data: dict):
    d = task_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(data, ensure_ascii=False, indent=2))


def truncate_text(text: str, max_len: int = MAX_RESULT_LENGTH) -> str:
    """截断文本到指定长度"""
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"... (truncated, total {len(text)} chars)"


def cmd_init(args):
    """初始化任务追踪"""
    steps = [s.strip() for s in args.steps.split(",") if s.strip()] if args.steps else []

    task_plan = {
        "task_id": args.task_id,
        "goal": args.goal,
        "agent": args.agent,
        "notify_user": getattr(args, "notify_user", ""),
        "requester": getattr(args, "requester", ""),
        "created_at": now_str(),
        "status": "running",
        "steps": [
            {"id": i + 1, "name": s, "status": "pending"}
            for i, s in enumerate(steps)
        ],
        "current_step": 0,
        "progress_pct": 0
    }
    save_file(args.task_id, "task_plan.json", task_plan)

    progress = {
        "task_id": args.task_id,
        "events": [
            {"time": now_str(), "action": "task_created",
             "detail": f"Goal: {args.goal}, Agent: {args.agent}"}
        ]
    }
    save_file(args.task_id, "progress.json", progress)

    print(json.dumps({"ok": True, "task_id": args.task_id,
                       "trace_dir": str(task_dir(args.task_id))}))


def cmd_checkpoint(args):
    """记录检查点"""
    task_plan = load_file(args.task_id, "task_plan.json")
    progress = load_file(args.task_id, "progress.json")

    if not task_plan:
        print(json.dumps({"ok": False, "error": f"Task {args.task_id} not found"}))
        sys.exit(1)

    # 更新步骤状态 - 支持数字ID和步骤名两种匹配
    step_updated = False
    for step in task_plan.get("steps", []):
        step_name = step.get("name", "")
        if str(step["id"]) == args.step or step_name.strip() == args.step.strip():
            step["status"] = args.status
            step_updated = True
            break

    # 更新进度百分比
    total = len(task_plan.get("steps", []))
    if total > 0:
        completed = sum(1 for s in task_plan["steps"]
                        if s["status"] in ("completed", "done"))
        task_plan["progress_pct"] = int(completed / total * 100)

    task_plan["status"] = "running"
    save_file(args.task_id, "task_plan.json", task_plan)

    # 记录事件
    note = args.note or ""
    progress["events"].append({
        "time": now_str(),
        "action": "checkpoint",
        "step": args.step,
        "status": args.status,
        "detail": note
    })
    save_file(args.task_id, "progress.json", progress)

    print(json.dumps({"ok": True, "task_id": args.task_id,
                       "step": args.step, "status": args.status,
                       "matched": step_updated}))


def _write_result(task_id, status, output, duration_ms=None, last_step=None):
    """写入最终结果"""
    task_plan = load_file(task_id, "task_plan.json")
    progress = load_file(task_id, "progress.json")

    result = {
        "task_id": task_id,
        "status": status,
        "output": output,
        "last_step": last_step or "",
        "duration_ms": duration_ms or 0,
        "completed_at": now_str()
    }

    # 从 progress 中找最后成功步骤
    if not last_step:
        for event in reversed(progress.get("events", [])):
            if event.get("status") in ("completed", "done"):
                result["last_step"] = event.get("step", "unknown")
                break

    save_file(task_id, "result.json", result)

    # 更新 task_plan
    if task_plan:
        task_plan["status"] = status
        task_plan["completed_at"] = now_str()
        save_file(task_id, "task_plan.json", task_plan)

    # 记录事件
    if progress.get("events") is not None:
        progress["events"].append({
            "time": now_str(),
            "action": "task_completed",
            "status": status,
            "detail": output[:200] if output else ""
        })
        save_file(task_id, "progress.json", progress)

    return result


def cmd_complete(args):
    """标记任务完成"""
    result = _write_result(
        args.task_id, "completed",
        args.output or "Task completed successfully",
        args.duration
    )
    print(json.dumps({"ok": True, **result}))


def cmd_fail(args):
    """标记任务失败"""
    result = _write_result(
        args.task_id, "failed",
        args.reason,
        args.duration,
        args.last_step
    )
    print(json.dumps({"ok": True, **result}))


def cmd_timeout(args):
    """标记任务超时 - Coordinator 兜底"""
    task_plan = load_file(args.task_id, "task_plan.json")
    progress = load_file(args.task_id, "progress.json")

    # 优先用 args 传入的 last_step，否则从 progress 找
    last_step = args.last_step
    last_checkpoint = "unknown"

    for event in reversed(progress.get("events", [])):
        if event.get("status") in ("completed", "done", "running"):
            last_checkpoint = f"Step {event.get('step', '?')}: {event.get('detail', '')}"
            if not last_step:
                last_step = event.get("step", "unknown")
            break

    if not last_step:
        last_step = "unknown"

    total_steps = len(task_plan.get("steps", []))
    completed_steps = sum(1 for s in task_plan.get("steps", [])
                          if s.get("status") in ("completed", "done"))

    timeout_report = (
        f"⏰ 任务超时\n"
        f"任务目标: {task_plan.get('goal', 'unknown')}\n"
        f"执行Agent: {task_plan.get('agent', 'unknown')}\n"
        f"进度: {completed_steps}/{total_steps} 步完成 "
        f"({task_plan.get('progress_pct', 0)}%)\n"
        f"最后成功步骤: {last_checkpoint}\n"
        f"超时时间: {args.duration or 'unknown'}ms\n"
        f"建议: 检查最后成功步骤之后的内容，"
        f"可能需要手动继续或调整参数重试"
    )

    result = _write_result(
        args.task_id, "timeout",
        timeout_report,
        args.duration,
        last_step
    )
    print(json.dumps({"ok": True, **result}))


def cmd_status(args):
    """查看任务状态"""
    task_plan = load_file(args.task_id, "task_plan.json")
    result = load_file(args.task_id, "result.json")

    if not task_plan:
        print(json.dumps({"ok": False, "error": f"Task {args.task_id} not found"}))
        sys.exit(1)

    output = {
        "ok": True,
        "task_id": args.task_id,
        "goal": task_plan.get("goal"),
        "agent": task_plan.get("agent"),
        "status": task_plan.get("status"),
        "progress_pct": task_plan.get("progress_pct", 0),
        "created_at": task_plan.get("created_at"),
        "has_result": bool(result),
        "result_status": result.get("status") if result else None
    }
    print(json.dumps(output, ensure_ascii=False))


def cmd_list(args):
    """列出任务"""
    if not TRACE_DIR.exists():
        print(json.dumps({"ok": True, "tasks": []}))
        return

    tasks = []
    for d in sorted(TRACE_DIR.iterdir()):
        if not d.is_dir():
            continue
        tp = load_file(d.name, "task_plan.json")
        if not tp:
            continue
        if args.status and tp.get("status") != args.status:
            continue
        tasks.append({
            "task_id": d.name,
            "goal": tp.get("goal", "")[:60],
            "agent": tp.get("agent", ""),
            "status": tp.get("status", ""),
            "progress_pct": tp.get("progress_pct", 0),
            "created_at": tp.get("created_at", "")
        })

    print(json.dumps({"ok": True, "count": len(tasks), "tasks": tasks},
                      ensure_ascii=False))


def cmd_cleanup(args):
    """清理过期追踪记录"""
    if not TRACE_DIR.exists():
        print(json.dumps({"ok": True, "cleaned": 0}))
        return

    cutoff = time.time() - (args.max_age_hours * 3600)
    cleaned = 0

    for d in sorted(TRACE_DIR.iterdir()):
        if not d.is_dir():
            continue
        tp = load_file(d.name, "task_plan.json")
        if not tp:
            continue
        if tp.get("status") in ("completed", "failed", "timeout"):
            created_str = tp.get("created_at", "")
            try:
                ct = datetime.fromisoformat(created_str)
                if ct.timestamp() < cutoff:
                    shutil.rmtree(d)
                    cleaned += 1
            except (ValueError, TypeError):
                pass

    print(json.dumps({"ok": True, "cleaned": cleaned}))


def cmd_tool_call(args):
    """记录工具调用"""
    task_plan = load_file(args.task_id, "task_plan.json")
    if not task_plan:
        print(json.dumps({"ok": False, "error": f"Task {args.task_id} not found"}))
        sys.exit(1)

    # 加载或创建 tool_calls.json
    tool_calls = load_file(args.task_id, "tool_calls.json")
    if not tool_calls:
        tool_calls = {"task_id": args.task_id, "calls": []}

    # 解析 args
    args_dict = {}
    if args.args:
        try:
            args_dict = json.loads(args.args)
        except json.JSONDecodeError:
            args_dict = {"raw": args.args}

    # 截断结果
    result_text = truncate_text(args.result or "", MAX_RESULT_LENGTH)

    # 创建调用记录
    call_record = {
        "tool_name": args.tool_name,
        "args": args_dict,
        "result": result_text,
        "timestamp": now_str(),
        "context": args.context or ""
    }

    tool_calls["calls"].append(call_record)
    save_file(args.task_id, "tool_calls.json", tool_calls)

    # 同时记录到 progress 事件流
    progress = load_file(args.task_id, "progress.json")
    if progress.get("events") is not None:
        progress["events"].append({
            "time": now_str(),
            "action": "tool_call",
            "tool": args.tool_name,
            "detail": f"Args: {json.dumps(args_dict)[:100]}"
        })
        save_file(args.task_id, "progress.json", progress)

    print(json.dumps({"ok": True, "call_count": len(tool_calls["calls"])}))


def cmd_prompt_snapshot(args):
    """记录Prompt快照"""
    task_plan = load_file(args.task_id, "task_plan.json")
    if not task_plan:
        print(json.dumps({"ok": False, "error": f"Task {args.task_id} not found"}))
        sys.exit(1)

    # 加载或创建 prompt_snapshots.json
    snapshots = load_file(args.task_id, "prompt_snapshots.json")
    if not snapshots:
        snapshots = {"task_id": args.task_id, "snapshots": []}

    # 解析 metadata
    metadata = {}
    if args.metadata:
        try:
            metadata = json.loads(args.metadata)
        except json.JSONDecodeError:
            metadata = {"raw": args.metadata}

    # 截断 prompt
    prompt_text = truncate_text(args.prompt or "", MAX_PROMPT_LENGTH)

    # 创建快照记录
    snapshot = {
        "prompt_content": prompt_text,
        "timestamp": now_str(),
        "metadata": metadata
    }

    snapshots["snapshots"].append(snapshot)
    save_file(args.task_id, "prompt_snapshots.json", snapshots)

    print(json.dumps({"ok": True, "snapshot_count": len(snapshots["snapshots"])}))


def cmd_trace_summary(args):
    """输出追踪摘要"""
    task_plan = load_file(args.task_id, "task_plan.json")
    result = load_file(args.task_id, "result.json")
    progress = load_file(args.task_id, "progress.json")
    tool_calls = load_file(args.task_id, "tool_calls.json")
    prompt_snapshots = load_file(args.task_id, "prompt_snapshots.json")

    if not task_plan:
        print(json.dumps({"ok": False, "error": f"Task {args.task_id} not found"}))
        sys.exit(1)

    summary = {
        "ok": True,
        "task_id": args.task_id,
        "goal": task_plan.get("goal"),
        "agent": task_plan.get("agent"),
        "status": task_plan.get("status"),
        "progress_pct": task_plan.get("progress_pct", 0),
        "created_at": task_plan.get("created_at"),
        "result": result.get("output") if result else None,
        "events_count": len(progress.get("events", [])),
        "tool_calls_count": len(tool_calls.get("calls", [])),
        "prompt_snapshots_count": len(prompt_snapshots.get("snapshots", []))
    }

    # 包含最近的工具调用
    if tool_calls.get("calls"):
        summary["recent_tool_calls"] = tool_calls["calls"][-3:]  # 最近3次

    print(json.dumps(summary, ensure_ascii=False, indent=2))


SIGNAL_DIR = Path(os.path.expanduser(os.environ.get("SIGNAL_DIR", "~/.openclaw/workspace/data/signals")))
PID_FILE = TRACE_DIR / "watch.pid"
WATCH_LOG = TRACE_DIR / "watch.log"


def _watch_log(msg: str):
    """Append timestamped log to watch.log"""
    try:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        with open(WATCH_LOG, "a") as f:
            f.write(f"[{now_str()}] {msg}\n")
    except Exception:
        pass


def _write_signal(task_id: str, signal_type: str, data: dict):
    """Write a signal file for heartbeat to process"""
    try:
        SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
        signal_file = SIGNAL_DIR / f"{task_id}_{signal_type}.json"
        data["task_id"] = task_id
        data["type"] = signal_type
        data["created_at"] = now_str()
        signal_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        _watch_log(f"SIGNAL_WRITTEN: {signal_file.name} for {task_id}")
    except Exception as e:
        _watch_log(f"SIGNAL_WRITE_FAILED: {task_id} {signal_type} - {e}")
        print(f"[watch] ERROR writing signal: {e}", flush=True)


def _notify_user_and_wake_session(task_id: str, signal_type: str, data: dict):
    """Notify user + wake LLM session via openclaw CLI"""
    goal = data.get("goal", "")
    elapsed_min = data.get("elapsed_min", 0)
    last_step = data.get("last_step", "")

    # Resolve notify target (priority: task_plan.notify_user > requester session > default)
    tp = load_file(task_id, "task_plan.json")
    notify_target = ""
    if tp:
        notify_target = (tp.get("notify_user") or "").strip()
    if not notify_target:
        # 从 requester session key 解析
        requester = (tp or {}).get("requester", "") if tp else ""
        resolved = _resolve_notify_target(requester)
        if resolved:
            notify_target = resolved
            _watch_log(f"NOTIFY_RESOLVED_FROM_REQUESTER: {task_id} requester={requester} -> {resolved}")
    if not notify_target:
        if DEFAULT_NOTIFY_USER:
            notify_target = DEFAULT_NOTIFY_USER
            _watch_log(f"NOTIFY_FALLBACK_DEFAULT: {task_id} using DEFAULT_NOTIFY_USER={DEFAULT_NOTIFY_USER}")

    if signal_type == "stale":
        emoji, title = "⏰", "任务停滞"
        detail = f"已运行 {elapsed_min} 分钟无进展\n最后步骤: {last_step}"
        event_text = (f"[Task Monitor] 任务停滞告警\n"
                      f"任务: {goal}\n"
                      f"已运行 {elapsed_min} 分钟无进展\n"
                      f"最后步骤: {last_step}\n"
                      f"请检查任务状态并决定是否干预。")
    elif signal_type == "timeout":
        emoji, title = "🔴", "任务超时"
        detail = f"已运行 {elapsed_min} 分钟，已自动标记 timeout"
        event_text = (f"[Task Monitor] 任务超时\n"
                      f"任务: {goal}\n"
                      f"已运行 {elapsed_min} 分钟\n"
                      f"已自动标记 timeout。\n"
                      f"请检查任务结果并通知用户。")
    else:
        return

    # 1. Wake LLM session
    try:
        result = subprocess.run(
            ["/opt/homebrew/bin/openclaw", "system", "event",
             "--mode", "now", "--text", event_text, "--timeout", "10000"],
            capture_output=True, text=True, timeout=15
        )
        _watch_log(f"WAKE_SENT: {task_id} via system event (exit={result.returncode} stdout={result.stdout.strip()[:300]} stderr={result.stderr.strip()[:300]})")
    except Exception as e:
        _watch_log(f"WAKE_FAILED: {task_id} - {e}")

    # 2. Notify user (feishu) — use task-level notify_user, then global default
    if not notify_target:
        if DEFAULT_NOTIFY_USER:
            notify_target = DEFAULT_NOTIFY_USER
            _watch_log(f"USER_NOTIFY_FALLBACK: {task_id} using DEFAULT_NOTIFY_USER={DEFAULT_NOTIFY_USER}")
    if notify_target:
        try:
            target = f"user:{notify_target}" if ":" not in notify_target else notify_target
            msg = f"{emoji} {title}: {goal}\n{detail}"
            result = subprocess.run(
                ["/opt/homebrew/bin/openclaw", "message", "send",
                 "--channel", "feishu",
                 "--target", target,
                 "--message", msg],
                capture_output=True, text=True, timeout=10
            )
            _watch_log(f"USER_NOTIFIED: {task_id} -> {target} (exit={result.returncode} stdout={result.stdout.strip()[:200]} stderr={result.stderr.strip()[:200]})")
        except Exception as e:
            _watch_log(f"USER_NOTIFY_FAILED: {task_id} - {e}")
    else:
        _watch_log(f"USER_NOTIFY_SKIPPED: {task_id} (no notify_user in task_plan, no DEFAULT_NOTIFY_USER set)")



def _get_last_checkpoint_time(task_id: str) -> float:
    """Get timestamp of last checkpoint for a task"""
    progress = load_file(task_id, "progress.json")
    events = progress.get("events", [])
    if not events:
        # Fall back to task_plan created_at
        tp = load_file(task_id, "task_plan.json")
        created = tp.get("created_at", "")
        if created:
            try:
                return datetime.fromisoformat(created).timestamp()
            except (ValueError, TypeError):
                pass
        return 0.0
    last = events[-1]
    ts = last.get("timestamp", "") or last.get("time", "")
    try:
        return datetime.fromisoformat(ts).timestamp() if ts else 0.0
    except (ValueError, TypeError):
        return 0.0


def _get_last_checkpoint_step(task_id: str) -> str:
    """Get description of last checkpoint step"""
    progress = load_file(task_id, "progress.json")
    events = progress.get("events", [])
    for event in reversed(events):
        step = event.get("step", "")
        detail = event.get("detail", "") or event.get("note", "")
        if step:
            return f"{step}: {detail}".strip()
    tp = load_file(task_id, "task_plan.json")
    return tp.get("goal", "unknown")


def cmd_watch(args):
    """Watch daemon - monitor running tasks and emit signals on stale/timeout"""
    interval = args.interval
    stale_threshold = args.stale_threshold
    timeout_threshold = args.timeout
    notify_user = args.notify_user

    # Write PID file
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    # Ensure SIGNAL_DIR exists on startup
    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)

    _watch_log(f"Watch daemon started PID={os.getpid()} interval={interval}s stale={stale_threshold}s timeout={timeout_threshold}s")
    print(f"[watch] Started PID={os.getpid()} interval={interval}s stale={stale_threshold}s timeout={timeout_threshold}s", flush=True)

    notified_stale = set()  # Track already-notified stale tasks

    try:
        while True:
            if not TRACE_DIR.exists():
                time.sleep(interval)
                continue

            for d in sorted(TRACE_DIR.iterdir()):
                if not d.is_dir():
                    continue
                tp = load_file(d.name, "task_plan.json")
                if not tp or tp.get("status") != "running":
                    continue

                last_cp_time = _get_last_checkpoint_time(d.name)
                if last_cp_time == 0:
                    _watch_log(f"SKIP {d.name}: last_cp_time=0 (no events)")
                    continue

                elapsed = time.time() - last_cp_time
                elapsed_min = int(elapsed / 60)

                _watch_log(f"SCAN {d.name}: elapsed={elapsed:.0f}s ({elapsed_min}min) stale_threshold={stale_threshold}s timeout={timeout_threshold}s")

                # Stale notification
                if elapsed > stale_threshold and d.name not in notified_stale:
                    last_step = _get_last_checkpoint_step(d.name)
                    signal_data = {
                        "task_id": d.name,
                        "goal": tp.get("goal", ""),
                        "agent": tp.get("agent", "main"),
                        "elapsed_min": elapsed_min,
                        "last_step": last_step,
                        "action": "check_and_handle",
                    }
                    _write_signal(d.name, "stale", signal_data)
                    notified_stale.add(d.name)
                    _watch_log(f"STALE: {d.name} elapsed={elapsed_min}min last_step={last_step}")
                    print(f"[watch] STALE: {d.name} elapsed={elapsed_min}min last_step={last_step}", flush=True)
                    _notify_user_and_wake_session(d.name, "stale", signal_data)

                # Timeout - mark and notify (only if NOT already marked stale to ensure stale fires first)
                if elapsed > timeout_threshold:
                    last_step = _get_last_checkpoint_step(d.name)
                    ns = argparse.Namespace(
                        task_id=d.name,
                        last_step=last_step,
                        duration=int(elapsed * 1000)
                    )
                    cmd_timeout(ns)
                    signal_data = {
                        "task_id": d.name,
                        "goal": tp.get("goal", ""),
                        "agent": tp.get("agent", "main"),
                        "elapsed_min": elapsed_min,
                        "last_step": last_step,
                        "action": "timeout",
                    }
                    _write_signal(d.name, "timeout", signal_data)
                    notified_stale.discard(d.name)
                    _watch_log(f"TIMEOUT: {d.name} elapsed={elapsed_min}min")
                    print(f"[watch] TIMEOUT: {d.name} elapsed={elapsed_min}min", flush=True)
                    _notify_user_and_wake_session(d.name, "timeout", signal_data)

                # Recovered from stale (new checkpoint came in)
                if d.name in notified_stale and elapsed <= stale_threshold * 0.8:
                    signal_data = {
                        "task_id": d.name,
                        "goal": tp.get("goal", ""),
                        "agent": tp.get("agent", "main"),
                        "elapsed_min": elapsed_min,
                        "action": "recovered",
                    }
                    _write_signal(d.name, "recovered", signal_data)
                    notified_stale.discard(d.name)
                    _watch_log(f"RECOVERED: {d.name}")
                    print(f"[watch] RECOVERED: {d.name}", flush=True)

            time.sleep(interval)
    except Exception as e:
        _watch_log(f"FATAL: {e}")
        print(f"[watch] FATAL ERROR: {e}", flush=True)
    except KeyboardInterrupt:
        print("\n[watch] Stopped.", flush=True)
        _watch_log("Stopped (KeyboardInterrupt)")
    finally:
        if PID_FILE.exists():
            PID_FILE.unlink()
        _watch_log("Watch daemon exited")


def cmd_watchdog(args):
    """Watchdog - 扫描 running 状态超时的任务，自动标记 timeout"""
    if not TRACE_DIR.exists():
        print(json.dumps({"ok": True, "stale_count": 0, "stale": []}))
        return

    stale_threshold = args.max_age_minutes * 60
    stale = []

    for d in sorted(TRACE_DIR.iterdir()):
        if not d.is_dir():
            continue
        tp = load_file(d.name, "task_plan.json")
        if not tp or tp.get("status") != "running":
            continue
        created_str = tp.get("created_at", "")
        try:
            ct = datetime.fromisoformat(created_str)
            elapsed = time.time() - ct.timestamp()
            if elapsed > stale_threshold:
                # 自动标记超时
                ns = argparse.Namespace(
                    task_id=d.name,
                    last_step="",
                    duration=int(elapsed * 1000)
                )
                cmd_timeout(ns)
                stale.append({"task_id": d.name,
                              "elapsed_min": int(elapsed / 60)})
        except (ValueError, TypeError):
            pass

    print(json.dumps({"ok": True, "stale_count": len(stale), "stale": stale},
                      ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Task Tracker")
    sub = parser.add_subparsers(dest="command")

    # init
    p = sub.add_parser("init")
    p.add_argument("task_id")
    p.add_argument("goal")
    p.add_argument("agent")
    p.add_argument("--steps", default="")
    p.add_argument("--notify-user", type=str, default="", help="User/group open_id to notify on stale/timeout (fallback)")
    p.add_argument("--requester", type=str, default="", help="Requester session key for auto-resolving notify target")

    # checkpoint
    p = sub.add_parser("checkpoint")
    p.add_argument("task_id")
    p.add_argument("step")
    p.add_argument("status", default="completed")
    p.add_argument("--note", default="")

    # complete
    p = sub.add_parser("complete")
    p.add_argument("task_id")
    p.add_argument("--output", default="")
    p.add_argument("--duration", type=int, default=0)

    # fail
    p = sub.add_parser("fail")
    p.add_argument("task_id")
    p.add_argument("reason")
    p.add_argument("--last-step", default="")
    p.add_argument("--duration", type=int, default=0)

    # timeout
    p = sub.add_parser("timeout")
    p.add_argument("task_id")
    p.add_argument("--last-step", default="")
    p.add_argument("--duration", type=int, default=0)

    # status
    p = sub.add_parser("status")
    p.add_argument("task_id")

    # list
    p = sub.add_parser("list")
    p.add_argument("--status", default="")

    # cleanup
    p = sub.add_parser("cleanup")
    p.add_argument("--max-age-hours", type=int, default=72)

    # Phase 2: 增强追踪命令
    # tool-call
    p = sub.add_parser("tool-call")
    p.add_argument("task_id")
    p.add_argument("tool_name")
    p.add_argument("--args", default="")
    p.add_argument("--result", default="")
    p.add_argument("--context", default="")

    # prompt-snapshot
    p = sub.add_parser("prompt-snapshot")
    p.add_argument("task_id")
    p.add_argument("--prompt", default="")
    p.add_argument("--metadata", default="")

    # trace-summary
    p = sub.add_parser("trace-summary")
    p.add_argument("task_id")

    # watchdog
    p = sub.add_parser("watchdog")
    p.add_argument("--max-age-minutes", type=int, default=30)

    # watch daemon
    p = sub.add_parser("watch")
    p.add_argument("--interval", type=int, default=60, help="Check interval in seconds")
    p.add_argument("--stale-threshold", type=int, default=300, help="Stale notification threshold in seconds")
    p.add_argument("--timeout", type=int, default=600, help="Timeout threshold in seconds")
    p.add_argument("--notify-user", type=str, default="", help="User open_id to notify")

    args = parser.parse_args()

    cmds = {
        "init": cmd_init, "checkpoint": cmd_checkpoint,
        "complete": cmd_complete, "fail": cmd_fail,
        "timeout": cmd_timeout, "status": cmd_status,
        "list": cmd_list, "cleanup": cmd_cleanup,
        "watchdog": cmd_watchdog,
        "watch": cmd_watch,
        # Phase 2: 增强追踪命令
        "tool-call": cmd_tool_call,
        "prompt-snapshot": cmd_prompt_snapshot,
        "trace-summary": cmd_trace_summary,
    }

    if args.command in cmds:
        cmds[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
