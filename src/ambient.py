"""Project-aware ambient audio for the Codex Windows app."""
from contextlib import closing
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid

import notify


ROOT = Path(__file__).resolve().parent
MEDIA_EXTENSIONS = {'.wav', '.mp3'}
POLL_SECONDS = 0.4
ERROR_ALREADY_EXISTS = 183
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PROCESS_PATH = 32768
DEFAULT_HOTKEY = 'Ctrl+Alt+P'
LEGACY_DEFAULT_HOTKEYS = {'Ctrl+Alt+Shift+M'}
HOTKEY_ID = 0x4353
WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_user32 = ctypes.WinDLL('user32', use_last_error=True)
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = wintypes.BOOL
_user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                 wintypes.UINT, wintypes.UINT, wintypes.UINT]
_user32.PeekMessageW.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]


_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_kernel32.Process32FirstW.restype = wintypes.BOOL
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_kernel32.Process32NextW.restype = wintypes.BOOL


def atomic_json(path, value):
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    temporary.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default


def default_settings():
    return {'enabled': False, 'muted': False, 'hotkey': DEFAULT_HOTKEY,
            'volume': 24, 'assignments': {}}


def migrate_default_hotkey(value):
    if not value or value in LEGACY_DEFAULT_HOTKEYS:
        return DEFAULT_HOTKEY
    return value


def load_settings():
    value = read_json(ROOT / 'ambient.json', default_settings())
    return value if isinstance(value, dict) else default_settings()


def expanded_path(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Choose an ambient audio source')
    path = Path(os.path.expandvars(value.strip()))
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def assignment_files(assignment):
    if not isinstance(assignment, dict):
        raise ValueError('The project soundscape is invalid')
    mode = assignment.get('mode', 'single')
    if mode == 'single':
        path = expanded_path(assignment.get('file', ''))
        if path.suffix.lower() not in MEDIA_EXTENSIONS or not path.is_file():
            raise ValueError('Choose an existing WAV or MP3 track')
        return [path]
    if mode != 'folder':
        raise ValueError('Choose a track or folder playlist')
    folder = expanded_path(assignment.get('folder', ''))
    if not folder.is_dir():
        raise ValueError('The ambient audio folder is unavailable')
    files = sorted((path.resolve() for path in folder.iterdir()
                    if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS),
                   key=lambda path: (path.name.casefold(), path.name))
    if not files:
        raise ValueError('The ambient audio folder contains no WAV or MP3 files')
    return files


def normalized_root(value):
    if not isinstance(value, str) or not value.strip():
        return None
    return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expandvars(value.strip()))))


def active_project_id(state, projects):
    """Use the current selection; workspace roots can outlive a task switch."""
    valid_ids = {project['id'] for project in projects}
    if 'selected-project' in state:
        selected = state['selected-project']
        if not isinstance(selected, dict) or selected.get('type') != 'local':
            return None
        selected_id = selected.get('projectId')
        return selected_id if isinstance(selected_id, str) and selected_id in valid_ids else None

    # Older Codex builds did not persist an explicit selection. Only use roots
    # when the field is absent, never to override an empty or unknown selection.
    active_roots = {root for value in state.get('active-workspace-roots', [])
                    if (root := normalized_root(value))}
    if not active_roots:
        return None
    projectless = state.get('thread-projectless-output-directories', {})
    if isinstance(projectless, dict):
        projectless_roots = {root for value in projectless.values()
                             if (root := normalized_root(value))}
        if active_roots & projectless_roots:
            return None
    matches = []
    for project in projects:
        roots = {root for value in project.get('rootPaths', [])
                 if (root := normalized_root(value))}
        if active_roots & roots:
            matches.append(project['id'])
    return matches[0] if len(matches) == 1 else None


def project_snapshot(codex_home):
    """Return saved local projects and the project owning the active task."""
    state = read_json(codex_home / '.codex-global-state.json', {})
    if not isinstance(state, dict):
        state = {}
    projects = state.get('local-projects', {}) if isinstance(state, dict) else {}
    if not isinstance(projects, dict):
        projects = {}
    result = []
    for key, project in projects.items():
        if not isinstance(project, dict):
            continue
        project_id = project.get('id') or key
        name = project.get('name') or 'Unnamed project'
        roots = project.get('rootPaths', [])
        result.append({'id': project_id, 'name': name,
                       'rootPaths': roots if isinstance(roots, list) else []})
    result.sort(key=lambda item: (item['name'].casefold(), item['id']))
    return result, active_project_id(state, result)


def assignment_for_project(project_id, projects, assignments):
    """Resolve an assignment after a local project ID changes."""
    if not project_id or not isinstance(assignments, dict):
        return None
    exact = assignments.get(project_id)
    if isinstance(exact, dict):
        return exact
    project = next((item for item in projects if item.get('id') == project_id), None)
    if not project:
        return None
    # Identity recovery is only for removed/migrated project IDs. A live
    # project's assignment must not apply to another project sharing a root.
    valid_ids = {item['id'] for item in projects}
    orphaned = [value for key, value in assignments.items()
                if key not in valid_ids and isinstance(value, dict)]
    roots = {root for value in project.get('rootPaths', [])
             if (root := normalized_root(value))}
    by_root = []
    for assignment in orphaned:
        saved_roots = {root for value in assignment.get('projectRootPaths', [])
                       if (root := normalized_root(value))}
        if roots & saved_roots:
            by_root.append(assignment)
    if len(by_root) == 1:
        return by_root[0]
    name = project.get('name')
    if isinstance(name, str) and name:
        by_name = [assignment for assignment in orphaned
                   if isinstance(assignment.get('projectName'), str)
                   and assignment['projectName'].casefold() == name.casefold()]
        if len(by_name) == 1:
            return by_name[0]
    return None


def enrich_assignment_identities(assignments, projects):
    """Attach stable matching data to assignments that still have a valid local ID."""
    project_by_id = {project['id']: project for project in projects}
    result = {}
    for project_id, assignment in assignments.items() if isinstance(assignments, dict) else ():
        if not isinstance(assignment, dict):
            continue
        value = dict(assignment)
        project = project_by_id.get(project_id)
        if project:
            value.setdefault('projectName', project.get('name', ''))
            value.setdefault('projectRootPaths', project.get('rootPaths', []))
        result[project_id] = value
    return result


def is_codex_desktop_image(value):
    """Recognize the desktop host without mistaking a standalone Codex CLI for it."""
    if not isinstance(value, str):
        return False
    path = value.replace('/', '\\').casefold()
    name = path.rsplit('\\', 1)[-1]
    if name not in ('chatgpt.exe', 'codex.exe'):
        return False
    return ('\\windowsapps\\openai.codex_' in path or
            ('\\openai\\codex\\' in path and '\\bin\\' not in path))


def process_image_path(process_id):
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(MAX_PROCESS_PATH)
        buffer = ctypes.create_unicode_buffer(size.value)
        return buffer.value if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer,
                                                                     ctypes.byref(size)) else None
    finally:
        _kernel32.CloseHandle(handle)


def codex_desktop_is_running():
    """Return true only while the Codex desktop host process exists."""
    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return False
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        found = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.szExeFile.casefold() in ('chatgpt.exe', 'codex.exe'):
                if is_codex_desktop_image(process_image_path(entry.th32ProcessID)):
                    return True
            found = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return False
    finally:
        _kernel32.CloseHandle(snapshot)


def parse_hotkey(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Choose a global soundscape shortcut')
    names = {'ctrl': ('Ctrl', MOD_CONTROL), 'control': ('Ctrl', MOD_CONTROL),
             'alt': ('Alt', MOD_ALT), 'shift': ('Shift', MOD_SHIFT), 'win': ('Win', MOD_WIN),
             'windows': ('Win', MOD_WIN)}
    modifiers = {}
    key = None
    virtual_key = None
    for raw_part in value.split('+'):
        part = raw_part.strip()
        lowered = part.casefold()
        if lowered in names:
            canonical, flag = names[lowered]
            if canonical in modifiers:
                raise ValueError('The soundscape shortcut repeats a modifier')
            modifiers[canonical] = flag
            continue
        if key is not None:
            raise ValueError('The soundscape shortcut must contain one key')
        upper = part.upper()
        if len(upper) == 1 and upper in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789':
            key, virtual_key = upper, ord(upper)
        elif upper.startswith('F') and upper[1:].isdigit() and 1 <= int(upper[1:]) <= 24:
            key, virtual_key = upper, 0x70 + int(upper[1:]) - 1
        else:
            raise ValueError('Use a letter, number, or F1 to F24 for the soundscape shortcut')
    if not modifiers or key is None:
        raise ValueError('The soundscape shortcut needs a modifier and one key')
    order = ('Ctrl', 'Alt', 'Shift', 'Win')
    canonical = '+'.join([name for name in order if name in modifiers] + [key])
    mask = MOD_NOREPEAT
    for flag in modifiers.values():
        mask |= flag
    return canonical, mask, virtual_key


class GlobalHotkey:
    def __init__(self):
        self.spec = None
        self.registered = False
        self.error = None

    def configure(self, spec):
        if spec == self.spec:
            return
        self.close()
        self.spec = spec
        try:
            canonical, modifiers, virtual_key = parse_hotkey(spec)
            message = wintypes.MSG()
            _user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            ctypes.set_last_error(0)
            if not _user32.RegisterHotKey(None, HOTKEY_ID, modifiers, virtual_key):
                error = ctypes.get_last_error()
                raise ValueError(f'{canonical} is already in use or Windows rejected it. Error {error}.')
            self.spec = canonical
            self.registered = True
            self.error = None
        except ValueError as error:
            self.error = str(error)

    def presses(self):
        if not self.registered:
            return 0
        message = wintypes.MSG()
        count = 0
        while _user32.PeekMessageW(ctypes.byref(message), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE):
            if message.wParam == HOTKEY_ID:
                count += 1
        return count

    def close(self):
        if self.registered:
            _user32.UnregisterHotKey(None, HOTKEY_ID)
        self.registered = False


def plugin_is_enabled(codex_home):
    import tomlkit
    installation = read_json(ROOT / 'plugin-install.json', {})
    plugin_ids = installation.get('pluginIds', []) if isinstance(installation, dict) else []
    try:
        plugins = tomlkit.parse((codex_home / 'config.toml').read_text(encoding='utf-8-sig')).get('plugins', {})
        return any(plugins.get(key, {}).get('enabled', False) for key in plugin_ids)
    except (OSError, ValueError, TypeError):
        return False


class MediaTrack:
    def __init__(self, path, volume):
        notify.validate_volume(volume)
        self.path = Path(path).resolve(strict=True)
        if self.path.suffix.lower() not in MEDIA_EXTENSIONS:
            raise ValueError('Choose a WAV or MP3 track')
        text = str(self.path)
        if any(character in text for character in ('"', '\r', '\n')):
            raise ValueError('The audio filename contains unsupported characters')
        self.alias = 'codexambient' + uuid.uuid4().hex
        self.closed = False
        notify.mci(f'open "{text}" type mpegvideo alias {self.alias}')
        try:
            notify.mci(f'set {self.alias} time format milliseconds')
            self.length = int(notify.mci(f'status {self.alias} length'))
            if self.length <= 0:
                raise ValueError('The ambient track is empty')
            self.set_volume(volume)
        except Exception:
            self.close()
            raise

    def set_volume(self, volume):
        notify.validate_volume(volume)
        notify.mci(f'setaudio {self.alias} volume to {round(volume * 10)}')

    def play(self, position=0):
        position = max(0, min(int(position), max(0, self.length - 250)))
        notify.mci(f'play {self.alias} from {position}')

    def pause(self):
        if self.mode() == 'playing':
            notify.mci(f'pause {self.alias}')

    def resume(self):
        if self.mode() == 'paused':
            notify.mci(f'resume {self.alias}')

    def mode(self):
        return notify.mci(f'status {self.alias} mode').strip().casefold()

    def position(self):
        return int(notify.mci(f'status {self.alias} position'))

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                notify.mci(f'close {self.alias}')
            except ValueError:
                pass


def probe_media(path):
    track = MediaTrack(path, 0)
    track.close()


class PositionStore:
    def __init__(self, path):
        self.path = path

    def load(self, project_id):
        try:
            with closing(sqlite3.connect(self.path, timeout=2)) as connection, connection:
                connection.execute('CREATE TABLE IF NOT EXISTS positions '
                                   '(project_id TEXT PRIMARY KEY, path TEXT NOT NULL, position_ms INTEGER NOT NULL, updated REAL NOT NULL)')
                row = connection.execute('SELECT path, position_ms FROM positions WHERE project_id = ?',
                                         (project_id,)).fetchone()
            return row if row else (None, 0)
        except (OSError, sqlite3.Error):
            return None, 0

    def save(self, project_id, path, position):
        if not project_id or not path:
            return
        try:
            with closing(sqlite3.connect(self.path, timeout=2)) as connection, connection:
                connection.execute('CREATE TABLE IF NOT EXISTS positions '
                                   '(project_id TEXT PRIMARY KEY, path TEXT NOT NULL, position_ms INTEGER NOT NULL, updated REAL NOT NULL)')
                connection.execute('INSERT INTO positions VALUES (?, ?, ?, ?) '
                                   'ON CONFLICT(project_id) DO UPDATE SET path=excluded.path, '
                                   'position_ms=excluded.position_ms, updated=excluded.updated',
                                   (project_id, str(path), max(0, int(position)), time.time()))
        except (OSError, sqlite3.Error):
            pass


class AmbientController:
    def __init__(self, codex_home):
        self.codex_home = codex_home
        self.store = PositionStore(ROOT / 'ambient-positions.sqlite')
        self.project_id = None
        self.assignment_key = None
        self.files = []
        self.assignment = None
        self.index = 0
        self.track = None
        self.active = False
        self.system_volume = 1.0
        self.effective_volume = 0.0
        self.last_saved = 0
        self.last_error = None
        self.retry_at = 0

    def save_position(self):
        if not self.track or not self.project_id:
            return
        try:
            self.store.save(self.project_id, self.track.path, self.track.position())
        except ValueError:
            pass
        self.last_saved = time.monotonic()

    def close_track(self, remember=True):
        if self.track:
            if remember:
                self.save_position()
            self.track.close()
        self.track = None
        self.active = False

    def choose_saved_position(self, project_id, files):
        saved_path, position = self.store.load(project_id)
        if saved_path:
            saved = Path(saved_path)
            for index, path in enumerate(files):
                if str(path).casefold() == str(saved).casefold():
                    return index, position
        return 0, 0

    def open_track(self, position=0):
        attempts = len(self.files)
        while attempts:
            path = self.files[self.index]
            try:
                self.track = MediaTrack(path, self.volume)
                self.track.play(position)
                self.active = True
                self.last_error = None
                return
            except (OSError, ValueError):
                if self.track:
                    self.track.close()
                self.track = None
                self.index = (self.index + 1) % len(self.files)
                position = 0
                attempts -= 1
        self.last_error = 'No playable WAV or MP3 files were found for this project.'
        self.retry_at = time.monotonic() + 10

    def activate(self, project_id, assignment, volume):
        key = json.dumps(assignment, sort_keys=True)
        changed = project_id != self.project_id or key != self.assignment_key
        self.volume = volume
        if changed:
            self.close_track()
            self.project_id = project_id
            self.assignment_key = key
            self.assignment = assignment
            self.files = assignment_files(assignment)
            self.index, position = self.choose_saved_position(project_id, self.files)
            self.open_track(position)
            return
        if not self.track and time.monotonic() >= self.retry_at:
            self.open_track()
            return
        if self.track:
            self.track.set_volume(volume)
            if self.track.mode() == 'paused':
                self.track.resume()
            self.active = True

    def pause(self):
        if self.track and self.active:
            self.track.pause()
            self.save_position()
        self.active = False

    def suspend(self):
        """Release the media handle while preserving the saved resume position."""
        self.close_track()
        self.project_id = None
        self.assignment_key = None
        self.files = []
        self.assignment = None
        self.index = 0

    def advance_if_finished(self):
        if not self.track or not self.active or self.track.mode() != 'stopped':
            return
        current_path = self.track.path
        self.close_track(remember=False)
        refreshed = assignment_files(self.assignment)
        current_index = next((index for index, path in enumerate(refreshed)
                              if str(path).casefold() == str(current_path).casefold()), -1)
        self.files = refreshed
        self.index = (current_index + 1) % len(self.files)
        self.store.save(self.project_id, self.files[self.index], 0)
        self.open_track()

    def status(self, selected_project=None, settings=None, hotkey=None, codex_running=False):
        settings = settings if isinstance(settings, dict) else {}
        return {'pid': os.getpid(), 'executable': str(Path(sys_executable()).resolve()),
                'projectId': self.project_id, 'selectedProjectId': selected_project,
                'playing': bool(self.track and self.active),
                'track': self.track.path.name if self.track else None,
                'codexRunning': bool(codex_running),
                'muted': settings.get('muted', False),
                'systemVolume': round(self.system_volume * 100),
                'effectiveVolume': round(self.effective_volume, 2),
                'hotkey': settings.get('hotkey', DEFAULT_HOTKEY),
                'hotkeyRegistered': bool(hotkey and hotkey.registered),
                'hotkeyError': hotkey.error if hotkey else None,
                'error': self.last_error, 'updated': time.time()}

    def sync_playback(self, settings):
        codex_running = codex_desktop_is_running()
        projects, selected_id = project_snapshot(self.codex_home)
        assignments = settings.get('assignments', {}) if isinstance(settings.get('assignments', {}), dict) else {}
        assignment = assignment_for_project(selected_id, projects, assignments)
        plugin_enabled = plugin_is_enabled(self.codex_home)
        should_play = (codex_running and settings.get('enabled', False) and plugin_enabled
                       and not settings.get('muted', False) and isinstance(assignment, dict))
        try:
            if should_play:
                configured_volume = settings.get('volume', 24)
                notify.validate_volume(configured_volume)
                self.system_volume = notify.system_volume_scalar()
                self.effective_volume = notify.effective_volume(
                    configured_volume, self.system_volume)
                self.activate(selected_id, assignment, self.effective_volume)
                self.advance_if_finished()
                if self.active and time.monotonic() - self.last_saved >= 5:
                    self.save_position()
            elif not codex_running or not plugin_enabled:
                self.effective_volume = 0.0
                self.suspend()
            else:
                self.effective_volume = 0.0
                self.pause()
        except (OSError, ValueError) as error:
            self.effective_volume = 0.0
            self.close_track()
            self.last_error = str(error)
            self.retry_at = time.monotonic() + 10
        return selected_id, codex_running

    def run(self):
        session_token = uuid.uuid4().hex
        atomic_json(ROOT / 'ambient-session.json', {'pid': os.getpid(), 'token': session_token,
                                                    'executable': str(Path(sys_executable()).resolve())})
        selected_id = None
        codex_running = False
        settings = load_settings()
        hotkey = GlobalHotkey()
        try:
            while True:
                control = read_json(ROOT / 'ambient-control.json', {})
                if isinstance(control, dict) and control.get('stopToken') == session_token:
                    break
                settings = load_settings()
                hotkey.configure(settings.get('hotkey', DEFAULT_HOTKEY))
                if hotkey.presses() % 2:
                    settings['muted'] = not settings.get('muted', False)
                    atomic_json(ROOT / 'ambient.json', settings)
                selected_id, codex_running = self.sync_playback(settings)
                atomic_json(ROOT / 'ambient-status.json', self.status(selected_id, settings, hotkey,
                                                                         codex_running))
                time.sleep(POLL_SECONDS)
        finally:
            self.close_track()
            hotkey.close()
            session = read_json(ROOT / 'ambient-session.json', {})
            if isinstance(session, dict) and session.get('token') == session_token:
                (ROOT / 'ambient-session.json').unlink(missing_ok=True)
            atomic_json(ROOT / 'ambient-status.json', self.status(selected_id, settings, hotkey,
                                                                     codex_running))


def sys_executable():
    import sys
    return sys.executable


def process_exists(process_id):
    if not isinstance(process_id, int) or process_id <= 0:
        return False
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
    if not handle:
        return False
    _kernel32.CloseHandle(handle)
    return True


def ensure_running(codex_home, executable):
    session = read_json(ROOT / 'ambient-session.json', {})
    if isinstance(session, dict) and process_exists(session.get('pid')):
        if str(session.get('executable', '')).casefold() == str(Path(executable).resolve()).casefold():
            return False
        stop_running()
    subprocess.Popen([str(executable), '--codex-home', str(codex_home), 'ambient'],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW)
    return True


def stop_running(timeout=3):
    session = read_json(ROOT / 'ambient-session.json', {})
    if not isinstance(session, dict) or not process_exists(session.get('pid')):
        (ROOT / 'ambient-session.json').unlink(missing_ok=True)
        return False
    atomic_json(ROOT / 'ambient-control.json', {'stopToken': session.get('token')})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process_exists(session.get('pid')):
        time.sleep(0.1)
    return True


def run(codex_home):
    name = 'Local\\CodexSoundsAmbient-' + hashlib.sha256(str(codex_home).casefold().encode()).hexdigest()[:20]
    mutex = _kernel32.CreateMutexW(None, False, name)
    if not mutex:
        return 1
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(mutex)
        return 0
    try:
        AmbientController(codex_home).run()
        return 0
    finally:
        _kernel32.CloseHandle(mutex)
