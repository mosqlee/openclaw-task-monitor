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

## ClawTeam 任务强制 Checklist

> ⚠️ **ClawTeam 任务必须走 bridge 打通 task-monitor，否则 Watch Daemon 扫不到！**

```
□ Step 0: bridge init — launch 前初始化团队追踪
  bash ~/.openclaw/workspace/scripts/clawteam-bridge.sh init <team-name> '<项目目标>'
  # → 自动创建 ct-{team}-init 任务，含 GATE0~GATE6 共7个步骤

□ Step 1: 每个 GATE 完成时 → bridge gate
  bash ~/.openclaw/workspace/scripts/clawteam-bridge.sh gate <team-name> <0-6> completed '<简述>'
  # → checkpoint 记录 + signal 文件 + system event 唤醒主 session

□ Step 2: Worker 实时通知 → bridge notify（可选但推荐）
  bash ~/.openclaw/workspace/scripts/clawteam-bridge.sh notify <team-name> <agent名> '<内容>'
  # → signal 文件 + system event（主 Agent 阻塞期间也能收到）

□ Step 3: 团队完成/失败 → bridge complete/fail
  bash ~/.openclaw/workspace/scripts/clawteam-bridge.sh complete <team-name> '<输出摘要>'
  # 或
  bash ~/.openclaw/workspace/scripts/clawteam-bridge.sh fail <team-name> '<原因>'

□ Step 4: 验证（可选）
  bash ~/.openclaw/workspace/scripts/clawteam-bridge.sh status <team-name>
  ls ~/.openclaw/workspace/data/signals/*<team-name>* 2>/dev/null
```
