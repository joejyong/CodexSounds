from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import audio_import


class AudioImportTests(unittest.TestCase):
    def test_urls_are_trimmed_and_deduplicated(self):
        urls = audio_import.parse_urls(' https://youtu.be/abc \n\nhttps://youtu.be/abc\nhttps://www.youtube.com/watch?v=def ')
        self.assertEqual(urls, ['https://youtu.be/abc', 'https://www.youtube.com/watch?v=def'])

    def test_non_youtube_and_insecure_urls_are_rejected(self):
        for url in ('https://example.com/video', 'http://youtu.be/abc', 'file:///video.mp4'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                audio_import.parse_urls(url)

    def test_queue_has_a_bounded_size(self):
        urls = [f'https://youtu.be/video{i}' for i in range(audio_import.MAX_URLS + 1)]
        with self.assertRaises(ValueError):
            audio_import.parse_urls(urls)

    def test_destination_is_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / 'ambient' / 'imports'
            self.assertEqual(audio_import.destination_path(str(destination)), destination.resolve())
            self.assertTrue(destination.is_dir())

    def test_download_command_disables_playlists_and_keeps_existing_files(self):
        command = audio_import.download_command(Path('yt-dlp.exe'), Path('ffmpeg.exe'),
                                                Path('C:/Audio'), 'https://youtu.be/abc')
        self.assertIn('--no-playlist', command)
        self.assertIn('--no-overwrites', command)
        self.assertEqual(command[-1], 'https://youtu.be/abc')
        self.assertIn('128K', command)

    def test_tool_status_requires_both_programs(self):
        with patch.object(audio_import, 'find_tool', side_effect=[Path('yt-dlp.exe'), None]), \
             patch.object(audio_import.shutil, 'which', return_value=None):
            self.assertFalse(audio_import.tools_status()['ready'])


if __name__ == '__main__':
    unittest.main()
