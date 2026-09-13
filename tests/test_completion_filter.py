from contextlib import closing
import concurrent.futures
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import notify


class CompletionFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.data = self.home / 'notification-sounds'
        self.data.mkdir()
        self.root_patch = patch.object(notify, 'ROOT', self.data)
        self.root_patch.start()
        with closing(sqlite3.connect(self.home / 'state_5.sqlite')) as connection, connection:
            connection.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT)')
            connection.executemany('INSERT INTO threads VALUES (?, ?)', [
                ('parent', 'vscode'),
                ('child', json.dumps({'subagent': {'thread_spawn': {'parent_thread_id': 'parent'}}})),
                ('review', json.dumps({'subagent': 'review'})),
                ('unknown', 'unknown'),
            ])
        (self.data / 'sounds.json').write_text(json.dumps({'enabled': True, 'volume': 19}))
        (self.data / 'forward-command.json').write_text('[]')

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def event(self, thread='parent', turn='turn1', **extra):
        return {'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn,
                'last-assistant-message': 'Finished', **extra}

    def invoke(self, event, allow=True):
        with patch.object(notify, 'select_sound', return_value=('chime', Path('chime.wav'))) as select, \
             patch.object(notify, 'play_sound', return_value=True) as play:
            notify.main(json.dumps(event), allow_sound=allow)
            report = json.loads((self.data / 'last-event.json').read_text())
            return report, select.call_count, play.call_count

    def test_internal_completions_do_not_select_or_play(self):
        for origin in ('child', 'review'):
            report, selected, played = self.invoke(self.event(origin))
            self.assertEqual(report['suppressed'], 'internal-agent')
            self.assertEqual((selected, played), (0, 0))

    def test_parent_final_plays_once_and_next_turn_can_play(self):
        self.assertEqual(self.invoke(self.event())[2], 1)
        report, selected, played = self.invoke(self.event())
        self.assertEqual(report['suppressed'], 'duplicate-completion')
        self.assertEqual((selected, played), (0, 0))
        self.assertEqual(self.invoke(self.event(turn='turn2'))[2], 1)

    def test_progress_empty_final_missing_and_unknown_are_silent(self):
        cases = [self.event(type='agent-message'), self.event(**{'last-assistant-message': ''}),
                 self.event(**{'turn-id': None}), self.event('missing'), self.event('unknown')]
        for event in cases:
            self.assertEqual(self.invoke(event)[1:], (0, 0))

    def test_concurrent_duplicates_only_one_reservation(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: notify.claim_completion(self.event()), range(16)))
        self.assertEqual(sum(results), 1)

    def test_other_callback_is_forwarded_even_for_child(self):
        (self.data / 'forward-command.json').write_text('["other.exe", "notify"]')
        event = self.event('child')
        with patch.object(notify.subprocess, 'Popen') as forward:
            self.invoke(event)
            self.assertEqual(forward.call_args.args[0], ['other.exe', 'notify', json.dumps(event)])

    def test_missing_database_stays_quiet_and_history_contains_no_text(self):
        (self.home / 'state_5.sqlite').unlink()
        report, _, played = self.invoke(self.event())
        self.assertEqual((report['suppressed'], played), ('unknown-origin', 0))
        with closing(sqlite3.connect(self.data / 'notification-events.sqlite')) as connection, connection:
            saved = connection.execute('SELECT report FROM history').fetchone()[0]
        self.assertNotIn('Finished', saved)

    def test_plugin_disabled_and_mute_do_not_reserve_turn(self):
        self.assertTrue(self.invoke(self.event(), allow=False)[0]['pluginDisabled'])
        self.assertEqual(self.invoke(self.event())[2], 1)


if __name__ == '__main__':
    unittest.main()
