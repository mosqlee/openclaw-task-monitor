# ClawTeam ↔ Task Monitor 集成指南

## 问题背景

用户使用 **ClawTeam**（多 Agent 编排 CLI 工具）执行开发任务时，存在两套平行的监控体系：

```
┌──────────────────────────────┐     ┌──────────────────────────────┐
│       ClawTeam 生态          │     │    task-monitor 生态         │
│                              │     │                              │
│  clawteam-run.sh             │     │  task-coordinator (init/     │
│  ├── launch 团队              │     │    checkpoint/complete)      │
│  ├── runtime watch            │     │  progress-monitor (插件)      │
│  └── task wait               │     │  Watch Daemon (停滞/超时)     │
│                              │     │                              │
│  ❌ 通知链路断裂:             │     │  ❌ 追踪不到 ClawTeam 任务:   │
│  主 Agent 在 wait 阻塞        │     │  完全孤立，watch daemon 扫不到 │
│  中间通知收不到               │     │                              │
└──────────────────────────────┘     └──────────────────────────────┘
```

**核心问题**：
1. 主 Agent (Kev) 在 `wait $WAIT_PID` 阻塞等 ClawTeam 完成，期间收不到中间通知
2. task-monitor 已安装但完全孤立 — ClawTeam 任务没走 task-coordinator，Watch Daemon 扫不到
3. 两套平行体系零打通

## 架构方案

### Bridge 脚本的角色

```
                    ┌─────────────────────────┐
                    │   clawteam-bridge.sh     │
                    │   (纯 shell, 无依赖)      │
                    └─────┬───────┬───────┬───┘
                          │       │       │
           ┌──────────────▼┐  ┌──▼───┐ ┌▼──────────────┐
           │ task_tracker.py│  │signal│ │openclaw system │
           │ (checkpoint/   │  │文件  │ │event          │
           │  complete/fail)│  │JSON  │ │(唤醒主session) │
           └───────┬────────┘  └──┬───┘ └───────────────┘
                   │              │
          ┌────────▼────────┐  ┌──▼──────────────┐
          │ data/task-traces│  │data/signals/    │
          │ /ct-{team}-init │  │(heartbeat 收集)  │
          └─────────────────┘  └─────────────────┘
```

**数据流（完整生命周期）**：

```
clawteam-run.sh 启动
  │
  ├─→ bridge init <team> <goal>
  │     → task_tracker.py init ct-{team}-init --steps GATE0..GATE6
  │     → 写入 task_plan.json 到 data/task-traces/
  │
  ├─→ [ClawTeam Worker 执行中...]
  │
  ├─→ bridge gate <team> 0 completed "诊断完成"    ← 每个 GATE 完成时
  │     → task_tracker.py checkpoint ... GATE0-diagnosis done
  │     → 写 signal JSON 文件
  │     → openclaw system event 唤醒主 session
  │
  ├─→ bridge notify <team> coder "实现了认证模块"   ← Worker 实时消息
  │     → 写 signal JSON 文件
  │     → openclaw system event 唤醒主 session
  │
  ├─→ [重复 gate/notify 直到所有 GATE 完成]
  │
  ├─→ bridge complete <team> "全部通过"
  │     → task_tracker.py complete ...
  │     → openclaw system event 唤醒主 session
  │
  └─→ [或] bridge fail <team> "编译错误"
        → task_tracker.py fail ...
        → openclaw system event 唤醒主 session
```

## 集成步骤（3 步）

### Step 1: 安装 Bridge

```bash
cd /path/to/openclaw-task-monitor
bash scripts/setup.sh

# 或手动复制:
cp scripts/clawteam-bridge.sh ~/.openclaw/workspace/scripts/clawteam-bridge.sh
chmod +x ~/.openclaw/workspace/scripts/clawteam-bridge.sh
```

安装后确认可用：
```bash
bash ~/.openclaw/workspace/scripts/clawteam-bridge.sh help
```

### Step 2: 修改 `clawteam-run.sh` 集成 Bridge

在 `~/.clawteam/scripts/clawteam-run.sh` 的关键位置插入 bridge 调用：

```bash
#!/bin/bash
# clawteam-run.sh (集成版示例)
set -euo pipefail

BRIDGE="$HOME/.openclaw/workspace/scripts/clawteam-bridge.sh"
TEAM_NAME="${1:?用法: clawteam-run.sh <team-name> '<goal>' [timeout]}"
GOAL="${2:-}"
TIMEOUT="${3:-3600}"

# ====== 新增: 初始化追踪 ======
if [ -n "$GOAL" ]; then
    bash "$BRIDGE" init "$TEAM_NAME" "$GOAL" || true
fi

# ====== 原有逻辑: launch 团队 ======
# ... clawteam launch ...

# ====== 原有逻辑: runtime watch + task wait ======
# runtime watch 的回调中增加 bridge notify/gate:

# 当检测到 GATE 完成时:
#   bash "$BRIDGE" gate "$TEAM_NAME" $GATE_ID completed "$NOTE"

# 当收到 worker inbox 消息时:
#   bash "$BRIDGE" notify "$TEAM_NAME" "$FROM_AGENT" "$CONTENT"

# ====== 最终结果处理 ======
EXIT_CODE=$?
if [ $EXIT_CODE -eq 0 ]; then
    bash "$BRIDGE" complete "$TEAM_NAME" "$FINAL_OUTPUT" || true
else
    bash "$BRIDGE" fail "$TEAM_NAME" "exit code $EXIT_CODE" || true
fi
```

### Step 3: 验证集成

```bash
# 检查 bridge 是否正常工作
bash "$BRIDGE" status my-team

# 检查 signal 文件是否生成
ls ~/.openclaw/workspace/data/signals/*my-team* 2>/dev/null

# 检查 task-traces 有数据
python3 ~/.openclaw/workspace/skills/task-coordinator/scripts/task_tracker.py \
    status ct-my-team-init
```

## 与现有组件的关系

| 组件 | 与 Bridge 的关系 | 变化 |
|------|-----------------|------|
| **task-coordinator** | Bridge 调用其 `init/checkpoint/complete/fail` | 无变化，Bridge 是调用方 |
| **progress-monitor 插件** | 无直接关系，并行工作 | 无变化 |
| **Watch Daemon** | Bridge 创建的 task 被 Watch Daemon 自动扫描 | 无变化，自动受益 |
| **Signal Layer** | Bridge 写入 signal 文件，Heartbeat 收集 | 无变化，信号格式兼容 |
| **trace-query** | 可查询 Bridge 创建的任务轨迹 | 无变化，自动可见 |
| **ClawTeam CLI** | 不修改，只改 wrapper 脚本 | 无变化 |

## 注意事项

### 1. Best-Effort 设计

Bridge 所有操作都是 **best-effort**：
- tracker 脚本不存在 → 跳过，不报错
- signal 写入失败 → 记录日志，继续执行
- openclaw event 发送失败 → gateway 可能未运行，忽略
- **任何失败都不会影响 ClawTeam 主流程**

### 2. Task ID 格式约定

Bridge 统一使用 `ct-{team-name}-init` 格式的 task ID：
- `ct-` 前缀标识来源为 ClawTeam
- team-name 来自 ClawTeam 团队名
- `-init` 后缀因为一个团队对应一个 task_tracker 任务

### 3. GATE 映射

ClawTeam 的 7 个 GATE 自动映射到 task-coordinator 的 step 名称：

| Gate ID | Step Name | 说明 |
|---------|-----------|------|
| 0 | GATE0-diagnosis | 产品诊断 |
| 1 | GATE1-prd | PRD 文档 |
| 2 | GATE2-prototype | 原型设计 |
| 3 | GATE3-architecture | 架构设计 |
| 4 | GATE4-development | 开发实现 |
| 5 | GATE5-testing | 测试验证 |
| 6 | GATE6-deployment | 部署上线 |

### 4. 特殊字符安全

- Signal 文件名中的特殊字符会被替换为 `_`
- 内容截断到 300 字符避免 shell / 文件系统问题
- Python heredoc 方式写入 JSON，避免引号转义问题

### 5. 环境变量覆盖

| 变量 | 默认值 | 用途 |
|------|--------|------|
| `TASK_TRACKER_PYTHON` | `python3` | Python 解释器路径 |
| `TASK_TRACKER_SCRIPT` | 自动检测 | task_tracker.py 路径 |
| `OPENCLAW_BIN` | 自动检测 (`which openclaw`) | openclaw CLI 路径 |
| `SIGNAL_DIR` | `~/.openclaw/workspace/data/signals` | Signal 文件目录 |

### 6. 向后兼容

- Bridge 是**纯增量**添加，不修改任何现有文件的功能
- 不改 OpenClaw 核心 session 逻辑或事件流
- 不改 progress-monitor 插件的 hook 逻辑
- 不改 ClawTeam CLI 本身
