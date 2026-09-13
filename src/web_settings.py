"""Private loopback settings panel, opened by the plugin inside Codex."""
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

import notify
import ambient
import audio_import
from app import atomic_json, configure_ambient_background, read_json


def current(data, codex_home):
    config = read_json(data / 'sounds.json')
    ambient_config = read_json(data / 'ambient.json', ambient.default_settings())
    projects, selected_project_id = ambient.project_snapshot(codex_home)
    machine = os.environ.get('COMPUTERNAME', '')
    ambient_status = read_json(data / 'ambient-status.json', {})
    assignments = ambient.enrich_assignment_identities(ambient_config.get('assignments', {}), projects)
    name = next((v for k, v in config.get('machines', {}).items() if k.casefold() == machine.casefold()), config['defaultSound'])
    return {'machine': machine, 'enabled': config.get('enabled', True), 'volume': config.get('volume', 100),
            'mode': config.get('playbackMode', 'single'), 'folder': config.get('soundFolder', ''),
            'sound': name, 'sounds': config['sounds'], 'projects': projects,
            'selectedProjectId': selected_project_id,
            'ambient': {'enabled': ambient_config.get('enabled', False),
                        'muted': ambient_config.get('muted', False),
                        'hotkey': ambient_config.get('hotkey', ambient.DEFAULT_HOTKEY),
                        'volume': ambient_config.get('volume', 24),
                        'assignments': assignments,
                        'status': ambient_status if isinstance(ambient_status, dict) else {}}}


def candidate(data, values):
    config = read_json(data / 'sounds.json')
    if not isinstance(values.get('enabled'), bool):
        raise ValueError('Enabled must be true or false')
    notify.validate_volume(values.get('volume'))
    if values.get('mode') not in ('single', 'folder'):
        raise ValueError('Choose single sound or folder cycling')
    name = values.get('sound')
    custom = values.get('customFile')
    if custom:
        if not isinstance(custom, str):
            raise ValueError('Choose an audio file')
        path = Path(custom)
        notify.validate_sound(path)
        stem = path.stem
        name = stem
        suffix = 2
        while name in config['sounds'] and config['sounds'][name] != str(path):
            name = f'{stem} {suffix}'
            suffix += 1
        config['sounds'][name] = str(path)
    if not isinstance(name, str) or name not in config['sounds']:
        raise ValueError('Choose a sound')
    folder = values.get('folder', '')
    if not isinstance(folder, str):
        raise ValueError('Choose a sound folder')
    machine = os.environ.get('COMPUTERNAME', '')
    machines = config.setdefault('machines', {})
    key = next((k for k in machines if k.casefold() == machine.casefold()), machine)
    machines[key] = name
    config.update(enabled=values['enabled'], volume=values['volume'], playbackMode=values['mode'], soundFolder=folder.strip())
    if config['enabled']:
        _, path = notify.select_sound(config, machine)
        notify.validate_sound(path)
    return config


def ambient_candidate(values):
    if not isinstance(values, dict) or not isinstance(values.get('enabled'), bool):
        raise ValueError('Project soundscapes must be enabled or disabled')
    if not isinstance(values.get('muted'), bool):
        raise ValueError('Soundscape mute must be true or false')
    hotkey, _, _ = ambient.parse_hotkey(values.get('hotkey'))
    notify.validate_volume(values.get('volume'))
    assignments = values.get('assignments', {})
    if not isinstance(assignments, dict) or len(assignments) > 500:
        raise ValueError('Project soundscape assignments are invalid')
    cleaned = {}
    for project_id, assignment in assignments.items():
        if not isinstance(project_id, str) or not project_id or not isinstance(assignment, dict):
            raise ValueError('A project soundscape assignment is invalid')
        mode = assignment.get('mode', 'single')
        if mode not in ('single', 'folder'):
            raise ValueError('Choose a track or folder playlist')
        value = {'mode': mode, 'file': assignment.get('file', '').strip(),
                 'folder': assignment.get('folder', '').strip()}
        project_name = assignment.get('projectName', '')
        project_roots = assignment.get('projectRootPaths', [])
        if not isinstance(project_name, str) or len(project_name) > 500:
            raise ValueError('A project soundscape name is invalid')
        if (not isinstance(project_roots, list) or len(project_roots) > 50 or
                any(not isinstance(root, str) or len(root) > 32768 for root in project_roots)):
            raise ValueError('A project soundscape path is invalid')
        value.update(projectName=project_name, projectRootPaths=project_roots)
        files = ambient.assignment_files(value)
        if mode == 'single':
            ambient.probe_media(files[0])
        cleaned[project_id] = value
    return {'enabled': values['enabled'], 'muted': values['muted'], 'hotkey': hotkey,
            'volume': values['volume'], 'assignments': cleaned}


def open_panel(codex_home):
    data = codex_home / 'notification-sounds'
    installation = read_json(data / 'plugin-install.json')
    if not installation:
        raise ValueError('Run plugin setup on this workstation first')
    executable = installation['executable']
    previous = read_json(data / 'web-session.json', {})
    if previous.get('executable') == executable:
        try:
            with urlopen(previous['url'].replace('/?token=', '/api/health?token='), timeout=1) as response:
                if json.load(response).get('service') == 'codex-sounds':
                    return {'url': previous['url'], 'openIn': 'Codex browser panel'}
        except Exception:
            pass
    previous_token = previous.get('token')
    subprocess.Popen([executable, '--codex-home', str(codex_home), 'serve'],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(100):
        session = read_json(data / 'web-session.json', {})
        if session.get('token') and session.get('token') != previous_token and session.get('executable') == executable:
            return {'url': session['url'], 'openIn': 'Codex browser panel'}
        time.sleep(.1)
    raise ValueError('The settings panel did not start within 10 seconds')


def serve(codex_home):
    data = codex_home / 'notification-sounds'
    notify.ROOT = data
    ambient.ROOT = data
    token = secrets.token_urlsafe(32)
    page = (Path(__file__).parent / 'settings.html').read_bytes()
    last_activity = [time.monotonic()]
    playback_lock = threading.Lock()
    import_job = audio_import.ImportJob()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def authorized(self):
            request = urlsplit(self.path)
            supplied = self.headers.get('X-Codex-Sound-Token') or parse_qs(request.query).get('token', [''])[0]
            origin = f'http://127.0.0.1:{self.server.server_port}'
            return (self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'
                    and self.headers.get('Origin', origin) == origin and hmac.compare_digest(supplied, token))

        def send(self, value, code=200, html=False):
            encoded = value if html else json.dumps(value).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'self'")
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if not self.authorized():
                return self.send({'error': 'This settings link is not valid. Open settings through Codex Sounds again.'}, 403)
            last_activity[0] = time.monotonic()
            route = urlsplit(self.path).path
            if route == '/':
                self.send(page, html=True)
            elif route == '/api/health':
                self.send({'service': 'codex-sounds'})
            elif route == '/api/settings':
                self.send(current(data, codex_home))
            elif route == '/api/import-status':
                status = import_job.snapshot()
                status['installCommands'] = audio_import.install_commands()
                self.send(status)
            else:
                self.send({'error': 'Not found'}, 404)

        def do_POST(self):
            if not self.authorized():
                return self.send({'error': 'Settings request denied'}, 403)
            last_activity[0] = time.monotonic()
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 <= size <= 65536:
                    raise ValueError('Settings request is too large')
                values = json.loads(self.rfile.read(size) or '{}')
                route = urlsplit(self.path).path
                if route == '/api/settings':
                    sound_config = candidate(data, values)
                    ambient_config = ambient_candidate(values.get('ambient'))
                    atomic_json(data / 'sounds.json', sound_config)
                    atomic_json(data / 'ambient.json', ambient_config)
                    installation = read_json(data / 'plugin-install.json', {})
                    configure_ambient_background(codex_home, installation['executable'], ambient_config['enabled'])
                    self.send(current(data, codex_home))
                elif route == '/api/preview':
                    config = candidate(data, values)
                    name, path = notify.select_sound(config, os.environ.get('COMPUTERNAME', ''))
                    if not playback_lock.acquire(blocking=False):
                        raise ValueError('A preview is already playing')
                    def play():
                        try:
                            notify.play_sound(path, config['volume'])
                        finally:
                            playback_lock.release()
                    threading.Thread(target=play, daemon=True).start()
                    self.send({'message': 'Previewing ' + name if config['volume'] else 'Preview is silent at 0% volume.'})
                elif route == '/api/import':
                    urls = audio_import.parse_urls(values.get('urls'))
                    destination = audio_import.destination_path(values.get('destination'))
                    self.send(import_job.start_download(urls, destination), 202)
                elif route == '/api/import-tools':
                    self.send(import_job.start_install(), 202)
                elif route == '/api/import-cancel':
                    self.send(import_job.cancel())
                elif route in ('/api/browse-folder', '/api/browse-file',
                               '/api/browse-ambient-folder', '/api/browse-ambient-file',
                               '/api/browse-import-folder'):
                    import tkinter as tk
                    from tkinter import filedialog
                    root = tk.Tk()
                    root.withdraw()
                    root.attributes('-topmost', True)
                    try:
                        ambient_source = 'ambient' in route
                        import_source = 'import' in route
                        folder_title = ('Choose MP3 destination folder' if import_source else
                                        'Choose ambient playlist folder' if ambient_source else 'Choose sound folder')
                        chosen = (filedialog.askdirectory(parent=root, title=folder_title)
                                  if route.endswith('folder') else filedialog.askopenfilename(parent=root,
                                  title='Choose ambient track' if ambient_source else 'Choose notification sound',
                                  filetypes=[('WAV and MP3', '*.wav *.mp3')]))
                    finally:
                        root.destroy()
                    self.send({'path': chosen})
                else:
                    self.send({'error': 'Not found'}, 404)
            except Exception as error:
                self.send({'error': str(error)}, 400)

    server = HTTPServer(('127.0.0.1', 0), Handler)
    server.timeout = 30
    executable = read_json(data / 'plugin-install.json')['executable']
    url = f'http://127.0.0.1:{server.server_port}/?token={token}'
    atomic_json(data / 'web-session.json', {'url': url, 'token': token, 'executable': executable, 'pid': os.getpid()})
    try:
        while time.monotonic() - last_activity[0] < 7200 or import_job.snapshot()['running']:
            server.handle_request()
    finally:
        server.server_close()
