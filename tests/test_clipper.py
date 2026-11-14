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


class ClipperSelectionTests(IsolatedOutputMixin, unittest.TestCase):
    def _build_units(self, count=12, step=10.0, text_fn=None):
        units = []
        for i in range(count):
            start = i * step + 0.5
            end = start + (step - 1.5)
            txt = text_fn(i) if text_fn else f'Unidade numero {i} sobre energia solar com 40 porcento de economia.'
            units.append({
                'indice': i,
                'inicio': round(start, 3),
                'fim': round(end, 3),
                'texto': txt,
                'palavras': [],
            })
        return units

    def _fake_info_and_trans(self, units, duracao=130.0):
        info = {
            'id': 'vid_sel',
            'titulo': 'Energia Solar',
            'duracao': duracao,
            'fonte_url': 'https://youtu.be/vid_sel',
            'canal': 'CanalSolar',
            'autorizado': True,
            'credito': '@CanalSolar',
            'data': '2026-11-13T00:00:00Z',
        }
        trans = {
            'video_id': 'vid_sel',
            'duracao': duracao,
            'modelo': 'small',
            'idioma': 'pt',
            'unidades': units,
        }
        return info, trans

    def test_index_to_time_padding_and_duration_repair(self):
        # Cada unidade dura ~8.5s (step=10s). Faixa 25..55s.
        units = self._build_units(count=12, step=10.0, text_fn=lambda i: (
            'Por que a bateria de 500 ciclos dura mais?' if i == 1
            else f'Fato {i} mostra resultado real sem mistério!'
        ))
        info, trans = self._fake_info_and_trans(units, duracao=130.0)

        def fake_ask(instruction, payload, model=None, temperature=0.55, log=print, num_ctx=4096):
            self.assertEqual(num_ctx, 4096)
            if 'ATÉ 3 trechos' in instruction:
                return {
                    'cortes': [
                        # Curto demais (1 unidade ~8.5s) -> deve reparar expandindo para >=25s
                        {'inicio_unidade': 1, 'fim_unidade': 1, 'nota': 8, 'gancho': 'Por que bateria de 500 ciclos dura mais?', 'motivo': 'Gancho forte.'},
                        # Longo demais (7 unidades ~68.5s) -> deve reparar aparando para <=55s
                        {'inicio_unidade': 5, 'fim_unidade': 11, 'nota': 7, 'gancho': 'Fato 5 mostra resultado real!', 'motivo': 'Explicação longa.'},
                        # Índice inválido -> descartado sem falhar
                        {'inicio_unidade': 99, 'fim_unidade': 102, 'nota': 9, 'gancho': 'Inválido', 'motivo': 'Fora'},
                    ]
                }
            if 'EXATAMENTE 2 frases' in instruction:
                return {'resumo': 'O vídeo explica baterias. Mostra economia prática.'}
            return {
                'resumo': ['Baterias duram 500 ciclos', 'Economia comprovada', 'Aplicação prática'],
                'potencial_cortes': 'alto',
                'justificativa_potencial': 'Ótimas explicações diretas.',
            }

        report = clipper.select_cuts_and_report(info, trans, min_duration=25.0, max_duration=55.0, num_cuts=5, ask=fake_ask, log=lambda *_: None)
        cands = report['candidatos']
        self.assertEqual(len(cands), 2)
        for c in cands:
            self.assertGreaterEqual(c['duracao'], 25.0)
            self.assertLessEqual(c['duracao'], 55.0)

        # Verifica folga (-0.15 / +0.25) no candidato reparado que começa na unidade 1 (10.5s)
        c_first = min(cands, key=lambda x: x['inicio'])
        self.assertEqual(c_first['inicio_unidade'], 1)
        self.assertAlmostEqual(c_first['inicio'], 10.5 - 0.15, places=2)
        self.assertAlmostEqual(c_first['fim'], units[c_first['fim_unidade']]['fim'] + 0.25, places=2)
        self.assertEqual(report['potencial_cortes'], 'alto')
        self.assertTrue((engine.OUTPUT / 'cortes' / 'vid_sel' / 'relatorio.json').is_file())

    def test_impossible_duration_discarded(self):
        # Unidade única com 80s (> max_duration=55s): impossível reparar para <=55s
        units = [{'indice': 0, 'inicio': 0.0, 'fim': 80.0, 'texto': 'Monólogo contínuo muito longo sem pausa.', 'palavras': []}]
        repaired = clipper._repair_unit_bounds(units, 0, 0, min_duration=25.0, max_duration=55.0, video_duration=80.0)
        self.assertIsNone(repaired)

    def test_dedupe_overlap_and_min_distance_10s(self):
        cands = [
            {'inicio': 10.0, 'fim': 40.0, 'nota': 9.0, 'gancho': 'A', 'motivo': 'A'},
            # >50% interseção com [10, 40] -> deve ser removido (nota menor)
            {'inicio': 15.0, 'fim': 45.0, 'nota': 7.5, 'gancho': 'B', 'motivo': 'B'},
            # <10s de distância após fim=40 (começa em 44s) -> ignorado na seleção
            {'inicio': 44.0, 'fim': 72.0, 'nota': 8.5, 'gancho': 'C', 'motivo': 'C'},
            # >=10s de distância após fim=40 (começa em 51s) -> mantido!
            {'inicio': 51.0, 'fim': 80.0, 'nota': 8.0, 'gancho': 'D', 'motivo': 'D'},
        ]
        deduped = clipper.dedupe_candidates(cands[:2])
        self.assertEqual([c['gancho'] for c in deduped], ['A'])
        selected = clipper.select_top_candidates(cands, max_cuts=5, min_distance_s=10.0)
        self.assertEqual([c['gancho'] for c in selected], ['A', 'D'])

    def test_invalid_hook_repaired_from_first_sentence(self):
        clip_text = 'O motor elétrico reduziu o consumo em 30 porcento na cidade. Depois veio o teste em rodovia.'
        # Número inventado (999) -> deve substituir pela 1ª frase truncada (<=10 palavras)
        repaired_num = clipper.validate_or_repair_hook('Motor economizou 999 litros na cidade!', clip_text)
        self.assertNotIn('999', repaired_num)
        self.assertLessEqual(len(repaired_num.split()), 10)
        self.assertTrue(repaired_num.startswith('O motor elétrico'))

        # <50% palavras de conteúdo presentes -> deve substituir
        repaired_halluc = clipper.validate_or_repair_hook('Astronautas descobriram água congelada em Marte ontem!', clip_text)
        self.assertNotIn('Astronautas', repaired_halluc)
        self.assertLessEqual(len(repaired_halluc.split()), 10)

        # Gancho fiel -> preservado
        valid = clipper.validate_or_repair_hook('Motor elétrico reduziu consumo em 30 porcento!', clip_text)
        self.assertEqual(valid, 'Motor elétrico reduziu consumo em 30 porcento!')

    def test_heuristic_fallback_when_all_llm_calls_fail(self):
        units = self._build_units(count=10, step=10.0, text_fn=lambda i: (
            'Como economizar 40 porcento de energia em casa? Veja o dado real!' if i in (0, 6)
            else f'Explicação clara número {i} com resultado prático!'
        ))
        info, trans = self._fake_info_and_trans(units, duracao=100.0)
        temps_seen = []

        def failing_ask(instruction, payload, model=None, temperature=0.55, log=print, num_ctx=4096):
            temps_seen.append(temperature)
            raise RuntimeError('Ollama offline simulado')

        report = clipper.select_cuts_and_report(info, trans, min_duration=25.0, max_duration=45.0, num_cuts=3, ask=failing_ask, log=lambda *_: None)
        # Temperaturas de retry devem respeitar 0.55 inicial, +0.05, teto 0.7
        self.assertEqual(temps_seen[:3], [0.55, 0.6, 0.65])
        self.assertTrue(all(t <= 0.7 for t in temps_seen))
        self.assertGreaterEqual(len(report['candidatos']), 1)
        self.assertEqual(len(report['resumo']), 1)
        self.assertIn(report['potencial_cortes'], ('alto', 'médio', 'baixo'))
        self.assertTrue(any('heurístico' in a.casefold() for a in report['avisos']))

    def test_failed_window_is_skipped_without_crashing_others(self):
        # Gera unidades suficientes para 2 janelas (>650 palavras)
        long_text = ' '.join(['palavra'] * 80)
        units = self._build_units(count=12, step=15.0, text_fn=lambda i: f'Por que o teste {i} funciona? {long_text}')
        info, trans = self._fake_info_and_trans(units, duracao=180.0)
        windows = clipper.build_windows(units, max_words=650, overlap_seconds=60.0)
        self.assertGreaterEqual(len(windows), 2)

        cut_window_calls = {'n': 0}

        def partial_ask(instruction, payload, model=None, temperature=0.55, log=print, num_ctx=4096):
            if 'ATÉ 3 trechos' in instruction:
                cut_window_calls['n'] += 1
                # Primeira janela falha nas 3 tentativas
                if cut_window_calls['n'] <= 3:
                    raise ValueError('JSON quebrado com motivo bem detalhado ' + ('X' * 120))
                # Segunda janela retorna corte válido
                last_idx = int(payload.splitlines()[-1].split(']')[0].lstrip('['))
                first_idx = max(0, last_idx - 2)
                return {
                    'cortes': [
                        {'inicio_unidade': first_idx, 'fim_unidade': last_idx, 'nota': 8, 'gancho': f'Por que o teste {first_idx} funciona?', 'motivo': 'Bom trecho'}
                    ]
                }
            if 'EXATAMENTE 2 frases' in instruction:
                return {'resumo': 'Frase um do resumo. Frase dois do resumo.'}
            return {'resumo': ['B1', 'B2', 'B3'], 'potencial_cortes': 'médio', 'justificativa_potencial': 'Bom.'}

        report = clipper.select_cuts_and_report(info, trans, min_duration=25.0, max_duration=55.0, num_cuts=3, ask=partial_ask, log=lambda *_: None)
        self.assertGreaterEqual(len(report['candidatos']), 1)
        self.assertTrue(any('Janela 1/' in a and 'ignorada' in a for a in report['avisos']))
        # Motivo longo preservado sem truncar abaixo de ~170-220 chars
        self.assertTrue(any(len(a) > 120 for a in report['avisos']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
