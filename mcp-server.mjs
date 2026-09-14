import { promises as fs } from "node:fs";
import { spawn } from "node:child_process";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  registerAppResource,
  registerAppTool,
  RESOURCE_MIME_TYPE,
} from "@modelcontextprotocol/ext-apps/server";
import { z } from "zod/v3";
import { refreshInstalledHelper, restoreAmbientPlayback } from "./src/ambient-startup.mjs";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)));
const SETTINGS_URI = "ui://codex-sounds/settings-v1.html";
const CODEX_HOME = resolve(process.env.CODEX_HOME || join(homedir(), ".codex"));
const HELPER = resolve(process.env.CODEX_SOUNDS_HELPER ||
  join(ROOT, "bin", "codex-sounds", "codex-sounds.exe"));
const ROUTES = [
  "settings",
  "preview",
  "import",
  "import-tools",
  "import-cancel",
  "import-status",
  "browse-folder",
  "browse-file",
  "browse-ambient-folder",
  "browse-ambient-file",
  "browse-import-folder",
];
const routeSchema = z.enum(ROUTES);
let session = null;
let sessionPromise = null;

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

const noteSvg = stroke => `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="${stroke}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/></svg>`;
const serverIcons = [
  { src: `data:image/svg+xml,${encodeURIComponent(noteSvg("#202020"))}`,
    mimeType: "image/svg+xml", sizes: ["any"], theme: "light" },
  { src: `data:image/svg+xml,${encodeURIComponent(noteSvg("#f2f2f2"))}`,
    mimeType: "image/svg+xml", sizes: ["any"], theme: "dark" },
];

const readOnlyAnnotations = {
  readOnlyHint: true,
  destructiveHint: false,
  openWorldHint: false,
  idempotentHint: true,
};

function runHelper(action) {
  return new Promise((resolvePromise, rejectPromise) => {
    const report = join(tmpdir(), `codex-sounds-mcp-${randomUUID()}.json`);
    const child = spawn(HELPER, ["--codex-home", CODEX_HOME, "--report", report, action], {
      windowsHide: true,
      stdio: "ignore",
    });
    const timer = setTimeout(() => {
      child.kill();
      rejectPromise(new Error("The Codex Sounds helper timed out."));
    }, 15000);
    child.once("error", error => {
      clearTimeout(timer);
      rejectPromise(error);
    });
    child.once("exit", async code => {
      clearTimeout(timer);
      try {
        const result = JSON.parse(await fs.readFile(report, "utf8"));
        if (result.error) throw new Error(result.error);
        if (code !== 0) throw new Error("The Codex Sounds helper failed.");
        resolvePromise(result);
      } catch (error) {
        rejectPromise(error);
      } finally {
        await fs.rm(report, { force: true }).catch(() => {});
      }
    });
  });
}

function validateSession(value) {
  const url = new URL(value?.url || "");
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || url.pathname !== "/") {
    throw new Error("The settings helper returned an invalid local address.");
  }
  const token = url.searchParams.get("token");
  if (!token) throw new Error("The settings helper did not return an access token.");
  return { origin: url.origin, token };
}

async function requestWithSession(activeSession, path, body) {
  const response = await fetch(`${activeSession.origin}/api/${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      "X-Codex-Sound-Token": activeSession.token,
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(20000),
    cache: "no-store",
  });
  const data = await response.json();
  if (!response.ok) throw new ApiError(data?.error || "The settings request failed.", response.status);
  return data;
}

async function ensureSession() {
  if (session) {
    try {
      await requestWithSession(session, "health");
      return session;
    } catch {
      session = null;
    }
  }
  if (!sessionPromise) {
    sessionPromise = runHelper("settings")
      .then(validateSession)
      .then(value => (session = value))
      .finally(() => { sessionPromise = null; });
  }
  return sessionPromise;
}

async function settingsRequest(path, body) {
  const activeSession = await ensureSession();
  try {
    return await requestWithSession(activeSession, path, body);
  } catch (error) {
    if (error instanceof ApiError && error.status !== 403 && error.status < 500) throw error;
    if (session !== activeSession) throw error;
    session = null;
    const replacement = await ensureSession();
    return requestWithSession(replacement, path, body);
  }
}

const server = new McpServer(
  { name: "codex-sounds", version: "1.3.8", icons: serverIcons },
  { capabilities: { tools: {}, resources: {} } },
);

const settingsHtml = await fs.readFile(join(ROOT, "src", "settings.html"), "utf8");

registerAppResource(server, "codex-sounds-settings", SETTINGS_URI, {}, async () => ({
  contents: [{
    uri: SETTINGS_URI,
    mimeType: RESOURCE_MIME_TYPE,
    text: settingsHtml.replace("const appMode=false;", "const appMode=true;"),
    _meta: { ui: { prefersBorder: false } },
  }],
}));

registerAppTool(server, "open_sound_settings", {
  title: "Codex Sounds",
  description: "Open Codex Sounds settings from the panel New tab menu.",
  inputSchema: {},
  outputSchema: { opened: z.boolean(), message: z.string().optional() },
  annotations: readOnlyAnnotations,
  _meta: {
    ui: { resourceUri: SETTINGS_URI, visibility: ["app"] },
    "openai/ui": { entrypoints: [{ type: "thread" }] },
    "openai/outputTemplate": SETTINGS_URI,
    "openai/widgetAccessible": true,
  },
}, async () => {
  try {
    await ensureSession();
    return { structuredContent: { opened: true }, content: [] };
  } catch (error) {
    return { structuredContent: { opened: false, message: error.message }, content: [] };
  }
});

registerAppTool(server, "sound_settings_request", {
  title: "Use Codex Sounds settings",
  description: "Read or change local Codex Sounds settings from its panel.",
  inputSchema: { path: routeSchema, body: z.record(z.any()).optional() },
  annotations: {
    readOnlyHint: false,
    destructiveHint: false,
    openWorldHint: false,
    idempotentHint: false,
  },
  _meta: {
    ui: { visibility: ["app"] },
    "openai/widgetAccessible": true,
  },
}, async ({ path, body }) => {
  try {
    return { structuredContent: { ok: true, data: await settingsRequest(path, body) }, content: [] };
  } catch (error) {
    return { structuredContent: { ok: false, error: error.message }, content: [] };
  }
});

const transport = new StdioServerTransport();
await server.connect(transport);
void refreshInstalledHelper(CODEX_HOME, HELPER, () => runHelper("setup"))
  .then(refreshed => refreshed || restoreAmbientPlayback(CODEX_HOME, HELPER));
