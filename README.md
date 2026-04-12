# OpenClaw Task Monitor

> OpenClaw 多智能体编排系统中的任务监控能力。追踪每个 SubAgent 的执行状态，自动检测停滞/超时，保证每个任务都有输出。

## 这是什么

当 OpenClaw 的 Main Agent 派发任务给 SubAgent 时，**任务可能会悄悄挂掉**——没有结果、没有通知、没有日志。这个系统解决这个问题：

- 🔍 **任务追踪**：每个 SubAgent 任务都有完整的生命周期追踪（计划→进度→结果）
- ⏰ **停滞检测**：后台 Watch Daemon 实时监控，5分钟无进展自动告警
- 🔴 **超时兜底**：10分钟无响应自动标记超时，生成结构化报告
- 📊 **轨迹检索**：查询历史任务，借鉴成功经验，避免重复踩坑
- 📢 **主动通知**：通过飞书/OpenClaw event 主动唤醒处理

## 系统架构

```
┌─────────────────────────────────────────────────┐
│                  Main Agent                      │
│  spawn → init 追踪 → SubAgent 执行 → complete   │
└──────┬──────────────────────────────────┬───────┘
       │                                  │
  ┌────▼─────┐                     ┌──────▼──────┐
  │ SubAgent │                     │ Watch Daemon │
  │ (执行者)  │                     │ (后台守护)   │
  └────┬─────┘                     └──────┬──────┘
       │                                  │
  ┌────▼──────────────────────────────────▼─────┐
  │         Data Layer (文件系统)                 │
  │  data/task-traces/{task-id}/                 │
  │  ├── task_plan.json    # 任务计划            │
  │  ├── progress.json     # 事件流              │
  │  ├── tool_calls.json   # 工具调用            │
  │  ├── prompt_snapshots  # Prompt 快照         │
  │  └── result.json       # 最终结果            │
  └──────────────────────────────────────────────┘
       │                                  │
  ┌────▼──────────────────────────────────▼─────┐
  │              Signal Layer                     │
  │  data/signals/{task-id}_{type}.json          │
  └────────────────────┬────────────────────────┘
                       │
                 ┌─────▼──────┐
                 │  HEARTBEAT  │
                 │  收集信号    │
                 │  → 通知用户  │
                 └────────────┘
```

详细架构说明见 [docs/architecture.md](docs/architecture.md)。

## 组件说明

### 1. task-coordinator（核心追踪器）

**位置**: `skills/task-coordinator/`

每次 spawn SubAgent 前后必须调用的任务管理工具。

| 命令 | 用途 |
|------|------|
| `init <id> <goal> <agent> [--steps ...]` | 初始化任务追踪 |
| `checkpoint <id> <step> <status>` | 记录步骤完成 |
| `complete <id> [--output ...]` | 标记成功 |
| `fail <id> <reason>` | 标记失败 |
| `timeout <id>` | 标记超时（Watch Daemon 自动调用） |
| `status <id>` | 查看任务状态 |
| `list [--status running]` | 列出所有任务 |
| `cleanup [--max-age-hours 72]` | 清理过期记录 |
| `watch [--interval 60 --stale-threshold 300 --timeout 600]` | 后台守护进程 |
| `watchdog [--max-age-minutes 30]` | 一次性扫描超时任务 |
| `tool-call <id> <tool>` | 记录工具调用 |
| `prompt-snapshot <id>` | 保存 Prompt 快照 |
| `trace-summary <id>` | 输出完整追踪摘要 |

### 2. trace-query（轨迹检索）

**位置**: `skills/trace-query/`

查询历史任务轨迹，支持相似任务匹配和失败模式分析。

| 命令 | 用途 |
|------|------|
| `similar --goal "..." --k 5` | 查询相似成功任务 |
| `failures [--step-type ...]` | 查询失败模式 |
| `trace --task-id ...` | 获取完整轨迹 |

### 3. Watch Daemon（后台守护进程）

通过 HEARTBEAT.md 配置自动启动，持续监控所有 running 状态的任务：

- **停滞告警**: 5分钟无新 checkpoint → 写 signal → 唤醒 LLM + 飞书通知
- **超时兜底**: 10分钟无响应 → 自动标记 timeout + 生成报告
- **恢复检测**: 停滞任务恢复后自动清理告警状态

### 4. Heartbeat 集成

在 `HEARTBEAT.md` 中配置两件事：
1. **Watch Daemon 保活**: 心跳时检查进程是否存活，不存活则重启
2. **Signal 收集**: 心跳时收集所有 signal 文件，AI 读取后决定如何处理

## 快速开始

### 前提条件

- Python 3.8+
- OpenClaw 已安装（`openclaw` CLI 可用）
- 已配置 OpenClaw workspace（默认 `~/.openclaw/workspace`）

### 一键安装

```bash
# 克隆仓库
git clone git@github.com:<your-org>/openclaw-task-monitor.git
cd openclaw-task-monitor

# 运行安装脚本
bash scripts/setup.sh

# 或指定 workspace 路径
bash scripts/setup.sh /path/to/your/openclaw/workspace
```

安装脚本会自动：
- ✅ 创建目录结构
- ✅ 复制 Skill 文件到 workspace
- ✅ 初始化数据目录
- ✅ 追加 Watch Daemon 配置到 HEARTBEAT.md

### 手动安装

```bash
# 1. 创建目录
WORKSPACE=~/.openclaw/workspace
mkdir -p $WORKSPACE/skills/task-coordinator/scripts
mkdir -p $WORKSPACE/skills/trace-query/scripts
mkdir -p $WORKSPACE/data/task-traces
mkdir -p $WORKSPACE/data/signals

# 2. 复制 skill 文件
cp -r skills/task-coordinator/* $WORKSPACE/skills/task-coordinator/
cp -r skills/trace-query/* $WORKSPACE/skills/trace-query/

# 3. 配置 HEARTBEAT.md
# 将 config/HEARTBEAT_SNIPPET.md 的内容追加到 $WORKSPACE/HEARTBEAT.md

# 4. 配置 AGENTS.md
# 将 config/AGENTS_SNIPPET.md 的内容追加到 $WORKSPACE/AGENTS.md
```

### 配置 AGENTS.md

确保你的 `AGENTS.md` 包含 spawn subagent 的三步 checklist，这样 AI 才会自动使用追踪系统：

```markdown
## Spawn Subagent 三步流程
1. check: trace-query 查历史 + task-coordinator init
2. spawn: sessions_spawn
3. complete: task-coordinator complete/fail/timeout
```

参考 `config/AGENTS_SNIPPET.md` 获取完整配置片段。

## 使用示例

### 完整的 SubAgent 调用流程

```
# Step 0: 查询相似历史任务
python3 skills/trace-query/scripts/query_api.py similar --goal "实现用户认证" --k 3

# Step 1: 初始化追踪
TASK_ID="task-$(date +%Y%m%d-%H%M%S)-user-auth"
python3 skills/task-coordinator/scripts/task_tracker.py init \
  "$TASK_ID" "实现用户认证模块" "claudecode" \
  --steps "设计模型,实现接口,编写测试" \
  --notify-user "ou_xxxxxxxxxxxx"

# Step 2: spawn SubAgent（用 TASK_ID 作为 label）
sessions_spawn({ task: "...", label: TASK_ID, ... })

# Step 3: SubAgent 完成后
python3 skills/task-coordinator/scripts/task_tracker.py complete \
  "$TASK_ID" --output "用户认证模块实现完成"
```

### 查看任务状态

```bash
# 查看所有任务
python3 skills/task-coordinator/scripts/task_tracker.py list

# 只看运行中的
python3 skills/task-coordinator/scripts/task_tracker.py list --status running

# 查看某个任务的详细状态
python3 skills/task-coordinator/scripts/task_tracker.py status "task-20260412-xxx"

# 查看完整追踪（含工具调用、Prompt 快照）
python3 skills/task-coordinator/scripts/task_tracker.py trace-summary "task-20260412-xxx"
```

### 查询历史经验

```bash
# 查找相似的成功任务
python3 skills/trace-query/scripts/query_api.py similar \
  --goal "实现用户认证模块" --k 5 --status completed

# 分析失败模式
python3 skills/trace-query/scripts/query_api.py failures
```

## 配置说明

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TASK_TRACE_DIR` | `~/.openclaw/workspace/data/task-traces` | 追踪数据目录 |
| `SIGNAL_DIR` | `~/.openclaw/workspace/data/signals` | Signal 文件目录 |

### Watch Daemon 参数

通过 HEARTBEAT.md 中的 `WATCHER_BIN` 命令调整：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--interval` | 60 | 扫描间隔（秒） |
| `--stale-threshold` | 300 | 停滞告警阈值（秒） |
| `--timeout` | 600 | 超时标记阈值（秒） |

### 飞书通知

在 `init` 时传入 `--notify-user` 指定接收告警的用户 open_id：

```bash
python3 skills/task-coordinator/scripts/task_tracker.py init \
  "$TASK_ID" "任务目标" "agent" \
  --notify-user "ou_xxxxxxxxxxxx"
```

## 开发指南

### 运行测试

```bash
cd skills/task-coordinator/scripts
python3 -m pytest test_task_tracker.py -v
```

### 项目结构

```
openclaw-task-monitor/
├── README.md                          # 本文件
├── LICENSE                            # MIT 许可证
├── skills/
│   ├── task-coordinator/
│   │   ├── SKILL.md                   # Skill 元数据和使用说明
│   │   ├── package.json               # 包信息
│   │   ├── .env.example               # 环境变量示例
│   │   └── scripts/
│   │       ├── task_tracker.py        # 核心追踪脚本
│   │       └── test_task_tracker.py   # 单元测试
│   └── trace-query/
│       ├── SKILL.md                   # Skill 元数据和使用说明
│       ├── package.json               # 包信息
│       ├── .env.example               # 环境变量示例
│       └── scripts/
│           └── query_api.py           # 轨迹检索脚本
├── config/
│   ├── HEARTBEAT_SNIPPET.md           # HEARTBEAT.md 配置片段
│   └── AGENTS_SNIPPET.md              # AGENTS.md 配置片段
├── docs/
│   └── architecture.md                # 详细架构说明
└── scripts/
    └── setup.sh                       # 一键安装脚本
```

### 扩展建议

- **通知渠道扩展**: 修改 `task_tracker.py` 中的 `_notify_user_and_wake_session` 函数，支持更多通知渠道
- **相似度算法升级**: 在 `query_api.py` 中替换 `simple_similarity` 为更高级的向量相似度
- **Web Dashboard**: 基于 task-traces 数据构建可视化面板
- **Prometheus 集成**: 暴露任务指标供监控系统采集

## 注意事项

1. **Watch Daemon 依赖 HEARTBEAT**: Watch Daemon 的保活检查写在 HEARTBEAT.md 中，确保你的 OpenClaw 配置启用了心跳
2. **Signal 清理**: 心跳会自动收集并清理 signal 文件，不需要手动清理
3. **数据清理**: 定期运行 `cleanup` 命令清理过期记录（默认保留72小时）
4. **PID 文件**: Watch Daemon 的 PID 文件在 `data/task-traces/watch.pid`，进程异常退出时可能残留，重启前确认无残留进程
5. **时区**: 所有时间戳使用 CST (Asia/Shanghai, UTC+8)

## License

MIT
