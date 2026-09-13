"""Background MP3 imports for audio the user is allowed to download."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import threading
from urllib.parse import urlsplit


MAX_URLS = 50
SUPPORTED_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com',
                   'youtu.be', 'www.youtube-nocookie.com'}
YTDLP_PACKAGE = 'yt-dlp.yt-dlp'
FFMPEG_PACKAGE = 'Gyan.FFmpeg'


def parse_urls(value):
    if isinstance(value, str):
        values = value.splitlines()
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        values = value
    else:
        raise ValueError('Paste one YouTube URL per line')
    urls = []
    seen = set()
    for raw in values:
        url = raw.strip()
        if not url:
            continue
        parsed = urlsplit(url)
        host = (parsed.hostname or '').casefold()
        if parsed.scheme != 'https' or host not in SUPPORTED_HOSTS:
            raise ValueError('Only HTTPS YouTube and youtu.be URLs are supported')
        if url not in seen:
            seen.add(url)
            urls.append(url)
    if not urls:
        raise ValueError('Paste at least one YouTube URL')
    if len(urls) > MAX_URLS:
        raise ValueError(f'Import up to {MAX_URLS} URLs at a time')
    return urls


def destination_path(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Choose a destination folder')
    path = Path(os.path.expandvars(value.strip())).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError('The destination must be a folder')
    return path.resolve()


def _tool_candidates(name):
    found = shutil.which(name)
    if found:
        yield Path(found)
    local_value = os.environ.get('LOCALAPPDATA')
    if local_value:
        local = Path(local_value)
        links = local / 'Microsoft' / 'WinGet' / 'Links'
        yield links / (name + '.exe')
        packages = local / 'Microsoft' / 'WinGet' / 'Packages'
        patterns = (['yt-dlp.yt-dlp_*/yt-dlp.exe'] if name == 'yt-dlp' else
                    ['Gyan.FFmpeg_*/ffmpeg-*/bin/ffmpeg.exe'])
        for pattern in patterns:
            yield from packages.glob(pattern)


def find_tool(name):
    for candidate in _tool_candidates(name):
        if candidate.is_file():
            return candidate.resolve()
    return None


def tools_status():
    ytdlp = find_tool('yt-dlp')
    ffmpeg = find_tool('ffmpeg')
    return {'ready': bool(ytdlp and ffmpeg), 'ytDlp': str(ytdlp or ''),
            'ffmpeg': str(ffmpeg or ''), 'winget': bool(shutil.which('winget'))}


def download_command(ytdlp, ffmpeg, destination, url):
    output = str(destination / '%(title).180B [%(id)s].%(ext)s')
    return [str(ytdlp), '--no-playlist', '--extract-audio', '--audio-format', 'mp3',
            '--audio-quality', '128K', '--ffmpeg-location', str(ffmpeg.parent),
            '--windows-filenames', '--no-overwrites', '--newline', '--output', output, url]


def terminate_process_tree(process):
    if process and process.poll() is None:
        subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, check=False)


class ImportJob:
    def __init__(self):
        self._lock = threading.Lock()
        self._process = None
        self._state = self._idle_state()

    @staticmethod
    def _idle_state():
        return {'running': False, 'kind': '', 'phase': 'idle', 'current': 0, 'total': 0,
                'completed': 0, 'failed': 0, 'cancelled': False, 'message': '', 'log': []}

    def snapshot(self):
        with self._lock:
            return {**self._state, 'log': list(self._state['log']), 'tools': tools_status()}

    def _update(self, **values):
        with self._lock:
            self._state.update(values)

    def _log(self, line):
        clean = line.strip()
        if not clean:
            return
        with self._lock:
            self._state['log'] = (self._state['log'] + [clean])[-80:]

    def _begin(self, kind, total, message):
        with self._lock:
            if self._state['running']:
                raise ValueError('Another audio import task is already running')
            self._state = {'running': True, 'kind': kind, 'phase': 'starting', 'current': 0,
                           'total': total, 'completed': 0, 'failed': 0, 'cancelled': False,
                           'message': message, 'log': []}

    def start_download(self, urls, destination):
        status = tools_status()
        if not status['ready']:
            raise ValueError('Install yt-dlp and FFmpeg before importing audio')
        self._begin('download', len(urls), 'Preparing audio import...')
        threading.Thread(target=self._download, args=(urls, destination, status), daemon=True).start()
        return self.snapshot()

    def _download(self, urls, destination, tools):
        completed = failed = 0
        try:
            for index, url in enumerate(urls, 1):
                with self._lock:
                    if self._state['cancelled']:
                        break
                self._update(phase='downloading', current=index,
                             message=f'Downloading {index} of {len(urls)}...')
                command = download_command(Path(tools['ytDlp']), Path(tools['ffmpeg']), destination, url)
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL, text=True, encoding='utf-8',
                                           errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
                with self._lock:
                    self._process = process
                    cancelled = self._state['cancelled']
                if cancelled:
                    terminate_process_tree(process)
                for line in process.stdout:
                    self._log(line)
                code = process.wait()
                with self._lock:
                    self._process = None
                    cancelled = self._state['cancelled']
                if cancelled:
                    break
                if code == 0:
                    completed += 1
                else:
                    failed += 1
                self._update(completed=completed, failed=failed)
            cancelled = self.snapshot()['cancelled']
            if cancelled:
                message = f'Import cancelled. {completed} file(s) completed.'
                phase = 'cancelled'
            elif failed:
                message = f'Import finished. {completed} completed, {failed} failed.'
                phase = 'finished-with-errors'
            else:
                message = f'Import finished. {completed} MP3 file(s) saved to {destination}.'
                phase = 'finished'
            self._update(running=False, phase=phase, completed=completed, failed=failed,
                         message=message)
        except Exception as error:
            self._log(str(error))
            self._update(running=False, phase='error', failed=failed + 1,
                         message='Audio import failed: ' + str(error))
        finally:
            with self._lock:
                self._process = None

    def start_install(self):
        if not shutil.which('winget'):
            raise ValueError('Windows Package Manager is not available on this PC')
        self._begin('install', 2, 'Installing yt-dlp and FFmpeg...')
        threading.Thread(target=self._install, daemon=True).start()
        return self.snapshot()

    def _install(self):
        try:
            packages = ((YTDLP_PACKAGE, 'yt-dlp'), (FFMPEG_PACKAGE, 'FFmpeg'))
            for index, (package, label) in enumerate(packages, 1):
                with self._lock:
                    if self._state['cancelled']:
                        break
                self._update(phase='installing', current=index, message=f'Installing {label}...')
                command = ['winget', 'install', '--id', package, '--exact', '--silent',
                           '--accept-package-agreements', '--accept-source-agreements',
                           '--disable-interactivity']
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL, text=True, encoding='utf-8',
                                           errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
                with self._lock:
                    self._process = process
                    cancelled = self._state['cancelled']
                if cancelled:
                    terminate_process_tree(process)
                for line in process.stdout:
                    self._log(line)
                code = process.wait()
                with self._lock:
                    self._process = None
                    cancelled = self._state['cancelled']
                if cancelled:
                    break
                if code != 0:
                    raise RuntimeError(f'{label} installer exited with code {code}')
                self._update(completed=index)
            cancelled = self.snapshot()['cancelled']
            ready = tools_status()['ready']
            if cancelled:
                self._update(running=False, phase='cancelled', message='Tool installation cancelled.')
            elif ready:
                self._update(running=False, phase='finished', completed=2,
                             message='yt-dlp and FFmpeg are ready.')
            else:
                self._update(running=False, phase='error',
                             message='Installation finished, but the tools could not be found. Reopen Codex and try again.')
        except Exception as error:
            self._log(str(error))
            self._update(running=False, phase='error', message='Tool installation failed: ' + str(error))
        finally:
            with self._lock:
                self._process = None

    def cancel(self):
        with self._lock:
            if not self._state['running']:
                idle = True
                process = None
            else:
                idle = False
                self._state['cancelled'] = True
                self._state['message'] = 'Cancelling...'
                process = self._process
        if idle:
            return self.snapshot()
        terminate_process_tree(process)
        return self.snapshot()


def install_commands():
    return ['winget install --id yt-dlp.yt-dlp --exact',
            'winget install --id Gyan.FFmpeg --exact']
