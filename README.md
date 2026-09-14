# Codex Sounds

Custom reply notifications and project soundscapes for Codex. Windows x64 only.

Only completed replies in user tasks play sounds. Internal subagent completions and progress events are silent. Duplicate callbacks for the same task and turn play at most once. This filter reads local Codex thread-origin metadata; if that metadata is unavailable, it stays silent. A bounded local event history records suppression reasons without storing conversation text.

Install the plugin in Codex, then ask **Set up reply sounds on this workstation**. After setup, fully quit and reopen Codex if requested. Open the right panel's New tab menu and choose **Codex Sounds** to load the controls without starting an agent turn. Asking **Open my sound settings in Codex** remains available as a fallback.

## Download

Download the ready-to-install [latest Windows x64 ZIP](https://github.com/joejyong/CodexSounds/releases/latest/download/codex-sounds-windows-x64.zip), or open the [latest release page](https://github.com/joejyong/CodexSounds/releases/latest) for its notes and SHA-256 checksum. Extract the ZIP before installing the contained `codex-sounds` folder in Codex.

## Right-panel settings

Codex Sounds registers an app-only task entrypoint named **Codex Sounds**. Codex lists it in the right panel's New tab menu. Selecting it opens the complete settings page through the plugin's MCP server. It does not send a prompt, start an agent turn, or use model tokens.

The panel completes the MCP Apps initialization handshake before loading settings, then reports size changes through the standard MCP Apps notification. The panel calls a small set of app-only MCP tools. Those tools proxy requests to the existing loopback settings service. The browser never receives the service's access token. The same page still works through the agent-opened browser route for compatibility.

After installing a build that adds or changes MCP tools, fully quit and reopen Codex so the desktop app refreshes its plugin catalog.

Choose a single WAV or MP3, set volume, or cycle through a folder in filename order. Preview does not advance the sequence. Sound choices, volume, and rotation state stay on each workstation. The background player includes its runtime; Python does not need to be installed.

## Importing ambient audio

The settings panel can queue up to 50 YouTube URLs and save each source as a 128 kbps MP3 in a folder you choose. It handles one URL at a time, shows the downloader output, keeps files that already exist, and lets you cancel the queue. Playlist expansion is disabled so one pasted URL cannot pull in an unexpected collection.

Use this only for audio you own, public-domain audio, or material whose creator permits downloading. YouTube Premium playback does not by itself grant permission to convert a video.

The importer calls the maintained `yt-dlp` and FFmpeg command-line tools. They are not bundled with this plugin. The settings panel detects them and can install their verified Windows Package Manager packages when you click **Install tools**. This keeps the plugin package small and leaves tool updates to Windows Package Manager.

## Project soundscapes

Assign one WAV or MP3 track, or a folder playlist, to each saved local Codex project from the same settings panel. A track loops. A folder plays in filename order and starts again after the final file. The player keeps going when you move into Unreal Engine, a browser, or another Windows app. Opening a standalone task or GPT conversation pauses it. Returning to the project resumes the saved track and position. Closing Codex saves the position and releases the Windows media handle.

Project soundscapes have a separate master switch, manual mute, volume, and configurable global mute shortcut. The default shortcut is `Ctrl+Alt+P`. It affects ambient audio only, so reply notifications still play. Both the ambient and notification volume settings are multiplied by the current Windows master output volume, including its mute state. Enabling soundscapes creates a per-user Windows startup shortcut for one single-instance controller. Whenever Codex starts, the plugin asks Windows to restore that controller outside Codex's process tree. This lets playback survive short-lived plugin processes while Codex loads the selected project. The controller remains dormant when Codex is closed and holds no media track or audio device. Setup removes orphaned controllers left by older installations. Disabling soundscapes removes the shortcut and stops the controller. Spotify and remote streaming playback are not supported.

Soundscapes follow Codex's explicit project selection. Older saved workspace roots cannot override a switch to another project or a standalone conversation. Projects without an assignment stay silent.

Assignments also record the project name and root folders. If Codex changes a local project ID during reinstall or migration, the controller can recover the assignment by matching that metadata after the original ID is gone. Projects that share folders keep separate assignments. Ambiguous names remain silent rather than selecting the wrong project.

## Another workstation

Use the plugin's Share action in Codex, or download and extract the [latest release ZIP](https://github.com/joejyong/CodexSounds/releases/latest/download/codex-sounds-windows-x64.zip). The release already contains the Node runtime dependencies needed by the right-panel settings entry. If you clone the source repository instead, run `npm ci` in the plugin folder before installing it. Run setup on that workstation. Custom sound files are not included. Copy your own clips separately and choose their new location in settings.

## Turning it off

Ask **Mute my reply sounds** for an immediate notification mute. Use the global shortcut or ask **Mute my project soundscapes** to pause ambient audio while keeping its master switch enabled. Project soundscapes can also be stopped with **Turn off my project soundscapes**. Disabling or uninstalling the plugin in the user-level Codex plugin settings silences both modes. Ask **Remove Codex Sounds** to disconnect the callback and background controller before uninstalling the plugin. Settings are preserved; your audio files are never deleted.

## Local operation

The settings panel uses a token-protected server on `127.0.0.1`, opened inside Codex. The right-panel entry calls this server through the plugin's MCP App, so its access token is never sent to the panel and opening it does not use agent tokens. The server stops after two hours without activity. Opening settings again starts it when needed. File selection uses a standard Windows file picker. Settings are not added to Codex's native Preferences screen.

The player preserves other notification handlers, including Codex's Windows computer-use helper. It never changes system volume. Notification clips must be playable WAV or MP3 files no longer than 60 seconds. Ambient tracks do not have that limit. Empty or unavailable notification folders fall back to the selected single sound.

The source is in `src`. The compiled helper is in `bin/codex-sounds`. Third-party license notices are in `licenses`. This package includes no personal audio, credentials, workstation configuration, yt-dlp, or FFmpeg binary.

## Development

The Python application and loopback service live in `src`. `mcp-server.mjs` registers the right-panel entrypoint and keeps local service credentials out of the browser. Install its Node dependencies before running or packaging the plugin:

```powershell
npm ci
```

Run the JavaScript checks and the Python regression suite:

```powershell
npm run check
npm test
python -m unittest discover -s tests
```

Rebuild the bundled Windows helper after changing Python or HTML source:

```powershell
.\scripts\Build-Plugin.ps1
```

Create a release by making sure the manifest and `package.json` use the intended version, then push a matching tag:

```powershell
git tag v1.3.8
git push origin v1.3.8
```

The Windows workflow runs every test, rebuilds the bundled helper, packages the plugin with its Node dependencies, verifies that the Tcl/Tk runtime is complete, writes a SHA-256 checksum, and publishes both files on GitHub Releases. Keep the ZIP asset name unchanged so the latest-download link remains valid.
