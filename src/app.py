"""Portable Windows entrypoint and reversible Codex notification setup."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

import tomlkit
import ambient
import notify


def atomic_json(path, value):
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    temporary.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def read_json(path, default=None):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else default


def publish_directory(staging, destination, attempts=30):
    """Publish a copied Windows bundle after short-lived scanner locks clear."""
    for attempt in range(attempts):
        try:
            os.replace(staging, destination)
            return
        except FileExistsError:
            if destination.exists():
                shutil.rmtree(staging, ignore_errors=True)
                return
            raise
        except PermissionError:
            if attempt + 1 == attempts:
                raise
            time.sleep(0.1)


def valid_command(command):
    return isinstance(command, list) and all(isinstance(part, str) for part in command)


def is_helper(command):
    return (len(command) >= 2 and command[1] == 'turn-ended'
            and Path(command[0]).name.lower() in ('codex-computer-use.exe', 'codex-computer-use-arm64.exe'))


def helper_child(command):
    if '--previous-notify' not in command:
        return [], list(command)
    index = command.index('--previous-notify')
    if index + 1 >= len(command):
        raise ValueError('The existing helper callback is incomplete')
    child = json.loads(command[index + 1])
    if not valid_command(child):
        raise ValueError('The existing helper callback is invalid')
    return child, command[:index] + command[index + 2:]


def ours(command, data):
    if not command:
        return False
    executable = Path(command[0])
    if executable.name.lower() == 'codex-sounds.exe' and 'notify' in command:
        return executable.resolve().is_relative_to(data.resolve())
    legacy = (data / 'notify.py').resolve()
    return any(Path(part).name.lower() == 'notify.py' and Path(part).resolve() == legacy for part in command[1:])


def remove_ours(command, data, forward, depth=0):
    if depth > 12:
        raise ValueError('Notification callback chain is too deeply nested')
    if ours(command, data):
        if ours(forward, data):
            raise ValueError('Notification callback would recurse')
        return forward
    if is_helper(command):
        child, base = helper_child(command)
        child = remove_ours(child, data, forward, depth + 1) if child else []
        return [*base, '--previous-notify', json.dumps(child)] if child else base
    return command


def write_notify(config_path, original, command):
    document = tomlkit.parse(original.decode('utf-8-sig'))
    before = document.unwrap()
    if command:
        document['notify'] = command
    elif 'notify' in document:
        del document['notify']
    updated = tomlkit.dumps(document).encode('utf-8')
    after = tomlkit.parse(updated.decode('utf-8')).unwrap()
    before.pop('notify', None)
    after.pop('notify', None)
    if before != after:
        raise ValueError('Refusing to change unrelated Codex settings')
    current = config_path.read_bytes() if config_path.exists() else b''
    if current != original:
        raise ValueError('Codex configuration changed during setup. Run setup again.')
    if updated != original:
        backup = config_path.parent / 'notification-sounds' / ('config-backup-' + str(time.time_ns()) + '.toml')
        backup.write_bytes(original)
        temporary = config_path.with_name('.config-sounds-' + uuid.uuid4().hex + '.tmp')
        temporary.write_bytes(updated)
        os.replace(temporary, config_path)
    return updated != original


def make_shortcut(executable, codex_home):
    # Paths are data passed through the environment, not PowerShell code.
    code = '''$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Programs')) 'Codex Sound Settings.lnk'))
$link.TargetPath = $env:CODEX_SOUND_EXE
$link.Arguments = $env:CODEX_SOUND_ARGS
$link.WorkingDirectory = Split-Path -Parent $env:CODEX_SOUND_EXE
$link.Description = 'Choose Codex reply sounds and volume'
$link.Save()
'''
    env = dict(os.environ, CODEX_SOUND_EXE=str(executable),
               CODEX_SOUND_ARGS=subprocess.list2cmdline(['--codex-home', str(codex_home), 'settings']))
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                    base64.b64encode(code.encode('utf-16le')).decode('ascii')],
                   env=env, check=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)


def ambient_startup_path():
    return (Path(os.environ['APPDATA']) / 'Microsoft' / 'Windows' / 'Start Menu' /
            'Programs' / 'Startup' / 'Codex Sounds Ambient.lnk')


def stop_orphan_ambient_processes(codex_home, keep_pid=0):
    """Stop ambient controllers under this installation that lost their session record."""
    code = r'''$root = [IO.Path]::GetFullPath($env:CODEX_SOUND_APP_ROOT)
$prefix = $root.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
$keep = [int]$env:CODEX_SOUND_KEEP_PID
Get-CimInstance Win32_Process -Filter "Name = 'codex-sounds.exe'" | ForEach-Object {
    if ($_.ExecutablePath -and $_.ProcessId -ne $keep) {
        $path = [IO.Path]::GetFullPath($_.ExecutablePath)
        $isOurs = $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
        $isAmbient = $_.CommandLine -match '(?i)(?:^|\s)ambient(?:\s|$)'
        if ($isOurs -and $isAmbient) { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
}
'''
    env = dict(os.environ, CODEX_SOUND_APP_ROOT=str(codex_home / 'notification-sounds' / 'apps'),
               CODEX_SOUND_KEEP_PID=str(int(keep_pid or 0)))
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                    base64.b64encode(code.encode('utf-16le')).decode('ascii')],
                   env=env, check=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)


def configure_ambient_background(codex_home, executable, enabled):
    """Install or remove the per-user startup entry and sync the controller."""
    import ambient
    ambient.ROOT = codex_home / 'notification-sounds'
    shortcut = ambient_startup_path()
    if enabled:
        session = read_json(ambient.ROOT / 'ambient-session.json', {})
        keep_pid = 0
        if (isinstance(session, dict) and ambient.process_exists(session.get('pid')) and
                str(session.get('executable', '')).casefold() == str(Path(executable).resolve()).casefold()):
            keep_pid = session['pid']
        elif isinstance(session, dict) and ambient.process_exists(session.get('pid')):
            ambient.stop_running()
        stop_orphan_ambient_processes(codex_home, keep_pid)
        code = '''$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($env:CODEX_AMBIENT_LINK)
$link.TargetPath = $env:CODEX_AMBIENT_EXE
$link.Arguments = $env:CODEX_AMBIENT_ARGS
$link.WorkingDirectory = Split-Path -Parent $env:CODEX_AMBIENT_EXE
$link.Description = 'Play project soundscapes for the active Codex project'
$link.Save()
'''
        env = dict(os.environ, CODEX_AMBIENT_LINK=str(shortcut), CODEX_AMBIENT_EXE=str(executable),
                   CODEX_AMBIENT_ARGS=subprocess.list2cmdline(['--codex-home', str(codex_home), 'ambient']))
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                        base64.b64encode(code.encode('utf-16le')).decode('ascii')],
                       env=env, check=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        ambient.ensure_running(codex_home, executable)
    else:
        shortcut.unlink(missing_ok=True)
        ambient.stop_running()
        stop_orphan_ambient_processes(codex_home)


def install(codex_home, bundle=None, shortcut=False):
    data = codex_home / 'notification-sounds'
    data.mkdir(parents=True, exist_ok=True)
    config_path = codex_home / 'config.toml'
    original = config_path.read_bytes() if config_path.exists() else b''
    current = tomlkit.parse(original.decode('utf-8-sig')).unwrap().get('notify', [])
    if not valid_command(current):
        raise ValueError('Existing notify setting must be an array of strings')
    forward = read_json(data / 'forward-command.json', [])
    if not valid_command(forward):
        raise ValueError('Saved notification callback must be an array of strings')
    base = remove_ours(current, data, forward)
    if ours(current, data) and not base:
        base = read_json(data / 'original-command.json', [])
    if not valid_command(base) or ours(base, data):
        raise ValueError('Cannot preserve the existing notification callback safely')
    bundle = bundle or Path(sys.executable).parent
    source_exe = bundle / 'codex-sounds.exe'
    if not source_exe.exists():
        raise ValueError('The portable Windows application is missing')
    build = hashlib.sha256(source_exe.read_bytes()).hexdigest()[:16]
    destination = data / 'apps' / build
    if not destination.exists():
        staging = data / 'apps' / ('.install-' + uuid.uuid4().hex)
        staging.parent.mkdir(exist_ok=True)
        shutil.copytree(bundle, staging)
        publish_directory(staging, destination)
    executable = destination / 'codex-sounds.exe'
    callback = [str(executable), '--codex-home', str(codex_home), 'notify']
    if is_helper(base):
        forward, helper = helper_child(base)
        configured = [*helper, '--previous-notify', json.dumps(callback)]
    else:
        forward, configured = base, callback
    if not (data / 'sounds.json').exists():
        atomic_json(data / 'sounds.json', {'defaultSound': 'chime', 'machines': {},
            'sounds': {'chime': '%WINDIR%\\Media\\chimes.wav', 'ding': '%WINDIR%\\Media\\Windows Ding.wav',
                       'notify': '%WINDIR%\\Media\\Windows Notify System Generic.wav'},
            'enabled': True, 'volume': 100, 'playbackMode': 'single', 'soundFolder': ''})
    ambient_path = data / 'ambient.json'
    ambient_settings = read_json(ambient_path, None)
    if not isinstance(ambient_settings, dict):
        ambient_settings = {'enabled': False, 'volume': 24, 'assignments': {}}
    ambient_settings.setdefault('muted', False)
    ambient_settings['hotkey'] = ambient.migrate_default_hotkey(ambient_settings.get('hotkey'))
    projects, _ = ambient.project_snapshot(codex_home)
    ambient_settings['assignments'] = ambient.enrich_assignment_identities(
        ambient_settings.get('assignments', {}), projects)
    atomic_json(ambient_path, ambient_settings)
    # Never change the user's volume, chosen folder, or rotation database on setup.
    previous_forward = (data / 'forward-command.json').read_bytes() if (data / 'forward-command.json').exists() else None
    atomic_json(data / 'forward-command.json', forward)
    try:
        changed = write_notify(config_path, original, configured)
    except Exception:
        if previous_forward is not None:
            (data / 'forward-command.json').write_bytes(previous_forward)
        else:
            (data / 'forward-command.json').unlink(missing_ok=True)
        raise
    plugin_ids = [key for key in tomlkit.parse(original.decode('utf-8-sig')).get('plugins', {}) if key.split('@')[0] == 'codex-sounds']
    atomic_json(data / 'plugin-install.json', {'version': '1.3.8', 'executable': str(executable),
                'pluginIds': plugin_ids or ['codex-sounds@personal'],
                'restoreCommand': base, 'installedCommand': configured})
    warnings = []
    if shortcut:
        try:
            make_shortcut(executable, codex_home)
        except Exception as error:
            warnings.append('Start shortcut: ' + str(error))
    try:
        configure_ambient_background(codex_home, executable,
                                     read_json(data / 'ambient.json', {}).get('enabled', False))
    except Exception as error:
        warnings.append('Project soundscapes: ' + str(error))
    return {'installed': True, 'dataDirectory': str(data), 'executable': str(executable),
            'restartRequired': changed, 'warnings': warnings}


def disconnect(codex_home):
    data = codex_home / 'notification-sounds'
    config_path = codex_home / 'config.toml'
    original = config_path.read_bytes()
    current = list(tomlkit.parse(original.decode('utf-8-sig')).get('notify', []))
    forward = read_json(data / 'forward-command.json', [])
    restored = remove_ours(current, data, forward)
    changed = write_notify(config_path, original, restored)
    settings = read_json(data / 'sounds.json', {})
    settings['enabled'] = False
    atomic_json(data / 'sounds.json', settings)
    ambient_settings = read_json(data / 'ambient.json', {'enabled': False, 'muted': False,
                                 'hotkey': ambient.DEFAULT_HOTKEY, 'volume': 24, 'assignments': {}})
    ambient_settings['enabled'] = False
    atomic_json(data / 'ambient.json', ambient_settings)
    installation = read_json(data / 'plugin-install.json', {})
    configure_ambient_background(codex_home, installation.get('executable', sys.executable), False)
    return {'disconnected': True, 'restartRequired': changed, 'settingsPreserved': True}


def status(codex_home):
    data = codex_home / 'notification-sounds'
    settings = read_json(data / 'sounds.json', {})
    ambient_settings = read_json(data / 'ambient.json', {})
    return {'installed': (data / 'plugin-install.json').exists(),
            'machine': os.environ.get('COMPUTERNAME'), 'dataDirectory': str(data),
            'enabled': settings.get('enabled', False), 'volume': settings.get('volume', 100),
            'playbackMode': settings.get('playbackMode', 'single'), 'soundFolder': settings.get('soundFolder', ''),
            'lastEvent': read_json(data / 'last-event.json'),
            'ambientEnabled': ambient_settings.get('enabled', False),
            'ambientMuted': ambient_settings.get('muted', False),
            'ambientHotkey': ambient_settings.get('hotkey', ambient.DEFAULT_HOTKEY),
            'ambientAssignments': len(ambient_settings.get('assignments', {})),
            'ambientStatus': read_json(data / 'ambient-status.json')}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--codex-home', type=Path, default=Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))))
    parser.add_argument('--report', type=Path)
    parser.add_argument('action', choices=['setup', 'settings', 'serve', 'status', 'mute', 'unmute',
                                           'ambient-on', 'ambient-off', 'ambient-mute', 'ambient-unmute',
                                           'ambient-toggle', 'ambient', 'disconnect', 'notify'],
                        nargs='?', default='settings')
    parser.add_argument('payload', nargs='?')
    args = parser.parse_args()
    codex_home = args.codex_home.resolve()
    data = codex_home / 'notification-sounds'
    notify.ROOT = data
    try:
        if args.action == 'ambient':
            import ambient
            ambient.ROOT = data
            return ambient.run(codex_home)
        if args.action == 'notify':
            installation = read_json(data / 'plugin-install.json', {})
            plugins = tomlkit.parse((codex_home / 'config.toml').read_text(encoding='utf-8-sig')).get('plugins', {})
            allowed = any(plugins.get(key, {}).get('enabled', False) for key in installation.get('pluginIds', []))
            return notify.main(args.payload, allow_sound=allowed) if args.payload else 1
        if args.action == 'setup':
            result = install(codex_home)
        elif args.action == 'disconnect':
            result = disconnect(codex_home)
        elif args.action in ('mute', 'unmute'):
            settings = read_json(data / 'sounds.json')
            if settings is None:
                raise ValueError('Run setup before changing sound settings')
            settings['enabled'] = args.action == 'unmute'
            atomic_json(data / 'sounds.json', settings)
            result = status(codex_home)
        elif args.action in ('ambient-on', 'ambient-off'):
            ambient_settings = read_json(data / 'ambient.json')
            installation = read_json(data / 'plugin-install.json', {})
            if ambient_settings is None or not installation.get('executable'):
                raise ValueError('Run setup before changing project soundscapes')
            ambient_settings['enabled'] = args.action == 'ambient-on'
            atomic_json(data / 'ambient.json', ambient_settings)
            configure_ambient_background(codex_home, installation['executable'], ambient_settings['enabled'])
            result = status(codex_home)
        elif args.action in ('ambient-mute', 'ambient-unmute', 'ambient-toggle'):
            ambient_settings = read_json(data / 'ambient.json')
            if ambient_settings is None:
                raise ValueError('Run setup before changing project soundscapes')
            if args.action == 'ambient-toggle':
                ambient_settings['muted'] = not ambient_settings.get('muted', False)
            else:
                ambient_settings['muted'] = args.action == 'ambient-mute'
            atomic_json(data / 'ambient.json', ambient_settings)
            result = status(codex_home)
        elif args.action == 'settings':
            from web_settings import open_panel
            result = open_panel(codex_home)
        elif args.action == 'serve':
            from web_settings import serve
            serve(codex_home)
            return 0
        else:
            result = status(codex_home)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(args.report, result)
        elif sys.stdout:
            print(json.dumps(result))
        return 0
    except Exception as error:
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(args.report, {'error': str(error)})
        elif args.action == 'settings':
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror('Codex Sounds', str(error), parent=root)
            root.destroy()
        elif sys.stderr:
            print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
