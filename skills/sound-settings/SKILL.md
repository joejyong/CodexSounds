---
name: sound-settings
description: Configure Codex reply notification sounds and project soundscapes on Windows, open the settings panel inside Codex, assign looped tracks or folder playlists to projects, mute or unmute sounds, or disconnect the integration.
---

# Codex Sounds

Use the bundled helper. Resolve the plugin root two directories above this SKILL.md's folder. The helper is `scripts/Manage-Sounds.ps1` at that root. It bundles the Windows x64 runtime, so do not install Python, pip packages, or a separate settings application.

Run commands with the full resolved script path:

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File '<plugin-root>\scripts\Manage-Sounds.ps1' -Action Status
```

Actions return JSON. Supported actions are `Setup`, `Settings`, `Status`, `Mute`, `Unmute`, `AmbientOn`, `AmbientOff`, `AmbientMute`, `AmbientUnmute`, `AmbientToggle`, and `Disconnect`.

## Open settings in Codex

1. Run `Status`. If this workstation has not been set up, run `Setup` when the user has requested sound setup or configuration. This registers the callback for every completed reply while preserving existing callbacks and this workstation's saved sound choices.
2. Run `Settings`. It starts or reuses the local settings panel and returns a `url`.
3. Use Codex's `open_in_codex` tool with a browser target and that exact URL. This opens the controls in a Codex panel. If that tool is unavailable, give the user the returned link. Do not open the old Tkinter settings window or launch an external browser when the Codex panel tool is available.

The panel lets the user configure reply notifications, assign a looped track or folder playlist to each saved local Codex project, and import authorised audio from a list of YouTube URLs into a chosen MP3 folder. Project soundscapes use their own volume and master switch. Windows file/folder dialogs are used only for selecting local paths. Do not manually trigger a sound when merely opening settings.

The MP3 importer accepts up to 50 HTTPS YouTube URLs per queue and deliberately disables playlist expansion. It processes one video at a time at 128 kbps, keeps existing files, and supports cancellation. Use it only for audio the user owns, public-domain audio, or material whose creator permits downloading. It needs `yt-dlp` and FFmpeg; the settings panel can install their verified Windows Package Manager packages after the user clicks **Install tools**. Neither tool is bundled in the plugin.

The URL contains a local access token. Use it to open the panel, but do not put it into a shared artifact or public message. Settings are local to the workstation running this task. For a remote workstation, run the plugin there; do not pretend a localhost URL opens another machine's settings.

## Setup and removal

- `Setup` is safe to repeat, preserves existing preferences and rotation state, and returns `restartRequired`. If true, ask the user to fully quit and reopen Codex after the work is done. Do not restart the app during active work.
- `Mute` and `Unmute` apply immediately. Use these for the corresponding requests.
- `AmbientOn` and `AmbientOff` control project soundscapes without changing reply notifications. Enabling starts the background controller and creates a per-user startup shortcut. Disabling stops it and removes that shortcut.
- `AmbientMute`, `AmbientUnmute`, and `AmbientToggle` change the persistent manual mute state. The configured global shortcut does the same thing without opening Codex. Its default is `Ctrl+Alt+P`.
- `Disconnect` restores other notification callbacks and preserves sound preferences. Use it before removing the plugin if the user asks to uninstall this integration. Then use Codex's plugin uninstall tool. Do not delete the user's sound folder.
- After copying/installing the plugin on another workstation, run `Setup` there. Do not copy this workstation's configuration, callback paths, sound files, or logs unless the user explicitly requests them.

Automatic alerts use the Codex completion callback. Do not play an additional sound at the end of a reply through this skill. A successful callback result establishes accepted playback, not that the user heard it. Diagnose missed sounds with `Status` before repeatedly asking for listening tests.

Project soundscapes follow the project that owns the active local task. They keep playing when another Windows app has focus. Closing Codex saves the position and releases the media handle; the single controller remains dormant so it can detect a later Codex launch. Opening a standalone task or GPT conversation pauses the soundscape and saves its position. Switching projects saves the current track and position, then resumes the active project's audio. Assignments include project roots and names as fallbacks when Codex changes a local project ID. Folder playlists use filename order and loop. The global shortcut mutes only soundscapes, not reply notifications. Spotify and remote streaming playback are not supported; downloaded MP3 files are local sources.

`Setup` removes ambient controller processes left by older plugin installations before starting the current single-instance controller. Use it after reinstalling or updating the plugin on another workstation.

Only user-task completion events play sounds. Internal agents, progress events, and repeated completion callbacks are suppressed. If `lastEvent.suppressed` is `unknown-origin`, the local Codex thread metadata could not establish that this was a user task. Check that metadata lookup before changing audio settings. A bounded diagnostic history is stored in `notification-sounds/notification-events.sqlite`; it contains event identifiers and playback outcomes, not conversation text.
