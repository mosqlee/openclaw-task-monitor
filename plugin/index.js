// progress-monitor v1.3 — 监控 subagent + exec + Claude Code 进度，停滞时通知
// v1.3: 异步通知、通知上限、自动清理结束任务、更宽松的频率
const path = require("path");
const fs = require("fs");
const os = require("os");
const { exec, execSync } = require("child_process");

const TRACE_FILE = path.join(os.homedir(), ".openclaw/workspace/data/task-traces/plugin-tracked.json");
const DEBUG_FILE = path.join(os.homedir(), ".openclaw/workspace/data/task-traces/plugin-debug.log");

const tasks = new Map();
let runtime, cfg;

// 通知去重：防止并发重复发送
const notifyingSet = new Set();
const NOTIFY_DEDUP_TTL = 10000; // 10秒内同key不重复发送

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

function resolveNotifyTarget(requesterSessionKey) {
  if (!requesterSessionKey) return "";
  const parts = requesterSessionKey.split(":");
  try {
    if (parts.length >= 5 && parts[2] === "feishu") {
      const targetType = parts[3];
      const targetId = parts[4];
      if (targetType === "direct" && targetId.startsWith("ou_")) return `user:${targetId}`;
      if (targetType === "group" && targetId.startsWith("oc_")) return `chat:${targetId}`;
    }
  } catch {}
  return "";
}

function getNotifyTarget(task) {
  const fromRequester = resolveNotifyTarget(task.requesterSessionKey);
  if (fromRequester) return fromRequester;
  const defaultUser = cfg?.userOpenId || "";
  if (defaultUser) return `user:${defaultUser}`;
  return "";
}

// 异步发送飞书通知（不阻塞主线程）
function sendFeishuNotification(target, emoji, title, message, dedupKey) {
  if (!target) return;
  // 去重检查
  if (dedupKey && notifyingSet.has(dedupKey)) return;
  if (dedupKey) {
    notifyingSet.add(dedupKey);
    setTimeout(() => notifyingSet.delete(dedupKey), NOTIFY_DEDUP_TTL);
  }
  const fullMsg = `${emoji} ${title}: ${message}`;
  const escaped = fullMsg.replace(/'/g, "'\"'\"'");
  exec(
    `openclaw message send --channel feishu --target "${target}" --message '${escaped}'`,
    { timeout: 10000, encoding: "utf-8" },
    (err, stdout, stderr) => {
      if (err) {
        debugLog(`FEISHU_FAILED: to=${target} error=${err.message?.slice(0, 200)}`);
      } else {
        debugLog(`FEISHU_SENT: to=${target} stdout=${(stdout || "").trim().slice(0, 200)}`);
      }
    }
  );
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
    debugLog(`SIGNAL: ${filename} (elapsed ${Math.round(elapsed / 60000)}min)`);

    const notifyTarget = getNotifyTarget(task);
    const label = task.label || task.command?.slice(0, 40) || task.childSessionKey;
    sendFeishuNotification(
      notifyTarget, "⏰", "任务停滞",
      `${label}\n已运行 ${Math.round(elapsed / 60000)} 分钟无进展`,
      `stale:${task.childSessionKey}`
    );
  } catch {}
}

function startTimer(key, timeoutMs, signalDir) {
  const task = tasks.get(key);
  if (!task || task.timer) return;
  debugLog(`TIMER_START: ${key} timeout=${timeoutMs}ms`);
  task.timer = setTimeout(() => {
    const elapsed = Date.now() - task.startTime;
    debugLog(`TIMER_FIRE: ${key} elapsed=${Math.round(elapsed / 1000)}s`);

    // 检查通知上限
    task.notifyCount = (task.notifyCount || 0) + 1;
    if (task.notifyCount >= (cfg.maxStaleNotifyCount || 10)) {
      debugLog(`TIMER_MAX_NOTIFY: ${key} reached limit ${task.notifyCount}`);
      const notifyTarget = getNotifyTarget(task);
      const label = task.label || task.childSessionKey;
      sendFeishuNotification(
        notifyTarget, "🗑️", "任务通知已达到上限",
        `${label}\n已发送 ${task.notifyCount} 次停滞通知，自动清理`,
        `max-notify:${key}`
      );
      cleanupTask(key);
      return;
    }

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

function cleanupTask(key) {
  const task = tasks.get(key);
  if (!task) return;
  clearTimeout(task.timer);
  tasks.delete(key);
  debugLog(`CLEANUP: ${key}`);
  try {
    const trace = loadTrace();
    if (trace[key]) {
      trace[key].endedAt = Date.now();
      trace[key].outcome = "auto_cleaned";
      saveTrace(trace);
    }
  } catch {}
}

function extractExecCommand(event) {
  try {
    const params = event?.params ?? {};
    const cmd = params?.command ?? params?.cmd ?? "";
    return cmd.length > 120 ? cmd.slice(0, 120) + "..." : cmd;
  } catch { return ""; }
}

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
    const timeoutMs = cfg.staleTimeoutMs ?? 300000; // 5分钟
    const signalDir = cfg.signalDir ?? "~/.openclaw/workspace/data/signals";
    const execTimeoutMs = cfg.execStaleTimeoutMs ?? timeoutMs;

    debugLog(`PLUGIN_LOADED: progress-monitor v1.3 (async notify, max_count=${cfg.maxStaleNotifyCount || 10}, default user=${cfg?.userOpenId || "none"})`);

    // gateway_start: 恢复未完成任务（带过期清理）
    api.on("gateway_start", (event, ctx) => {
      debugLog("GATEWAY_START");
      try {
        const trace = loadTrace();
        const maxAge = timeoutMs * 3; // 超过3倍超时时间的任务直接丢弃
        const now = Date.now();
        let cleaned = 0;
        for (const [key, t] of Object.entries(trace)) {
          if (t.endedAt) continue;
          const age = now - (t.startTime ?? now);
          if (age > maxAge) {
            trace[key].endedAt = now;
            trace[key].outcome = "expired_on_restart";
            cleaned++;
            debugLog(`GATEWAY_EXPIRED: ${key} age=${Math.round(age / 60000)}min > maxAge=${Math.round(maxAge / 60000)}min`);
            continue;
          }
          tasks.set(key, { ...t, timer: null, startTime: t.startTime ?? now });
          startTimer(key, timeoutMs, signalDir);
        }
        if (cleaned > 0) saveTrace(trace);
        debugLog(`GATEWAY_RESTORED: ${tasks.size} tasks, expired ${cleaned}`);
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
          notifyCount: 0,
        };
        tasks.set(key, task);
        startTimer(key, timeoutMs, signalDir);
        debugLog(`SUBAGENT_SPAWNED: ${key} label=${task.label}`);
        const trace = loadTrace();
        trace[key] = { ...task, timer: undefined };
        saveTrace(trace);

        // Auto-register with task-coordinator
        try {
          const trackerScript = path.join(
            os.homedir(), ".openclaw/workspace/skills/task-coordinator/scripts/task_tracker.py"
          );
          const taskId = key.replace(/[^a-zA-Z0-9._-]/g, "_");
          const goal = task.label || `Subagent: ${event.agentId}`;
          const agent = event.agentId || "main";
          const requester = task.requesterSessionKey || "";
          execSync(
            ["python3", trackerScript, "init", taskId, goal, agent, "--requester", requester].map(c => `'${c}'`).join(" "),
            { timeout: 5000, encoding: "utf-8", stdio: ["pipe", "pipe", "pipe"] }
          );
          debugLog(`TASK_TRACKER_INIT: ${taskId}`);
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
        cleanupTask(key);
        const trace = loadTrace();
        if (trace[key]) {
          trace[key].endedAt = event.endedAt ?? Date.now();
          trace[key].outcome = event.outcome;
          saveTrace(trace);
        }

        try {
          const trackerScript = path.join(
            os.homedir(), ".openclaw/workspace/skills/task-coordinator/scripts/task_tracker.py"
          );
          const taskId = key.replace(/[^a-zA-Z0-9._-]/g, "_");
          const outcome = event.outcome || "completed";
          const isFail = outcome.includes("fail") || outcome.includes("error") || outcome.includes("timeout");
          const cmd = isFail
            ? `python3 '${trackerScript}' fail '${taskId}' 'Subagent ended: ${outcome}'`
            : `python3 '${trackerScript}' complete '${taskId}' --output 'Subagent ended: ${outcome}'`;
          execSync(cmd, { timeout: 5000, encoding: "utf-8", stdio: ["pipe", "pipe", "pipe"] });
          debugLog(`TASK_TRACKER_${isFail ? "FAIL" : "COMPLETE"}: ${taskId}`);
        } catch (e) {
          debugLog(`TASK_TRACKER_END_FAIL: ${key} error=${e.message?.slice(0, 200)}`);
        }
      } catch {}
    });

    // ========== Claude Code 进度监控（改进版） ==========
    const claudeProgressDir = expandDir(cfg.claudeProgressDir ?? "~/.openclaw/workspace/data/claude-progress");
    const claudeStaleTimeoutMs = cfg.claudeStaleTimeoutMs ?? 600000; // 10分钟
    const claudeNotifyIntervalMs = cfg.claudeNotifyIntervalMs ?? 300000; // 5分钟
    const CLAUDE_MAX_NOTIFY = cfg.claudeMaxNotifyCount || 10;
    const CLAUDE_FILE_MAX_AGE = 1800000; // 30分钟无更新自动清理
    let claudeSessions = new Map(); // sessionId -> { lastNotify, notifyCount, lastFileUpdate }
    let claudeFsWatcher = null;

    function isClaudeEnded(latest) {
      // Claude hooks 写入 session_end 表示会话结束
      if (latest.event === "session_end" || latest.event === "session_end_error") return true;
      // 没有活跃工具且没有活跃事件
      if (!latest.tool && !latest.event) return true;
      return false;
    }

    function claudeEndCleanup(sessionId, reason) {
      claudeSessions.delete(sessionId);
      const latestPath = path.join(claudeProgressDir, `${sessionId}_latest.json`);
      try {
        if (fs.existsSync(latestPath)) {
          fs.unlinkSync(latestPath);
          debugLog(`CLAUDE_CLEANUP: ${sessionId} ${reason}, file deleted`);
        }
      } catch {}
    }

    function checkClaudeProgress() {
      try {
        if (!fs.existsSync(claudeProgressDir)) return;
        const files = fs.readdirSync(claudeProgressDir).filter(f => f.endsWith("_latest.json"));
        const now = Date.now();

        for (const file of files) {
          const sessionId = file.replace("_latest.json", "");
          const latestPath = path.join(claudeProgressDir, file);
          try {
            const latest = JSON.parse(fs.readFileSync(latestPath, "utf-8"));
            const elapsed = now - latest.timestamp;

            // 检查 Claude 是否已结束
            if (isClaudeEnded(latest)) {
              claudeEndCleanup(sessionId, "session_ended");
              continue;
            }

            // 超过30分钟文件没更新，自动清理
            if (!claudeSessions.has(sessionId)) {
              claudeSessions.set(sessionId, { lastNotify: 0, notifyCount: 0, lastFileUpdate: now });
            }
            const session = claudeSessions.get(sessionId);
            if (now - (session.lastFileUpdate || 0) > CLAUDE_FILE_MAX_AGE) {
              claudeEndCleanup(sessionId, "file_expired");
              continue;
            }
            session.lastFileUpdate = now;

            // 通知限频：5分钟一次
            if (now - session.lastNotify < claudeNotifyIntervalMs) continue;

            // 通知上限检查
            if (session.notifyCount >= CLAUDE_MAX_NOTIFY) {
              // 只发一次上限通知
              if (session.notifyCount === CLAUDE_MAX_NOTIFY) {
                session.notifyCount++;
                sendFeishuNotification(
                  `user:${cfg?.userOpenId || ""}`, "🗑️", "Claude 通知已达到上限",
                  `会话 ${sessionId.slice(0, 8)}... 已发送 ${CLAUDE_MAX_NOTIFY} 次通知，自动清理`,
                  `claude-max-notify:${sessionId}`
                );
                claudeEndCleanup(sessionId, "max_notify_reached");
              }
              continue;
            }

            // 发进度通知
            const toolSummary = latest.tool
              ? `${latest.tool}: ${(latest.input || "").slice(0, 50)}`
              : latest.event || "unknown";
            const notifyTarget = `user:${cfg?.userOpenId || ""}`;
            if (cfg?.userOpenId) {
              sendFeishuNotification(notifyTarget, "🔧", "Claude 进度",
                `${toolSummary}\n已运行 ${Math.round(elapsed / 1000)}秒`,
                `claude-progress:${sessionId}`
              );
              session.lastNotify = now;
              session.notifyCount++;
              debugLog(`CLAUDE_PROGRESS: ${sessionId} ${toolSummary} (notify #${session.notifyCount})`);
            }

            // 停滞检测：超过10分钟
            if (elapsed >= claudeStaleTimeoutMs && cfg?.userOpenId) {
              sendFeishuNotification(notifyTarget, "⚠️", "Claude 可能卡住",
                `${toolSummary}\n已停滞 ${Math.round(elapsed / 60000)}分钟`,
                `claude-stale:${sessionId}`
              );
            }
          } catch (e) {
            debugLog(`CLAUDE_READ_FAIL: ${file} ${e.message?.slice(0, 100)}`);
          }
        }
      } catch (e) {
        debugLog(`CLAUDE_CHECK_FAIL: ${e.message?.slice(0, 100)}`);
      }
    }

    // ACP session 关联 Claude 进度
    api.on("subagent_spawned", (event, ctx) => {
      if (event.agentId !== "claude" && !(event.agentId && event.agentId.includes("claude"))) return;
      claudeSessions.set(event.childSessionKey, { lastNotify: 0, notifyCount: 0, lastFileUpdate: Date.now() });
      debugLog(`CLAUDE_ACP_START: ${event.childSessionKey}`);
      checkClaudeProgress();
    });

    api.on("subagent_ended", (event, ctx) => {
      const key = event.targetSessionKey;
      if (!claudeSessions.has(key)) return;
      claudeEndCleanup(key, "subagent_ended");
    });

    // fs.watch 监听 Claude 进度文件
    try {
      if (fs.existsSync(claudeProgressDir)) {
        claudeFsWatcher = fs.watch(claudeProgressDir, (eventType, filename) => {
          if (!filename?.endsWith("_latest.json")) return;
          debugLog(`CLAUDE_FILE_CHANGED: ${filename} ${eventType}`);
          checkClaudeProgress();
        });
        debugLog(`CLAUDE_FS_WATCH_ENABLED: ${claudeProgressDir}`);
      } else {
        debugLog(`CLAUDE_FS_WATCH_SKIP: ${claudeProgressDir} not found`);
      }
    } catch (e) {
      debugLog(`CLAUDE_FS_WATCH_FAIL: ${e.message?.slice(0, 100)}`);
    }

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
          notifyCount: 0,
        };
        tasks.set(key, task);
        debugLog(`EXEC_BEFORE: ${key} cmd=${command.slice(0, 50)}`);
        const trace = loadTrace();
        trace[key] = { ...task, timer: undefined, status: "pending" };
        saveTrace(trace);
      } catch {}
    });

    // after_tool_call: 标记旧的 exec 为 completed，启动新的 timer
    api.on("after_tool_call", (event, ctx) => {
      try {
        if (event?.toolName !== "exec") return;
        const sessionKey = ctx?.sessionKey ?? "unknown";

        // 标记同 session 下所有旧的 running exec 为 completed
        for (const [k, t] of tasks) {
          if (t.type === "exec" && t.sessionKey === sessionKey && t.status === "running") {
            t.status = "completed";
            clearTimeout(t.timer);
            debugLog(`EXEC_OLD_COMPLETED: ${k}`);
          }
        }

        // 找到最新的 pending exec 并启动
        let foundKey = null;
        for (const [k, t] of tasks) {
          if (t.type === "exec" && t.sessionKey === sessionKey && t.status === "pending") {
            foundKey = k;
          }
        }
        if (!foundKey) {
          debugLog(`EXEC_AFTER_NO_TASK: session=${sessionKey}`);
          return;
        }
        const task = tasks.get(foundKey);
        task.status = "running";
        startTimer(foundKey, execTimeoutMs, signalDir);
        debugLog(`EXEC_AFTER: ${foundKey} status=running, timer started (${execTimeoutMs}ms)`);
        const trace = loadTrace();
        if (trace[foundKey]) {
          trace[foundKey].status = "running";
          saveTrace(trace);
        }
      } catch {}
    });
  },
};
