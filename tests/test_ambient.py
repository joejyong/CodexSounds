from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import ambient
import app
import web_settings


class AmbientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_active_workspace_wins_when_selected_project_is_stale(self):
        state = {
            'local-projects': {
                'b': {'id': 'b', 'name': 'Zulu', 'rootPaths': ['C:\\Zulu']},
                'a': {'id': 'a', 'name': 'Alpha', 'rootPaths': ['C:\\Alpha']},
            },
            'selected-project': {'type': 'local', 'projectId': 'a'},
            'active-workspace-roots': ['C:\\Zulu'],
        }
        (self.root / '.codex-global-state.json').write_text(json.dumps(state), encoding='utf-8')
        projects, selected = ambient.project_snapshot(self.root)
        self.assertEqual([project['id'] for project in projects], ['a', 'b'])
        self.assertEqual(selected, 'b')

    def test_bundle_publish_retries_a_temporary_windows_lock(self):
        staging = self.root / 'staging'
        destination = self.root / 'destination'
        staging.mkdir()
        with patch.object(app.os, 'replace', side_effect=[PermissionError(), None]) as replace, \
             patch.object(app.time, 'sleep') as sleep:
            app.publish_directory(staging, destination, attempts=2)
        self.assertEqual(replace.call_count, 2)
        sleep.assert_called_once_with(0.1)

    def test_no_active_workspace_stays_silent_even_with_selected_project(self):
        state = {'local-projects': {'a': {'id': 'a', 'name': 'Alpha', 'rootPaths': ['C:\\Alpha']}},
                 'selected-project': {'type': 'local', 'projectId': 'a'},
                 'active-workspace-roots': []}
        (self.root / '.codex-global-state.json').write_text(json.dumps(state), encoding='utf-8')
        self.assertIsNone(ambient.project_snapshot(self.root)[1])

    def test_projectless_workspace_stays_silent_instead_of_using_stale_project(self):
        state = {'local-projects': {'a': {'id': 'a', 'name': 'Alpha', 'rootPaths': ['C:\\Alpha']}},
                 'selected-project': {'type': 'local', 'projectId': 'a'},
                 'active-workspace-roots': ['C:\\Standalone'],
                 'thread-projectless-output-directories': {'thread': 'C:\\Standalone'}}
        (self.root / '.codex-global-state.json').write_text(json.dumps(state), encoding='utf-8')
        self.assertIsNone(ambient.project_snapshot(self.root)[1])

    def test_selected_project_is_fallback_for_unlisted_worktree_root(self):
        state = {'local-projects': {'a': {'id': 'a', 'name': 'Alpha', 'rootPaths': ['C:\\Alpha']}},
                 'selected-project': {'type': 'local', 'projectId': 'a'},
                 'active-workspace-roots': ['C:\\Worktrees\\Alpha-feature']}
        (self.root / '.codex-global-state.json').write_text(json.dumps(state), encoding='utf-8')
        self.assertEqual(ambient.project_snapshot(self.root)[1], 'a')

    def test_hotkey_is_canonical_and_requires_a_modifier(self):
        canonical, modifiers, virtual_key = ambient.parse_hotkey('shift + control + alt + m')
        self.assertEqual(canonical, ambient.DEFAULT_HOTKEY)
        self.assertEqual(virtual_key, ord('M'))
        self.assertTrue(modifiers & ambient.MOD_NOREPEAT)
        with self.assertRaises(ValueError):
            ambient.parse_hotkey('M')

    def test_settings_candidate_preserves_manual_mute_and_canonicalizes_hotkey(self):
        result = web_settings.ambient_candidate({
            'enabled': True, 'muted': True, 'hotkey': 'shift + control + m',
            'volume': 18, 'assignments': {},
        })
        self.assertTrue(result['muted'])
        self.assertEqual(result['hotkey'], 'Ctrl+Shift+M')

    def test_codex_desktop_image_excludes_cli_process(self):
        self.assertTrue(ambient.is_codex_desktop_image(
            r'C:\Program Files\WindowsApps\OpenAI.Codex_26.908.0_x64__id\app\ChatGPT.exe'))
        self.assertTrue(ambient.is_codex_desktop_image(
            r'C:\Program Files\WindowsApps\OpenAI.Codex_26.908.0_x64__id\app\Codex.exe'))
        self.assertFalse(ambient.is_codex_desktop_image(
            r'C:\Users\Joe\AppData\Local\OpenAI\Codex\bin\build\codex.exe'))
        self.assertFalse(ambient.is_codex_desktop_image(r'C:\Tools\ChatGPT.exe'))

    def test_assignment_falls_back_to_project_root_then_name(self):
        projects = [{'id': 'new-id', 'name': 'Alpha', 'rootPaths': [r'C:\Work\Alpha']}]
        by_root = {'old-id': {'mode': 'folder', 'projectName': 'Old name',
                              'projectRootPaths': [r'C:\Work\Alpha']}}
        self.assertIs(ambient.assignment_for_project('new-id', projects, by_root), by_root['old-id'])
        by_name = {'old-id': {'mode': 'folder', 'projectName': 'Alpha',
                              'projectRootPaths': [r'D:\Elsewhere']}}
        self.assertIs(ambient.assignment_for_project('new-id', projects, by_name), by_name['old-id'])

    def test_assignment_fallback_rejects_ambiguous_names(self):
        projects = [{'id': 'new-id', 'name': 'Alpha', 'rootPaths': [r'C:\Work\Alpha']}]
        assignments = {'one': {'projectName': 'Alpha'}, 'two': {'projectName': 'Alpha'}}
        self.assertIsNone(ambient.assignment_for_project('new-id', projects, assignments))

    def test_existing_assignment_is_enriched_during_setup(self):
        projects = [{'id': 'same-id', 'name': 'Alpha', 'rootPaths': [r'C:\Work\Alpha']}]
        result = ambient.enrich_assignment_identities(
            {'same-id': {'mode': 'folder', 'folder': r'C:\Audio'}}, projects)
        self.assertEqual(result['same-id']['projectName'], 'Alpha')
        self.assertEqual(result['same-id']['projectRootPaths'], [r'C:\Work\Alpha'])

    def test_settings_candidate_preserves_project_identity(self):
        folder = self.root / 'playlist'
        folder.mkdir()
        (folder / 'rain.mp3').write_bytes(b'x')
        result = web_settings.ambient_candidate({
            'enabled': True, 'muted': False, 'hotkey': ambient.DEFAULT_HOTKEY,
            'volume': 20, 'assignments': {'old-id': {
                'mode': 'folder', 'file': '', 'folder': str(folder),
                'projectName': 'Alpha', 'projectRootPaths': [r'C:\Work\Alpha'],
            }},
        })
        assignment = result['assignments']['old-id']
        self.assertEqual(assignment['projectName'], 'Alpha')
        self.assertEqual(assignment['projectRootPaths'], [r'C:\Work\Alpha'])

    def test_folder_playlist_is_sorted_and_excludes_subfolders(self):
        folder = self.root / 'playlist'
        folder.mkdir()
        for name in ('10 Rain.mp3', '02 Wind.wav', 'notes.txt', '01 Birds.MP3'):
            (folder / name).write_bytes(b'x')
        (folder / 'subfolder').mkdir()
        (folder / 'subfolder' / 'hidden.mp3').write_bytes(b'x')
        files = ambient.assignment_files({'mode': 'folder', 'folder': str(folder)})
        self.assertEqual([path.name for path in files], ['01 Birds.MP3', '02 Wind.wav', '10 Rain.mp3'])

    def test_position_store_remembers_project_track(self):
        store = ambient.PositionStore(self.root / 'positions.sqlite')
        store.save('project-a', self.root / 'rain.mp3', 12345)
        self.assertEqual(store.load('project-a'), (str(self.root / 'rain.mp3'), 12345))
        self.assertEqual(store.load('project-b'), (None, 0))

    def test_single_track_accepts_long_media_probe(self):
        track = self.root / 'long.mp3'
        track.write_bytes(b'placeholder')
        with patch.object(ambient, 'probe_media') as probe:
            files = ambient.assignment_files({'mode': 'single', 'file': str(track)})
            ambient.probe_media(files[0])
        probe.assert_called_once_with(track.resolve())

    def test_suspend_releases_track_and_forgets_active_identity(self):
        class FakeTrack:
            path = self.root / 'rain.mp3'

            def position(self):
                return 3210

            def close(self):
                self.closed = True

        controller = ambient.AmbientController(self.root)
        controller.project_id = 'alpha'
        controller.assignment_key = 'saved'
        controller.track = FakeTrack()
        controller.active = True
        with patch.object(controller.store, 'save') as save:
            controller.suspend()
        save.assert_called_once_with('alpha', self.root / 'rain.mp3', 3210)
        self.assertIsNone(controller.track)
        self.assertIsNone(controller.project_id)

    def test_controller_suspends_when_codex_desktop_exits(self):
        controller = ambient.AmbientController(self.root)
        settings = {'enabled': True, 'muted': False, 'volume': 20,
                    'assignments': {'alpha': {'mode': 'folder', 'folder': r'C:\Audio'}}}
        with patch.object(ambient, 'codex_desktop_is_running', return_value=False), \
             patch.object(ambient, 'project_snapshot', return_value=([{'id': 'alpha'}], 'alpha')), \
             patch.object(ambient, 'plugin_is_enabled', return_value=True), \
             patch.object(controller, 'suspend') as suspend, \
             patch.object(controller, 'activate') as activate:
            selected, running = controller.sync_playback(settings)
        self.assertEqual(selected, 'alpha')
        self.assertFalse(running)
        suspend.assert_called_once_with()
        activate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
