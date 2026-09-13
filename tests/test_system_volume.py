from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import ambient
import notify


class SystemVolumeTests(unittest.TestCase):
    def test_effective_volume_multiplies_app_and_system_levels(self):
        self.assertEqual(notify.effective_volume(80, 0.25), 20)
        self.assertEqual(notify.effective_volume(80, 0), 0)

    def test_unavailable_system_volume_keeps_configured_level(self):
        with patch.object(notify, '_read_system_volume_scalar', side_effect=OSError()):
            self.assertEqual(notify.system_volume_scalar(), 1.0)

    def test_wav_playback_scales_samples_by_system_volume(self):
        with patch.object(notify, 'sound_bytes', return_value=b'wav') as sound_bytes, \
             patch.object(notify.winsound, 'PlaySound') as play:
            self.assertTrue(notify.play_sound('tone.wav', 80, 0.25))
        sound_bytes.assert_called_once_with('tone.wav', 20)
        play.assert_called_once()

    def test_mp3_playback_sets_system_adjusted_mci_volume(self):
        with patch.object(notify, 'mp3_device') as device, \
             patch.object(notify, 'mci') as mci:
            device.return_value.__enter__.return_value = 'track'
            self.assertTrue(notify.play_sound('tone.mp3', 80, 0.25))
        self.assertEqual(mci.call_args_list[0].args[0], 'setaudio track volume to 200')

    def test_ambient_controller_refreshes_system_volume(self):
        assignment = {'mode': 'single', 'file': r'C:\Audio\rain.mp3'}
        settings = {'enabled': True, 'muted': False, 'volume': 80,
                    'assignments': {'alpha': assignment}}
        with tempfile.TemporaryDirectory() as directory:
            controller = ambient.AmbientController(Path(directory))
            with patch.object(ambient, 'codex_desktop_is_running', return_value=True), \
                 patch.object(ambient, 'project_snapshot', return_value=([{'id': 'alpha'}], 'alpha')), \
                 patch.object(ambient, 'assignment_for_project', return_value=assignment), \
                 patch.object(ambient, 'plugin_is_enabled', return_value=True), \
                 patch.object(notify, 'system_volume_scalar', return_value=0.25), \
                 patch.object(controller, 'activate') as activate:
                controller.sync_playback(settings)
        activate.assert_called_once_with('alpha', assignment, 20)
        self.assertEqual(controller.status()['systemVolume'], 25)
        self.assertEqual(controller.status()['effectiveVolume'], 20)


if __name__ == '__main__':
    unittest.main()
