---
name: trace-query
description: 任务轨迹检索接口 - 查询相似成功任务、失败模式分析。触发词：查询相似任务、失败模式、任务轨迹。
version: 1.0.0
last_updated: 2026-04-03
---

# Trace Query - 任务轨迹检索

查询历史任务轨迹，支持相似任务匹配和失败模式分析。

## 使用方法

```bash
python3 skills/trace-query/scripts/query_api.py <command> [options]
```

### 查询相似成功任务

```bash
python3 skills/trace-query/scripts/query_api.py similar \
  --goal "实现用户认证模块" \
  --k 5 \
  --status completed
```

返回最相似的k个成功任务轨迹。

### 查询失败模式

```bash
python3 skills/trace-query/scripts/query_api.py failures \
  --step-type implementation
```

返回特定步骤的常见失败模式聚合分析。

### 获取任务完整轨迹

```bash
python3 skills/trace-query/scripts/query_api.py trace \
  --task-id task-20260403-xxxx
```

返回指定任务的完整轨迹详情。

## 数据来源

从 `TASK_TRACE_DIR` 环境变量配置的目录读取（默认 `~/.openclaw/workspace/data/task-traces/`）：
- task_plan.json - 任务元数据
- progress.json - 执行事件日志
- tool_calls.json - 工具调用记录
- prompt_snapshots.json - Prompt快照
- result.json - 最终结果

## 输出格式

- `similar`: JSON数组，每个元素包含task_id、goal、steps、result摘要
- `failures`: JSON对象，包含patterns数组（失败原因聚合）
- `trace`: JSON对象，完整轨迹详情

## 集成建议

opencode启动时可调用：
```python
similar_tasks = query_similar(goal="新任务目标", k=3)
failures = query_failures(step_type="implementation")
context = f"参考相似成功案例：{similar_tasks}\n避免失败模式：{failures}"
```