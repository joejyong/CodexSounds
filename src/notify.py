"""Codex notify callback: forward the original event and play local audio."""
from contextlib import contextmanager, closing
import ctypes
from ctypes import wintypes
import json
import io
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import time
import uuid
import wave
import winsound

ROOT = Path(__file__).resolve().parent
SOUND_EXTENSIONS = {'.wav', '.mp3'}
_winmm = ctypes.WinDLL('winmm')
_winmm.mciSendStringW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.UINT, wintypes.HWND]
_winmm.mciSendStringW.restype = wintypes.DWORD
_winmm.mciGetErrorStringW.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.UINT]
_winmm.mciGetErrorStringW.restype = wintypes.BOOL
_ole32 = ctypes.WinDLL('ole32')


class GUID(ctypes.Structure):
    _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]

    @classmethod
    def from_string(cls, value):
        data = uuid.UUID(value).bytes_le
        return cls(int.from_bytes(data[:4], 'little'), int.from_bytes(data[4:6], 'little'),
                   int.from_bytes(data[6:8], 'little'), (ctypes.c_ubyte * 8)(*data[8:]))


CLSID_MMDEVICE_ENUMERATOR = GUID.from_string('bcde0395-e52f-467c-8e3d-c4579291692e')
IID_IMMDEVICE_ENUMERATOR = GUID.from_string('a95664d2-9614-4f35-a746-de8db63617e6')
IID_IAUDIO_ENDPOINT_VOLUME = GUID.from_string('5cdf2c82-841e-4546-9722-0cf74078229a')
CLSCTX_ALL = 23
COINIT_APARTMENTTHREADED = 0x2
RPC_E_CHANGED_MODE = -2147417850
E_RENDER = 0
E_MULTIMEDIA = 1

_ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_ole32.CoInitializeEx.restype = ctypes.c_long
_ole32.CoUninitialize.argtypes = []
_ole32.CoUninitialize.restype = None
_ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
_ole32.CoCreateInstance.restype = ctypes.c_long


def _check_hresult(result):
    if result < 0:
        raise OSError(result, 'Windows Core Audio request failed')


def _com_method(interface, index, result_type, *argument_types):
    table = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(result_type, ctypes.c_void_p, *argument_types)(table[index])


def _release_com(interface):
    if interface:
        _com_method(interface, 2, wintypes.ULONG)(interface)


def _read_system_volume_scalar():
    """Read the default multimedia output's master volume and mute state."""
    initialized = _ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if initialized < 0 and initialized != RPC_E_CHANGED_MODE:
        _check_hresult(initialized)
    enumerator = ctypes.c_void_p()
    endpoint = ctypes.c_void_p()
    endpoint_volume = ctypes.c_void_p()
    try:
        _check_hresult(_ole32.CoCreateInstance(
            ctypes.byref(CLSID_MMDEVICE_ENUMERATOR), None, CLSCTX_ALL,
            ctypes.byref(IID_IMMDEVICE_ENUMERATOR), ctypes.byref(enumerator)))
        get_default_endpoint = _com_method(
            enumerator, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p))
        _check_hresult(get_default_endpoint(
            enumerator, E_RENDER, E_MULTIMEDIA, ctypes.byref(endpoint)))
        activate = _com_method(
            endpoint, 3, ctypes.c_long, ctypes.POINTER(GUID), wintypes.DWORD,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        _check_hresult(activate(
            endpoint, ctypes.byref(IID_IAUDIO_ENDPOINT_VOLUME), CLSCTX_ALL, None,
            ctypes.byref(endpoint_volume)))
        level = ctypes.c_float()
        get_master_level = _com_method(
            endpoint_volume, 9, ctypes.c_long, ctypes.POINTER(ctypes.c_float))
        _check_hresult(get_master_level(endpoint_volume, ctypes.byref(level)))
        muted = wintypes.BOOL()
        get_mute = _com_method(
            endpoint_volume, 15, ctypes.c_long, ctypes.POINTER(wintypes.BOOL))
        _check_hresult(get_mute(endpoint_volume, ctypes.byref(muted)))
        return 0.0 if muted.value else max(0.0, min(1.0, float(level.value)))
    finally:
        _release_com(endpoint_volume)
        _release_com(endpoint)
        _release_com(enumerator)
        if initialized >= 0:
            _ole32.CoUninitialize()


def system_volume_scalar():
    """Return 1.0 if Windows cannot report the default output's master level."""
    try:
        return _read_system_volume_scalar()
    except (OSError, ValueError):
        return 1.0


def effective_volume(volume, system_scalar=None):
    validate_volume(volume)
    if system_scalar is None:
        system_scalar = system_volume_scalar()
    if isinstance(system_scalar, bool) or not isinstance(system_scalar, (int, float)):
        raise ValueError('System volume must be between 0 and 1')
    system_scalar = max(0.0, min(1.0, system_scalar))
    return volume * system_scalar


def validate_volume(volume):
    if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not 0 <= volume <= 100:
        raise ValueError('Volume must be between 0 and 100')


def mci(command):
    response = ctypes.create_unicode_buffer(1024)
    result = _winmm.mciSendStringW(command, response, len(response), None)
    if result:
        _winmm.mciGetErrorStringW(result, response, len(response))
        raise ValueError('Windows audio: ' + (response.value or str(result)))
    return response.value


@contextmanager
def mp3_device(path):
    path = str(Path(path).resolve(strict=True))
    if any(character in path for character in ('"', '\r', '\n')):
        raise ValueError('The audio filename contains unsupported characters')
    alias = 'codexsound' + uuid.uuid4().hex
    mci(f'open "{path}" type mpegvideo alias {alias}')
    try:
        mci(f'set {alias} time format milliseconds')
        duration = int(mci(f'status {alias} length'))
        if not 0 < duration <= 60000:
            raise ValueError('Choose an MP3 clip longer than 0 and no longer than 60 seconds')
        yield alias
    finally:
        mci(f'close {alias}')


def validate_sound(path):
    extension = Path(path).suffix.lower()
    if extension == '.mp3':
        with mp3_device(path):
            pass
    elif extension == '.wav':
        sound_bytes(path, 100)
    else:
        raise ValueError('Choose a WAV or MP3 file')


def sound_bytes(path, volume):
    """Scale PCM samples without touching device or session volume."""
    validate_volume(volume)
    with wave.open(str(path), 'rb') as source:
        params = source.getparams()
        if params.comptype != 'NONE' or params.sampwidth not in (1, 2, 3, 4):
            raise ValueError('Choose an uncompressed PCM WAV file')
        if params.nframes > params.framerate * 60:
            raise ValueError('Choose a WAV clip no longer than 60 seconds')
        frames = source.readframes(params.nframes)
        if len(frames) != params.nframes * params.nchannels * params.sampwidth:
            raise ValueError('WAV file is incomplete')
    if volume != 100:
        width = params.sampwidth
        scaled = bytearray(len(frames))
        for offset in range(0, len(frames), width):
            sample = int.from_bytes(frames[offset:offset + width], 'little', signed=width != 1)
            if width == 1:
                sample -= 128
            sample = round(sample * volume / 100)
            if width == 1:
                sample += 128
            scaled[offset:offset + width] = sample.to_bytes(width, 'little', signed=width != 1)
        frames = scaled
    output = io.BytesIO()
    with wave.open(output, 'wb') as destination:
        destination.setparams(params)
        destination.writeframes(frames)
    return output.getvalue()


def play_sound(path, volume=100, system_scalar=None):
    volume = effective_volume(volume, system_scalar)
    if Path(path).suffix.lower() == '.mp3':
        with mp3_device(path) as alias:
            if volume == 0:
                return False
            mci(f'setaudio {alias} volume to {round(volume * 10)}')
            mci(f'play {alias} from 0 wait')
        return True
    data = sound_bytes(path, volume)
    if volume == 0:
        return False
    winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
    return True


def read_json(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8-sig"))


def folder_sounds(config):
    value = config.get('soundFolder', '').strip()
    if not value:
        raise ValueError('Choose a folder containing WAV or MP3 sounds')
    folder = Path(os.path.expandvars(value))
    if not folder.is_absolute():
        folder = ROOT / folder
    if not folder.is_dir():
        raise ValueError('The sound folder is unavailable')
    paths = sorted((path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in SOUND_EXTENSIONS),
                   key=lambda path: (path.name.casefold(), path.name))
    if not paths:
        raise ValueError('The selected folder contains no WAV or MP3 files')
    return folder.resolve(), paths


def next_folder_sound(config, machine, advance=False):
    folder, paths = folder_sounds(config)
    # Each process reserves one position in a short transaction, so simultaneous
    # replies cannot read the same position and overwrite each other's progress.
    connection = sqlite3.connect(ROOT / 'rotation.sqlite', timeout=10)
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS positions (source TEXT PRIMARY KEY, last_name TEXT NOT NULL)')
        connection.execute('BEGIN IMMEDIATE')
        key = machine.casefold() + '\n' + str(folder).casefold()
        row = connection.execute('SELECT last_name FROM positions WHERE source = ?', (key,)).fetchone()
        last = row[0] if row else None
        start = next((i for i, path in enumerate(paths) if last is not None and path.name.casefold() > last), 0)
        for path in paths[start:] + paths[:start]:
            try:
                validate_sound(path)
            except (OSError, ValueError, EOFError, wave.Error):
                continue
            if advance:
                connection.execute('INSERT INTO positions VALUES (?, ?) ON CONFLICT(source) DO UPDATE SET last_name = excluded.last_name',
                                   (key, path.name.casefold()))
            connection.commit()
            return path.name, path
        raise ValueError('The folder contains no playable WAV or MP3 clips of 60 seconds or less')
    finally:
        connection.close()


def select_sound(config, machine, advance=False):
    if config.get('playbackMode', 'single') == 'folder':
        return next_folder_sound(config, machine, advance)
    name = next(
        (value for key, value in config.get("machines", {}).items()
         if key.casefold() == machine.casefold()),
        config["defaultSound"],
    )
    sounds = {key.casefold(): value for key, value in config["sounds"].items()}
    path = Path(os.path.expandvars(sounds[name.casefold()]))
    if not path.is_absolute():
        path = ROOT / path
    if path.suffix.lower() not in SOUND_EXTENSIONS or not path.is_file():
        raise ValueError("Selected sound must be an existing WAV or MP3 file")
    return name, path


def completion_scope(event):
    """The notify payload omits agent origin; read it from local thread metadata."""
    if event.get('type') != 'agent-turn-complete':
        return 'not-complete'
    thread_id, turn_id = event.get('thread-id'), event.get('turn-id')
    if not isinstance(thread_id, str) or not thread_id or not isinstance(turn_id, str) or not turn_id:
        return 'missing-identifiers'
    if 'last-assistant-message' in event and not event['last-assistant-message']:
        return 'no-final-reply'
    databases = sorted(ROOT.parent.glob('state_*.sqlite'),
                       key=lambda path: int(path.stem.split('_')[-1]) if path.stem.split('_')[-1].isdigit() else -1,
                       reverse=True)
    for database in databases:
        try:
            with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as connection, connection:
                row = connection.execute('SELECT source FROM threads WHERE id = ?', (thread_id,)).fetchone()
            if not row:
                continue
            source = row[0]
            try:
                source = json.loads(source)
            except (ValueError, TypeError):
                pass
            if isinstance(source, dict) and 'subagent' in source:
                return 'internal-agent'
            if isinstance(source, str) and 'subagent' in source.casefold():
                return 'internal-agent'
            if isinstance(source, str) and source and source.casefold() != 'unknown':
                return 'user-task'
            return 'unknown-origin'
        except (OSError, sqlite3.Error):
            continue
    # If Codex changes its metadata schema, stay quiet and record the reason.
    return 'unknown-origin'


def claim_completion(event):
    """Reserve a turn across simultaneous callback processes before selecting audio."""
    with closing(sqlite3.connect(ROOT / 'notification-events.sqlite', timeout=10)) as connection, connection:
        connection.execute('CREATE TABLE IF NOT EXISTS completed_turns '
                           '(thread_id TEXT, turn_id TEXT, received REAL, PRIMARY KEY(thread_id, turn_id))')
        result = connection.execute('INSERT OR IGNORE INTO completed_turns VALUES (?, ?, ?)',
                                    (event['thread-id'], event['turn-id'], time.time()))
        claimed = result.rowcount == 1
        connection.execute('DELETE FROM completed_turns WHERE received < ?', (time.time() - 30 * 86400,))
        return claimed


def record_event(report):
    """Keep a bounded diagnostic history containing no conversation text."""
    try:
        with closing(sqlite3.connect(ROOT / 'notification-events.sqlite', timeout=10)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, report TEXT)')
            connection.execute('INSERT INTO history (report) VALUES (?)', (json.dumps(report),))
            connection.execute('DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 500)')
    except (OSError, sqlite3.Error):
        pass


def main(payload, allow_sound=True):
    report = {"timestamp": time.time(), "errors": []}
    # Forward the exact argument, without shell parsing or inspecting its content.
    # A sound failure must not prevent the existing notification helper running.
    try:
        command = read_json("forward-command.json")
        if command:
            subprocess.Popen(
                [*command, payload], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        report["forwarded"] = bool(command)
    except Exception as error:
        report["errors"].append("forward: " + type(error).__name__)
    try:
        event = json.loads(payload)
        report["type"] = event.get("type")
        report["threadId"] = event.get("thread-id")
        report["turnId"] = event.get("turn-id")
        if event.get("type") == "agent-turn-complete":
            config = read_json("sounds.json")
            scope = completion_scope(event) if allow_sound and config.get('enabled', True) else None
            report['scope'] = scope
            if not allow_sound:
                report['pluginDisabled'] = True
            elif not config.get('enabled', True):
                report['suppressed'] = 'muted'
            elif scope != 'user-task':
                report['suppressed'] = scope
            elif not claim_completion(event):
                report['suppressed'] = 'duplicate-completion'
            else:
                volume = config.get("volume", 100)
                system_scalar = system_volume_scalar()
                adjusted_volume = effective_volume(volume, system_scalar)
                machine = os.environ.get("COMPUTERNAME", "")
                try:
                    name, path = select_sound(config, machine, advance=adjusted_volume > 0)
                except Exception:
                    if config.get('playbackMode') != 'folder':
                        raise
                    report['folderFallback'] = True
                    name, path = select_sound({**config, 'playbackMode': 'single'}, machine)
                played = play_sound(path, volume, system_scalar)
                report["sound"] = name
                report["volume"] = volume
                report["systemVolume"] = round(system_scalar * 100)
                report["effectiveVolume"] = round(adjusted_volume, 2)
                report["playbackAccepted"] = played
        else:
            report['suppressed'] = 'not-complete'
    except Exception as error:
        report["errors"].append("sound: " + type(error).__name__)
    record_event(report)
    # Only event identifiers and result status are saved, never conversation text.
    temporary = ROOT / (".last-event-" + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
        os.replace(temporary, ROOT / "last-event.json")
    except OSError:
        temporary.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]) if len(sys.argv) == 2 else 1)
