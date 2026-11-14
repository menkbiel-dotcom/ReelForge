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
        self.assertIn('boxblur=20:5', c1['fc'])
        self.assertIn('scale=1080:960:force_original_aspect_ratio=decrease', c1['fc'])
        self.assertIn('[top][bottom]vstack=inputs=2[stacked]', c1['fc'])
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
        self.assertIn('scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960', c2['fc'])
        self.assertIn(r'\pos(540,1650)', c2['ass'])
        self.assertNotIn(',Hook,', c2['ass'])


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
            out_mp4 = clip_render.render_clip(
                info, transcription, candidate, gameplay=gp, cut_index=1,
                modo_corte='ajustar', posicao_legenda='juncao',
                titulo_na_tela=True, credito_na_tela=True, log=lambda *_: None,
            )
            self.assertTrue(out_mp4.is_file())
            self.assertGreater(out_mp4.stat().st_size, 1000)
            probe = subprocess.run(
                [ffmpeg_exe, '-hide_banner', '-i', str(out_mp4)],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
            )
            self.assertIn('1080x1920', probe.stderr)
            self.assertIn('30 fps', probe.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)