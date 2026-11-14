import json
import subprocess
import sys
import tempfile
import threading
import types
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


class ClipperTranscriptionTests(IsolatedOutputMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        clipper._whisper_models.clear()

    def _make_fake_info(self):
        vdir = engine.OUTPUT / 'cortes' / 'vid_tr'
        vdir.mkdir(parents=True, exist_ok=True)
        src = vdir / 'source.mp4'
        src.write_bytes(b'fake-mp4')
        return {
            'id': 'vid_tr',
            'titulo': 'Aula Teste',
            'duracao': 60.0,
            'source_path': str(src),
            'fonte_url': 'https://youtu.be/vid_tr',
            'canal': 'CanalTeste',
            'autorizado': True,
            'credito': '@CanalTeste',
            'data': '2026-11-13T00:00:00Z',
        }

    def test_group_words_into_units_by_punctuation_and_pause(self):
        words = [
            {'texto': 'Primeira', 'inicio': 0.0, 'fim': 0.4},
            {'texto': 'frase.', 'inicio': 0.45, 'fim': 0.9},
            {'texto': 'Segunda', 'inicio': 1.0, 'fim': 1.4},
            {'texto': 'parte', 'inicio': 1.45, 'fim': 1.8},
            # Pausa > 0.7s
            {'texto': 'Depois', 'inicio': 2.7, 'fim': 3.1},
            {'texto': 'da', 'inicio': 3.15, 'fim': 3.3},
            {'texto': 'pausa!', 'inicio': 3.35, 'fim': 3.9},
        ]
        units = clipper.group_words_into_units(words)
        self.assertEqual(len(units), 3)
        self.assertEqual(units[0]['texto'], 'Primeira frase.')
        self.assertEqual(units[1]['texto'], 'Segunda parte')
        self.assertEqual(units[2]['texto'], 'Depois da pausa!')
        self.assertEqual([u['indice'] for u in units], [0, 1, 2])
        self.assertEqual(len(units[2]['palavras']), 3)

    def test_transcribe_video_cache_invalidation_and_progress(self):
        info = self._make_fake_info()
        transcribe_calls = []
        logs = []

        def make_fake_fw(model_name, device=None, compute_type=None, download_root=None, local_files_only=False):
            self.assertEqual(device, 'cpu')
            self.assertEqual(compute_type, 'int8')
            self.assertTrue(local_files_only)

            class FakeModel:
                def transcribe(self_inner, path, language='pt', beam_size=5, word_timestamps=True, vad_filter=False, condition_on_previous_text=False):
                    transcribe_calls.append((model_name, language, vad_filter, word_timestamps))
                    self.assertTrue(word_timestamps)
                    self.assertTrue(vad_filter)
                    w1 = types.SimpleNamespace(word=' Olá', start=0.0, end=0.5)
                    w2 = types.SimpleNamespace(word=' mundo.', start=0.55, end=1.1)
                    w3 = types.SimpleNamespace(word=' Tudo', start=2.2, end=2.6)
                    w4 = types.SimpleNamespace(word=' bem?', start=2.65, end=3.1)
                    segs = [
                        types.SimpleNamespace(end=1.1, words=[w1, w2]),
                        types.SimpleNamespace(end=60.0, words=[w3, w4]),
                    ]
                    return iter(segs), types.SimpleNamespace(duration=60.0)

            return FakeModel()

        fake_fw_mod = types.SimpleNamespace(WhisperModel=make_fake_fw)
        with patch.dict(sys.modules, {'faster_whisper': fake_fw_mod}), \
             patch('engine.load_settings', return_value={'whisper_model': 'small'}):
            res1 = clipper.transcribe_video(info, language='pt', log=logs.append)
            self.assertEqual(len(transcribe_calls), 1)
            self.assertEqual(res1['modelo'], 'small')
            self.assertEqual(res1['idioma'], 'pt')
            self.assertEqual(len(res1['unidades']), 2)
            self.assertTrue(res1['autorizado'])
            self.assertTrue(any('100%' in line and 'estimado' in line for line in logs))

            # Mesma config reutiliza cache
            res2 = clipper.transcribe_video(info, language='pt', log=logs.append)
            self.assertEqual(len(transcribe_calls), 1)
            self.assertEqual(res2['unidades'], res1['unidades'])

            # Mudar idioma invalida cache
            res3 = clipper.transcribe_video(info, language='en', log=logs.append)
            self.assertEqual(len(transcribe_calls), 2)
            self.assertEqual(res3['idioma'], 'en')

            # Mudar modelo invalida cache
            res4 = clipper.transcribe_video(info, language='en', model_name='base', log=logs.append)
            self.assertEqual(len(transcribe_calls), 3)
            self.assertEqual(res4['modelo'], 'base')

    def test_transcribe_video_slice_and_cooperative_cancel(self):
        info = self._make_fake_info()
        info['duracao'] = 600.0
        cancel_ev = threading.Event()

        def make_fake_fw(*args, **kwargs):
            class FakeModel:
                def transcribe(self_inner, path, **kw):
                    w1 = types.SimpleNamespace(word=' Trecho', start=1.0, end=1.5)
                    w2 = types.SimpleNamespace(word=' cortado.', start=1.6, end=2.0)

                    def gen():
                        yield types.SimpleNamespace(end=2.0, words=[w1, w2])
                        cancel_ev.set()
                        yield types.SimpleNamespace(end=4.0, words=[w1])

                    return gen(), types.SimpleNamespace(duration=120.0)

            return FakeModel()

        fake_fw_mod = types.SimpleNamespace(WhisperModel=make_fake_fw)
        with patch.dict(sys.modules, {'faster_whisper': fake_fw_mod}), \
             patch('clipper._extract_audio_slice') as mock_slice:
            with self.assertRaises(clipper.CancelledError):
                clipper.transcribe_video(
                    info,
                    model_name='small',
                    start_min=2.0,
                    end_min=4.0,
                    cancel_event=cancel_ev,
                    log=lambda *_: None,
                )
            mock_slice.assert_called_once()
            # Cancelamento não deve gravar transcricao.json incompleto
            self.assertFalse((engine.OUTPUT / 'cortes' / 'vid_tr' / 'transcricao.json').exists())

        # Sem cancelar, tempos do trecho devem ser deslocados em +120s
        cancel_ev2 = threading.Event()

        def make_fake_fw_ok(*args, **kwargs):
            class FakeModel:
                def transcribe(self_inner, path, **kw):
                    w1 = types.SimpleNamespace(word=' Minuto', start=1.0, end=1.5)
                    w2 = types.SimpleNamespace(word=' dois.', start=1.6, end=2.0)
                    return iter([types.SimpleNamespace(end=2.0, words=[w1, w2])]), types.SimpleNamespace(duration=120.0)

            return FakeModel()

        clipper._whisper_models.clear()
        with patch.dict(sys.modules, {'faster_whisper': types.SimpleNamespace(WhisperModel=make_fake_fw_ok)}), \
             patch('clipper._extract_audio_slice'):
            sliced = clipper.transcribe_video(
                info,
                model_name='small',
                start_min=2.0,
                end_min=4.0,
                cancel_event=cancel_ev2,
                log=lambda *_: None,
            )
        self.assertAlmostEqual(sliced['palavras'][0]['inicio'], 121.0, places=2)
        self.assertAlmostEqual(sliced['unidades'][0]['fim'], 122.0, places=2)

    def test_missing_whisper_model_friendly_error_message(self):
        info = self._make_fake_info()

        def fail_load(*args, **kwargs):
            raise FileNotFoundError('local model missing')

        with patch.dict(sys.modules, {'faster_whisper': types.SimpleNamespace(WhisperModel=fail_load)}):
            with self.assertRaises(RuntimeError) as ctx:
                clipper.transcribe_video(info, model_name='small', log=lambda *_: None)
        msg = str(ctx.exception)
        self.assertIn('instalar_legendas.bat', msg)
        self.assertIn("WhisperModel('small'", msg)


if __name__ == '__main__':
    unittest.main(verbosity=2)
