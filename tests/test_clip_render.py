import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clip_render
import engine
import media_tools
from tests._isolation import IsolatedOutputMixin


def _has_ffmpeg() -> bool:
    try:
        exe = media_tools.ffmpeg()
        return bool(exe and Path(exe).exists())
    except Exception:
        return False


HAS_FFMPEG = _has_ffmpeg()


class ClipRenderUnitTests(IsolatedOutputMixin, unittest.TestCase):
    def _setup_fixture(self):
        vdir = engine.OUTPUT / 'cortes' / 'vid_r1'
        vdir.mkdir(parents=True, exist_ok=True)
        src = vdir / 'source.mp4'
        src.write_bytes(b'fake-source-video')
        gp = engine.OUTPUT / 'gp_test.mp4'
        gp.write_bytes(b'fake-gameplay')

        info = {
            'id': 'vid_r1',
            'titulo': 'Vídeo de Teste',
            'duracao': 60.0,
            'source_path': str(src),
            'fonte_url': 'https://youtu.be/vid_r1',
            'canal': 'CanalR',
            'autorizado': True,
            'credito': '@CanalR',
            'data': '2026-11-13T00:00:00Z',
        }
        transcription = {
            'video_id': 'vid_r1',
            'modelo': 'small',
            'idioma': 'pt',
            'palavras': [
                {'texto': 'Antes', 'inicio': 5.0, 'fim': 5.5},
                {'texto': 'Por', 'inicio': 10.0, 'fim': 10.3},
                {'texto': 'que', 'inicio': 10.35, 'fim': 10.6},
                {'texto': 'isso', 'inicio': 10.65, 'fim': 11.0},
                {'texto': 'funciona', 'inicio': 11.1, 'fim': 11.6},
                {'texto': 'tão', 'inicio': 11.7, 'fim': 12.0},
                {'texto': 'bem?', 'inicio': 12.05, 'fim': 12.5},
                {'texto': 'Depois', 'inicio': 45.0, 'fim': 45.5},
            ],
            'unidades': [],
        }
        candidate = {
            'indice': 1,
            'inicio': 10.0,
            'fim': 36.0,
            'nota': 8.9,
            'nota_llm': 8.0,
            'bonus_heuristico': 0.9,
            'gancho': 'Por que isso funciona tão bem?',
            'motivo': 'Pergunta forte com explicação clara.',
            'texto': 'Por que isso funciona tão bem?',
        }
        return info, transcription, candidate, gp

    def test_render_clip_filtergraph_ajustar_preencher_and_ass_no_whisper(self):
        info, transcription, candidate, gp = self._setup_fixture()
        captured = []

        def fake_run(cmd):
            cmd_list = list(map(str, cmd))
            fc_idx = cmd_list.index('-filter_complex') + 1
            fc = cmd_list[fc_idx]
            m = re.search(r"subtitles='([^']+)'", fc)
            ass_raw = m.group(1).replace('\\:', ':').replace('\\\\', '\\').replace('/', '\\') if m else ''
            # Unescape simple windows path from escape_filter_path
            ass_clean = m.group(1).replace('\\:', ':').replace("\\'", "'").replace('\\,', ',').replace('\\[', '[').replace('\\]', ']')
            ass_text = Path(ass_clean).read_text(encoding='utf-8') if Path(ass_clean).is_file() else ''
            captured.append({'cmd': cmd_list, 'fc': fc, 'ass': ass_text})
            out_path = Path(cmd_list[-1])
            out_path.write_bytes(b'fake-mp4')
            return ''

        # Bloqueia faster_whisper para provar que NÃO roda Whisper novamente
        with patch.dict(sys.modules, {'faster_whisper': None}), \
             patch.object(engine, 'run', side_effect=fake_run), \
             patch.object(engine, 'ffprobe_duration', return_value=120.0):
            mp4_adj = clip_render.render_clip(
                info, transcription, candidate, gameplay=gp, cut_index=1,
                modo_corte='ajustar', posicao_legenda='juncao',
                titulo_na_tela=True, credito_na_tela=True, log=lambda *_: None,
            )
            mp4_fill = clip_render.render_clip(
                info, transcription, candidate, gameplay=gp, cut_index=2,
                modo_corte='preencher', posicao_legenda='rodape',
                titulo_na_tela=False, credito_na_tela=True, log=lambda *_: None,
            )

        self.assertTrue(mp4_adj.name.startswith('corte_01_'))
        self.assertTrue(mp4_adj.is_file())
        json_adj = mp4_adj.with_suffix('.json')
        self.assertTrue(json_adj.is_file())
        meta = json.loads(json_adj.read_text(encoding='utf-8'))
        self.assertTrue(meta['autorizado'])
        self.assertEqual(meta['credito'], '@CanalR')
        self.assertEqual(meta['fonte_url'], 'https://youtu.be/vid_r1')
        self.assertEqual(meta['modo_corte'], 'ajustar')
        self.assertEqual(meta['posicao_legenda'], 'juncao')

        # Verifica filtergraph e flags do modo ajustar
        c1 = captured[0]
        self.assertIn('-threads', c1['cmd'])
        self.assertEqual(c1['cmd'][c1['cmd'].index('-threads') + 1], '0')
        self.assertIn('-preset', c1['cmd'])
        self.assertEqual(c1['cmd'][c1['cmd'].index('-preset') + 1], 'veryfast')
        self.assertIn('-crf', c1['cmd'])
        self.assertEqual(c1['cmd'][c1['cmd'].index('-crf') + 1], '20')
        self.assertIn('+faststart', c1['cmd'])
        self.assertIn('-stream_loop', c1['cmd'])
        self.assertIn('boxblur=20:5', c1['fc'])
        self.assertIn('scale=1080:960:force_original_aspect_ratio=decrease', c1['fc'])
        self.assertIn('[top][bottom]vstack=inputs=2[stacked]', c1['fc'])
        self.assertIn('vstack', c1['fc'])
        self.assertNotIn('xfade', c1['fc'])
        self.assertIn('trim=start=0:end=26.0000,setpts=PTS-STARTPTS[bottom]', c1['fc'])
        self.assertIn('vignette=PI/6', c1['fc'])
        self.assertIn('loudnorm=I=-14:TP=-1.5:LRA=11', c1['fc'])
        self.assertNotIn('[1:a]', c1['fc'])  # Áudio da gameplay mudo

        # Verifica ASS do modo ajustar + junção + gancho 3s + crédito
        ass1 = c1['ass']
        self.assertIn(r'\pos(540,960)', ass1)
        self.assertIn('POR QUE ISSO FUNCIONA TÃO BEM?', ass1)
        self.assertIn('0:00:03.00,Hook', ass1)
        self.assertIn('@CanalR', ass1)
        # Palavras fora do intervalo (Antes, Depois) não devem aparecer
        self.assertNotIn('ANTES', ass1)
        self.assertNotIn('DEPOIS', ass1)
        # Tempos deslocados pelo início (10.0s -> 0:00:00.00)
        self.assertIn('Dialogue: 0,0:00:00.00,', ass1)

        # Verifica modo preencher + rodapé + sem título na tela
        c2 = captured[1]
        self.assertTrue(mp4_fill.name.startswith('corte_02_'))
        self.assertNotIn('boxblur', c2['fc'])
        self.assertNotIn('xfade', c2['fc'])
        self.assertIn('scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960:(in_w-1080)*0.5000:(in_h-960)/2', c2['fc'])
        self.assertIn(r'\pos(540,1650)', c2['ass'])
        self.assertNotIn(',Hook,', c2['ass'])

    def test_crop_x_filter_and_opencv_fallback_and_median(self):
        vid_dir = clip_render.clipper.cortes_root() / 'vid_crop'
        vid_dir.mkdir(parents=True, exist_ok=True)
        src = vid_dir / 'source.mp4'
        src.write_bytes(b'src')
        gp = vid_dir / 'gp.mp4'
        gp.write_bytes(b'gp')
        info = {'id': 'vid_crop', 'source_path': str(src), 'autorizado': True}
        trans = {'palavras': [{'texto': 'Teste', 'inicio': 1.0, 'fim': 2.0}]}
        cand = {'indice': 1, 'inicio': 0.0, 'fim': 10.0, 'gancho': 'Gancho'}

        captured = []

        def fake_run(cmd):
            fc = cmd[cmd.index('-filter_complex') + 1]
            captured.append(fc)
            Path(cmd[-1]).write_bytes(b'mp4')

        # (1) crop_x explícito (0.25) gera filtro deslocado horizontalmente
        with patch.object(engine, 'run', side_effect=fake_run), \
             patch.object(engine, 'ffprobe_duration', return_value=30.0):
            clip_render.render_clip(
                info, trans, cand, gameplay=gp, modo_corte='preencher', crop_x=0.25, log=lambda *_: None
            )
        self.assertIn('crop=1080:960:(in_w-1080)*0.2500:(in_h-960)/2', captured[-1])

        # (2) Sem OpenCV instalado -> volta a 0.5 sem erro e loga aviso
        logs = []
        with patch.dict(sys.modules, {'cv2': None}):
            val = clip_render.detect_face_crop_x(src, 0.0, 10.0, log=logs.append)
        self.assertEqual(val, 0.5)
        self.assertTrue(any('0.5' in m for m in logs))

        # (3) Com OpenCV mockado -> mediana do centro X normalizado dos rostos
        class FakeFrame:
            shape = (1080, 1920, 3)

        class FakeCap:
            def isOpened(self):
                return True
            def set(self, *_a):
                pass
            def read(self):
                return True, FakeFrame()
            def release(self):
                pass

        # Centros X: (192+192/2)/1920 = 0.15, (576+192/2)/1920 = 0.35, (1344+192/2)/1920 = 0.75 -> mediana 0.35
        seq = [
            [(192, 100, 192, 192)],
            [(576, 100, 192, 192)],
            [(576, 100, 192, 192)],
            [(1344, 100, 192, 192)],
            [(1344, 100, 192, 192)],
        ]
        call_idx = {'i': 0}

        class FakeClassifier:
            def empty(self):
                return False
            def detectMultiScale(self, *_a, **_kw):
                i = call_idx['i']
                call_idx['i'] += 1
                return seq[i % len(seq)]

        class FakeCv2:
            CAP_PROP_POS_MSEC = 0
            COLOR_BGR2GRAY = 6
            class data:
                haarcascades = ''
            @staticmethod
            def CascadeClassifier(_p):
                return FakeClassifier()
            @staticmethod
            def VideoCapture(_p):
                return FakeCap()
            @staticmethod
            def cvtColor(frame, _code):
                return frame

        with patch.dict(sys.modules, {'cv2': FakeCv2}), \
             patch.object(engine, 'run', side_effect=fake_run), \
             patch.object(engine, 'ffprobe_duration', return_value=30.0):
            med = clip_render.detect_face_crop_x(src, 0.0, 10.0, num_samples=5, log=lambda *_: None)
            self.assertAlmostEqual(med, 0.35, places=4)
            clip_render.render_clip(
                info, trans, cand, gameplay=gp, modo_corte='preencher', enquadramento='auto', log=lambda *_: None
            )
        self.assertIn('crop=1080:960:(in_w-1080)*0.3500:(in_h-960)/2', captured[-1])

    def test_item44_sfx_peak_early_energy_drop_filtergraph_and_migration(self):
        import wave
        import numpy as np
        import sfx
        import music_cortes

        with tempfile.TemporaryDirectory() as td:
            td_root = Path(td)
            # (1) sfx gera WAV estéreo 44,1 kHz com pico <= -1 dBFS e energia nos primeiros 100 ms
            for kind, expected_dur in (('impacto', 1.2), ('whoosh', 0.8)):
                wav_path = sfx.ensure_sfx(kind, root=td_root)
                self.assertIsNotNone(wav_path)
                self.assertTrue(wav_path.is_file())
                with wave.open(str(wav_path), 'rb') as wf:
                    self.assertEqual(wf.getnchannels(), 2)
                    self.assertEqual(wf.getframerate(), 44100)
                    n_frames = wf.getnframes()
                    self.assertAlmostEqual(n_frames / 44100.0, expected_dur, delta=0.05)
                    raw = np.frombuffer(wf.readframes(n_frames), dtype='<i2').astype(np.float64) / 32767.0
                stereo = raw.reshape(-1, 2)
                peak = float(np.max(np.abs(stereo)))
                limit_1dbfs = 10.0 ** (-1.0 / 20.0)
                self.assertLessEqual(peak, limit_1dbfs + 1e-4, f'Pico de {kind} excedeu -1 dBFS: {peak}')
                first_100ms = stereo[:int(0.10 * 44100)]
                early_rms = float(np.sqrt(np.mean(first_100ms * first_100ms)))
                self.assertGreater(early_rms, 0.05, f'{kind} sem energia suficiente nos primeiros 100 ms')

            # (2) analyze_drop em sinal sintético (silêncio + rajada aos 10 s) devolve ~9.9 +- 0.3
            synth_wav = td_root / 'burst_10s.wav'
            sr = 22050
            dur_s = 20
            samples = np.zeros(sr * dur_s, dtype=np.float64)
            t_burst = np.arange(sr * 10, dtype=np.float64) / sr
            rng = np.random.default_rng(123)
            samples[sr * 10:] = 0.7 * np.sin(2.0 * np.pi * 110.0 * t_burst) + 0.2 * rng.standard_normal(len(t_burst))
            pcm_burst = np.clip(samples * 32767.0, -32767, 32767).astype('<i2')
            with wave.open(str(synth_wav), 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(pcm_burst.tobytes())

            if HAS_FFMPEG:
                drop_t = music_cortes.analyze_drop(synth_wav)
                self.assertAlmostEqual(drop_t, 9.9, delta=0.3)
                self.assertTrue((td_root / 'burst_10s.wav.drop.json').is_file())
                # Segunda chamada lê do cache
                self.assertAlmostEqual(music_cortes.analyze_drop(synth_wav), drop_t, places=3)

            # (3) Migração musica_ativa -> musica_modo
            m_true = clip_render.clipper.migrate_cortes_settings({'musica_ativa': True})
            self.assertEqual(m_true['musica_modo'], 'baixa')
            m_false = clip_render.clipper.migrate_cortes_settings({'musica_ativa': False})
            self.assertEqual(m_false['musica_modo'], 'desligada')
            m_default = clip_render.clipper.migrate_cortes_settings({})
            self.assertEqual(m_default['musica_modo'], 'impacto')
            self.assertEqual(m_default['musica_volume'], 22)
            self.assertEqual(m_default['impacto_inicial'], 'impacto')
            self.assertEqual(m_default['impacto_volume'], 70)

            # (4) Filtergraph do modo impacto contém sidechaincompress, adelay e alimiter e grava metadados
            info, transcription, candidate, gp = self._setup_fixture()
            captured_fc = []

            def fake_run(cmd):
                cmd_list = list(map(str, cmd))
                fc = cmd_list[cmd_list.index('-filter_complex') + 1]
                captured_fc.append(fc)
                Path(cmd_list[-1]).write_bytes(b'mp4')

            with patch.object(engine, 'ROOT', td_root), \
                 patch.object(engine, 'run', side_effect=fake_run), \
                 patch.object(engine, 'ffprobe_duration', return_value=30.0):
                out_imp = clip_render.render_clip(
                    info, transcription, candidate, gameplay=gp, cut_index=3,
                    modo_corte='preencher', musica_modo='impacto', musica_volume=22,
                    impacto_inicial='impacto', impacto_volume=70, crop_x=0.5, log=lambda *_: None,
                )
            self.assertTrue(captured_fc)
            fc_imp = captured_fc[-1]
            self.assertIn('sidechaincompress', fc_imp)
            self.assertIn('adelay', fc_imp)
            self.assertIn('alimiter', fc_imp)
            meta_imp = json.loads(out_imp.with_suffix('.json').read_text(encoding='utf-8'))
            self.assertEqual(meta_imp['musica_modo'], 'impacto')
            self.assertEqual(meta_imp['impacto_inicial'], 'impacto')
            self.assertIsNotNone(meta_imp['faixa'])
            self.assertIn('start', meta_imp)


class ClipRenderIntegrationTests(IsolatedOutputMixin, unittest.TestCase):
    @unittest.skipUnless(HAS_FFMPEG, 'Requer FFmpeg instalado para teste de integração sintético')
    def test_render_synthetic_lavfi_clip(self):
        ffmpeg_exe = media_tools.ffmpeg()
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            src = td_path / 'source.mp4'
            gp = td_path / 'gameplay.mp4'
            subprocess.run([
                ffmpeg_exe, '-y', '-v', 'error',
                '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=30:duration=4',
                '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100:duration=4',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', str(src),
            ], check=True)
            subprocess.run([
                ffmpeg_exe, '-y', '-v', 'error',
                '-f', 'lavfi', '-i', 'testsrc=size=640x480:rate=30:duration=5',
                '-f', 'lavfi', '-i', 'sine=frequency=220:sample_rate=44100:duration=5',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', str(gp),
            ], check=True)

            info = {
                'id': 'lavfi_test',
                'titulo': 'Teste Sintético',
                'duracao': 4.0,
                'source_path': str(src),
                'fonte_url': str(src),
                'canal': 'local',
                'autorizado': True,
                'credito': '@TesteLocal',
                'data': '2026-11-13T00:00:00Z',
            }
            transcription = {
                'video_id': 'lavfi_test',
                'modelo': 'small',
                'idioma': 'pt',
                'palavras': [
                    {'texto': 'Teste', 'inicio': 0.6, 'fim': 1.1},
                    {'texto': 'real', 'inicio': 1.15, 'fim': 1.7},
                    {'texto': 'funcionando!', 'inicio': 1.75, 'fim': 2.5},
                ],
                'unidades': [],
            }
            candidate = {
                'indice': 1,
                'inicio': 0.5,
                'fim': 3.5,
                'nota': 9.0,
                'gancho': 'Teste real funcionando!',
                'motivo': 'Teste sintético.',
                'texto': 'Teste real funcionando!',
            }
            with patch.object(engine, 'ROOT', td_path):
                out_mp4 = clip_render.render_clip(
                    info, transcription, candidate, gameplay=gp, cut_index=1,
                    modo_corte='ajustar', posicao_legenda='juncao',
                    titulo_na_tela=True, credito_na_tela=True,
                    musica_modo='impacto', musica_volume=22,
                    impacto_inicial='impacto', impacto_volume=70,
                    log=lambda *_: None,
                )
            self.assertTrue(out_mp4.is_file())
            self.assertGreater(out_mp4.stat().st_size, 1000)
            probe = subprocess.run(
                [ffmpeg_exe, '-hide_banner', '-i', str(out_mp4)],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
            )
            self.assertIn('1080x1920', probe.stderr)
            self.assertIn('30 fps', probe.stderr)
            self.assertIn('Audio:', probe.stderr)
            dur_rendered = engine.ffprobe_duration(out_mp4)
            self.assertAlmostEqual(dur_rendered, 3.0, delta=0.35)


if __name__ == '__main__':
    unittest.main(verbosity=2)