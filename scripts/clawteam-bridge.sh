#!/bin/bash
# ============================================
# ClawTeam ↔ Task Monitor Bridge
# 把 ClawTeam 的 GATE/Phase 完成事件桥接到 task-monitor
#
# 用法:
#   bridge init <team> <goal>                    # 初始化团队追踪
#   bridge gate <team> <gate_id> <status> [note] # GATE checkpoint
#   bridge notify <team> <from> <content>        # Worker 实时通知→signal+event
#   bridge complete <team> [output]              # 团队完成
#   bridge fail <team> [reason]                  # 团队失败
#   bridge status <team>                         # 查询团队状态
# ============================================
set -euo pipefail

# --- 路径配置 ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

TRACKER="${TASK_TRACKER_PYTHON:-python3}"

# 优先用环境变量，再尝试项目相对路径，最后尝试安装后的标准位置
TRACKER_SCRIPT="${TASK_TRACKER_SCRIPT:-}"
if [ -z "$TRACKER_SCRIPT" ] || [ ! -f "$TRACKER_SCRIPT" ]; then
    TRACKER_SCRIPT="$PROJECT_DIR/skills/task-coordinator/scripts/task_tracker.py"
fi
if [ ! -f "$TRACKER_SCRIPT" ]; then
    TRACKER_SCRIPT="$HOME/.openclaw/workspace/skills/task-coordinator/scripts/task_tracker.py"
fi

# 动态检测 openclaw 路径（不硬编码）
OPENCLAW_BIN="${OPENCLAW_BIN:-}"
if [ -z "$OPENCLAW_BIN" ]; then
    OPENCLAW_BIN="$(command -v openclaw 2>/dev/null || which openclaw 2>/dev/null || echo 'openclaw')"
fi

SIGNAL_DIR="${SIGNAL_DIR:-$HOME/.openclaw/workspace/data/signals}"

ACTION="${1:-help}"; shift || true

# 统一 task ID 格式: ct-{team-name}（第二个位置参数是 team name）
TEAM_NAME="${1:-unknown}"
shift || true  # 消费 team name 参数，后续 $@ 只剩命令特定参数
[ "$ACTION" = "status" ] || [ "$ACTION" = "help" ] || {
    TEAM_ID="ct-$TEAM_NAME"
}

log() { echo "[bridge] $(date '+%H:%M:%S') $*"; }

# ============================================================
# Python helper: 安全写入 signal JSON 文件
# 用环境变量传递数据，避免内联参数的引号转义问题
# ============================================================
_write_signal_py() {
    local signal_type="$1"
    shift

    mkdir -p "$SIGNAL_DIR"

    # 通过环境变量传递所有字段（避免 shell 引号地狱）
    export _BRIDGE_SIGNAL_TYPE="$signal_type"
    export _BRIDGE_TEAM_ID="${TEAM_ID:-}"
    export _BRIDGE_TEAM_NAME="$TEAM_NAME"

    # 动态处理剩余的 key=value 参数
    local i=0
    for arg in "$@"; do
        case "$arg" in
            *=*)
                local k="${arg%%=*}"
                local v="${arg#*=}"
                export "_BRIDGE_FIELD_${i}_KEY"="$k"
                export "_BRIDGE_FIELD_${i}_VAL"="$v"
                i=$((i + 1))
                ;;
        esac
    done
    export _BRIDGE_FIELD_COUNT="$i"

    python3 <<'PYEOF'
import json, os, re
from datetime import datetime

signal_type = os.environ.get("_BRIDGE_SIGNAL_TYPE", "unknown")
team_name   = os.environ.get("_BRIDGE_TEAM_NAME", "")
team_id     = os.environ.get("_BRIDGE_TEAM_ID", "")

data = {
    "source": "clawteam-bridge",
    "type": signal_type,
    "team": team_name,
    "timestamp": datetime.now().isoformat()
}
if team_id:
    data["task_id"] = team_id

# 从环境变量读取动态字段
count = int(os.environ.get("_BRIDGE_FIELD_COUNT", "0"))
for i in range(count):
    key = os.environ.get(f"_BRIDGE_FIELD_{i}_KEY", "")
    val = os.environ.get(f"_BRIDGE_FIELD_{i}_VAL", "")
    if key:
        data[key] = val

# 构建安全的文件名
safe_team = re.sub(r'[^a-zA-Z0-9_-]', '_', team_name or "unknown")
ts_tag = datetime.now().strftime('%H%M%S')
filename = f"{safe_team}_{signal_type}_{ts_tag}.json"

signal_dir = os.environ.get("SIGNAL_DIR",
    os.path.expanduser("~/.openclaw/workspace/data/signals"))
os.makedirs(signal_dir, exist_ok=True)
path = os.path.join(signal_dir, filename)

with open(path, 'w') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print(f"SIGNAL: {path}")
PYEOF

    # 清理环境变量
    unset _BRIDGE_SIGNAL_TYPE _BRIDGE_TEAM_ID _BRIDGE_TEAM_NAME _BRIDGE_FIELD_COUNT
    for i in $(seq 0 10); do
        unset "_BRIDGE_FIELD_${i}_KEY" 2>/dev/null || true
        unset "_BRIDGE_FIELD_${i}_VAL" 2>/dev/null || true
    done
}

# ============================================================
# 发送 openclaw system event（best-effort，失败不影响主流程）
# ============================================================
_send_event() {
    local text="$1"
    local timeout_ms="${2:-5000}"
    $OPENCLAW_BIN system event --mode now --text "$text" --timeout "$timeout_ms" 2>/dev/null || \
        log "⚠️ system event 发送失败（可能 gateway 未运行）"
}

# ============================================================
# GATE ID → Step Name 映射
# ============================================================
_gate_to_step() {
    case "${1:-0}" in
        0) echo "GATE0-diagnosis" ;;
        1) echo "GATE1-prd" ;;
        2) echo "GATE2-prototype" ;;
        3) echo "GATE3-architecture" ;;
        4) echo "GATE4-development" ;;
        5) echo "GATE5-testing" ;;
        6) echo "GATE6-deployment" ;;
        *) echo "GATE${1}" ;;
    esac
}

# ============================================================
# Commands
# ============================================================

cmd_init() {
    local goal="${1:-ClawTeam execution}"
    TASK_ID="ct-${TEAM_NAME}-init"

    log "初始化团队追踪: $TASK_ID (goal: $goal)"

    if [ -f "$TRACKER_SCRIPT" ]; then
        $TRACKER "$TRACKER_SCRIPT" init "$TASK_ID" "$goal" "clawteam-team" \
            --steps "GATE0-diagnosis,GATE1-prd,GATE2-prototype,GATE3-architecture,GATE4-development,GATE5-testing,GATE6-deployment" \
            || log "⚠️ tracker init 失败（非致命）"
    else
        log "⚠️ tracker script 不存在: $TRACKER_SCRIPT（跳过 init）"
    fi

    log "✅ 追踪已初始化: $TASK_ID"
}

cmd_gate() {
    local gate_id="${1:?用法: bridge gate <team> <gate_id> <status> [note]}"
    local gate_status="${2:-completed}"
    local gate_note="${3:-}"

    local step_name
    step_name=$(_gate_to_step "$gate_id")
    TASK_ID="ct-${TEAM_NAME}-init"

    log "GATE ${gate_id} ${gate_status}: ${step_name}${gate_note:+ — $gate_note}"

    # 写 task-coordinator checkpoint
    if [ -f "$TRACKER_SCRIPT" ]; then
        $TRACKER "$TRACKER_SCRIPT" checkpoint "$TASK_ID" "$step_name" "$gate_status" \
            --note "$gate_note" 2>/dev/null || log "⚠️ tracker checkpoint 失败（非致命）"
    fi

    # 写 signal 文件
    _write_signal_py "gate_complete" \
        "gate=$gate_id" "status=$gate_status" "note=$gate_note" "step_name=$step_name" \
        2>/dev/null || log "⚠️ signal 写入失败（非致命）"

    # 通过 openclaw system event 唤醒主 session
    _send_event "[ClawTeam] GATE ${gate_id} ${gate_status} for team ${TEAM_NAME}: ${gate_note:-$step_name}"

    log "✅ GATE ${gate_id} 信号已发送"
}

cmd_notify() {
    local from_agent="${1:-unknown}"
    local content="${2:-}"

    # 截断过长内容（避免 shell / 文件名问题）
    local content_short
    content_short=$(echo "$content" | head -c 300 | tr '\n' ' ')

    log "📬 Worker 通知 from $from_agent: ${content_short:0:80}..."

    # 写 signal
    _write_signal_py "worker_notify" \
        "from_agent=$from_agent" "content=$content_short" \
        2>/dev/null || log "⚠️ signal 写入失败（非致命）"

    # 唤醒主 session
    _send_event "[ClawTeam] $from_agent: $content_short"

    log "📬 Worker 通知已桥接: $from_agent"
}

cmd_complete() {
    local output="${1:-ClawTeam execution completed}"
    TASK_ID="ct-${TEAM_NAME}-init"

    log "团队完成: $TEAM_NAME — $output"

    if [ -f "$TRACKER_SCRIPT" ]; then
        $TRACKER "$TRACKER_SCRIPT" complete "$TASK_ID" --output "$output" 2>/dev/null || \
            log "⚠️ tracker complete 失败（非致命）"
    fi

    _send_event "[ClawTeam] ✅ Team ${TEAM_NAME} completed: $output"

    log "✅ 团队已完成"
}

cmd_fail() {
    local reason="${1:-Unknown failure}"
    TASK_ID="ct-${TEAM_NAME}-init"

    log "团队失败: $TEAM_NAME — $reason"

    if [ -f "$TRACKER_SCRIPT" ]; then
        $TRACKER "$TRACKER_SCRIPT" fail "$TASK_ID" "$reason" 2>/dev/null || \
            log "⚠️ tracker fail 失败（非致命）"
    fi

    _send_event "[ClawTeam] ❌ Team ${TEAM_NAME} failed: $reason"

    log "❌ 团队已标记失败"
}

cmd_status() {
    TASK_ID="ct-${TEAM_NAME}-init"

    if [ -f "$TRACKER_SCRIPT" ]; then
        $TRACKER "$TRACKER_SCRIPT" status "$TASK_ID" 2>/dev/null || \
            echo "{\"ok\":false,\"error\":\"Task not found or tracker error\"}"
    else
        echo "{\"ok\":false,\"error\":\"tracker script not found at $TRACKER_SCRIPT\"}"
    fi
}

cmd_help() {
    cat <<'EOF'
ClawTeam ↔ Task Monitor Bridge

用法: clawteam-bridge.sh <command> <team-name> [...args]

Commands:
  init <team> <goal>                        初始化团队追踪
  gate <team> <gate_id> <status> [note]      GATE checkpoint (gate_id: 0-6)
  notify <team> <from_agent> <content>       Worker 实时通知→signal+event
  complete <team> [output]                   团队完成
  fail <team> [reason]                       团队失败
  status <team>                              查询团队状态
  help                                       显示帮助信息

Environment Variables:
  TASK_TRACKER_PYTHON       Python 解释器 (默认: python3)
  TASK_TRACKER_SCRIPT       task_tracker.py 路径 (自动检测)
  OPENCLAW_BIN              openclaw CLI 路径 (自动检测)
  SIGNAL_DIR                Signal 文件目录 (默认: ~/.openclaw/workspace/data/signals)

Examples:
  clawteam-bridge.sh init my-game "复刻黄金矿工"
  clawteam-bridge.sh gate my-game 0 completed "产品诊断完成"
  clawteam-bridge.sh notify my-game coder "实现了用户认证模块"
  clawteam-bridge.sh complete my-game "全部GATE通过"
  clawteam-bridge.sh status my-game
EOF
}

# ============================================================
# Dispatch
# ============================================================
case "$ACTION" in
    init)      cmd_init "$@" ;;
    gate)      cmd_gate "$@" ;;
    notify)    cmd_notify "$@" ;;
    complete)  cmd_complete "$@" ;;
    fail)      cmd_fail "$@" ;;
    status)    cmd_status ;;
    help|--help|-h) cmd_help ;;
    *)
        echo "❌ 未知命令: $ACTION" >&2
        echo "" >&2
        cmd_help
        exit 1
        ;;
esac
