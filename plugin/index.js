// progress-monitor — 监控 subagent + exec 长任务进度，停滞时通知用户
const path = require("path");
const fs = require("fs");
const os = require("os");
const { execSync } = require("child_process");

const TRACE_FILE = path.join(os.homedir(), ".openclaw/workspace/data/task-traces/plugin-tracked.json");
const DEBUG_FILE = path.join(os.homedir(), ".openclaw/workspace/data/task-traces/plugin-debug.log");

const tasks = new Map();
let runtime, cfg;

function expandDir(dir) {
  return dir.startsWith("~") ? dir.replace("~", os.homedir()) : dir;
}

function loadTrace() {
  try { return JSON.parse(fs.readFileSync(TRACE_FILE, "utf-8")); } catch { return {}; }
}

function saveTrace(data) {
  try {
    fs.mkdirSync(path.dirname(TRACE_FILE), { recursive: true });
    fs.writeFileSync(TRACE_FILE, JSON.stringify(data, null, 2));
  } catch {}
}

function debugLog(msg) {
  try {
    fs.mkdirSync(path.dirname(DEBUG_FILE), { recursive: true });
    fs.appendFileSync(DEBUG_FILE, `[${new Date().toISOString()}] ${msg}\n`);
  } catch {}
}

/**
 * 从 requesterSessionKey 解析通知目标
 * 格式: agent:{agent_id}:{channel}:{type}:{target}
 *   agent:main:feishu:direct:ou_xxx  → user:ou_xxx
 *   agent:main:feishu:group:oc_xxx   → chat:oc_xxx
 *   agent:main:web:xxx               → "" (无法解析)
 */
function resolveNotifyTarget(requesterSessionKey) {
  if (!requesterSessionKey) return "";
  const parts = requesterSessionKey.split(":");
  try {
    if (parts.length >= 5 && parts[2] === "feishu") {
      const targetType = parts[3]; // direct / group
      const targetId = parts[4];   // ou_xxx / oc_xxx
      if (targetType === "direct" && targetId.startsWith("ou_")) {
        return `user:${targetId}`;
      }
      if (targetType === "group" && targetId.startsWith("oc_")) {
        return `chat:${targetId}`;
      }
    }
  } catch {}
  return "";
}

/**
 * 获取通知目标（优先级: requesterSessionKey 解析 > userOpenId 配置）
 */
function getNotifyTarget(task) {
  // 1. 从 requesterSessionKey 解析
  const fromRequester = resolveNotifyTarget(task.requesterSessionKey);
  if (fromRequester) {
    debugLog(`NOTIFY_RESOLVED: ${task.childSessionKey} from requester -> ${fromRequester}`);
    return fromRequester;
  }
  // 2. 使用插件配置的默认 userOpenId
  const defaultUser = cfg?.userOpenId || "";
  if (defaultUser) {
    debugLog(`NOTIFY_FALLBACK: ${task.childSessionKey} using userOpenId=${defaultUser}`);
    return `user:${defaultUser}`;
  }
  debugLog(`NOTIFY_NONE: ${task.childSessionKey} no target resolved`);
  return "";
}

function sendFeishuNotification(target, emoji, title, message) {
  if (!target) return;
  try {
    const fullMsg = `${emoji} ${title}: ${message}`;
    const result = execSync(
      `openclaw message send --channel feishu --target "${target}" --message '${fullMsg.replace(/'/g, "'\"'\"'")}'`,
      { timeout: 10000, encoding: "utf-8", stdio: ["pipe", "pipe", "pipe"] }
    );
    debugLog(`FEISHU_SENT: to=${target} exit=0 stdout=${result.trim().slice(0, 200)}`);
  } catch (e) {
    debugLog(`FEISHU_FAILED: to=${target} error=${e.message?.slice(0, 200)}`);
  }
}

function staleNotify(task, elapsed, signalDir) {
  const dir = expandDir(signalDir);
  try {
    fs.mkdirSync(dir, { recursive: true });
    const signal = {
      childSessionKey: task.childSessionKey,
      type: task.type === "exec" ? "exec_stale" : "stale",
      task_type: task.type,
      elapsed_min: Math.round(elapsed / 60000),
      agentId: task.agentId,
      label: task.label,
      command: task.command ?? undefined,
      requesterSessionKey: task.requesterSessionKey,
      timestamp: Date.now(),
    };
    const filename = task.type === "exec"
      ? `exec-stale-${task.sessionKey}-${Date.now()}.json`
      : `stale-${task.childSessionKey}-${Date.now()}.json`;
    fs.writeFileSync(path.join(dir, filename), JSON.stringify(signal));
    debugLog(`SIGNAL: ${filename} (elapsed ${Math.round(elapsed/60000)}min)`);

    // 直接发飞书通知
    const notifyTarget = getNotifyTarget(task);
    const label = task.label || task.command?.slice(0, 40) || task.childSessionKey;
    sendFeishuNotification(
      notifyTarget,
      "⏰",
      "任务停滞",
      `${label}\n已运行 ${Math.round(elapsed / 60000)} 分钟无进展`
    );
  } catch {}
}

function startTimer(key, timeoutMs, signalDir) {
  const task = tasks.get(key);
  if (!task || task.timer) return;
  debugLog(`TIMER_START: ${key} timeout=${timeoutMs}ms`);
  task.timer = setTimeout(() => {
    const elapsed = Date.now() - task.startTime;
    debugLog(`TIMER_FIRE: ${key} elapsed=${Math.round(elapsed/1000)}s`);
    staleNotify(task, elapsed, signalDir);
    try { runtime?.system?.requestHeartbeatNow?.(); } catch {}
  }, timeoutMs).unref();
}

function resetTimer(key, timeoutMs, signalDir) {
  const task = tasks.get(key);
  if (!task) return;
  clearTimeout(task.timer);
  task.timer = null;
  startTimer(key, timeoutMs, signalDir);
}

// 从 before_tool_call event 提取 exec 命令
function extractExecCommand(event) {
  try {
    const params = event?.params ?? {};
    const cmd = params?.command ?? params?.cmd ?? "";
    return cmd.length > 120 ? cmd.slice(0, 120) + "..." : cmd;
  } catch { return ""; }
}

// 从 before_tool_call params 检测是否为后台 exec
function isBackgroundExec(event) {
  try {
    const params = event?.params ?? {};
    return params?.background === true || params?.background === "true";
  } catch { return false; }
}

module.exports = {
  register(api) {
    if (!api.on) return;

    runtime = api.runtime;
    cfg = api.pluginConfig ?? {};
    const timeoutMs = cfg.staleTimeoutMs ?? 300000;
    const signalDir = cfg.signalDir ?? "~/.openclaw/workspace/data/signals";
    const execTimeoutMs = cfg.execStaleTimeoutMs ?? timeoutMs;

    debugLog(`PLUGIN_LOADED: progress-monitor v1.2 (with direct feishu notification, default user=${cfg?.userOpenId || "none"})`);

    // gateway_start: 恢复未完成任务
    api.on("gateway_start", (event, ctx) => {
      debugLog("GATEWAY_START");
      try {
        const trace = loadTrace();
        for (const [key, t] of Object.entries(trace)) {
          if (t.endedAt) continue;
          tasks.set(key, { ...t, timer: null, startTime: t.startTime ?? Date.now() });
          startTimer(key, timeoutMs, signalDir);
        }
      } catch {}
    });

    // subagent_spawned
    api.on("subagent_spawned", (event, ctx) => {
      try {
        const key = event.childSessionKey;
        const task = {
          type: "subagent",
          childSessionKey: key,
          agentId: event.agentId,
          label: event.label,
          runId: event.runId,
          requesterSessionKey: ctx?.requesterSessionKey,
          startTime: Date.now(),
          timer: null,
        };
        tasks.set(key, task);
        startTimer(key, timeoutMs, signalDir);
        debugLog(`SUBAGENT_SPAWNED: ${key} label=${task.label} requester=${task.requesterSessionKey}`);
        const trace = loadTrace();
        trace[key] = { ...task, timer: undefined };
        saveTrace(trace);

        // Auto-register with task-coordinator (task_tracker.py init)
        try {
          const trackerScript = path.join(
            os.homedir(), ".openclaw/workspace/skills/task-coordinator/scripts/task_tracker.py"
          );
          const taskId = key.replace(/[^a-zA-Z0-9._-]/g, "_");
          const goal = task.label || `Subagent: ${event.agentId}`;
          const agent = event.agentId || "main";
          const requester = task.requesterSessionKey || "";
          const initCmd = [
            "python3", trackerScript, "init", taskId, goal, agent,
            "--requester", requester,
          ];
          const initResult = execSync(initCmd.map(c => `'${c}'`).join(" "), {
            timeout: 5000, encoding: "utf-8", stdio: ["pipe", "pipe", "pipe"],
          });
          debugLog(`TASK_TRACKER_INIT: ${taskId} result=${initResult.trim()}`);
        } catch (e) {
          debugLog(`TASK_TRACKER_INIT_FAIL: ${key} error=${e.message?.slice(0, 200)}`);
        }
      } catch {}
    });

    // agent_end: 重置计时器
    api.on("agent_end", (event, ctx) => {
      try {
        for (const [key, task] of tasks) {
          if (task.requesterSessionKey === ctx?.sessionKey) {
            resetTimer(key, timeoutMs, signalDir);
          }
        }
      } catch {}
    });

    // subagent_ended: 清理 + 通知 task-coordinator
    api.on("subagent_ended", (event, ctx) => {
      try {
        const key = event.targetSessionKey;
        const task = tasks.get(key);
        clearTimeout(task?.timer);
        tasks.delete(key);
        debugLog(`SUBAGENT_ENDED: ${key}`);
        const trace = loadTrace();
        if (trace[key]) {
          trace[key].endedAt = event.endedAt ?? Date.now();
          trace[key].outcome = event.outcome;
          saveTrace(trace);
        }

        // Notify task-coordinator (complete/fail)
        try {
          const trackerScript = path.join(
            os.homedir(), ".openclaw/workspace/skills/task-coordinator/scripts/task_tracker.py"
          );
          const taskId = key.replace(/[^a-zA-Z0-9._-]/g, "_");
          const duration = task ? Date.now() - task.startTime : 0;
          const outcome = event.outcome || "completed";
          const isFail = outcome.includes("fail") || outcome.includes("error") || outcome.includes("timeout");
          const cmd = isFail
            ? `python3 '${trackerScript}' fail '${taskId}' 'Subagent ended: ${outcome}' --duration ${duration}`
            : `python3 '${trackerScript}' complete '${taskId}' --output 'Subagent ended: ${outcome}' --duration ${duration}`;
          const result = execSync(cmd, {
            timeout: 5000, encoding: "utf-8", stdio: ["pipe", "pipe", "pipe"],
          });
          debugLog(`TASK_TRACKER_${isFail ? "FAIL" : "COMPLETE"}: ${taskId} duration=${duration}ms result=${result.trim()}`);
        } catch (e) {
          debugLog(`TASK_TRACKER_END_FAIL: ${key} error=${e.message?.slice(0, 200)}`);
        }
      } catch {}
    });

    // ========== Exec 长任务监控 ==========
    api.on("before_tool_call", (event, ctx) => {
      try {
        if (event?.toolName !== "exec") return;
        const command = extractExecCommand(event);
        if (!command || command.length < 10) return;
        if (!isBackgroundExec(event)) {
          debugLog(`EXEC_SKIP_FOREGROUND: ${command.slice(0, 50)}`);
          return;
        }
        const sessionKey = ctx?.sessionKey ?? "unknown";
        const key = `exec:${sessionKey}:${Date.now()}`;
        const task = {
          type: "exec",
          key,
          sessionKey,
          childSessionKey: key,
          agentId: ctx?.agentId,
          label: `exec: ${command.slice(0, 40)}`,
          runId: ctx?.runId,
          requesterSessionKey: sessionKey,
          startTime: Date.now(),
          timer: null,
          command,
        };
        tasks.set(key, task);
        debugLog(`EXEC_BEFORE: ${key} cmd=${command.slice(0, 50)} requester=${sessionKey}`);
        const trace = loadTrace();
        trace[key] = { ...task, timer: undefined, status: "pending" };
        saveTrace(trace);
      } catch {}
    });

    // after_tool_call: 确认后台 exec 启动，开始计时
    api.on("after_tool_call", (event, ctx) => {
      try {
        if (event?.toolName !== "exec") return;
        const sessionKey = ctx?.sessionKey ?? "unknown";
        let foundKey = null;
        for (const [k, t] of tasks) {
          if (t.type === "exec" && t.sessionKey === sessionKey && t.status !== "completed") {
            foundKey = k;
            break;
          }
        }
        if (!foundKey) {
          debugLog(`EXEC_AFTER_NO_TASK: session=${sessionKey}`);
          return;
        }
        const task = tasks.get(foundKey);
        startTimer(foundKey, execTimeoutMs, signalDir);
        task.status = "running";
        debugLog(`EXEC_AFTER: ${foundKey} status=running, timer started (${execTimeoutMs}ms)`);
        const trace = loadTrace();
        if (trace[foundKey]) {
          trace[foundKey].status = "running";
          trace[foundKey].timerStartedAt = Date.now();
          saveTrace(trace);
        }
      } catch {}
    });
  },
};
