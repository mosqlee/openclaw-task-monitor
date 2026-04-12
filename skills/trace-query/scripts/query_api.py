#!/usr/bin/env python3
"""
Trace Query API - 任务轨迹检索接口

功能：
1. query_similar_tasks - 查询相似成功任务
2. query_failure_patterns - 查询失败模式
3. get_task_trace - 获取完整轨迹
"""

import argparse
import json
import os
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

TRACE_DIR = Path.home() / ".openclaw" / "workspace" / "data" / "task-traces"


def load_task_traces(status_filter: Optional[str] = None) -> List[Dict]:
    """加载所有任务轨迹"""
    if not TRACE_DIR.exists():
        return []
    
    tasks = []
    for task_dir in TRACE_DIR.iterdir():
        if not task_dir.is_dir():
            continue
        
        task_id = task_dir.name
        plan_file = task_dir / "task_plan.json"
        result_file = task_dir / "result.json"
        
        if not plan_file.exists():
            continue
        
        try:
            plan = json.loads(plan_file.read_text())
            result = {}
            if result_file.exists():
                result = json.loads(result_file.read_text())
            
            task_data = {
                "task_id": task_id,
                "goal": plan.get("goal", ""),
                "agent": plan.get("agent", ""),
                "steps": plan.get("steps", []),
                "status": result.get("status", plan.get("status", "unknown")),
                "result": result.get("output", ""),
                "duration_ms": result.get("duration_ms", 0),
                "created_at": plan.get("created_at", ""),
                "completed_at": result.get("completed_at", "")
            }
            
            if status_filter and task_data["status"] != status_filter:
                continue
            
            tasks.append(task_data)
        except Exception as e:
            print(f"Warning: Failed to load {task_id}: {e}")
    
    return tasks


def simple_similarity(goal1: str, goal2: str) -> float:
    """简单文本相似度（Jaccard）"""
    words1 = set(goal1.lower().split())
    words2 = set(goal2.lower().split())
    
    if not words1 or not words2:
        return 0.0
    
    intersection = words1 & words2
    union = words1 | words2
    
    return len(intersection) / len(union)


def query_similar_tasks(goal: str, k: int = 5, status: str = "completed") -> List[Dict]:
    """查询相似成功任务"""
    tasks = load_task_traces(status_filter=status)
    
    # 计算相似度
    scored_tasks = []
    for task in tasks:
        sim_score = simple_similarity(goal, task["goal"])
        scored_tasks.append((task, sim_score))
    
    # 排序取top-k
    scored_tasks.sort(key=lambda x: -x[1])
    top_tasks = [t[0] for t in scored_tasks[:k]]
    
    # 提取关键信息
    results = []
    for task in top_tasks:
        results.append({
            "task_id": task["task_id"],
            "goal": task["goal"],
            "agent": task["agent"],
            "steps": task["steps"],
            "result_summary": task["result"][:200] if task["result"] else "",
            "duration_seconds": task["duration_ms"] / 1000,
            "similarity": simple_similarity(goal, task["goal"])
        })
    
    return results


def query_failure_patterns(step_type: Optional[str] = None) -> Dict:
    """查询失败模式"""
    tasks = load_task_traces(status_filter="failed")
    
    # 聚合失败原因
    patterns = {}
    for task in tasks:
        # 提取失败步骤
        steps = task.get("steps", [])
        for step in steps:
            if step_type and step_type not in step.lower():
                continue
            
            # 简单聚合
            pattern_key = step
            if pattern_key not in patterns:
                patterns[pattern_key] = {
                    "count": 0,
                    "examples": []
                }
            
            patterns[pattern_key]["count"] += 1
            if len(patterns[pattern_key]["examples"]) < 3:
                patterns[pattern_key]["examples"].append({
                    "task_id": task["task_id"],
                    "goal": task["goal"][:100]
                })
    
    # 按频率排序
    sorted_patterns = sorted(
        patterns.items(),
        key=lambda x: -x[1]["count"]
    )
    
    return {
        "total_failed_tasks": len(tasks),
        "patterns": [
            {
                "step": p[0],
                "count": p[1]["count"],
                "examples": p[1]["examples"]
            }
            for p in sorted_patterns[:10]
        ]
    }


def get_task_trace(task_id: str) -> Optional[Dict]:
    """获取完整轨迹"""
    task_dir = TRACE_DIR / task_id
    
    if not task_dir.exists():
        return None
    
    trace = {"task_id": task_id}
    
    # 加载所有文件
    files = [
        "task_plan.json",
        "progress.json",
        "tool_calls.json",
        "prompt_snapshots.json",
        "result.json"
    ]
    
    for filename in files:
        filepath = task_dir / filename
        if filepath.exists():
            try:
                trace[filename.replace(".json", "")] = json.loads(filepath.read_text())
            except Exception as e:
                trace[filename.replace(".json", "")] = {"error": str(e)}
    
    return trace


def main():
    parser = argparse.ArgumentParser(description="Trace Query API")
    subparsers = parser.add_subparsers(dest="command", help="命令")
    
    # similar 命令
    similar_parser = subparsers.add_parser("similar", help="查询相似成功任务")
    similar_parser.add_argument("--goal", required=True, help="任务目标描述")
    similar_parser.add_argument("--k", type=int, default=5, help="返回数量")
    similar_parser.add_argument("--status", default="completed", help="状态过滤")
    
    # failures 命令
    failures_parser = subparsers.add_parser("failures", help="查询失败模式")
    failures_parser.add_argument("--step-type", help="步骤类型过滤")
    
    # trace 命令
    trace_parser = subparsers.add_parser("trace", help="获取完整轨迹")
    trace_parser.add_argument("--task-id", required=True, help="任务ID")
    
    args = parser.parse_args()
    
    if args.command == "similar":
        result = query_similar_tasks(args.goal, args.k, args.status)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    
    elif args.command == "failures":
        result = query_failure_patterns(args.step_type)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    
    elif args.command == "trace":
        result = get_task_trace(args.task_id)
        if result:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(json.dumps({"error": "Task not found"}, ensure_ascii=False))
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()