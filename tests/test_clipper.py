import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clipper
import engine
from tests._isolation import IsolatedOutputMixin


class ClipperSourceTests(IsolatedOutputMixin, unittest.TestCase):
    def test_requires_explicit_authorization(self):
        with self.assertRaises(ValueError) as ctx:
            clipper.prepare_source('https://www.youtube.com/watch?v=abc123', authorized=False)
        self.assertIn(clipper.AUTH_CONFIRM_TEXT, str(ctx.exception))

    def test_url_download_success_and_cache_reuse(self):
        url = 'https://www.youtube.com/watch?v=vid123'
        meta = {
            'id': 'vid123',
            'title': 'Palestra de Tecnologia',
            'channel': 'CanalTechOficial',
            'duration': 740.5,
            'webpage_url': url,
        }
        calls = []

        def fake_run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=None):
            calls.append(cmd)
            self.assertEqual(cmd[:3], [sys.executable, '-m', 'yt_dlp'])
            for forbidden in ('--cookies', '--cookies-from-browser', '--username', '--password'):
                self.assertNotIn(forbidden, cmd)
            if '--dump-single-json' in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(meta), stderr='')
            out_idx = cmd.index('-o') + 1
            out_path = Path(cmd[out_idx])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b'fake-mp4-data')
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

        with patch('clipper.subprocess.run', side_effect=fake_run):
            info1 = clipper.prepare_source(url, authorized=True, log=lambda *_: None)

        self.assertEqual(len(calls), 2)
        self.assertEqual(info1['id'], 'vid123')
        self.assertEqual(info1['fonte_url'], url)
        self.assertEqual(info1['canal'], 'CanalTechOficial')
        self.assertIs(info1['autorizado'], True)
        self.assertEqual(info1['credito'], '@CanalTechOficial')
        self.assertTrue(info1['data'])

        info_file = engine.OUTPUT / 'cortes' / 'vid123' / 'info.json'
        source_file = engine.OUTPUT / 'cortes' / 'vid123' / 'source.mp4'
        self.assertTrue(info_file.is_file())
        self.assertTrue(source_file.is_file())
        disk_info = json.loads(info_file.read_text(encoding='utf-8'))
        self.assertTrue(disk_info['autorizado'])
        self.assertEqual(disk_info['credito'], '@CanalTechOficial')

        # Segunda chamada deve reutilizar cache sem chamar yt-dlp novamente
        with patch('clipper.subprocess.run', side_effect=AssertionError('não deveria chamar yt-dlp')) as mock_run:
            info2 = clipper.prepare_source(url, authorized=True, credito='@custom', log=lambda *_: None)
            mock_run.assert_not_called()
        self.assertEqual(info2['credito'], '@custom')
        self.assertEqual(info2['id'], 'vid123')

    def test_url_video_unavailable_friendly_error(self):
        proc = subprocess.CompletedProcess(
            ['python', '-m', 'yt_dlp'],
            1,
            stdout='',
            stderr='ERROR: [youtube] xyz: Video unavailable. This video has been removed by the uploader',
        )
        with patch('clipper.subprocess.run', return_value=proc):
            with self.assertRaises(RuntimeError) as ctx:
                clipper.prepare_source('https://youtu.be/xyz', authorized=True, log=lambda *_: None)
        msg = str(ctx.exception).casefold()
        self.assertIn('indisponível', msg)
        self.assertNotIn('traceback', msg)

    def test_url_timeout_friendly_error(self):
        with patch(
            'clipper.subprocess.run',
            side_effect=subprocess.TimeoutExpired(cmd=['python', '-m', 'yt_dlp'], timeout=60),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                clipper.prepare_source('https://youtu.be/slow', authorized=True, meta_timeout=60, log=lambda *_: None)
        msg = str(ctx.exception).casefold()
        self.assertIn('tempo esgotado', msg)
        self.assertIn('yt-dlp', msg)

    def test_local_file_nonexistent_raises_friendly_error(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            clipper.prepare_source('C:/nao_existe/video_inexistente_12345.mp4', authorized=True, log=lambda *_: None)
        self.assertIn('não encontrado', str(ctx.exception).casefold())

    def test_local_file_success_and_cli_authorization(self):
        with tempfile.TemporaryDirectory() as td:
            local_mp4 = Path(td) / 'entrevista.mp4'
            local_mp4.write_bytes(b'dummy-local-mp4')
            with patch('clipper._measure_duration_safe', return_value=125.0):
                info = clipper.prepare_source(str(local_mp4), authorized=True, credito='@meu_canal', log=lambda *_: None)
            self.assertTrue(info['id'].startswith('local_'))
            self.assertTrue(info['autorizado'])
            self.assertEqual(info['credito'], '@meu_canal')
            self.assertEqual(info['canal'], 'local')
            self.assertEqual(info['duracao'], 125.0)
            self.assertTrue((engine.OUTPUT / 'cortes' / info['id'] / 'info.json').is_file())


if __name__ == '__main__':
    unittest.main(verbosity=2)
