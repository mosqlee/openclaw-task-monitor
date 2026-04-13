#!/usr/bin/env bash
# OpenClaw Task Monitor - 一键初始化脚本
# 用法: bash setup.sh [workspace_path]
set -euo pipefail

WORKSPACE="${1:-$HOME/.openclaw/workspace}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "🔧 OpenClaw Task Monitor 安装向导"
echo "================================"
echo "目标 workspace: $WORKSPACE"
echo ""

# 检查依赖
echo "📋 检查依赖..."
for cmd in python3 git gh node; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "❌ 缺少依赖: $cmd"
        echo "请先安装 $cmd"
        exit 1
    fi
done
echo "✅ 依赖检查通过"

# 检查 OpenClaw 是否在运行
OPENCLAW_DIR="${WORKSPACE}/../.."
if [ ! -d "$OPENCLAW_DIR/extensions" ]; then
    OPENCLAW_DIR="$HOME/.openclaw"
fi

# 创建目录结构
echo ""
echo "📁 创建目录结构..."
mkdir -p "$WORKSPACE/skills/task-coordinator/scripts"
mkdir -p "$WORKSPACE/skills/trace-query/scripts"
mkdir -p "$WORKSPACE/data/task-traces"
mkdir -p "$WORKSPACE/data/signals"
mkdir -p "$OPENCLAW_DIR/extensions/progress-monitor"
echo "✅ 目录结构创建完成"

# 安装 progress-monitor 插件
echo ""
echo "🔌 安装 progress-monitor 插件..."
cp "$PROJECT_DIR/plugin/openclaw.plugin.json" "$OPENCLAW_DIR/extensions/progress-monitor/"
cp "$PROJECT_DIR/plugin/index.js" "$OPENCLAW_DIR/extensions/progress-monitor/"

# 确保 openclaw.json 的 plugins.allow 包含 progress-monitor
OPENCLAW_CONFIG="$OPENCLAW_DIR/openclaw.json"
if [ -f "$OPENCLAW_CONFIG" ]; then
    # 使用 node 来安全地修改 JSON
    node -e "
const fs = require('fs');
const cfg = JSON.parse(fs.readFileSync('$OPENCLAW_CONFIG', 'utf8'));
const allow = (cfg.plugins && cfg.plugins.allow) || [];
if (!allow.includes('progress-monitor')) {
    allow.push('progress-monitor');
    cfg.plugins = cfg.plugins || {};
    cfg.plugins.allow = allow;
    fs.writeFileSync('$OPENCLAW_CONFIG', JSON.stringify(cfg, null, 2));
    console.log('Added progress-monitor to plugins.allow');
} else {
    console.log('progress-monitor already in plugins.allow');
}
" 2>/dev/null || echo "⚠️  无法自动修改 openclaw.json，请手动添加 \"progress-monitor\" 到 plugins.allow"
else
    echo "⚠️  openclaw.json 不存在，请手动配置 plugins.allow 包含 progress-monitor"
fi
echo "✅ progress-monitor 插件安装完成"

# 复制文件
echo ""
echo "📋 复制 Skill 文件..."

# task-coordinator
cp "$PROJECT_DIR/skills/task-coordinator/SKILL.md" "$WORKSPACE/skills/task-coordinator/"
cp "$PROJECT_DIR/skills/task-coordinator/package.json" "$WORKSPACE/skills/task-coordinator/"
cp "$PROJECT_DIR/skills/task-coordinator/.env.example" "$WORKSPACE/skills/task-coordinator/"
cp "$PROJECT_DIR/skills/task-coordinator/scripts/task_tracker.py" "$WORKSPACE/skills/task-coordinator/scripts/"
cp "$PROJECT_DIR/skills/task-coordinator/scripts/test_task_tracker.py" "$WORKSPACE/skills/task-coordinator/scripts/"

# trace-query
cp "$PROJECT_DIR/skills/trace-query/SKILL.md" "$WORKSPACE/skills/trace-query/"
cp "$PROJECT_DIR/skills/trace-query/package.json" "$WORKSPACE/skills/trace-query/"
cp "$PROJECT_DIR/skills/trace-query/.env.example" "$WORKSPACE/skills/trace-query/"
cp "$PROJECT_DIR/skills/trace-query/scripts/query_api.py" "$WORKSPACE/skills/trace-query/scripts/"

echo "✅ Skill 文件复制完成"

# 配置 HEARTBEAT.md
HEARTBEAT_FILE="$WORKSPACE/HEARTBEAT.md"
if [ -f "$HEARTBEAT_FILE" ]; then
    # 检查是否已包含 Watch Daemon 配置
    if ! grep -q "watch.pid" "$HEARTBEAT_FILE"; then
        echo ""
        echo "📝 追加 Watch Daemon 配置到 HEARTBEAT.md..."
        cat >> "$HEARTBEAT_FILE" <<'HEREDOC'

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
HEREDOC
        echo "✅ HEARTBEAT.md 配置已追加"
    else
        echo "⚠️  HEARTBEAT.md 已包含 Watch Daemon 配置，跳过"
    fi
else
    echo "⚠️  HEARTBEAT.md 不存在，请手动创建并参考 config/HEARTBEAT_SNIPPET.md"
fi

# 验证安装
echo ""
echo "🧪 验证安装..."
if python3 "$WORKSPACE/skills/task-coordinator/scripts/task_tracker.py" list > /dev/null 2>&1; then
    echo "✅ task_tracker.py 可执行"
else
    echo "❌ task_tracker.py 验证失败"
fi

if python3 "$WORKSPACE/skills/trace-query/scripts/query_api.py" --help > /dev/null 2>&1; then
    echo "✅ query_api.py 可执行"
else
    echo "❌ query_api.py 验证失败"
fi

# 安装 ClawTeam Bridge
echo ""
echo "🌉 安装 ClawTeam ↔ Task Monitor Bridge..."
cp "$PROJECT_DIR/scripts/clawteam-bridge.sh" "$WORKSPACE/scripts/clawteam-bridge.sh"
chmod +x "$WORKSPACE/scripts/clawteam-bridge.sh"
echo "✅ ClawTeam Bridge 已安装到 $WORKSPACE/scripts/"

echo ""
echo "🎉 安装完成！"
echo ""
echo "📌 后续操作："
echo "  1. 重启 OpenClaw Gateway 以加载 progress-monitor 插件："
echo "     openclaw gateway restart"
echo "  2. 在 AGENTS.md 中添加 spawn subagent checklist（参考 config/AGENTS_SNIPPET.md）"
echo "  3. 如需飞书通知，Watch Daemon 会自动从 USER.md 读取 open_id"
echo "  4. Watch Daemon 阈值可在 HEARTBEAT.md 的 WATCHER_BIN 中调整："
echo "     --interval 60      扫描间隔（秒）"
echo "     --stale-threshold 300  停滞告警阈值（秒）"
echo "     --timeout 600      超时阈值（秒）"
echo ""
echo "📖 运行测试："
echo "  cd $WORKSPACE/skills/task-coordinator/scripts && python3 -m pytest test_task_tracker.py -v"
echo ""
echo "🌉 ClawTeam 集成（可选）："
echo "  Bridge 脚本已安装到: $WORKSPACE/scripts/clawteam-bridge.sh"
echo "  在 clawteam-run.sh 的 GATE 回调中调用 bridge 即可打通追踪："
echo ""
echo "    # launch 前:"
echo "    bash $WORKSPACE/scripts/clawteam-bridge.sh init <team-name> '<goal>'"
echo ""
echo "    # 每个 GATE 完成时:"
echo "    bash $WORKSPACE/scripts/clawteam-bridge.sh gate <team-name> <gate_id> completed '<note>'"
echo ""
echo "    # Worker 实时通知:"
echo "    bash $WORKSPACE/scripts/clawteam-bridge.sh notify <team-name> <agent> '<content>'"
echo ""
echo "    # 团队完成/失败:"
echo "    bash $WORKSPACE/scripts/clawteam-bridge.sh complete <team-name> '<output>'"
echo "    bash $WORKSPACE/scripts/clawteam-bridge.sh fail <team-name> '<reason>'"
echo ""
echo "  详细文档见: docs/clawteam-integration.md"
