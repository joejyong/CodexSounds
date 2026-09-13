import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const isolatedHome = await mkdtemp(join(tmpdir(), "codex-sounds-panel-"));
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
  assert.match(resource.contents[0].text, /const appMode=true;/);
  assert.match(resource.contents[0].text, /notifyIntrinsicHeight/);
  assert.match(resource.contents[0].text, /sound_settings_request/);
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
