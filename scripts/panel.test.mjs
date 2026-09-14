import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createContext, Script } from "node:vm";
import { EventEmitter } from "node:events";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { restoreAmbientPlayback } from "../src/ambient-startup.mjs";

const isolatedHome = await mkdtemp(join(tmpdir(), "codex-sounds-panel-"));
const launches = [];
const fakeChild = new EventEmitter();
fakeChild.kill = () => {};
assert.equal(await restoreAmbientPlayback("C:\\Codex", "C:\\plugin\\codex-sounds.exe", {
  readFile: async () => JSON.stringify({ enabled: true }),
  env: { SystemRoot: "C:\\Windows", PRESERVED: "yes" },
  spawn(command, args, options) {
    launches.push({ command, args, options });
    queueMicrotask(() => fakeChild.emit("exit", 0));
    return fakeChild;
  },
}), true);
assert.equal(launches.length, 1);
assert.equal(launches[0].command,
  "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe");
assert.deepEqual(launches[0].args.slice(0, 4),
  ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand"]);
assert.match(Buffer.from(launches[0].args[4], "base64").toString("utf16le"),
  /Invoke-CimMethod -ClassName Win32_Process -MethodName Create/);
assert.deepEqual(launches[0].options, {
  windowsHide: true,
  stdio: "ignore",
  env: {
    SystemRoot: "C:\\Windows",
    PRESERVED: "yes",
    CODEX_SOUNDS_AMBIENT_EXE: "C:\\plugin\\codex-sounds.exe",
    CODEX_SOUNDS_AMBIENT_HOME: "C:\\Codex",
  },
});
assert.equal(await restoreAmbientPlayback("C:\\Codex", "unused.exe", {
  readFile: async () => JSON.stringify({ enabled: false }),
  spawn() { throw new Error("Disabled soundscapes must not start the player."); },
}), false);
const client = new Client({ name: "codex-sounds-panel-test", version: "1.0.0" });
const transport = new StdioClientTransport({
  command: process.execPath,
  args: [fileURLToPath(new URL("../mcp-server.mjs", import.meta.url))],
  env: {
    ...process.env,
    CODEX_HOME: join(isolatedHome, ".codex"),
    CODEX_SOUNDS_HELPER: join(isolatedHome, "missing-helper.exe"),
  },
  stderr: "inherit",
});

try {
  await client.connect(transport);
  const listed = await client.listTools();
  assert.deepEqual(listed.tools.map(tool => tool.name).sort(),
    ["open_sound_settings", "sound_settings_request"]);
  const openTool = listed.tools.find(tool => tool.name === "open_sound_settings");
  assert.equal(openTool.title, "Codex Sounds");
  assert.deepEqual(openTool._meta["openai/ui"].entrypoints, [{ type: "thread" }]);
  assert.deepEqual(openTool._meta.ui.visibility, ["app"]);
  assert.deepEqual(openTool.inputSchema.properties, {});
  const resource = await client.readResource({ uri: openTool._meta.ui.resourceUri });
  const panelHtml = resource.contents[0].text;
  const panelScript = panelHtml.match(/<script>([\s\S]*)<\/script>/)?.[1];
  assert.ok(panelScript, "Panel script is missing.");
  new Script(panelScript, { filename: "settings-panel.js" });
  assert.match(panelHtml, /const appMode=true;/);
  assert.match(panelHtml, /<head>[\s\S]*<\/head>\s*<body>/);
  assert.match(panelHtml, /bridgeRequest\('ui\/initialize'/);
  assert.match(panelHtml, /protocolVersion:'2026-01-26'/);
  assert.match(panelHtml, /bridgeNotification\('ui\/notifications\/initialized'\)/);
  assert.match(panelHtml, /bridgeNotification\('ui\/notifications\/size-changed'/);
  assert.match(panelHtml, /data\.method==='ui\/resource-teardown'/);
  assert.match(panelHtml, /sound_settings_request/);
  assert.match(panelHtml,
    /async function start\(\)\{try\{await initializeApp\(\);render\(await api\('settings'\)\)/);

  const outbound = [];
  const listeners = [];
  const elements = new Map();
  const makeElement = () => {
    const element = {
      checked: false,
      children: [],
      className: "",
      dataset: {},
      disabled: false,
      textContent: "",
      value: "",
      classList: { toggle() {} },
      setAttribute() {},
      replaceChildren(...children) { this.children = children; },
    };
    Object.defineProperty(element, "selectedOptions", {
      get() { return this.children.filter(child => child.value === this.value); },
    });
    return element;
  };
  const dispatch = data => listeners.forEach(listener => listener({
    data,
    source: parent,
  }));
  const settings = {
    machine: "Test workstation",
    enabled: true,
    volume: 50,
    folder: "",
    sounds: { Default: "default.wav" },
    sound: "Default",
    mode: "single",
    ambient: {
      assignments: {},
      enabled: false,
      muted: false,
      hotkey: "Ctrl+Alt+Shift+M",
      volume: 24,
      status: {},
    },
    projects: [],
  };
  const importStatus = {
    tools: { ready: false, winget: false },
    running: false,
    message: "Import tools unavailable in test.",
    log: [],
  };
  const parent = {
    postMessage(message) {
      outbound.push(message);
      if (message.id === undefined) return;
      if (message.method === "ui/initialize") {
        dispatch({
          jsonrpc: "2.0",
          id: message.id,
          result: {
            protocolVersion: "2026-01-26",
            hostInfo: { name: "codex-test", version: "1.0.0" },
            hostCapabilities: {},
            hostContext: { theme: "dark" },
          },
        });
        return;
      }
      if (message.method === "tools/call") {
        const path = message.params.arguments.path;
        dispatch({
          jsonrpc: "2.0",
          id: message.id,
          result: {
            structuredContent: {
              ok: true,
              data: path === "settings" ? settings : importStatus,
            },
          },
        });
      }
    },
  };
  const window = {
    innerWidth: 420,
    parent,
    addEventListener(type, listener) {
      if (type === "message") listeners.push(listener);
    },
  };
  const document = {
    body: { scrollHeight: 640 },
    documentElement: { scrollHeight: 640 },
    createElement: makeElement,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement());
      return elements.get(id);
    },
  };
  const context = createContext({
    clearTimeout,
    console,
    document,
    Error,
    location: { search: "" },
    Math,
    Promise,
    requestAnimationFrame: callback => callback(),
    setTimeout,
    structuredClone,
    URLSearchParams,
    window,
  });
  new Script(panelScript, { filename: "settings-panel.js" }).runInContext(context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(outbound[0].method, "ui/initialize");
  assert.equal(outbound[1].method, "ui/notifications/initialized");
  assert.equal(outbound.find(message => message.method === "tools/call")
    ?.params.arguments.path, "settings");
  assert.ok(outbound.some(message =>
    message.method === "ui/notifications/size-changed"));
  assert.equal(elements.get("machine").textContent,
    "This workstation · Test workstation");
  const opened = await client.callTool({ name: "open_sound_settings", arguments: {} });
  assert.equal(opened.structuredContent.opened, false);
  assert.equal(typeof opened.structuredContent.message, "string");
  assert.ok(opened.structuredContent.message.length > 0);
  const invalid = await client.callTool({ name: "sound_settings_request",
    arguments: { path: "not-an-api" } });
  assert.equal(invalid.isError, true);
  console.log("Codex Sounds panel MCP checks passed.");
} finally {
  await client.close();
  await transport.close();
  await rm(isolatedHome, { recursive: true, force: true });
}
