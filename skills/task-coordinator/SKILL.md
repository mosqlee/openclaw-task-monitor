---
name: task-coordinator
description: 任务追踪与超时兜底。每次 spawn subagent 前自动创建追踪，超时/失败后保证有输出。触发词：调度任务、spawn、派发任务。
version: 1.0.0
phase: 1
---

# Task Coordinator - 任务追踪与超时兜底

> **核心目标**：每个任务都有追踪，失败也有报告，超时也有人兜底。

## 什么时候用这个 Skill？

**每次 spawn subagent 执行任务时，都必须使用本 Skill。**

具体来说：
- `sessions_spawn` 调用前 → 初始化追踪
- SubAgent 完成后 → 写入结果
- SubAgent 超时 → 生成超时报告

**不需要用的场景**：
- 直接执行（不 spawn）的简单任务
- 心跳检查

---

## 工具

```bash
TRACKER="python3 ~/.openclaw/workspace/skills/task-coordinator/scripts/task_tracker.py"
```

---

## 执行流程

### Step 1: spawn 前初始化（必须）

在调用 `sessions_spawn` 之前，先创建追踪：

```bash
# 生成 task_id（用日期+简短描述）
TASK_ID="task-$(date +%Y%m%d-%H%M%S)-{简短关键词}"

# 初始化追踪
python3 skills/task-coordinator/scripts/task_tracker.py init "$TASK_ID" \
  "任务目标描述" \
  "agent名称" \
  --steps "步骤1,步骤2,步骤3"
```

然后正常 spawn：
```javascript
sessions_spawn({
  task: "任务描述。追踪ID: {TASK_ID}",
  agentId: "xxx",
  label: TASK_ID  // 用 task_id 作为 label，方便追踪
})
```

### Step 2: SubAgent 完成后（必须）

SubAgent 完成后，写入结果：

```bash
# 成功
python3 skills/task-coordinator/scripts/task_tracker.py complete "$TASK_ID" \
  --output "结果摘要" \
  --duration 12345

# 失败（SubAgent 报告了错误）
python3 skills/task-coordinator/scripts/task_tracker.py fail "$TASK_ID" \
  "失败原因" \
  --last-step "最后步骤" \
  --duration 12345
```

### Step 3: 超时兜底（当 SubAgent 没有返回时）

如果 spawn 后超过预期时间没有收到 SubAgent 的返回：

```bash
# 读取最后进度
python3 skills/task-coordinator/scripts/task_tracker.py status "$TASK_ID"

# 生成超时报告
python3 skills/task-coordinator/scripts/task_tracker.py timeout "$TASK_ID" \
  --last-step "最后步骤（从 status 获取）" \
  --duration 600000
```

然后**必须**向用户发送超时通知，包含 progress.json 中的最后进度。

---

## 追踪文件位置

```
$TASK_TRACE_DIR/{task-id}/  (默认 ~/.openclaw/workspace/data/task-traces/)
├── task_plan.json    # 目标、步骤、状态、进度
├── progress.json     # 事件流（每步记录）
└── result.json       # 最终结果（成功/失败/超时）
```

---

## 查看所有任务

```bash
# 列出所有
python3 skills/task-coordinator/scripts/task_tracker.py list

# 只看运行中的
python3 skills/task-coordinator/scripts/task_tracker.py list --status running

# 只看失败的
python3 skills/task-coordinator/scripts/task_tracker.py list --status failed
```

## 清理旧记录

```bash
# 清理 72 小时前的已完成/失败/超时记录（默认）
python3 skills/task-coordinator/scripts/task_tracker.py cleanup

# 清理 24 小时前的
python3 skills/task-coordinator/scripts/task_tracker.py cleanup --max-age-hours 24
```

## Watchdog（心跳集成）

心跳时会自动执行 watchdog，扫描 running 超过 30 分钟的任务。
发现超时任务会自动标记 timeout 并通知用户。

```bash
# 手动触发
python3 skills/task-coordinator/scripts/task_tracker.py watchdog --max-age-minutes 30
```

---

## 最小失败输出契约

**每个任务必须有 result.json**。不管是成功、失败、还是超时，都必须有这个文件。

result.json 包含：
```json
{
  "task_id": "xxx",
  "status": "completed|failed|timeout",
  "output": "结果内容或失败原因",
  "last_step": "最后成功步骤",
  "duration_ms": 12345,
  "completed_at": "2026-04-03T08:30:00+08:00"
}
```

---

## 超时时间参考

| 任务类型 | 建议超时 | 示例 |
|----------|---------|------|
| 简单查询 | 30s | 获取K线数据 |
| 单 Agent 分析 | 3-5min | 财务分析 |
| 编码任务 | 10-20min | 实现功能模块 |
| 多步骤编排 | 20-30min | 调研+写作 |
| 复杂编排 | 30-60min | 完整项目 |

---

## 3-Strike 协议（Phase 1 简化版）

```
SubAgent 失败
│
├── Strike 1: 同一 Agent 重试（补充上下文）
│   └── 失败 ↓
├── Strike 2: 换 Agent 或换方法
│   └── 失败 ↓
└── Strike 3: 输出结构化失败报告给用户
    ├── 包含：做了什么、到哪了、为什么失败
    ├── 包含：建议用户怎么做
    └── 标记 result.json status=failed
```

每次 Strike 都记录到 progress.json：
```bash
python3 skills/task-coordinator/scripts/task_tracker.py checkpoint "$TASK_ID" \
  "strike-N" "failed" --note "失败原因和下次策略"
```

---

## AGENTS.md 迁移规则

### 🔧 task-coordinator + trace-query 扩展使用规则

**⚠️ 按任务类型判断，不预测时间！**

| 任务类型 | task-coordinator | trace-query |
|----------|:-:|:-:|
| spawn subagent | ✅ | ✅ |
| exec（background / yieldMs>10s / timeout>60s） | ✅ | ❌ |
| 多步骤操作（>2步串行） | ✅ | ❌ |
| 重复性/周期性任务 | ❌ | ✅ |
| coding-agent / 长程研究 | ✅ | ✅ |
| 单步即时操作 | ❌ | ❌ |

### 🚨 spawn subagent 强制 Checklist

```
□ Step 0: trace-query — 查同类任务历史
  python3 ~/.openclaw/workspace/skills/trace-query/scripts/trace_query.py search "任务关键词"
  目的：借鉴成功经验、避坑失败模式

□ Step 1: task-coordinator init — 为【每个】subagent 单独建追踪
  python3 ~/.openclaw/workspace/skills/task-coordinator/scripts/task_tracker.py init \
    "task-$(date +%Y%m%d-%H%M%S)-子任务名" "子任务目标" "agent名" \
    --steps "步骤1,步骤2"

□ Step 2: sessions_spawn

□ Step 3: 完成后 complete/fail/timeout
  python3 ~/.openclaw/workspace/skills/task-coordinator/scripts/task_tracker.py complete "$TASK_ID" --output "结果"
```

**常见错误：**
- ❌ 只给总任务建一个追踪，不给每个 subagent 单独建 → 中间某批超时无法定位
- ❌ trace-query 完全跳过 → 重复踩坑
- ❌ 总任务 init 掩盖了子任务缺失 → 粒度不够

### Task Coordinator 调用方式

```bash
# spawn 前初始化追踪
python3 ~/.openclaw/workspace/skills/task-coordinator/scripts/task_tracker.py init \
  "task-$(date +%Y%m%d-%H%M%S)-关键词" "任务目标" "agent名" --steps "步骤1,步骤2"

# 完成后写结果
python3 ~/.openclaw/workspace/skills/task-coordinator/scripts/task_tracker.py complete "$TASK_ID" --output "结果"
# 或失败：
python3 ~/.openclaw/workspace/skills/task-coordinator/scripts/task_tracker.py fail "$TASK_ID" "原因"
# 或超时兜底：
python3 ~/.openclaw/workspace/skills/task-coordinator/scripts/task_tracker.py timeout "$TASK_ID" --last-step "步骤"
```

**核心规则**：每个任务必须有 `result.json`，不管成功、失败还是超时。

**不做的事：**
- ❌ 不猜时间，按客观特征判断
- ❌ 不给即时任务加追踪
- ❌ 不用一个总追踪替代多个子任务追踪
