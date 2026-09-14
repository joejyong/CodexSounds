import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { spawn } from "node:child_process";

const brokerScript = `$executable = $env:CODEX_SOUNDS_AMBIENT_EXE
$codexHome = $env:CODEX_SOUNDS_AMBIENT_HOME
if ([string]::IsNullOrWhiteSpace($executable) -or
    [string]::IsNullOrWhiteSpace($codexHome) -or
    $executable.Contains('"') -or $codexHome.Contains('"')) { exit 2 }
$commandLine = '"' + $executable + '" --codex-home "' + $codexHome + '" ambient'
$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $commandLine }
if ([int]$result.ReturnValue -ne 0) { exit [int]$result.ReturnValue }`;
const encodedBrokerScript = Buffer.from(brokerScript, "utf16le").toString("base64");

export async function restoreAmbientPlayback(codexHome, helper, dependencies = {}) {
  const read = dependencies.readFile || readFile;
  const launch = dependencies.spawn || spawn;
  const environment = dependencies.env || process.env;
  try {
    const settingsPath = join(codexHome, "notification-sounds", "ambient.json");
    const settings = JSON.parse(await read(settingsPath, "utf8"));
    if (settings?.enabled !== true) return false;
    const windowsRoot = environment.SystemRoot || environment.WINDIR || "C:\\Windows";
    const powershell = join(windowsRoot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
    return await new Promise(resolve => {
      const child = launch(powershell,
        ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encodedBrokerScript], {
          windowsHide: true,
          stdio: "ignore",
          env: {
            ...environment,
            CODEX_SOUNDS_AMBIENT_EXE: helper,
            CODEX_SOUNDS_AMBIENT_HOME: codexHome,
          },
        });
      let settled = false;
      const finish = value => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(value);
      };
      const timer = setTimeout(() => {
        child.kill();
        finish(false);
      }, 10000);
      timer.unref?.();
      child.once("error", () => finish(false));
      child.once("exit", code => finish(code === 0));
    });
  } catch {
    return false;
  }
}
