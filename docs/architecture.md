# 系统架构

## 整体架构

```
┌─────────────────────────────────────────────────────────┐
│                    OpenClaw Main Agent                   │
│                                                         │
│  用户请求 → spawn subagent → task-coordinator init      │
│           ← subagent 完成 ← task-coordinator complete   │
└──────────┬──────────────────────────────────┬───────────┘
           │                                  │
     ┌─────▼─────┐                    ┌──────▼──────┐
     │  SubAgent │                    │ Watch Daemon │
     │  (执行者)  │                    │  (后台守护)  │
     └─────┬─────┘                    └──────┬──────┘
           │                                  │
     ┌─────▼─────────────────────────────────▼──────┐
     │            Data Layer (文件系统)                │
     │  ~/.openclaw/workspace/data/task-traces/       │
     │  ├── {task-id}/                               │
     │  │   ├── task_plan.json    # 任务计划         │
     │  │   ├── progress.json     # 事件流           │
     │  │   ├── tool_calls.json   # 工具调用记录     │
     │  │   ├── prompt_snapshots  # Prompt 快照      │
     │  │   └── result.json       # 最终结果         │
     │  └── watch.pid             # 守护进程 PID    │
     └───────────────────────────────────────────────┘
           │                                  │
     ┌─────▼─────────────────────────────────▼──────┐
     │              Signal Layer                      │
     │  ~/.openclaw/workspace/data/signals/          │
     │  ├── {task-id}_stale.json                     │
     │  ├── {task-id}_timeout.json                   │
     │  └── {task-id}_recovered.json                 │
     └──────────────────┬───────────────────────────┘
                        │
                  ┌─────▼──────┐
                  │  HEARTBEAT  │
                  │  (心跳轮询)  │
                  │  收集信号    │
                  │  → 通知用户  │
                  └────────────┘
```

## 数据流

### SubAgent 生命周期追踪

```
1. Main Agent 收到任务
2. trace-query similar → 查询相似历史任务
3. task-tracker init → 创建 task_plan.json + progress.json
4. sessions_spawn → 启动 SubAgent
5. SubAgent 执行中:
   ├── checkpoint → 更新 progress.json 事件流
   ├── tool-call → 记录工具调用到 tool_calls.json
   └── prompt-snapshot → 保存 Prompt 快照
6. SubAgent 完成:
   ├── complete → 写 result.json (status=completed)
   ├── fail → 写 result.json (status=failed)
   └── (无返回) → watch daemon 超时兜底
```

### Watch Daemon 工作机制

```
Watch Daemon (每60秒扫描一次)
│
├── 扫描 task-traces/ 下所有 status=running 的任务
│
├── 计算每个任务的"最后活跃时间"（从 progress.json 事件流）
│   elapsed = now - last_checkpoint_time
│
├── elapsed > 300s (5min) 且未通知过
│   → 写 stale signal → 通过 openclaw system event 唤醒 LLM session
│   → 如果 task_plan 中有 notify_user，发送飞书通知
│
├── elapsed > 600s (10min)
│   → 自动调用 cmd_timeout() 标记任务超时
│   → 写 timeout signal → 唤醒 LLM + 飞书通知
│
└── 已 stale 任务恢复 (elapsed < 240s)
    → 写 recovered signal
    → 从 notified_stale 集合中移除
```

### Signal 通信流程

```
Watch Daemon                    Heartbeat                      Main Agent
     │                              │                              │
     │── 写 stale.json ──────────→  │                              │
     │                              │── 收集所有 signals ─────────→│
     │                              │── 读取 _heartbeat_pending ──→│
     │                              │                              │── 判断信号类型
     │                              │                              │── 通知用户
     │                              │                              │── 决定是否干预
```

## 核心组件

### task_tracker.py

| 命令 | 用途 |
|------|------|
| `init` | 初始化任务追踪（创建 task_plan + progress） |
| `checkpoint` | 记录步骤完成（更新 progress 事件流） |
| `complete` | 标记成功，写 result.json |
| `fail` | 标记失败，写 result.json |
| `timeout` | 标记超时，生成超时报告 |
| `status` | 查看任务当前状态 |
| `list` | 列出所有任务（支持状态过滤） |
| `cleanup` | 清理过期记录 |
| `watchdog` | 一次性扫描超时任务 |
| `watch` | 后台守护进程（持续监控） |
| `tool-call` | 记录工具调用 |
| `prompt-snapshot` | 保存 Prompt 快照 |
| `trace-summary` | 输出完整追踪摘要 |

### query_api.py

| 命令 | 用途 |
|------|------|
| `similar` | 查询相似成功任务（Jaccard 相似度） |
| `failures` | 查询失败模式聚合分析 |
| `trace` | 获取指定任务的完整轨迹 |

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TASK_TRACE_DIR` | `~/.openclaw/workspace/data/task-traces` | 追踪数据目录 |
| `SIGNAL_DIR` | `~/.openclaw/workspace/data/signals` | Signal 文件目录 |

## 超时阈值参考

| 任务类型 | 建议超时 | Stale 告警 |
|----------|---------|-----------|
| 简单查询 | 30s | 15s |
| 单 Agent 分析 | 5min | 3min |
| 编码任务 | 20min | 10min |
| 多步骤编排 | 30min | 15min |
| 复杂编排 | 60min | 20min |

Watch Daemon 默认: interval=60s, stale=300s(5min), timeout=600s(10min)
