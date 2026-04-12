// progress-monitor — 监控 subagent + exec 长任务进度，停滞时通知用户
const path = require("path");
const fs = require("fs");
const os = require("os");

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

// 从 after_tool_call event 检测是否为后台 exec（params 中有 background=true）
function isBackgroundExecAfter(event) {
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

    debugLog("PLUGIN_LOADED: progress-monitor v1.1 (with exec support)");

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
        debugLog(`SUBAGENT_SPAWNED: ${key} label=${task.label}`);
        const trace = loadTrace();
        trace[key] = { ...task, timer: undefined };
        saveTrace(trace);
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

    // subagent_ended: 清理
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
      } catch {}
    });

    // ========== Exec 长任务监控 ==========
    // before_tool_call: 检测后台 exec 调用
    api.on("before_tool_call", (event, ctx) => {
      try {
        if (event?.toolName !== "exec") return;
        const command = extractExecCommand(event);
        if (!command || command.length < 10) return;
        // 只监控后台 exec（前台 exec 是同步的，不存在停滞问题）
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
        debugLog(`EXEC_BEFORE: ${key} cmd=${command.slice(0, 50)}`);
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
        // 找到对应的 exec task
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

        // after_tool_call 触发说明 exec 已经启动了，开始计时
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
