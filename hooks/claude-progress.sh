#!/bin/bash
# Claude Progress Writer - 将 Claude Code 事件写入进度文件
# 
# 此脚本由 Claude Code 的 hooks 机制触发，将工具调用和会话事件
# 写入 JSONL 文件，供 progress-monitor 插件实时监控。
#
# 安装方式见 README.md 的 "Claude Code 进度监控" 章节。
#
# Hook 输入格式（JSON，通过 stdin 传入）:
#   {
#     "session_id": "...",       // Claude session ID
#     "tool_name": "Bash",       // 工具名（PreToolUse/PostToolUse）
#     "tool_input": { ... },     // 工具输入
#     "tool_output": "...",      // 工具输出（PostToolUse）
#     "error": "...",            // 错误信息
#     "agent_type": "...",       // 子代理类型（SubagentStart/Stop）
#     "outcome": "...",          // 执行结果
#     "reason": "..."            // 停止原因（Stop/StopFailure）
#   }

set -euo pipefail

EVENT_TYPE="${1:-unknown}"
INPUT=$(cat)

# 解析关键信息（jq 必须可用）
if ! command -v jq &>/dev/null; then
  exit 0
fi

SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"')
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // ""')
TOOL_INPUT=$(echo "$INPUT" | jq -r '.tool_input.command // .tool_input.file_path // .tool_input // ""' | head -c 200)
TOOL_OUTPUT=$(echo "$INPUT" | jq -r '.tool_output // ""' | head -c 500)
ERROR=$(echo "$INPUT" | jq -r '.error // ""')
AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // ""')
OUTCOME=$(echo "$INPUT" | jq -r '.outcome // ""')
REASON=$(echo "$INPUT" | jq -r '.reason // ""')

# 进度文件路径（与 progress-monitor 插件约定一致）
PROGRESS_DIR="${CLAUDE_PROGRESS_DIR:-$HOME/.openclaw/workspace/data/claude-progress}"
mkdir -p "$PROGRESS_DIR"

PROGRESS_FILE="$PROGRESS_DIR/${SESSION_ID}.jsonl"

# 写入事件（JSONL 格式，追加）
jq -n \
  --arg event "$EVENT_TYPE" \
  --arg tool "$TOOL_NAME" \
  --arg input "$TOOL_INPUT" \
  --arg output "$TOOL_OUTPUT" \
  --arg error "$ERROR" \
  --arg agent "$AGENT_TYPE" \
  --arg outcome "$OUTCOME" \
  --arg reason "$REASON" \
  --arg time "$(date -Iseconds)" \
  --argjson ts "$(date +%s)000" \
  '{
    event: $event,
    tool: $tool,
    input: $input,
    output: $output,
    error: $error,
    agent: $agent,
    outcome: $outcome,
    reason: $reason,
    time: $time,
    timestamp: $ts
  }' >> "$PROGRESS_FILE"

# 更新最新状态文件（供 progress-monitor 插件 fs.watch 快速读取）
LATEST_FILE="$PROGRESS_DIR/${SESSION_ID}_latest.json"
jq -n \
  --arg event "$EVENT_TYPE" \
  --arg tool "$TOOL_NAME" \
  --arg input "$TOOL_INPUT" \
  --arg time "$(date -Iseconds)" \
  --argjson ts "$(date +%s)000" \
  '{event: $event, tool: $tool, input: $input, time: $time, timestamp: $ts}' > "$LATEST_FILE"
