const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

function loadPluginWithHome(homeDir) {
  process.env.HOME = homeDir;
  process.env.USERPROFILE = homeDir;
  delete require.cache[require.resolve("./index.js")];
  return require("./index.js");
}

function createApi() {
  const handlers = new Map();
  return {
    runtime: { system: {} },
    pluginConfig: {},
    on(name, handler) {
      handlers.set(name, handler);
    },
    handlers,
  };
}

function writePlan(homeDir, filename, status = "IN_PROGRESS") {
  const planDir = path.join(homeDir, ".openclaw/workspace/data/task-traces");
  fs.mkdirSync(planDir, { recursive: true });
  const filePath = path.join(planDir, filename);
  fs.writeFileSync(filePath, [
    "# Execution Plan",
    "<!-- version: 1 -->",
    "**Task:** test task",
    "**Created:** 2026-05-17 12:00",
    `**Status:** ${status}`,
    "",
    "## Steps",
    "- [ ] 1. first step",
    "- [ ] 2. second step",
    "",
  ].join("\n"));
  return filePath;
}

test("before_prompt_build injects only the current session execution plan", async () => {
  const homeDir = fs.mkdtempSync(path.join(os.tmpdir(), "progress-monitor-"));
  const plugin = loadPluginWithHome(homeDir);
  const api = createApi();

  writePlan(homeDir, "plan-agent_main_feishu_direct_ou_abc.md");
  writePlan(homeDir, "execution-plan.md");
  writePlan(homeDir, "plan-other-session.md");

  plugin.register(api);

  const beforePromptBuild = api.handlers.get("before_prompt_build");
  assert.equal(typeof beforePromptBuild, "function");

  const result = await beforePromptBuild({}, {
    sessionKey: "agent:main:feishu:direct:ou_abc",
  });

  assert.match(result.appendContext, /test task/);
  assert.match(result.appendContext, /plan-agent_main_feishu_direct_ou_abc\.md/);
  assert.doesNotMatch(result.appendContext, /execution-plan\.md/);
  assert.doesNotMatch(result.appendContext, /plan-other-session\.md/);
});

test("before_prompt_build removes DONE execution plan for the current session", async () => {
  const homeDir = fs.mkdtempSync(path.join(os.tmpdir(), "progress-monitor-"));
  const plugin = loadPluginWithHome(homeDir);
  const api = createApi();
  const planPath = writePlan(homeDir, "plan-agent_main.md", "DONE");

  plugin.register(api);

  const result = await api.handlers.get("before_prompt_build")({}, {
    sessionKey: "agent:main",
  });

  assert.equal(result, undefined);
  assert.equal(fs.existsSync(planPath), false);
});
