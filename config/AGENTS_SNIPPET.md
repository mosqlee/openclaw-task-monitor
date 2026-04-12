# AGENTS.md 配置片段

将以下内容添加到你的 `~/.openclaw/workspace/AGENTS.md` 中。

## Spawn Subagent 三步流程

1. **check**: trace-query 查历史 + task-coordinator init
2. **spawn**: sessions_spawn
3. **complete**: task-coordinator complete/fail/timeout

## task-coordinator + trace-query 使用规则

| 任务类型 | task-coordinator | trace-query |
|----------|:-:|:-:|
| spawn subagent | ✅ | ✅ |
| exec（background / yieldMs>10s / timeout>60s） | ✅ | ❌ |
| 多步骤操作（>2步串行） | ✅ | ❌ |
| 重复性/周期性任务 | ❌ | ✅ |
| coding-agent / 长程研究 | ✅ | ✅ |
| 单步即时操作 | ❌ | ❌ |

## spawn subagent 强制 Checklist

```
□ Step 0: trace-query — 查同类任务历史
  python3 ~/.openclaw/workspace/skills/trace-query/scripts/query_api.py search "任务关键词"

□ Step 1: task-coordinator init — 为【每个】subagent 单独建追踪
  python3 ~/.openclaw/workspace/skills/task-coordinator/scripts/task_tracker.py init \
    "task-$(date +%Y%m%d-%H%M%S)-子任务名" "子任务目标" "agent名" \
    --steps "步骤1,步骤2"

□ Step 2: sessions_spawn

□ Step 3: 完成后 complete/fail/timeout
  python3 ~/.openclaw/workspace/skills/task-coordinator/scripts/task_tracker.py complete "$TASK_ID" --output "结果"
```
