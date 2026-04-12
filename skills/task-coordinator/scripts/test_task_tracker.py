#!/usr/bin/env python3
"""
Task Tracker 单元测试

测试增强的 trace 记录功能
"""

import unittest
import subprocess
import json
import time
import os
import shutil
from pathlib import Path

# 测试数据目录
TEST_TRACE_DIR = Path(os.path.expanduser(os.environ.get("TASK_TRACE_DIR", "~/.openclaw/workspace/data/task-traces")))


class TestTaskTrackerEnhanced(unittest.TestCase):
    """测试增强功能"""

    @classmethod
    def setUpClass(cls):
        """测试前清理"""
        # 使用时间戳确保任务ID唯一
        cls.test_task_id = f"test-task-{int(time.time() * 1000)}"

    def _run_cmd(self, cmd: str) -> dict:
        """运行命令并解析JSON结果"""
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, cwd=os.path.dirname(__file__)
        )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"Invalid JSON: {result.stdout}", "stderr": result.stderr}

    def test_01_init_task(self):
        """测试初始化任务"""
        result = self._run_cmd(
            f'python3 task_tracker.py init {self.test_task_id} '
            f'"测试任务" "test-agent" --steps "step1,step2,step3"'
        )
        self.assertTrue(result.get("ok"), f"初始化失败: {result}")
        self.assertEqual(result["task_id"], self.test_task_id)

    def test_02_checkpoint(self):
        """测试检查点"""
        result = self._run_cmd(
            f'python3 task_tracker.py checkpoint {self.test_task_id} 1 completed --note "完成步骤1"'
        )
        self.assertTrue(result.get("ok"), f"检查点失败: {result}")
        self.assertTrue(result.get("matched"), "步骤未匹配")

    def test_03_tool_call(self):
        """测试工具调用记录"""
        # 第一次调用
        result = self._run_cmd(
            f'python3 task_tracker.py tool-call {self.test_task_id} "read" '
            f'--args \'{{"file": "test.py"}}\' '
            f'--result "文件内容已读取" '
            f'--context "分析阶段"'
        )
        self.assertTrue(result.get("ok"), f"工具调用记录失败: {result}")
        self.assertEqual(result.get("call_count"), 1)

        # 第二次调用
        time.sleep(0.1)
        result = self._run_cmd(
            f'python3 task_tracker.py tool-call {self.test_task_id} "write" '
            f'--args \'{{"file": "test.py", "content": "hello"}}\' '
            f'--result "文件已写入" '
            f'--context "实现阶段"'
        )
        self.assertTrue(result.get("ok"), f"工具调用记录失败: {result}")
        self.assertEqual(result.get("call_count"), 2)

    def test_04_tool_call_result_truncation(self):
        """测试结果截断"""
        long_result = "x" * 1000  # 超过500字符
        result = self._run_cmd(
            f'python3 task_tracker.py tool-call {self.test_task_id} "exec" '
            f'--args \'{{"cmd": "test"}}\' '
            f'--result "{long_result}"'
        )
        self.assertTrue(result.get("ok"), f"工具调用记录失败: {result}")

        # 读取文件验证截断
        tool_calls_file = TEST_TRACE_DIR / self.test_task_id / "tool_calls.json"
        with open(tool_calls_file) as f:
            tool_calls = json.load(f)

        last_call = tool_calls["calls"][-1]
        self.assertIn("truncated", last_call["result"])
        self.assertLessEqual(len(last_call["result"]), 600)  # 500 + 后缀

    def test_05_prompt_snapshot(self):
        """测试Prompt快照"""
        prompt = "这是一个测试Prompt，用于验证快照功能"
        metadata = json.dumps({"iteration": 1, "model": "test-model"})

        result = self._run_cmd(
            f'python3 task_tracker.py prompt-snapshot {self.test_task_id} '
            f'--prompt \'{prompt}\' '
            f'--metadata \'{metadata}\''
        )
        self.assertTrue(result.get("ok"), f"Prompt快照失败: {result}")
        self.assertEqual(result.get("snapshot_count"), 1)

        # 验证存储
        snapshots_file = TEST_TRACE_DIR / self.test_task_id / "prompt_snapshots.json"
        with open(snapshots_file) as f:
            snapshots = json.load(f)

        self.assertEqual(len(snapshots["snapshots"]), 1)
        self.assertEqual(snapshots["snapshots"][0]["prompt_content"], prompt)
        self.assertEqual(snapshots["snapshots"][0]["metadata"]["iteration"], 1)

    def test_06_trace_summary(self):
        """测试追踪摘要"""
        result = self._run_cmd(
            f'python3 task_tracker.py trace-summary {self.test_task_id}'
        )
        self.assertTrue(result.get("ok"), f"追踪摘要失败: {result}")
        self.assertEqual(result["task_id"], self.test_task_id)
        self.assertGreaterEqual(result["tool_calls_count"], 3)  # 至少3次工具调用
        self.assertGreaterEqual(result["prompt_snapshots_count"], 1)  # 至少1个快照
        self.assertIn("recent_tool_calls", result)

    def test_07_status(self):
        """测试状态查询"""
        result = self._run_cmd(
            f'python3 task_tracker.py status {self.test_task_id}'
        )
        self.assertTrue(result.get("ok"), f"状态查询失败: {result}")
        self.assertEqual(result["status"], "running")

    def test_08_complete_task(self):
        """测试完成任务"""
        result = self._run_cmd(
            f'python3 task_tracker.py complete {self.test_task_id} '
            f'--output "测试完成" --duration 5000'
        )
        self.assertTrue(result.get("ok"), f"完成任务失败: {result}")
        self.assertEqual(result["status"], "completed")

    def test_09_final_summary(self):
        """测试最终摘要"""
        result = self._run_cmd(
            f'python3 task_tracker.py trace-summary {self.test_task_id}'
        )
        self.assertTrue(result.get("ok"), f"追踪摘要失败: {result}")
        self.assertEqual(result["status"], "completed")
        self.assertIsNotNone(result["result"])

    def test_10_cleanup(self):
        """测试清理功能"""
        # 使用单独的任务测试清理
        cleanup_task_id = f"cleanup-test-{int(time.time() * 1000)}"
        self._run_cmd(
            f'python3 task_tracker.py init {cleanup_task_id} '
            f'"清理测试" "test-agent"'
        )
        self._run_cmd(
            f'python3 task_tracker.py complete {cleanup_task_id} '
            f'--output "完成"'
        )

        # 验证目录存在
        task_dir = TEST_TRACE_DIR / cleanup_task_id
        self.assertTrue(task_dir.exists(), "任务目录不存在")

        # 清理0小时前的任务（立即清理已完成的）
        result = self._run_cmd(
            'python3 task_tracker.py cleanup --max-age-hours 0'
        )
        self.assertTrue(result.get("ok"), f"清理失败: {result}")


class TestTaskTrackerBasic(unittest.TestCase):
    """测试基础功能"""

    def test_list_empty(self):
        """测试列出任务"""
        result = subprocess.run(
            'python3 task_tracker.py list',
            shell=True, capture_output=True, text=True, cwd=os.path.dirname(__file__)
        )
        # 应该返回有效的JSON
        try:
            data = json.loads(result.stdout)
            self.assertTrue(data.get("ok"))
            self.assertIn("tasks", data)
        except json.JSONDecodeError:
            self.fail(f"无效的JSON输出: {result.stdout}")

    def test_status_not_found(self):
        """测试查询不存在的任务"""
        result = subprocess.run(
            'python3 task_tracker.py status nonexistent-task-id',
            shell=True, capture_output=True, text=True, cwd=os.path.dirname(__file__)
        )
        # 应该返回错误
        try:
            data = json.loads(result.stdout)
            self.assertFalse(data.get("ok"))
        except json.JSONDecodeError:
            # 或者返回非零退出码
            pass


if __name__ == "__main__":
    # 运行测试
    unittest.main(verbosity=2)