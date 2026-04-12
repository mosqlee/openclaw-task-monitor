# HEARTBEAT.md 配置片段

将以下内容添加到你的 `~/.openclaw/workspace/HEARTBEAT.md` 中。

```bash
# ====================================
# Watch Daemon 保活 + Signal 处理
# ====================================

# 1. 检查 watch 守护进程是否在运行，未运行则后台拉起
WATCHER_PID_FILE=~/.openclaw/workspace/data/task-traces/watch.pid
WATCHER_BIN="python3 ~/.openclaw/workspace/skills/task-coordinator/scripts/task_tracker.py watch --interval 60 --stale-threshold 300 --timeout 600"
if [ -f "$WATCHER_PID_FILE" ]; then
  PID=$(cat "$WATCHER_PID_FILE")
  if ! ps -p "$PID" > /dev/null 2>&1; then
    $WATCHER_BIN &
  fi
else
  $WATCHER_BIN &
fi

# 2. 收集 signal 文件内容到一个汇总文件（供 AI 后续读取并发通知）
SIGNAL_DIR=~/.openclaw/workspace/data/signals
SIGNAL_SUMMARY=~/.openclaw/workspace/data/signals/_heartbeat_pending.json
python3 -c "
import json, glob, os
signals = []
for f in sorted(glob.glob('$SIGNAL_DIR/*.json')):
    try:
        signals.append(json.load(open(f)))
        os.remove(f)
    except: pass
if signals:
    with open('$SIGNAL_SUMMARY','w') as out:
        json.dump(signals, out, ensure_ascii=False, indent=2)
    print(f'COLLECTED_SIGNALS: {len(signals)}')
else:
    print('NO_SIGNALS')
"

# ====================================
# Task Watchdog - 扫描超时任务（保留作为 watch 守护的补充）
# ====================================
# watch 守护会自动标记超时，此处 watchdog 作为兜底
# 命令: python3 ~/.openclaw/workspace/skills/task-coordinator/scripts/task_tracker.py watchdog --max-age-minutes 30
```
