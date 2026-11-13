"""Testes offline do fluxo automático RSS -> Ollama (rede e modelo simulados)."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
import news_auto
import render_pipeline
import voice_studio
from tests._isolation import IsolatedOutputMixin

HAS_NUMPY = importlib.util.find_spec('numpy') is not None


def entry(title, source, link):
    return {'title': f'{title} - {source}', 'source': {'title': source}, 'link': link,
            'published': 'Sun, 08 Nov 2026 02:00:00 GMT', 'summary': ''}


FEED = [entry('Governo anuncia novo programa de internet nas escolas públicas', 'G1', 'https://a/1'),
        entry('TECNOLOGIA 🖥️ post viral sobre celulares', 'Instagram', 'https://a/2'),
        entry('Sony relança Walkman com preço de R$ 32 mil no Brasil', 'O Globo', 'https://a/3')]


class NewsAutoTests(IsolatedOutputMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.db = patch.object(engine, 'DB', Path(self.temp.name) / 'h.sqlite3')
        self.db.start()

    def tearDown(self):
        self.db.stop()
        self.temp.cleanup()
        super().tearDown()

    def test_candidates_strip_source_and_skip_social_posts(self):
        with patch('news_auto.read_feed', return_value=FEED):
            items = news_auto.gather_candidates('tecnologia')
        self.assertEqual([i['source'] for i in items], ['G1', 'O Globo'])
        self.assertFalse(items[0]['title'].endswith('G1'))

    def test_used_story_is_not_offered_again(self):
        with patch('news_auto.read_feed', return_value=FEED):
            first = news_auto.gather_candidates('tecnologia')[0]
            engine.mark_used(dict(first, source_type='rss'))
            again = news_auto.gather_candidates('tecnologia')
        self.assertNotIn(first['id'], [i['id'] for i in again])

    def test_ollama_picks_story_by_index(self):
        cands = [{'title': 'A', 'source': '', 'published': ''}, {'title': 'B', 'source': '', 'published': ''}]
        with patch.object(engine, 'load_settings', return_value={'escolha_pauta_llm': True}):
            story = news_auto.pick_story(cands, ask=lambda i, p: {'index': '1', 'reason': 'ok'})
        self.assertEqual(story['title'], 'B')

    def test_script_alternates_speakers_and_rejects_invented_numbers(self):
        story = {'title': 'Sony relança Walkman com preço de R$ 32 mil no Brasil',
                 'body': 'A Sony confirmou o novo Walkman por R$ 32 mil no mercado brasileiro com foco em áudio de estúdio. ' * 3}
        facts_resp = {'fatos': ['A Sony relançou o Walkman no Brasil.', 'O aparelho custa R$ 32 mil.',
                                'O foco desse modelo é áudio de estúdio.', 'O modelo chega este mês.', 'É voltado a profissionais.']}
        bad = {'lines': [{'speaker': 0, 'text': 'Pagaria 50 mil reais num Walkman?', 'fato': 1}] + [{'speaker': 0, 'text': 'O aparelho custa 32 mil.', 'fato': 1}] * 5}
        good = {'lines': [{'speaker': 1, 'text': 'Naruto: Pagaria 32 mil reais num Walkman hoje?'},
                          {'speaker': 0, 'text': 'R$ 32 mil é preço de carro!'},
                          {'speaker': 1, 'text': 'A Sony lançou esse modelo focado em estúdio.'},
                          {'speaker': 0, 'text': 'Ele chega este mês voltado a profissionais.'},
                          {'speaker': 1, 'text': 'Caramba, só colecionador!'},
                          {'speaker': 0, 'text': 'Você compraria o Walkman da Sony no Brasil?'}]}
        answers = iter([facts_resp, bad, good, {'nota': 9, 'problemas': []}])
        lines = news_auto.write_script(story, max_words=80, ask=lambda i, p: next(answers), loop=False)
        self.assertEqual([l['speaker'] for l in lines], [1, 0, 1, 0, 1, 0])
        self.assertEqual(lines[0]['text'], 'Pagaria 32 mil reais num Walkman hoje?')

    def test_article_text_from_rss_is_cleaned(self):
        body = ('<p>Resumo</p><p>A Anthropic fechou um acordo de US$ 3,4 bilhões para comprar geradores.</p>'
                '<p>Leia também: outra coisa qualquer aqui</p><figure><figcaption>Foto: X</figcaption></figure>'
                '<p>The post Anthropic appeared first on Olhar Digital.</p>')
        text = news_auto.article_text({'summary': '', 'content': [{'value': body}]})
        self.assertIn('US$ 3,4 bilhões', text)
        self.assertNotIn('Leia também', text)
        self.assertNotIn('appeared first', text)
        self.assertNotIn('Foto', text)

    def test_generic_script_is_rejected_when_article_exists(self):
        story = {'title': 'Anthropic compra geradores no Texas', 'related': [],
                 'body': 'A Anthropic fechou um acordo de US$ 3,4 bilhões com a Caterpillar no Texas para alimentar data centers com sua rede. ' * 10}
        vague = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in
                           ('Sabia que uma empresa fez um acordo?', 'Isso é importante.',
                            'A tecnologia avança.', 'O que você acha disso?')]}
        with self.assertRaisesRegex(ValueError, 'genérico'):
            news_auto.validate_lines(vague, story, 60, min_lines=4)
        rich = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in
                          ('Por que a Anthropic gastou 3,4 bilhões?', 'O acordo foi direto com a Caterpillar.',
                           'Os geradores vão para o Texas.', 'Será que a rede aguenta os data centers?')]}
        lines, problem = news_auto.validate_lines(rich, story, 60, min_lines=4)
        self.assertEqual(problem, '')
        self.assertEqual(len(lines), 4)

    def test_validation_without_bridge_allows_odd_turns_and_rejects_dangling_ending(self):
        self.assertFalse(hasattr(news_auto, 'loop_bridge'))
        self.assertFalse(hasattr(news_auto, 'DEFAULT_BRIDGE'))
        self.assertFalse(news_auto.loop_enabled())
        story = {'title': 'Sony relança Walkman por R$ 32 mil no Brasil',
                 'body': 'A Sony lançou no Brasil o novo Walkman por R$ 32 mil para audiófilos ouvirem música com alta definição. ' * 3}
        odd_complete = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in (
            'Pagaria R$ 32 mil num Walkman novo?',
            'A Sony trouxe o aparelho de volta ao Brasil.',
            'Ele é feito pela Sony para audiófilos ouvirem música.',
            'Mesmo assim o preço do novo Walkman assustou.',
            'Vale R$ 32 mil só para ouvir música?'
        )]}
        lines, problem = news_auto.validate_lines(odd_complete, story, 80, loop=False, min_lines=4, max_lines=8)
        self.assertEqual(problem, '')
        self.assertEqual(len(lines), 5)
        dangling = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in (
            'Pagaria R$ 32 mil num Walkman novo?',
            'A Sony trouxe o aparelho de volta ao Brasil.',
            'Ele é feito pela Sony para audiófilos ouvirem música.',
            'E todo mundo no Brasil quer o novo Walkman porque'
        )]}
        with self.assertRaisesRegex(ValueError, 'ponte incompleta'):
            news_auto.validate_lines(dangling, story, 80, loop=False, min_lines=4, max_lines=8)

    def test_vehicle_citation_and_telejornal_phrases_are_rejected(self):
        story = {'title': 'Petrobras anuncia novo poço na Bacia de Campos',
                 'source': 'CPG Click Petróleo e Gás',
                 'body': 'A Petrobras confirmou a descoberta de 200 milhões de barris na Bacia de Campos. ' * 4}
        bad_cpg = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in (
            'Viu a descoberta de 200 milhões de barris?',
            'A Petrobras achou petróleo na Bacia de Campos.',
            'Dados confirmados pela CPG Click Petróleo e Gás.',
            'Será que a gasolina baixa agora?'
        )]}
        with self.assertRaisesRegex(ValueError, 'veículo|confirmado por'):
            news_auto.validate_lines(bad_cpg, story, 80, min_lines=4)
        bad_uol = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in (
            'Viu a descoberta de 200 milhões de barris?',
            'A Petrobras achou petróleo na Bacia de Campos.',
            'Tudo isso está confirmado pelo Uol nesta segunda-feira.',
            'Será que a gasolina baixa agora?'
        )]}
        with self.assertRaisesRegex(ValueError, 'veículo|confirmado por'):
            news_auto.validate_lines(bad_uol, story, 80, min_lines=4)

    def test_related_headlines_are_rejected_as_fact_evidence(self):
        story = {
            'title': 'MEC divulga gabarito oficial do Enem 2026 com 180 questões',
            'body': 'O Inep e o MEC liberaram o gabarito das 180 questões do Enem no Brasil. ' * 5,
            'related': ['Daniela Toffoli comenta diferença entre valorizar e elogiar (G1)',
                        'Operação investiga desvio de R$ 999 milhões (UOL)'],
        }
        self.assertNotIn('Toffoli', news_auto.source_text(story))
        self.assertNotIn('999', news_auto.source_text(story))
        leaked_entity = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in (
            'Já conferiu as 180 questões do Enem?',
            'O MEC e o Inep liberaram o gabarito no Brasil.',
            'Daniela Toffoli até comentou o tema.',
            'Quantos acertos você conseguiu fazer?'
        )]}
        with self.assertRaisesRegex(ValueError, 'Contaminação|manchete relacionada'):
            news_auto.validate_lines(leaked_entity, story, 80, min_lines=4)
        leaked_number = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in (
            'Já conferiu as 180 questões do Enem?',
            'O MEC e o Inep liberaram o gabarito no Brasil.',
            'Foram 999 milhões envolvidos no caso.',
            'Quantos acertos você conseguiu fazer?'
        )]}
        with self.assertRaisesRegex(ValueError, 'Números ausentes'):
            news_auto.validate_lines(leaked_number, story, 80, min_lines=4)
        with self.assertRaisesRegex(ValueError, 'sem corpo'):
            news_auto.require_minimum_source({'title': 'Só título curto', 'body': ''})

    def test_two_step_extraction_and_quality_filter_regenerates_below_7(self):
        story = {
            'title': 'Anthropic fecha acordo de US$ 3,4 bilhões em geradores no Texas',
            'body': ('A Anthropic assinou um contrato de US$ 3,4 bilhões com a Caterpillar no Texas '
                     'para garantir energia até 2045 em seus data centers de inteligência artificial. ' * 5),
        }
        step1_facts = {'fatos': [
            'A Anthropic fechou contrato de US$ 3,4 bilhões para abastecer data centers.',
            'A Anthropic comprou geradores da Caterpillar por US$ 3,4 bilhões.',
            'A rede elétrica do Texas tem risco de faltar luz no resto do estado.',
            'O acordo garante energia até 2045.',
            'O objetivo é abastecer data centers de IA.',
        ]}
        script_v1 = {'lines': [
            {'speaker': 0, 'text': 'Por que gastar 3,4 bilhões em geradores?', 'fato': 0},
            {'speaker': 1, 'text': 'A Anthropic comprou tudo da Caterpillar.', 'fato': 1},
            {'speaker': 0, 'text': 'A rede elétrica fica no Texas.', 'fato': 2},
            {'speaker': 1, 'text': 'A energia vai até 2045 nos data centers.', 'fato': 3},
            {'speaker': 0, 'text': 'O objetivo é abastecer data centers de IA.', 'fato': 4},
            {'speaker': 1, 'text': 'Será que a Anthropic gastou 3,4 bilhões mesmo?', 'fato': 0}
        ]}
        eval_low = {'nota': 5, 'problemas': ['Falas finais pouco espontâneas']}
        script_v2 = {'lines': [
            {'speaker': 0, 'text': 'Por que gastar 3,4 bilhões só em gerador?', 'fato': 0},
            {'speaker': 1, 'text': 'Cara, a Anthropic comprou tudo da Caterpillar!', 'fato': 1},
            {'speaker': 0, 'text': 'É que a rede elétrica do Texas não aguenta mais.', 'fato': 2},
            {'speaker': 1, 'text': 'Eles garantiram energia até 2045 pros data centers.', 'fato': 3},
            {'speaker': 0, 'text': 'Mano, é muita energia!', 'reacao': True},
            {'speaker': 1, 'text': 'Será que vai faltar luz pro resto do estado?', 'fato': 2}
        ]}
        eval_high = {'nota': 9, 'problemas': []}
        responses = iter([step1_facts, script_v1, eval_low, script_v2, eval_high])
        calls = []

        def fake_ask(inst, payload):
            calls.append((inst, payload))
            return next(responses)

        lines = news_auto.write_script(story, max_words=100, ask=fake_ask, loop=False)
        self.assertEqual(story.get('quality_score'), 9)
        self.assertEqual(len(calls), 5)  # 1 extração + 1 roteiro v1 + 1 nota 5 + 1 roteiro v2 + 1 nota 9
        self.assertIn('Será que vai faltar luz', lines[-1]['text'])

    @unittest.skipUnless(HAS_NUMPY, 'Requer numpy instalado')
    def test_generated_music_loops_seamlessly(self):
        import music, wave, numpy as np
        dest = Path(self.temp.name) / 'm.wav'
        music.generate(dest, bpm=120, repeats=1)
        with wave.open(str(dest)) as w:
            data = np.frombuffer(w.readframes(w.getnframes()), '<i2').reshape(-1, 2)[:, 0].astype(float)
        jump = abs(data[0] - data[-1])
        typical = np.mean(np.abs(np.diff(data)))
        self.assertLess(jump, typical * 20)

    def test_actor_windows_cover_pauses_one_actor_at_a_time(self):
        lines = [{'speaker': 0, 'start': .25, 'end': 2}, {'speaker': 1, 'start': 2.14, 'end': 4},
                 {'speaker': 0, 'start': 4.14, 'end': 6}]
        a, b = render_pipeline.turns(lines, 6.5, 0), render_pipeline.turns(lines, 6.5, 1)
        self.assertEqual(a, [(.25, 2.14), (4.14, 6.5)])
        self.assertEqual(b, [(2.14, 4.14)])

    def test_whisper_trailing_prompt_echo_with_zero_duration_is_ignored(self):
        import captions
        raw = [{"text": "Ele", "start": 0.0, "end": 0.3}, {"text": "mistura", "start": 0.3, "end": 0.76},
               {"text": "croissão", "start": 0.76, "end": 1.38}, {"text": "e", "start": 1.38, "end": 1.46},
               {"text": "waffle", "start": 1.46, "end": 1.82}, {"text": "de", "start": 1.82, "end": 1.98},
               {"text": "forma", "start": 1.98, "end": 2.26}, {"text": "única.", "start": 2.26, "end": 2.68},
               {"text": "Ele", "start": 2.68, "end": 2.7}, {"text": "mistura", "start": 2.7, "end": 2.82},
               {"text": "croissant", "start": 2.82, "end": 2.82}, {"text": "e", "start": 2.82, "end": 2.82},
               {"text": "waffle", "start": 2.82, "end": 2.84}, {"text": "de", "start": 2.84, "end": 2.84},
               {"text": "forma", "start": 2.84, "end": 2.84}, {"text": "única.", "start": 2.84, "end": 2.84}]
        words = captions.match_words('Ele mistura croissant e waffle de forma única.', raw, 2.85)
        self.assertEqual(len(words), 8)
        self.assertEqual(words[2]['text'], 'croissant')
        self.assertAlmostEqual(words[2]['start'], 0.76)
        self.assertAlmostEqual(words[2]['end'], 1.38)
        self.assertAlmostEqual(words[-1]['end'], 2.68)
        self.assertFalse(any(w['estimated'] for w in words))

    def test_slurred_tts_splits_and_partial_anchors_never_fail_alignment(self):
        import captions
        raw = [{"text": "Comunicão,", "start": 0.0, "end": 0.66}, {"text": "irão", "start": 0.84, "end": 1.08},
               {"text": "para", "start": 1.08, "end": 1.22}, {"text": "a", "start": 1.22, "end": 1.4},
               {"text": "questão", "start": 1.4, "end": 1.5}, {"text": "de", "start": 1.5, "end": 1.68},
               {"text": "estoura", "start": 1.68, "end": 2.08}, {"text": "gel", "start": 2.08, "end": 2.24},
               {"text": "política!", "start": 2.24, "end": 2.68}]
        line = {'text': 'EUA e Irã caíram na questão de história e geopolítica.', 'duration': 3.05, 'audio': 'dummy'}
        with patch('captions.whisper_words', return_value=raw):
            captions.align_lines([line], '.', mode='whisper')
        self.assertEqual(line['words'][-1]['text'], 'geopolítica.')
        self.assertFalse(line['words'][-1]['estimated'])
        self.assertAlmostEqual(line['words'][-1]['start'], 2.08, places=2)
        self.assertAlmostEqual(line['words'][-1]['end'], 2.68, places=2)

    def test_turn_range_scales_with_article_context_up_to_12(self):
        self.assertEqual(news_auto.turn_range({'title': 'Curta', 'body': ''}, 120), (6, 8))
        self.assertEqual(news_auto.turn_range({'title': 'Média', 'body': 'palavra ' * 120}, 120), (6, 10))
        self.assertEqual(news_auto.turn_range({'title': 'Rica', 'body': 'palavra ' * 350}, 140), (6, 12))

    def test_voicestudio_reference_reuses_memory_cache_within_ttl(self):
        voice_studio._ref_cache.clear()
        voices_resp = Mock(status_code=200)
        voices_resp.json.return_value = {'voices': [{'type': 'profile', 'name': 'Sasuke', 'voice_id': 'v-sasuke'}]}
        voices_resp.raise_for_status = Mock()
        audio_resp = Mock(status_code=200, content=b'RIFF-ref-audio-bytes')
        audio_resp.raise_for_status = Mock()
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.get.side_effect = [voices_resp, audio_resp]
        with patch('voice_studio._ensure_voicestudio_running'), patch('voice_studio.requests.Session', return_value=session):
            first = voice_studio.reference('sasuke')
            second = voice_studio.reference('  SASUKE  ')
        self.assertEqual(first, b'RIFF-ref-audio-bytes')
        self.assertEqual(second, b'RIFF-ref-audio-bytes')
        self.assertEqual(session.get.call_count, 2)  # 1 /v1/audio/voices + 1 /profiles/.../audio na 1ª chamada; 0 na 2ª

    def test_prepared_reference_caches_on_disk_and_memory_without_retranscribing(self):
        voice_studio._prepared_cache.clear()
        cache_dir = Path(self.temp.name) / 'vs_cache'
        ref_bytes = b'RIFF-test-voice-reference-sample'

        def fake_run(cmd):
            Path(cmd[-1]).write_bytes(b'RIFF-short-24k-mono')
            return ''

        fake_words = [{'text': 'Olá'}, {'text': 'mundo'}, {'text': 'teste'}, {'text': 'voz'}]
        with patch('voice_studio.run', side_effect=fake_run) as run_mock, \
             patch('voice_studio.whisper_words', return_value=fake_words) as whisper_mock:
            p1, t1 = voice_studio._prepared_reference(ref_bytes, cache_dir)
            p2, t2 = voice_studio._prepared_reference(ref_bytes, cache_dir)
            voice_studio._prepared_cache.clear()
            p3, t3 = voice_studio._prepared_reference(ref_bytes, cache_dir)

        self.assertEqual((p1, t1), (p2, t2))
        self.assertEqual((p1, t1), (p3, t3))
        self.assertEqual(t1, 'Olá mundo teste voz')
        self.assertEqual(run_mock.call_count, 1)
        self.assertEqual(whisper_mock.call_count, 1)
        self.assertTrue(p1.is_file())
        self.assertTrue(p1.with_suffix('.txt').is_file())

    def test_render_filtergraph_contains_vignette_acompressor_and_loudnorm(self):
        gameplay = Path(self.temp.name) / 'bg.mp4'
        gameplay.write_bytes(b'fake-mp4')
        audio0 = Path(self.temp.name) / 'a0.mp3'
        audio1 = Path(self.temp.name) / 'a1.mp3'
        audio0.write_bytes(b'fake-mp3-0')
        audio1.write_bytes(b'fake-mp3-1')
        lines = [
            {'speaker': 0, 'text': 'Primeira fala aqui?', 'duration': 1.2, 'audio': audio0},
            {'speaker': 1, 'text': 'Segunda fala aqui!', 'duration': 1.3, 'audio': audio1},
        ]

        async def fake_fit(lns, *args, **kwargs):
            return lns

        cmds = []

        def fake_run(cmd):
            cmds.append([str(c) for c in cmd])
            Path(cmd[-1]).write_bytes(b'out')
            return ''

        with patch.object(engine, 'OUTPUT', Path(self.temp.name) / 'out'), \
             patch.object(engine, 'fit_narration', side_effect=fake_fit), \
             patch.object(engine, 'run', side_effect=fake_run), \
             patch.object(engine, 'ffprobe_duration', return_value=30.0), \
             patch.object(engine, 'character', side_effect=lambda p, i, m: Path(p).write_bytes(b'png')), \
             patch.object(engine, 'mark_used'), \
             patch('captions.align_lines'), \
             patch('captions.write_ass', side_effect=lambda l, p, *a, **k: Path(p).write_text('ass', encoding='utf-8')), \
             patch.dict('os.environ', {'REELFORGE_MUSIC': '0'}):
            render_pipeline.render(engine, {'title': 'T'}, lines, gameplay, fps=30, max_seconds=30)

        self.assertEqual(len(cmds), 2)
        mix_cmd, final_cmd = ' '.join(cmds[0]), ' '.join(cmds[1])
        self.assertIn('highpass=f=80,acompressor=threshold=-18dB:ratio=2.2:attack=10:release=120,alimiter=limit=0.95:level=false', mix_cmd)
        self.assertIn('vignette=PI/6', final_cmd)
        self.assertIn('loudnorm=I=-14:TP=-1.5:LRA=11', final_cmd)
        self.assertIn('-threads 0', final_cmd)

    def test_evaluate_script_fails_closed_after_retry_when_ollama_errors(self):
        lines = [
            {'speaker': 0, 'text': 'Por que gastar 3,4 bilhões só em gerador?'},
            {'speaker': 1, 'text': 'Cara, a Anthropic comprou tudo da Caterpillar!'},
            {'speaker': 0, 'text': 'A rede elétrica do Texas não aguenta mais.'},
            {'speaker': 1, 'text': 'Será que vai faltar luz pro resto do estado?'},
        ]
        logs = []
        calls = {'n': 0}

        def failing_ask(inst, payload):
            calls['n'] += 1
            raise RuntimeError('Ollama offline')

        res = news_auto.evaluate_script(lines, {'title': 'T'}, ask=failing_ask, log=logs.append)
        self.assertEqual(calls['n'], 2)
        self.assertIsNone(res['nota'])
        self.assertIn('avaliação indisponível', res['problemas'])
        self.assertTrue(any('indisponível' in m for m in logs))

        # Se falhar na 1ª e responder na 2ª tentativa, aproveita a nota real:
        seq = iter([ ValueError('json inválido'), {'nota': 9, 'problemas': []} ])
        def flaky_ask(inst, payload):
            item = next(seq)
            if isinstance(item, Exception):
                raise item
            return item

        res_ok = news_auto.evaluate_script(lines, {'title': 'T'}, ask=flaky_ask, log=logs.append)
        self.assertEqual(res_ok['nota'], 9)

    def test_low_score_after_two_regenerations_saves_rejected_and_raises_unless_flag(self):
        story = {
            'title': 'Anthropic fecha acordo de US$ 3,4 bilhões em geradores no Texas',
            'body': ('A Anthropic assinou um contrato de US$ 3,4 bilhões com a Caterpillar no Texas '
                     'para garantir energia até 2045 em seus data centers de inteligência artificial. ' * 5),
        }
        facts_resp = {'fatos': [
            'A Anthropic fechou contrato de US$ 3,4 bilhões.',
            'A Anthropic comprou geradores da Caterpillar por US$ 3,4 bilhões.',
            'A rede elétrica do Texas tem risco de faltar luz no resto do estado.',
            'O acordo garante energia até 2045.',
            'O objetivo é abastecer data centers de IA.',
        ], 'contradicao': 'Investimento recorde mas risco de falta de luz.', 'gancho': 'Por que gastar 3,4 bilhões só em gerador?'}
        script = {'lines': [
            {'speaker': 0, 'text': 'Por que gastar 3,4 bilhões só em gerador?', 'fato': 0},
            {'speaker': 1, 'text': 'Cara, a Anthropic comprou tudo da Caterpillar!', 'fato': 1},
            {'speaker': 0, 'text': 'É que a rede elétrica do Texas não aguenta mais.', 'fato': 2},
            {'speaker': 1, 'text': 'Eles garantiram energia até 2045 pros data centers.', 'fato': 3},
            {'speaker': 0, 'text': 'Mano, é muita energia!', 'reacao': True},
            {'speaker': 1, 'text': 'Será que vai faltar luz pro resto do estado?', 'fato': 2}
        ]}
        low_eval = {'nota': 5, 'problemas': [{'i': 1, 'trecho': 'comprou tudo da Caterpillar', 'tipo': 'informalidade', 'motivo': 'Diálogo pouco natural'}]}
        out_dir = Path(self.temp.name) / 'out_rej'
        # Inicial + 2 regenerações = 3 roteiros avaliados com nota 5 -> levanta RuntimeError e salva rejected_*.json
        responses = iter([facts_resp, script, low_eval, script, low_eval, script, low_eval])
        with patch.object(engine, 'OUTPUT', out_dir), patch.dict('os.environ', {'REELFORGE_ACCEPT_LOW_SCORE': '0'}):
            with self.assertRaisesRegex(RuntimeError, 'reprovado no filtro de qualidade'):
                news_auto.write_script(story, max_words=100, ask=lambda i, p: next(responses),
                                       loop=False, log=lambda *a: None)
        rejected_files = list(out_dir.glob('rejected_*.json'))
        self.assertEqual(len(rejected_files), 1)
        saved = news_auto.json.loads(rejected_files[0].read_text(encoding='utf-8'))
        self.assertEqual(saved['nota'], 5)
        self.assertTrue(any('Diálogo pouco natural' in str(p) for p in saved['problemas']))

        # Com a flag ativa (--aceitar-nota-baixa / REELFORGE_ACCEPT_LOW_SCORE=1), permite seguir:
        responses_flag = iter([facts_resp, script, low_eval, script, low_eval, script, low_eval])
        with patch.object(engine, 'OUTPUT', out_dir), patch.dict('os.environ', {'REELFORGE_ACCEPT_LOW_SCORE': '1'}):
            accepted = news_auto.write_script(story, max_words=100, ask=lambda i, p: next(responses_flag),
                                              loop=False, log=lambda *a: None)
        self.assertEqual(len(accepted), 6)

    def test_deterministic_check_deducts_two_points_and_lists_reasons(self):
        # Sem pergunta, com fala repetida e 3 falas começando com "Cara":
        bad_lines = [
            {'speaker': 0, 'text': 'Cara isso aqui mudou tudo no mercado agora.'},
            {'speaker': 1, 'text': 'Cara isso aqui mudou tudo no mercado agora.'},
            {'speaker': 0, 'text': 'Cara ninguém esperava esse resultado tão cedo.'},
            {'speaker': 1, 'text': 'Fato curto.'},
        ]
        review = news_auto.evaluate_script(
            bad_lines, {'title': 'T'},
            ask=lambda i, p: {'nota': 8, 'problemas': []},
            log=lambda *a: None,
        )
        self.assertEqual(review['nota'], 6)
        joined = ' | '.join(review['problemas'])
        self.assertIn('pergunta', joined)
        self.assertIn('repetida', joined)
        self.assertIn('duas anteriores', joined)

    def test_google_news_entry_with_li_summary_has_empty_body_and_fails_minimum_source(self):
        li_items = ''.join(
            f'<li>Manchete agregada número {i} com várias palavras extras sobre economia e política no país - Portal {i}</li>'
            for i in range(12)
        )
        gnews_entry = {
            'title': 'Banco Central anuncia nova medida cambial - Valor',
            'source': {'title': 'Valor'},
            'link': 'https://news.google.com/rss/articles/123',
            'published': 'Sun, 08 Nov 2026 02:00:00 GMT',
            'summary': f'<ol>{li_items}</ol>',
        }
        parsed = news_auto.parse_entry(gnews_entry, feed_name=None)
        self.assertEqual(parsed['body'], '')
        self.assertGreaterEqual(len(parsed['related']), 1)
        with self.assertRaisesRegex(ValueError, 'sem corpo'):
            news_auto.require_minimum_source(parsed)

    def test_fictional_few_shot_terms_are_rejected_when_absent_from_article(self):
        story = {
            'title': 'Sony relança Walkman por R$ 32 mil no Brasil',
            'body': 'A Sony lançou no Brasil o novo Walkman por R$ 32 mil focado em áudio de estúdio. ' * 4,
        }
        leaked = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in (
            'Pagaria R$ 32 mil num Walkman novo?',
            'A Sony lançou esse modelo no Brasil.',
            'Lá em Valoria a Zorbax tentou algo parecido.',
            'Vale tudo isso só para ouvir música?'
        )]}
        with self.assertRaisesRegex(ValueError, 'exemplos fictícios'):
            news_auto.validate_lines(leaked, story, 80, min_lines=4)

        prompts = []
        facts_resp = {'fatos': [
            'A Sony lançou o Walkman no Brasil.',
            'O preço oficial é de R$ 32 mil.',
            'O modelo é focado em áudio de estúdio.',
            'O público-alvo são profissionais de som.',
            'O aparelho chegou ao mercado nacional.',
        ], 'contradicao': 'Aparelho portátil com preço exorbitante de carro.', 'gancho': 'Pagaria R$ 32 mil num Walkman novo?'}
        good = {'lines': [
            {'speaker': 0, 'text': 'Pagaria R$ 32 mil num Walkman novo?', 'fato': 1},
            {'speaker': 1, 'text': 'O preço oficial é de R$ 32 mil no Brasil.', 'fato': 1},
            {'speaker': 0, 'text': 'O modelo é focado em áudio de estúdio.', 'fato': 2},
            {'speaker': 1, 'text': 'O público-alvo são profissionais de som.', 'fato': 3},
            {'speaker': 0, 'text': 'O aparelho chegou ao mercado nacional.', 'fato': 4},
            {'speaker': 1, 'text': 'Vale pagar 32 mil pelo preço oficial?', 'fato': 1}
        ]}
        seq = iter([facts_resp, good, {'nota': 9, 'problemas': []}])

        def capture_ask(inst, payload):
            prompts.append(inst)
            return next(seq)

        news_auto.write_script(story, max_words=80, ask=capture_ask, loop=False)
        self.assertIn('EXEMPLOS FICTÍCIOS: copie apenas o estilo e o ritmo, nunca os fatos', prompts[1])

    def test_spelled_out_numbers_and_romans_in_article_are_accepted_while_absent_rejected(self):
        story = {
            'title': 'Petrobras anuncia três novas plataformas na Bacia de Santos',
            'body': ('A Petrobras confirmou a construção de três novas plataformas na Bacia de Santos '
                     'com investimento da estatal no Brasil durante o século XXI. ' * 4),
            'related': ['Outra notícia cita sete navios no porto (UOL)'],
        }
        valid_with_digit = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in (
            'Sabia que a Petrobras terá 3 novas plataformas?',
            'A construção será lá na Bacia de Santos.',
            'É um projeto grande da estatal no Brasil.',
            'Será que a Petrobras vai construir mais plataformas?'
        )]}
        lines, problem = news_auto.validate_lines(valid_with_digit, story, 80, min_lines=4)
        self.assertEqual(problem, '')
        self.assertEqual(len(lines), 4)

        absent_digit = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in (
            'Sabia que a Petrobras terá 7 novas plataformas?',
            'A construção será lá na Bacia de Santos.',
            'É um projeto grande da estatal no Brasil.',
            'Será que a Petrobras vai construir mais plataformas?'
        )]}
        with self.assertRaisesRegex(ValueError, 'Números ausentes'):
            news_auto.validate_lines(absent_digit, story, 80, min_lines=4)

    def test_automatic_retries_next_candidate_when_chosen_story_lacks_minimum_source(self):
        cands = [
            {'id': '1', 'title': 'Só título curto', 'source': 'G1', 'url': 'https://a/1', 'published': '', 'body': ''},
            {'id': '2', 'title': 'Sony relança Walkman por R$ 32 mil no Brasil', 'source': 'O Globo', 'url': 'https://a/2',
             'published': '', 'body': 'A Sony confirmou o lançamento do Walkman por R$ 32 mil no Brasil com foco em estúdio. ' * 4},
        ]
        facts_resp = {'fatos': [
            'A Sony relançou o Walkman no Brasil.',
            'O aparelho custa R$ 32 mil.',
            'O foco é áudio de estúdio.',
            'A venda ocorre no mercado nacional.',
            'O produto mira audiófilos.',
        ], 'contradicao': 'Aparelho caro voltado a audiófilos.', 'gancho': 'Pagaria R$ 32 mil num Walkman novo?'}
        script_resp = {'lines': [
            {'speaker': 0, 'text': 'Pagaria R$ 32 mil num Walkman novo?'},
            {'speaker': 1, 'text': 'O preço oficial é de R$ 32 mil no Brasil.'},
            {'speaker': 0, 'text': 'O foco é áudio de estúdio profissional.'},
            {'speaker': 1, 'text': 'A venda no mercado nacional mira audiófilos!'},
            {'speaker': 0, 'text': 'Caramba, só colecionador!'},
            {'speaker': 1, 'text': 'Vale pagar 32 mil nesse aparelho no Brasil?'}
        ]}
        seq = iter([
            facts_resp,
            script_resp,
            {'nota': 9, 'problemas': []},
        ])
        with patch('news_auto.gather_candidates', return_value=cands), \
             patch('news_auto.enrich', return_value=[]):
            story, lines = news_auto.automatic('tecnologia', max_seconds=45, log=lambda *a: None, ask=lambda i, p: next(seq))
        self.assertEqual(story['id'], '2')
        self.assertEqual(len(lines), 6)

    def test_item1_tied_to_fact_and_reaction_rules(self):
        story = {
            'title': 'Brasil bate recorde e exporta 5,3 milhões de sacas de café em outubro',
            'body': ('O Brasil exportou 5,3 milhões de sacas de café em outubro. '
                     'A receita atingiu US$ 1,786 bilhão no período. '
                     'Os embarques para a Europa somaram 17,8 milhões de sacas. '
                     'O preço médio caiu 9,5% no acumulado do ano. '
                     'A colheita de conilon e robusta cresceu 72,5%. ' * 3),
            'extracted_facts': [
                'O Brasil exportou 5,3 milhões de sacas de café em outubro.',
                'A receita atingiu US$ 1,786 bilhão no período.',
                'Os embarques para a Europa somaram 17,8 milhões de sacas.',
                'O preço médio caiu 9,5% no acumulado do ano.',
                'A colheita de conilon e robusta cresceu 72,5%.',
            ]
        }
        # 1. Fala longa (>14 palavras) sem ligação com fatos (não cai em fato, reação ou opinião)
        bad_unclassified = {'lines': [
            {'speaker': 0, 'text': 'Sabia do novo recorde de café no Brasil?'},
            {'speaker': 1, 'text': 'Essa fala inventada não tem nenhuma relação com nada disso que estamos conversando agora neste exato momento aqui.'},
            {'speaker': 0, 'text': 'A receita atingiu US$ 1,786 bilhão no período.'},
            {'speaker': 1, 'text': 'A Europa recebeu 17,8 milhões de sacas e o preço caiu 9,5%.'},
        ]}
        with self.assertRaisesRegex(ValueError, 'opiniões próprias permitem no máximo 14 palavras'):
            news_auto.validate_lines(bad_unclassified, story, 80, min_lines=4)

        # 2. Reações consecutivas inferidas
        bad_consecutive_reactions = {'lines': [
            {'speaker': 0, 'text': 'Olha o recorde de 5,3 milhões no Brasil!'},
            {'speaker': 1, 'text': 'Caramba!'},
            {'speaker': 0, 'text': 'Mano do céu!'},
            {'speaker': 1, 'text': 'A receita atingiu US$ 1,786 bilhão e Europa somou 17,8 milhões e caiu 9,5%.'},
        ]}
        with self.assertRaisesRegex(ValueError, 'não podem ser consecutivas'):
            news_auto.validate_lines(bad_consecutive_reactions, story, 80, min_lines=4)

        # 3. Mais de 2 reações
        bad_3_reactions = {'lines': [
            {'speaker': 0, 'text': 'O Brasil exportou 5,3 milhões de sacas de café?'},
            {'speaker': 1, 'text': 'Caramba!'},
            {'speaker': 0, 'text': 'Sim, 5,3 milhões de sacas com receita de US$ 1,786 bilhão.'},
            {'speaker': 1, 'text': 'Pois é!'},
            {'speaker': 0, 'text': 'Europa comprou 17,8 milhões e preço caiu 9,5%.'},
            {'speaker': 1, 'text': 'Inacreditável!'},
            {'speaker': 0, 'text': 'O conilon cresceu 72,5% na colheita.'},
        ]}
        with self.assertRaisesRegex(ValueError, 'máximo 2 por roteiro'):
            news_auto.validate_lines(bad_3_reactions, story, 80, min_lines=4)

        # 4. Última fala sendo reação
        bad_last_reaction = {'lines': [
            {'speaker': 0, 'text': 'O Brasil exportou 5,3 milhões de sacas de café?'},
            {'speaker': 1, 'text': 'Caramba!'},
            {'speaker': 0, 'text': 'A receita foi de US$ 1,786 bilhão e Europa somou 17,8 milhões com queda de 9,5%.'},
            {'speaker': 1, 'text': 'Pois é!'},
        ]}
        with self.assertRaisesRegex(ValueError, 'última fala NÃO pode ser reação'):
            news_auto.validate_lines(bad_last_reaction, story, 80, min_lines=4)

        # 5. Fecho de enchimento (FILLER_CLOSE)
        bad_filler = {'lines': [
            {'speaker': 0, 'text': 'O Brasil exportou 5,3 milhões de sacas?'},
            {'speaker': 1, 'text': 'Caramba!'},
            {'speaker': 0, 'text': 'A receita foi de US$ 1,786 bilhão na Europa com 17,8 milhões.'},
            {'speaker': 1, 'text': 'Que legal ver o Brasil fazendo números assim!'},
        ]}
        with self.assertRaisesRegex(ValueError, 'fecho de enchimento'):
            news_auto.validate_lines(bad_filler, story, 80, min_lines=4)

        # 6. Número de outro fato é aceito mesmo com "fato": 0 errado (união + inferência)
        cross_fact = {'lines': [
            {'speaker': 0, 'text': 'Olha o recorde de 5,3 milhões de sacas de café!', 'fato': 0},
            {'speaker': 1, 'text': 'Caramba!'},
            {'speaker': 0, 'text': 'A receita atingiu US$ 1,786 bilhão no período.', 'fato': 0},
            {'speaker': 1, 'text': 'A Europa somou 17,8 milhões de sacas e o preço médio caiu 9,5%.', 'fato': 0},
        ]}
        res_cross, prob_cross = news_auto.validate_lines(cross_fact, story, 80, min_lines=4)
        self.assertEqual(prob_cross, '')
        self.assertEqual(res_cross[2]['fato'], 1)

        # 7. Roteiro válido sem campos fato/reacao (classificação inferida)
        valid = {'lines': [
            {'speaker': 0, 'text': 'Olha o recorde de 5,3 milhões de sacas de café!'},
            {'speaker': 1, 'text': 'Caramba, isso tudo?!'},
            {'speaker': 0, 'text': 'A receita atingiu US$ 1,786 bilhão no período.'},
            {'speaker': 1, 'text': 'E os embarques para a Europa somaram 17,8 milhões de sacas!'},
            {'speaker': 0, 'text': 'Mesmo assim o preço médio caiu 9,5% no acumulado.'},
            {'speaker': 1, 'text': 'Será que o conilon cresce ainda mais?'},
        ]}
        lines, prob = news_auto.validate_lines(valid, story, 80, min_lines=4)
        self.assertEqual(prob, '')
        self.assertEqual(len(lines), 6)
        self.assertTrue(lines[1].get('reacao'))
        self.assertEqual(lines[2].get('fato'), 1)

    def test_item2_quantity_pairs_and_qualifier_rules(self):
        # 1. _quantity_pairs direto
        pairs = news_auto._quantity_pairs('quase um bilhão')
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0], 1.0)
        self.assertEqual(pairs[0][1], 'bilhão')
        self.assertEqual(pairs[0].qualifier, 'quase')

        pairs_ext = news_auto._quantity_pairs('mais de dezesseis milhões')
        self.assertEqual(len(pairs_ext), 1)
        self.assertEqual(pairs_ext[0][0], 16.0)
        self.assertEqual(pairs_ext[0][1], 'milhão')
        self.assertEqual(pairs_ext[0].qualifier, 'mais de')

        pairs_curr = news_auto._quantity_pairs('US$ 1,786 bilhão')
        self.assertEqual(len(pairs_curr), 1)
        self.assertEqual(pairs_curr[0][0], 1.786)
        self.assertEqual(pairs_curr[0][1], 'bilhão')
        self.assertIsNone(pairs_curr[0].qualifier)

        # 2. Casos obrigatórios com validate_lines
        story = {
            'title': 'Exportações de café no Brasil batem recorde histórico',
            'body': (
                'O Brasil exportou 16,591 milhões de sacas nos primeiros quatro meses. '
                'A receita cambial somou US$ 1,786 bilhão, superando o recorde anterior '
                'de 1,657 bilhão registrado no ciclo passado. Os embarques cresceram no período.'
            ),
            'extracted_facts': [
                'O Brasil exportou 16,591 milhões de sacas nos primeiros quatro meses.',
                'A receita cambial somou US$ 1,786 bilhão.',
                'O recorde anterior era de 1,657 bilhão de dólares.',
                'Os embarques bateram recorde histórico.',
                'O mercado manteve ritmo forte nas vendas.',
            ]
        }

        # Caso 1 obrigatório: "quase um bilhão" com fonte 1,657 bilhão -> REJEITA (ceil(1.657) == 2, não 1)
        bad_quase_um = {'lines': [
            {'speaker': 0, 'text': 'Viu o novo recorde do café?', 'fato': 3},
            {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
            {'speaker': 0, 'text': 'Superou a marca anterior de quase um bilhão.', 'fato': 2},
            {'speaker': 1, 'text': 'Será que o preço do café continua caindo?', 'fato': 3},
        ]}
        with self.assertRaisesRegex(ValueError, 'Quantidade na fala 3 não condiz com a matéria'):
            news_auto.validate_lines(bad_quase_um, story, 80, min_lines=4)

        # Caso 2 obrigatório: "mais de dezesseis milhões" com fonte 16,591 milhões -> ACEITA (floor(16.591) == 16)
        valid_mais_de = {'lines': [
            {'speaker': 0, 'text': 'Os embarques bateram recorde no Brasil!', 'fato': 3},
            {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
            {'speaker': 0, 'text': 'Foram mais de dezesseis milhões de sacas.', 'fato': 0},
            {'speaker': 1, 'text': 'A receita foi de US$ 1,786 bilhão!', 'fato': 1},
            {'speaker': 0, 'text': 'Bateu a marca antiga de 1,657 bilhão.', 'fato': 2},
            {'speaker': 1, 'text': 'Será que os embarques continuam batendo recorde?', 'fato': 3},
        ]}
        lines, prob = news_auto.validate_lines(valid_mais_de, story, 100, min_lines=4)
        self.assertEqual(prob, '')
        self.assertEqual(len(lines), 6)

        # Caso 3 obrigatório: "US$ 1,786 bilhão" com fonte 1,786 bilhão -> ACEITA
        valid_exact = {'lines': [
            {'speaker': 0, 'text': 'Os embarques bateram recorde no Brasil!', 'fato': 3},
            {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
            {'speaker': 0, 'text': 'A receita foi de US$ 1,786 bilhão.', 'fato': 1},
            {'speaker': 1, 'text': 'Foram 16,591 milhões de sacas embarcadas.', 'fato': 0},
            {'speaker': 0, 'text': 'Bateu a marca antiga de 1,657 bilhão.', 'fato': 2},
            {'speaker': 1, 'text': 'Será que os embarques continuam batendo recorde?', 'fato': 3},
        ]}
        lines, prob = news_auto.validate_lines(valid_exact, story, 100, min_lines=4)
        self.assertEqual(prob, '')
        self.assertEqual(len(lines), 6)

    def test_item2_temporal_units_restriction(self):
        story = {
            'title': 'Exportações de café batem recorde em quatro primeiros meses',
            'body': (
                'O setor exportou nos quatro primeiros meses do ano volumes históricos. '
                'As remessas aumentaram sem interrupções durante os quatro primeiros meses.'
            ),
            'extracted_facts': [
                'O setor exportou nos quatro primeiros meses do ano.',
                'Volumes foram históricos nos quatro primeiros meses.',
                'As remessas aumentaram sem interrupções.',
                'O comércio de café segue forte.',
                'Produtores comemoram os resultados.',
            ]
        }

        # 1. Roteiro com "primeiro trimestre" com fonte "quatro primeiros meses" -> REJEITA
        bad_temporal_script = {'lines': [
            {'speaker': 0, 'text': 'Viu o recorde no primeiro trimestre?', 'fato': 0},
            {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
            {'speaker': 0, 'text': 'As remessas aumentaram sem interrupções.', 'fato': 2},
            {'speaker': 1, 'text': 'Será que o volume continua alto?', 'fato': 0},
        ]}
        with self.assertRaisesRegex(ValueError, 'Unidades temporais ausentes na matéria: trimestre'):
            news_auto.validate_lines(bad_temporal_script, story, 80, min_lines=4)

        # 2. extract_facts com fato contendo "primeiro trimestre" quando fonte tem apenas "quatro primeiros meses"
        calls = []
        def mock_ask(instruction, payload_str):
            calls.append(payload_str)
            if len(calls) < 2:
                return {'fatos': [
                    'Recorde histórico no primeiro trimestre.',
                    'Quatro primeiros meses tiveram alta.',
                    'As remessas aumentaram sem parar.',
                    'O café brasileiro bateu recorde.',
                    'Os produtores comemoram o resultado.',
                ]}
            return {'fatos': [
                'Recorde histórico nos quatro primeiros meses.',
                'Setor registrou volumes expressivos.',
                'As remessas aumentaram sem parar.',
                'O café brasileiro bateu recorde.',
                'Os produtores comemoram o resultado.',
            ], 'contradicao': 'Volume recorde com preços em oscilação.', 'gancho': 'Viu o recorde nos quatro primeiros meses?'}

        facts = news_auto.extract_facts(story, ask=mock_ask)
        self.assertEqual(len(facts), 5)
        self.assertIn('quatro primeiros meses', facts[0].lower())
        self.assertEqual(len(calls), 2)

    def test_item3_fact_checking_judge_and_criteria(self):
        import json
        story = {
            'title': 'Exportações de café no Brasil batem recorde histórico',
            'body': (
                'O Brasil exportou 16,591 milhões de sacas nos quatro primeiros meses. '
                'A receita cambial somou US$ 1,786 bilhão, superando o recorde anterior '
                'de 1,657 bilhão registrado no ciclo passado. A queda de 9,5% no preço médio '
                'aconteceu pelo aumento do conilon. Os embarques cresceram no período.'
            ),
            'extracted_facts': [
                'O Brasil exportou 16,591 milhões de sacas nos quatro primeiros meses.',
                'A receita cambial somou US$ 1,786 bilhão.',
                'O recorde anterior era de 1,657 bilhão de dólares.',
                'O preço médio caiu 9,5% pelo aumento do café conilon.',
                'Os embarques bateram recorde histórico.',
            ]
        }
        lines = [
            {'speaker': 0, 'text': 'Os embarques bateram recorde no Brasil!', 'fato': 4},
            {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
            {'speaker': 0, 'text': 'A receita foi de US$ 1,786 bilhão.', 'fato': 1},
            {'speaker': 1, 'text': 'Mas o preço médio caiu 9,5% no ano.', 'fato': 3},
            {'speaker': 0, 'text': 'Bateu a marca antiga de 1,657 bilhão.', 'fato': 2},
            {'speaker': 1, 'text': 'Será que o preço do café continua caindo?', 'fato': 3},
        ]

        # 1. verificar_fatos direto
        mock_facts_ans = {
            'falas': [
                {'i': 0, 'suportada': True, 'motivo': ''},
                {'i': 1, 'suportada': True, 'motivo': ''},
                {'i': 2, 'suportada': True, 'motivo': ''},
                {'i': 3, 'suportada': False, 'motivo': 'Contradiz a matéria sobre conilon'},
                {'i': 4, 'suportada': True, 'motivo': ''},
                {'i': 5, 'suportada': True, 'motivo': ''},
            ]
        }
        vf = news_auto.verificar_fatos(lines, story['extracted_facts'], ask=lambda i, p: mock_facts_ans)
        self.assertEqual(len(vf['falas']), 6)
        self.assertFalse(vf['falas'][3]['suportada'])
        self.assertEqual(vf['falas'][3]['motivo'], 'Contradiz a matéria sobre conilon')

        # 2. Fala não suportada forçando regeneração em write_script
        corrections_received = []
        def mock_writer(inst, payload_str):
            pay = json.loads(payload_str)
            if 'correcao' in pay:
                corrections_received.append(pay['correcao'])
            return {'lines': lines}

        judge_calls = []
        def mock_judge(inst, payload_str, model=None):
            judge_calls.append(inst)
            if 'checador de fatos' in inst.lower():
                if len(corrections_received) == 0:
                    return {
                        'falas': [
                            {'i': 0, 'suportada': True, 'motivo': ''},
                            {'i': 1, 'suportada': True, 'motivo': ''},
                            {'i': 2, 'suportada': False, 'motivo': 'Afirmação invertida'},
                            {'i': 3, 'suportada': True, 'motivo': ''},
                            {'i': 4, 'suportada': True, 'motivo': ''},
                            {'i': 5, 'suportada': True, 'motivo': ''},
                        ]
                    }
                return {'falas': [{'i': i, 'suportada': True, 'motivo': ''} for i in range(6)]}
            return {'nota': 8, 'responde_perguntas': True, 'contradiz_materia': False, 'problemas': []}

        out_lines = news_auto.write_script(story, max_words=100, ask=mock_writer, judge_ask=mock_judge, facts=story['extracted_facts'])
        self.assertEqual(len(out_lines), 6)
        self.assertEqual(len(corrections_received), 1)
        self.assertIn('Fala 3', corrections_received[0])
        self.assertIn('Afirmação invertida', corrections_received[0])

        # 3. HTTP 404 no juiz aciona fallback e grava juiz_fraco
        story_fallback = dict(story)
        calls_404 = []
        def mock_ask_404(inst, payload_str, model=None):
            calls_404.append(model)
            if model == 'qwen3:8b':
                raise RuntimeError('Ollama local respondeu HTTP 404.')
            return {'nota': 8, 'responde_perguntas': True, 'contradiz_materia': False, 'problemas': []}

        with patch.dict('os.environ', {'OLLAMA_MODEL': 'qwen3.5:4b', 'OLLAMA_JUDGE_MODEL': 'qwen3:8b'}):
            review = news_auto.evaluate_script(lines, story_fallback, ask=mock_ask_404)
        self.assertTrue(story_fallback.get('juiz_fraco'))
        self.assertIn('qwen3:8b', calls_404)
        self.assertIn('qwen3.5:4b', calls_404)
        self.assertEqual(review['nota'], 8)

        # 4. Nota limitada a 5 quando pergunta fica sem resposta ou contradiz a matéria
        def mock_no_answer(inst, payload_str, model=None):
            return {'nota': 9, 'responde_perguntas': False, 'contradiz_materia': False, 'problemas': []}
        rev_a = news_auto.evaluate_script(lines, story, ask=mock_no_answer)
        self.assertLessEqual(rev_a['nota'], 5)

        def mock_contradicts(inst, payload_str, model=None):
            return {'nota': 9, 'responde_perguntas': True, 'contradiz_materia': True, 'problemas': []}
        rev_b = news_auto.evaluate_script(lines, story, ask=mock_contradicts)
        self.assertLessEqual(rev_b['nota'], 5)

        def mock_problem_text(inst, payload_str, model=None):
            return {'nota': 8, 'problemas': ['A fala do Sasuke contradiz a matéria']}
        rev_c = news_auto.evaluate_script(lines, story, ask=mock_problem_text)
        self.assertLessEqual(rev_c['nota'], 5)

    def test_item4_step1_hook_and_contradiction(self):
        import json
        story = {
            'title': 'Brasil bate recorde de exportação de café em outubro',
            'body': (
                'O Brasil exportou 34,478 milhões de sacas de café, alta de 3,5%, '
                'mas a receita caiu 6,3%, para US$ 11,917 bilhões no período. '
                'O preço médio da saca caiu 9,5% com o aumento da venda de café conilon, '
                'que tem cotações inferiores no mercado global.'
            ),
        }
        source = news_auto.source_text(story)

        # 1. Validação de gancho válido e inválidos
        valid_hook = 'Por que a receita caiu 6,3% com mais café?'
        self.assertEqual(news_auto.validate_hook(valid_hook, story, source), valid_hook)

        # Gancho com mais de 10 palavras -> inválido
        long_hook = 'Por que o Brasil exportou tanto café para o mundo todo e ainda faturou muito menos?'
        self.assertIsNone(news_auto.validate_hook(long_hook, story, source))

        # Gancho com número inventado -> inválido
        invented_num_hook = 'Por que 99% das vendas caíram?'
        self.assertIsNone(news_auto.validate_hook(invented_num_hook, story, source))

        # 2. extract_facts devolve fatos, contradicao e gancho
        mock_step1 = {
            'fatos': [
                'O Brasil exportou 34,478 milhões de sacas de café.',
                'O volume representou alta de 3,5%.',
                'A receita caiu 6,3%, para US$ 11,917 bilhões.',
                'O preço médio da saca caiu 9,5%.',
                'A maior fatia foi de cafés conilon mais baratos.',
            ],
            'contradicao': 'O volume de café exportado subiu 3,5%, mas a receita caiu 6,3%.',
            'gancho': valid_hook,
        }

        facts_res = news_auto.extract_facts(story, ask=lambda i, p: mock_step1)
        self.assertEqual(len(facts_res), 5)
        self.assertEqual(facts_res['contradicao'], 'O volume de café exportado subiu 3,5%, mas a receita caiu 6,3%.')
        self.assertEqual(facts_res['gancho'], valid_hook)
        self.assertEqual(story.get('contradicao'), 'O volume de café exportado subiu 3,5%, mas a receita caiu 6,3%.')
        self.assertEqual(story.get('gancho'), valid_hook)

        # 3. write_script repassa o gancho como primeira fala obrigatória no payload e instruction
        received_payloads = []
        received_instructions = []

        def mock_writer(inst, payload_str):
            received_instructions.append(inst)
            received_payloads.append(json.loads(payload_str))
            return {'lines': [
                {'speaker': 0, 'text': valid_hook, 'fato': 2},
                {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
                {'speaker': 0, 'text': 'A receita caiu 6,3% para US$ 11,917 bilhões.', 'fato': 2},
                {'speaker': 1, 'text': 'O preço médio da saca caiu 9,5%.', 'fato': 3},
                {'speaker': 0, 'text': 'O Brasil exportou 34,478 milhões de sacas.', 'fato': 0},
                {'speaker': 1, 'text': 'Será que o Brasil exporta mais sacas?', 'fato': 0},
            ]}

        mock_judge = lambda i, p, model=None: {'falas': [{'i': idx, 'suportada': True, 'motivo': ''} for idx in range(6)]} if 'checador de fatos' in i.lower() else {'nota': 9, 'responde_perguntas': True, 'contradiz_materia': False, 'problemas': []}
        lines = news_auto.write_script(story, max_words=100, ask=mock_writer, judge_ask=mock_judge, facts=facts_res)
        self.assertEqual(lines[0]['text'], valid_hook)
        self.assertIn('primeira_fala_obrigatoria_gancho', received_payloads[0])
        self.assertEqual(received_payloads[0]['primeira_fala_obrigatoria_gancho'], valid_hook)
        self.assertIn('PRIMEIRA FALA OBRIGATÓRIA', received_instructions[0])
        self.assertIn('Perguntas feitas por um personagem DEVEM ser respondidas', received_instructions[0])

    def test_item5_clean_article_body_html_and_flat_text(self):
        # 1. HTML parsing: remove <a> isolados, <ul>/<ol>/<li>, <figure>, <aside> e classes/ids com related/leia/veja/saiba
        html_content = '''
        <p>O Brasil bateu recorde e exportou 5,3 milhões de sacas de café em outubro.</p>
        <ul class="noticias-relacionadas">
            <li><a href="http://news/1">Exportação de café cresce 31% e tem melhor agosto da história</a></li>
            <li><a href="http://news/2">Exportação de café verde do Brasil atinge maior nível em 20 meses</a></li>
        </ul>
        <ol>
            <li>Outro item de lista que deve sumir com números como 99%</li>
        </ol>
        <aside>
            <p>Texto lateral que não faz parte da matéria original.</p>
        </aside>
        <figure>
            <img src="foto.jpg" />
            <figcaption>Foto do porto de Santos</figcaption>
        </figure>
        <div class="box-related-news">
            <p>Mais notícias do agronegócio para você conferir.</p>
        </div>
        <div class="bloco-leia-tambem">
            <p>Leia também sobre o preço do trigo no Paraná.</p>
        </div>
        <section id="veja-mais-destaques">
            <p>Veja mais destaques da semana no agro nacional.</p>
        </section>
        <p><a href="http://link-isolado.com">Link Isolado em Parágrafo Sozinho</a></p>
        <a href="http://link-solto.com">Link Solto Fora de Bloco</a>
        <p>A receita foi de US$ 1,786 bilhão, de acordo com o <a href="http://cecafe.com.br">Conselho dos Exportadores</a>, uma alta de 7,8%.</p>
        '''
        cleaned_html = news_auto.article_text({'content:encoded': html_content})
        self.assertIn('5,3 milhões de sacas', cleaned_html)
        self.assertIn('US$ 1,786 bilhão', cleaned_html)
        self.assertIn('Conselho dos Exportadores', cleaned_html)  # inline <a> preservado
        self.assertNotIn('31%', cleaned_html)
        self.assertNotIn('melhor agosto da história', cleaned_html)
        self.assertNotIn('20 meses', cleaned_html)
        self.assertNotIn('99%', cleaned_html)
        self.assertNotIn('Texto lateral', cleaned_html)
        self.assertNotIn('porto de Santos', cleaned_html)
        self.assertNotIn('agronegócio', cleaned_html)
        self.assertNotIn('trigo no Paraná', cleaned_html)
        self.assertNotIn('agro nacional', cleaned_html)
        self.assertNotIn('Link Isolado', cleaned_html)
        self.assertNotIn('Link Solto', cleaned_html)

        # 2. Fallback para texto plano com manchetes coladas do café
        coffee_flat = (
            'O resultado deste ano ficou 2,9% acima daquele patamar. '
            'Exportação de café cresce 31% e tem melhor agosto da história '
            'Exportação de café verde do Brasil atinge maior nível em 20 meses '
            'Exportações de café solúvel sobem 20% em julho '
            'Segundo o presidente do Cecafé, Márcio Ferreira, o desempenho reflete a entrada mais intensa '
            'dos cafés da nova safra a partir de setembro , após chuvas atrasarem a colheita nas regiões produtoras.'
        )
        cleaned_flat = news_auto.article_text({'summary': coffee_flat})
        self.assertNotIn('31%', cleaned_flat)
        self.assertNotIn('melhor agosto da história', cleaned_flat)
        self.assertNotIn('20 meses', cleaned_flat)
        self.assertNotIn('20% em julho', cleaned_flat)
        self.assertIn('2,9%', cleaned_flat)
        self.assertIn('Segundo o presidente do Cecafé, Márcio Ferreira', cleaned_flat)

    def test_item1_round2_theme_coverage(self):
        # 1. Extração de palavras-chave normalizadas sem stopwords ou números
        title_fert = "Brasil precisa de R$ 460 bilhões para fertilizantes em 20 anos"
        kws_fert = news_auto.extract_title_keywords(title_fert)
        self.assertEqual(kws_fert, ['brasil', 'fertilizantes'])

        title_anth = "Anthropic fecha acordo de US$ 3,4 bilhões em geradores no Texas"
        kws_anth = news_auto.extract_title_keywords(title_anth)
        self.assertIn('anthropic', kws_anth)
        self.assertIn('geradores', kws_anth)
        self.assertIn('texas', kws_anth)

        # 2. Roteiro fixture de Fertilizantes é rejeitado por não citar 'fertilizantes'
        fixture_path = Path(__file__).parent / 'fixtures' / 'rejected_20261111_192441_627729.json'
        with open(fixture_path, encoding='utf-8') as f:
            fert_data = json.load(f)

        # Prepara fatos compatíveis com os números do roteiro da fixture
        story_fert = {
            'title': fert_data['title'],
            'body': (
                'O Brasil precisa de R$ 460 bilhões para fertilizantes nos próximos 20 anos. '
                'O investimento deve ficar entre R$ 220 bilhões e R$ 460 bilhões. '
                'Em média serão R$ 20 bilhões por ano para atingir R$ 400 bilhões. '
                'A meta é atingir de 40% a 50% de produção nacional até 2050.'
            )
        }
        facts_fert = [
            'O Brasil precisa de R$ 460 bilhões para fertilizantes nos próximos 20 anos.',
            'O investimento previsto fica entre R$ 220 bilhões e R$ 460 bilhões.',
            'A média é de R$ 20 bilhões por ano para alcançar R$ 400 bilhões.',
            'Juros altos e crédito difícil barram as empresas.',
            'A meta é atingir 40% a 50% de produção nacional até 2050.',
            'O país discute bilhões para segurança alimentar.',
        ]
        # Atribui fato a falas que não são reações curtas
        fact_mapping = {0: 0, 1: None, 2: 0, 3: 1, 4: 1, 5: 1, 6: 2, 7: 3, 8: 3, 9: 4, 10: 4, 11: 5}
        lines_with_facts = []
        for i, l in enumerate(fert_data['lines']):
            item = dict(l)
            if fact_mapping.get(i) is None:
                item['reacao'] = True
            else:
                item['fato'] = fact_mapping[i]
            lines_with_facts.append(item)

        raw_fert = {'lines': lines_with_facts}
        with self.assertRaisesRegex(ValueError, r'Roteiro não cobre as palavras-chave do título: ausente\(s\) fertilizantes'):
            news_auto.validate_lines(raw_fert, story_fert, budget=150, min_lines=4, facts=facts_fert)

        # 3. Se nenhuma palavra-chave aparecer nos 3 primeiros turnos, rejeita
        delayed_lines = {'lines': [
            {'speaker': 0, 'text': 'Você viu a projeção de 460 bilhões?', 'fato': 0},
            {'speaker': 1, 'text': 'Caramba, isso é muito alto!', 'reacao': True},
            {'speaker': 0, 'text': 'São 20 anos de prazo previstos.', 'fato': 0},
            {'speaker': 1, 'text': 'E o mínimo de 220 bilhões?', 'fato': 1},
            {'speaker': 0, 'text': 'O Brasil precisa de fertilizantes.', 'fato': 0},
            {'speaker': 1, 'text': 'Será que melhora a segurança alimentar?', 'fato': 5},
        ]}
        with self.assertRaisesRegex(ValueError, r'Nenhuma palavra-chave do título apareceu nos 3 primeiros turnos'):
            news_auto.validate_lines(delayed_lines, story_fert, budget=120, min_lines=4, facts=facts_fert)

        # 4. Quando cobre as palavras-chave nos primeiros turnos e no total, é aceito
        valid_lines = {'lines': [
            {'speaker': 0, 'text': 'O Brasil vai investir 460 bilhões?', 'fato': 0},
            {'speaker': 1, 'text': 'Sim, R$ 460 bilhões para fertilizantes!', 'fato': 0},
            {'speaker': 0, 'text': 'Isso vai levar cerca de 20 anos.', 'fato': 0},
            {'speaker': 1, 'text': 'Será que melhora a segurança alimentar?', 'fato': 5},
        ]}
        res, prob = news_auto.validate_lines(valid_lines, story_fert, budget=80, min_lines=4, facts=facts_fert)
        self.assertEqual(prob, '')
        self.assertEqual(len(res), 4)

        # 5. Roteiro fixture Anthropic cobre as palavras-chave do título
        anth_path = Path(__file__).parent / 'fixtures' / 'rejected_20261111_181036_244323.json'
        with open(anth_path, encoding='utf-8') as f:
            anth_data = json.load(f)
        story_anth = {
            'title': anth_data['title'],
            'body': (
                'A Anthropic fechou acordo de US$ 3,4 bilhões para geradores no Texas com a Caterpillar. '
                'O plano atende data centers até 2045 diante dos gargalos na rede elétrica estadual.'
            )
        }
        facts_anth = [
            'A Anthropic fechou acordo de US$ 3,4 bilhões.',
            'A Anthropic comprou geradores da Caterpillar por US$ 3,4 bilhões.',
            'A rede elétrica do Texas enfrenta limitações.',
            'A energia atenderá data centers até 2045.',
        ]
        import copy
        anth_valid = copy.deepcopy(anth_data)
        anth_valid['lines'][5] = {'speaker': 1, 'text': 'Será que a rede elétrica do Texas vai aguentar?', 'fato': 2}
        res_anth, prob_anth = news_auto.validate_lines(anth_valid, story_anth, budget=120, min_lines=4, facts=facts_anth)
        self.assertEqual(prob_anth, '')
        self.assertEqual(len(res_anth), 6)

    def test_item2_round2_answered_hook_requirement(self):
        # 1. Roteiro fixture de Fertilizantes tem gancho em pergunta não respondido
        fixture_path = Path(__file__).parent / 'fixtures' / 'rejected_20261111_192441_627729.json'
        with open(fixture_path, encoding='utf-8') as f:
            fert_data = json.load(f)

        # Ajusta para incluir a palavra fertilizantes na fala 2 para passar pelo Item 1
        fact_mapping = {0: 0, 1: None, 2: 0, 3: 1, 4: 1, 5: 1, 6: 2, 7: 3, 8: 3, 9: 4, 10: 4, 11: 5}
        lines = []
        for i, l in enumerate(fert_data['lines']):
            item = dict(l)
            if fact_mapping.get(i) is None:
                item['reacao'] = True
            elif i == 2:
                item['text'] = 'Exatamente, o Brasil precisa de fertilizantes para 20 anos.'
                item['fato'] = 0
            else:
                item['fato'] = fact_mapping[i]
            lines.append(item)

        story_fert = {
            'title': fert_data['title'],
            'body': (
                'O Brasil precisa de R$ 460 bilhões para fertilizantes nos próximos 20 anos. '
                'O investimento deve ficar entre R$ 220 bilhões e R$ 460 bilhões. '
                'Em média serão R$ 20 bilhões por ano para atingir R$ 400 bilhões. '
                'A meta é atingir de 40% a 50% de produção nacional até 2050.'
            )
        }
        facts_fert = [
            'O Brasil precisa de R$ 460 bilhões para fertilizantes nos próximos 20 anos.',
            'O investimento previsto fica entre R$ 220 bilhões e R$ 460 bilhões.',
            'A média é de R$ 20 bilhões por ano para alcançar R$ 400 bilhões.',
            'Juros altos e crédito difícil barram as empresas.',
            'A meta é atingir 40% a 50% de produção nacional até 2050.',
            'O país discute bilhões para segurança alimentar.',
        ]
        # Gancho: "Quanto dinheiro o Brasil gasta importando comida hoje?"
        # Falas 1 (reação) e 2 (fato 0 sobre R$ 460 bi para fertilizantes) não respondem com fatos sobre comida importada
        with self.assertRaisesRegex(ValueError, 'o gancho não foi respondido'):
            news_auto.validate_lines({'lines': lines}, story_fert, budget=150, min_lines=4, facts=facts_fert)

        # 2. Quando o fato da fala 1 ou 2 responde à pergunta (compartilha número ou >= 2 palavras de conteúdo), é aceito
        valid_qa = {'lines': [
            {'speaker': 0, 'text': 'Quanto dinheiro o Brasil precisa para fertilizantes?', 'fato': 0},
            {'speaker': 1, 'text': 'São R$ 460 bilhões para fertilizantes!', 'fato': 0},
            {'speaker': 0, 'text': 'Isso vai levar cerca de 20 anos.', 'fato': 0},
            {'speaker': 1, 'text': 'O objetivo é elevar a produção nacional.', 'fato': 4},
        ]}
        res, prob = news_auto.validate_lines(valid_qa, story_fert, budget=80, min_lines=4, facts=facts_fert)
        self.assertEqual(prob, '')
        self.assertEqual(len(res), 4)

        # 3. Gancho respondido pelo fato na fala 2 quando fala 1 é reação
        valid_reaction_then_ans = {'lines': [
            {'speaker': 0, 'text': 'Quanto dinheiro o Brasil precisa para fertilizantes?', 'fato': 0},
            {'speaker': 1, 'text': 'Número inacreditável!', 'reacao': True},
            {'speaker': 0, 'text': 'São R$ 460 bilhões para fertilizantes!', 'fato': 0},
            {'speaker': 1, 'text': 'A meta é atingir 50% até 2050.', 'fato': 4},
        ]}
        res, prob = news_auto.validate_lines(valid_reaction_then_ans, story_fert, budget=80, min_lines=4, facts=facts_fert)
        self.assertEqual(prob, '')
        self.assertEqual(len(res), 4)

    def test_item3_round2_non_decorative_fact(self):
        # 1. No Item 25, falas curtas de comentário próprio (<=14 palavras, sem dígitos, sem entidade nova)
        # são classificadas automaticamente como 'opinião' por annotate_lines.
        fixture_path = Path(__file__).parent / 'fixtures' / 'rejected_20261111_181036_244323.json'
        with open(fixture_path, encoding='utf-8') as f:
            anth_data = json.load(f)

        story = {
            'title': anth_data['title'],
            'body': (
                'A Anthropic fechou acordo de US$ 3,4 bilhões para geradores no Texas com a Caterpillar. '
                'O plano atende data centers até 2045 diante dos gargalos na rede elétrica estadual.'
            )
        }
        facts = [
            'A Anthropic fechou acordo de US$ 3,4 bilhões.',
            'A Anthropic comprou geradores da Caterpillar por US$ 3,4 bilhões.',
            'A rede elétrica do Texas enfrenta limitações com novos data centers.',
            'A energia atenderá data centers até 2045.',
        ]

        res_ok, prob_ok = news_auto.validate_lines(anth_data, story, budget=120, min_lines=4, facts=facts)
        self.assertEqual(prob_ok, '')
        self.assertTrue(res_ok[5].get('opiniao'))

        # 2. Opinião com entidade nova (OpenAI) ou >14 palavras sem fato é rejeitada com motivo claro
        import copy
        bad_opinion_entity = copy.deepcopy(anth_data)
        bad_opinion_entity['lines'][5] = {
            'speaker': 1,
            'text': 'Será que vai faltar energia para a OpenAI também?'
        }
        with self.assertRaisesRegex(ValueError, r'entidade "OpenAI" ausente nos fatos e no título'):
            news_auto.validate_lines(bad_opinion_entity, story, budget=120, min_lines=4, facts=facts)

        # 3. Fala com número do fato passa normalmente e infere fato correspondente
        number_line_data = copy.deepcopy(anth_data)
        number_line_data['lines'][5] = {
            'speaker': 1,
            'text': 'Isso vai mesmo até 2045?'
        }
        res_num, prob_num = news_auto.validate_lines(number_line_data, story, budget=120, min_lines=4, facts=facts)
        self.assertEqual(prob_num, '')
        self.assertEqual(res_num[5].get('fato'), 3)

    def test_item4_round2_variety_and_crutches(self):
        story = {
            'title': 'Exportações de café no Brasil batem recorde histórico',
            'body': (
                'O Brasil exportou 16,591 milhões de sacas nos quatro primeiros meses. '
                'A receita cambial somou US$ 1,786 bilhão, superando o recorde anterior '
                'de 1,657 bilhão registrado no ciclo passado. A queda de 9,5% no preço médio '
                'aconteceu pelo aumento do conilon. Os embarques cresceram no período.'
            ),
            'extracted_facts': [
                'O Brasil exportou 16,591 milhões de sacas nos quatro primeiros meses.',
                'A receita cambial somou US$ 1,786 bilhão.',
                'O recorde anterior era de 1,657 bilhão de dólares.',
                'O preço médio caiu 9,5% pelo aumento do café conilon.',
                'Os embarques bateram recorde histórico.',
            ]
        }
        facts = story['extracted_facts']

        # 1. Roteiro fixture Fertilizantes: fala 5 tem vocativo ("mano") em fala com número
        fert_path = Path(__file__).parent / 'fixtures' / 'rejected_20261111_192441_627729.json'
        with open(fert_path, encoding='utf-8') as f:
            fert_data = json.load(f)
        fert_story = {
            'title': fert_data['title'],
            'body': 'O Brasil precisa de R$ 220 bilhões a R$ 460 bilhões para fertilizantes em 20 anos. ' * 3
        }
        fert_facts = [
            'O Brasil precisa de R$ 220 bilhões a R$ 460 bilhões para fertilizantes em 20 anos.',
            'O investimento anual seria de cerca de R$ 20 bilhões.',
            'O plano abrange 20 anos de desenvolvimento.',
            'A meta é elevar a produção nacional.',
        ]
        fert_test_lines = [
            {'speaker': 0, 'text': 'Quanto dinheiro o Brasil precisa para fertilizantes?', 'fato': 0},
            {'speaker': 1, 'text': 'São R$ 460 bilhões para fertilizantes!', 'fato': 0},
            {'speaker': 0, 'text': 'Isso vai levar cerca de 20 anos.', 'fato': 0},
            {'speaker': 1, 'text': 'Entre R$ 220 bilhões e R$ 460 bilhões, mano.', 'fato': 0},
        ]
        with self.assertRaisesRegex(ValueError, r'contém vocativo \("mano"\) em fala que carrega número'):
            news_auto.validate_lines({'lines': fert_test_lines}, fert_story, budget=100, min_lines=4, facts=fert_facts)

        # 2. No máximo 1 fala começando com muletas de concordância
        bad_crutches = {'lines': [
            {'speaker': 0, 'text': 'O Brasil exportou café este ano?', 'fato': 0},
            {'speaker': 1, 'text': 'Exatamente, foram 16,591 milhões de sacas.', 'fato': 0},
            {'speaker': 0, 'text': 'Pois é, a receita cambial foi de US$ 1,786 bilhão.', 'fato': 1},
            {'speaker': 1, 'text': 'Será que os embarques batem outro recorde?', 'fato': 4},
        ]}
        with self.assertRaisesRegex(ValueError, r'No máximo 1 fala pode começar com muletas de concordância'):
            news_auto.validate_lines(bad_crutches, story, budget=100, min_lines=4, facts=facts)

        # 3. No máximo 3 vocativos no roteiro
        too_many_vocatives = {'lines': [
            {'speaker': 0, 'text': 'Viu esse recorde histórico no Brasil, Sasuke?', 'fato': 4},
            {'speaker': 1, 'text': 'Cara, os embarques bateram recorde histórico.', 'fato': 4},
            {'speaker': 0, 'text': 'Mano, a receita cambial subiu muito!', 'fato': 1},
            {'speaker': 1, 'text': 'Naruto, será que os embarques batem outro recorde?', 'fato': 4},
            {'speaker': 0, 'text': 'Foram 16,591 milhões de sacas.', 'fato': 0},
            {'speaker': 1, 'text': 'A receita somou US$ 1,786 bilhão.', 'fato': 1},
        ]}
        with self.assertRaisesRegex(ValueError, r'O roteiro tem 4 vocativos .* o limite máximo permitido é 3'):
            news_auto.validate_lines(too_many_vocatives, story, budget=100, min_lines=4, facts=facts)

        # 4. No máximo 50% das falas podem ser perguntas
        too_many_questions = {'lines': [
            {'speaker': 0, 'text': 'O Brasil exportou quanto café?', 'fato': 0},
            {'speaker': 1, 'text': 'Foram 16,591 milhões de sacas embarcadas?', 'fato': 0},
            {'speaker': 0, 'text': 'A receita cambial atingiu US$ 1,786 bilhão?', 'fato': 1},
            {'speaker': 1, 'text': 'Os embarques bateram recorde histórico.', 'fato': 4},
        ]}
        with self.assertRaisesRegex(ValueError, r'No máximo 50% das falas podem ser perguntas'):
            news_auto.validate_lines(too_many_questions, story, budget=100, min_lines=4, facts=facts)

        # 5. Sasuke deve fazer pelo menos 1 pergunta ou provocação
        sasuke_no_provocation = {'lines': [
            {'speaker': 0, 'text': 'O Brasil exportou 16,591 milhões de sacas.', 'fato': 0},
            {'speaker': 1, 'text': 'Os embarques bateram recorde histórico?', 'fato': 4},
            {'speaker': 0, 'text': 'A receita cambial somou US$ 1,786 bilhão.', 'fato': 1},
            {'speaker': 1, 'text': 'O preço médio caiu 9,5% no período.', 'fato': 3},
        ]}
        with self.assertRaisesRegex(ValueError, r'O Sasuke deve fazer pelo menos 1 pergunta ou provocação'):
            news_auto.validate_lines(sasuke_no_provocation, story, budget=100, min_lines=4, facts=facts)

        # 6. Roteiro válido com variedade equilibrada passa
        valid_script = {'lines': [
            {'speaker': 0, 'text': 'Viu esse novo recorde histórico de café?', 'fato': 4},
            {'speaker': 1, 'text': 'Cara, os embarques bateram recorde histórico!', 'fato': 4},
            {'speaker': 0, 'text': 'Exatamente, foram 16,591 milhões de sacas.', 'fato': 0},
            {'speaker': 1, 'text': 'A receita cambial somou US$ 1,786 bilhão.', 'fato': 1},
            {'speaker': 0, 'text': 'Mas o preço médio caiu 9,5% pelo conilon.', 'fato': 3},
            {'speaker': 1, 'text': 'Será que os embarques continuam batendo recorde?', 'fato': 4},
        ]}
        res, prob = news_auto.validate_lines(valid_script, story, budget=100, min_lines=4, facts=facts)
        self.assertEqual(prob, '')
        self.assertEqual(len(res), 6)

    def test_item5_round2_judge_with_evidence(self):
        lines = [
            {'speaker': 0, 'text': 'Por que gastar 3,4 bilhões só em gerador?'},
            {'speaker': 1, 'text': 'Cara, a Anthropic comprou tudo da Caterpillar!'},
            {'speaker': 0, 'text': 'É que a rede elétrica do Texas não aguenta mais.'},
            {'speaker': 1, 'text': 'Eles garantiram energia até 2045 pros data centers.'},
            {'speaker': 0, 'text': 'Mano, é muita energia!'},
            {'speaker': 1, 'text': 'Será que a rede elétrica vai aguentar?'},
        ]
        story = {'title': 'Anthropic compra geradores no Texas', 'body': 'Texto da matéria.'}

        # 1. Juiz que reprova por "gancho longo" (critério objetivo sem trecho válido ou tipo inválido)
        # Deve ser ignorado, logar a mensagem e subir a nota para 7
        logs = []
        mock_unverifiable = lambda i, p, m=None: {
            'nota': 5,
            'problemas': [
                {'i': 0, 'trecho': 'gancho longo demais com mais de dez palavras', 'tipo': 'objetivo', 'motivo': 'gancho longo'},
                'gancho com mais de 10 palavras',
            ]
        }
        res_ignored = news_auto.evaluate_script(lines, story, ask=mock_unverifiable, log=logs.append)
        self.assertEqual(res_ignored['nota'], 7)
        self.assertEqual(len(res_ignored['problemas']), 0)
        self.assertIn('reprovação sem problema verificável; ignorada', logs)

        # 2. Juiz que aponta trecho real da fala com tipo permitido ("informalidade", "ritmo", "repeticao")
        # Deve ser mantido e nota permanece < 7
        logs_real = []
        mock_real_snippet = lambda i, p, m=None: {
            'nota': 5,
            'problemas': [
                {'i': 1, 'trecho': 'comprou tudo da Caterpillar', 'tipo': 'informalidade', 'motivo': 'expressão muito informal'}
            ]
        }
        res_kept = news_auto.evaluate_script(lines, story, ask=mock_real_snippet, log=logs_real.append)
        self.assertEqual(res_kept['nota'], 5)
        self.assertEqual(len(res_kept['problemas']), 1)
        self.assertEqual(res_kept['problemas'][0]['trecho'], 'comprou tudo da Caterpillar')
        self.assertEqual(res_kept['problemas'][0]['tipo'], 'informalidade')
        self.assertNotIn('reprovação sem problema verificável; ignorada', logs_real)

        # 3. Trecho que não existe na fala i é descartado
        mock_absent_snippet = lambda i, p, m=None: {
            'nota': 4,
            'problemas': [
                {'i': 2, 'trecho': 'trecho inexistente no texto', 'tipo': 'ritmo', 'motivo': 'ruim'}
            ]
        }
        logs_absent = []
        res_absent = news_auto.evaluate_script(lines, story, ask=mock_absent_snippet, log=logs_absent.append)
        self.assertEqual(res_absent['nota'], 7)
        self.assertEqual(len(res_absent['problemas']), 0)
        self.assertIn('reprovação sem problema verificável; ignorada', logs_absent)

        # 4. Checagem determinística continua descontando 2 pontos se falhar
        bad_deterministic = [
            {'speaker': 0, 'text': 'Cara isso aqui mudou tudo no mercado agora.'},
            {'speaker': 1, 'text': 'Cara isso aqui mudou tudo no mercado agora.'},
            {'speaker': 0, 'text': 'Cara ninguém esperava esse resultado tão cedo.'},
            {'speaker': 1, 'text': 'Fato curto.'},
        ]
        res_det = news_auto.evaluate_script(bad_deterministic, story, ask=lambda i, p, m=None: {'nota': 8, 'problemas': []}, log=lambda *a: None)
        self.assertEqual(res_det['nota'], 6)
        self.assertTrue(len(res_det['problemas']) > 0)

    def test_item6_round2_fact_check_fails_closed(self):
        lines = [
            {'speaker': 0, 'text': 'Por que gastar 3,4 bilhões só em gerador?', 'fato': 0},
            {'speaker': 1, 'text': 'Cara, a Anthropic comprou tudo da Caterpillar!', 'fato': 1},
            {'speaker': 0, 'text': 'É que a rede elétrica do Texas não aguenta mais.', 'fato': 2},
            {'speaker': 1, 'text': 'Eles garantiram energia até 2045 pros data centers.', 'fato': 3},
            {'speaker': 0, 'text': 'Mano, é muita energia!', 'reacao': True},
            {'speaker': 1, 'text': 'Será que a rede elétrica vai aguentar?', 'fato': 2},
        ]
        facts = [
            'A Anthropic fechou contrato de US$ 3,4 bilhões.',
            'A Anthropic comprou geradores da Caterpillar por US$ 3,4 bilhões.',
            'A rede elétrica do Texas enfrenta limitações com novos data centers.',
            'A energia atenderá data centers até 2045.',
        ]
        story = {
            'title': 'Anthropic fecha acordo de US$ 3,4 bilhões em geradores no Texas',
            'body': 'A Anthropic comprou geradores da Caterpillar no Texas por US$ 3,4 bilhões para abastecer data centers até 2045. ' * 3,
            'extracted_facts': facts,
        }

        # 1. verificar_fatos direto tenta 2 vezes e retorna {"indisponivel": true} ao falhar
        call_count = {'n': 0}
        def failing_ask(inst, payload, model=None):
            call_count['n'] += 1
            raise RuntimeError('Erro de conexão com o Ollama')

        vf_res = news_auto.verificar_fatos(lines, facts, ask=failing_ask)
        self.assertEqual(call_count['n'], 2)
        self.assertTrue(vf_res.get('indisponivel'))

        # 2. Em write_script, indisponibilidade trata o roteiro como REPROVADO e salva rejected_*.json
        out_dir = Path(self.temp.name) / 'out_rej_facts'
        logs = []
        writer_mock = lambda i, p: {'lines': lines}

        with patch.object(engine, 'OUTPUT', out_dir), \
             patch.dict('os.environ', {'REELFORGE_ALLOW_NO_FACT_CHECK': '0'}):
            with self.assertRaisesRegex(RuntimeError, 'checagem de fatos indisponível'):
                news_auto.write_script(story, max_words=100, ask=writer_mock, judge_ask=failing_ask,
                                       facts=facts, log=logs.append)

        self.assertIn('verificação de fatos indisponível', logs)
        rejected_files = list(out_dir.glob('rejected_*.json'))
        self.assertTrue(len(rejected_files) >= 1)
        saved = json.loads(rejected_files[0].read_text(encoding='utf-8'))
        self.assertIn('checagem de fatos indisponível', saved.get('problemas', []))

        # 3. Com permitir_sem_checagem_de_fatos ativo (REELFORGE_ALLOW_NO_FACT_CHECK=1), prossegue
        logs_allowed = []
        def judge_with_failing_facts(inst, payload, model=None):
            if 'checador de fatos' in inst.lower():
                raise RuntimeError('Ollama offline para checagem')
            return {'nota': 8, 'problemas': []}

        with patch.object(engine, 'OUTPUT', out_dir), \
             patch.dict('os.environ', {'REELFORGE_ALLOW_NO_FACT_CHECK': '1'}):
            res_lines = news_auto.write_script(story, max_words=100, ask=writer_mock,
                                               judge_ask=judge_with_failing_facts, facts=facts,
                                               log=logs_allowed.append)
        self.assertEqual(len(res_lines), 6)
        self.assertTrue(any('seguindo sem checagem de fatos' in m for m in logs_allowed))

    def test_item7_round2_writer_prompt_and_transparency(self):
        story = {
            'title': 'Brasil bate recorde e exporta 5,3 milhões de sacas de café em outubro',
            'body': 'O Brasil exportou 5,3 milhões de sacas de café em outubro por US$ 1,786 bilhão, enquanto o preço médio caiu 9,5% e o conilon avançou no período. ' * 3,
            'extracted_facts': [
                'O Brasil exportou 5,3 milhões de sacas de café em outubro.',
                'A receita atingiu US$ 1,786 bilhão.',
                'O preço médio caiu 9,5% no ano.',
                'O conilon teve forte alta no período.',
            ]
        }
        lines = [
            {'speaker': 0, 'text': 'O Brasil exportou quanto café em outubro?', 'fato': 0},
            {'speaker': 1, 'text': 'Cara, isso tudo é impressionante!', 'reacao': True},
            {'speaker': 0, 'text': 'O Brasil exportou 5,3 milhões de sacas.', 'fato': 0},
            {'speaker': 1, 'text': 'A receita somou US$ 1,786 bilhão.', 'fato': 1},
            {'speaker': 0, 'text': 'Mas o preço médio caiu 9,5% no ano.', 'fato': 2},
            {'speaker': 1, 'text': 'E o conilon teve forte alta no período!', 'fato': 3},
        ]

        captured_instructions = []
        def capture_ask(inst, payload):
            captured_instructions.append(inst)
            return {'lines': lines}

        mock_judge = lambda i, p, m=None: (
            {'falas': [{'i': idx, 'suportada': True, 'motivo': ''} for idx in range(6)]}
            if 'checador de fatos' in i.lower() else
            {'nota': 8, 'problemas': []}
        )

        with patch.dict('os.environ', {'OLLAMA_MODEL': 'qwen3.5:4b', 'OLLAMA_JUDGE_MODEL': 'qwen3:8b'}):
            res_lines = news_auto.write_script(story, max_words=80, ask=capture_ask, judge_ask=mock_judge, facts=story['extracted_facts'])

        writer_inst = captured_instructions[0]
        self.assertIn('Sasuke (0): direto, seco e irônico', writer_inst)
        self.assertIn('Naruto (1): exagerado, curioso e espontâneo', writer_inst)
        self.assertIn('hoverboard', writer_inst)
        self.assertIn('Luminopolis', writer_inst)

        self.assertIn('modelos', story)
        self.assertEqual(story['modelos']['redator'], 'qwen3.5:4b')
        self.assertEqual(story['modelos']['juiz'], 'qwen3:8b')
        self.assertFalse(story['modelos']['juiz_fraco'])

        out_dir = Path(self.temp.name) / 'out_item7'
        with patch.object(engine, 'OUTPUT', out_dir):
            rej_path = news_auto._save_rejected_script(
                story,
                lines,
                {'nota': 4, 'problemas': [{'i': 0, 'trecho': 'Viu', 'tipo': 'ritmo', 'motivo': 'teste'}]}
            )
        rej_data = json.loads(rej_path.read_text(encoding='utf-8'))
        self.assertIn('modelos', rej_data)
        self.assertEqual(rej_data['modelos']['redator'], 'qwen3.5:4b')
        self.assertEqual(rej_data['modelos']['juiz'], 'qwen3:8b')
        self.assertTrue(rej_data['lines'][1].get('reacao'))
        self.assertEqual(rej_data['lines'][0].get('fato'), 0)
        self.assertEqual(rej_data['lines'][2].get('fato'), 0)

        # 4. Preservação de modelos e fatos no JSON do reel via engine.render
        with tempfile.TemporaryDirectory() as td:
            dummy_json = Path(td) / 'reel_dummy.json'
            dummy_mp4 = Path(td) / 'reel_dummy.mp4'
            dummy_json.write_text(json.dumps({'lines': [{'speaker': 0, 'text': 'fala 1'}, {'speaker': 1, 'text': 'fala 2'}]}), encoding='utf-8')
            with patch('render_pipeline.render', return_value=dummy_mp4):
                engine.render(story, lines, 'dummy.mp4')
            meta_res = json.loads(dummy_json.read_text(encoding='utf-8'))
            self.assertIn('modelos', meta_res)
            self.assertEqual(meta_res['modelos']['redator'], 'qwen3.5:4b')
            self.assertEqual(meta_res['lines'][0].get('fato'), 0)
            self.assertTrue(meta_res['lines'][1].get('reacao'))

    def test_strip_glued_headlines_dp_performance_and_clean(self):
        import time
        # (1) 5000 palavras capitalizadas sem pontuação terminam em menos de 1 s
        words_5000 = ' '.join(['Palavra' for _ in range(5000)])
        t0 = time.perf_counter()
        res_5000 = news_auto.strip_glued_headlines(words_5000)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 1.0)
        self.assertIsInstance(res_5000, str)

        # (2) o caso de "leia também" do café continua sendo limpo
        coffee_flat = (
            'O resultado deste ano ficou 2,9% acima daquele patamar. '
            'Exportação de café cresce 31% e tem melhor agosto da história '
            'Exportação de café verde do Brasil atinge maior nível em 20 meses '
            'Exportações de café solúvel sobem 20% em julho '
            'Segundo o presidente do Cecafé, Márcio Ferreira, o desempenho reflete a entrada mais intensa '
            'dos cafés da nova safra a partir de setembro , após chuvas atrasarem a colheita nas regiões produtoras.'
        )
        res_coffee = news_auto.strip_glued_headlines(coffee_flat)
        self.assertNotIn('31%', res_coffee)
        self.assertNotIn('melhor agosto da história', res_coffee)
        self.assertNotIn('20 meses', res_coffee)
        self.assertNotIn('20% em julho', res_coffee)
        self.assertIn('O resultado deste ano ficou 2,9% acima daquele patamar.', res_coffee)
        self.assertIn('Segundo o presidente do Cecafé', res_coffee)

    def test_strip_glued_headlines_random_inputs_match_legacy(self):
        # (3) um conjunto de entradas aleatórias pequenas dá o mesmo resultado que o comportamento atual
        def legacy(text):
            words = text.split()
            n = len(words)
            if n < 12:
                return text

            def has_any_punct(w):
                return any(w.endswith(p) for p in ('.', '!', '?', ';', ':', ','))

            def has_term_punct(w):
                return any(w.endswith(p) for p in ('.', '!', '?'))

            def is_upper_start(w):
                clean = w.lstrip('"\'“‘([')
                return bool(clean and clean[0].isupper() and not clean.startswith(','))

            def is_snippet(start_idx, end_idx):
                if not (6 <= end_idx - start_idx <= 20):
                    return False
                if not is_upper_start(words[start_idx]):
                    return False
                slice_words = words[start_idx:end_idx]
                if any(has_term_punct(w) for w in slice_words):
                    return False
                if has_any_punct(slice_words[-1]):
                    return False
                return True

            def can_transition_to_sentence_or_end(idx):
                if idx >= n:
                    return True
                if not is_upper_start(words[idx]) or has_any_punct(words[idx]):
                    return False
                for k in range(idx, n):
                    if has_term_punct(words[k]):
                        return True
                return False

            chains = []
            i = 0
            while i < n:
                if not is_upper_start(words[i]):
                    i += 1
                    continue

                def find_chains(curr_idx):
                    res = []
                    for next_idx in range(curr_idx + 6, min(n + 1, curr_idx + 21)):
                        if is_snippet(curr_idx, next_idx):
                            if next_idx == n or can_transition_to_sentence_or_end(next_idx):
                                res.append([curr_idx, next_idx])
                            if next_idx < n:
                                sub = find_chains(next_idx)
                                for s in sub:
                                    res.append([curr_idx] + s)
                    return res

                all_chains = find_chains(i)
                valid = [c for c in all_chains if len(c) >= 3 and can_transition_to_sentence_or_end(c[-1])]
                if valid:
                    best_chain = max(valid, key=lambda c: (len(c), c[-1] - c[0]))
                    chains.append((best_chain[0], best_chain[-1]))
                    i = best_chain[-1]
                else:
                    i += 1

            if not chains:
                return text

            res_words = []
            last_idx = 0
            for start_idx, end_idx in chains:
                res_words.extend(words[last_idx:start_idx])
                last_idx = end_idx
            res_words.extend(words[last_idx:])
            return ' '.join(res_words)

        import random
        rng = random.Random(42)
        word_pool_upper = ['Brasil', 'Mercado', 'Economia', 'Notícia', 'Preço', 'Governo', 'Safra', 'Exportação']
        word_pool_lower = ['de', 'da', 'do', 'em', 'com', 'para', 'alta', 'queda', 'nacional', 'milhões']
        puncts = ['', '', '', '.', '!', '?', ',']

        for test_idx in range(40):
            length = rng.randint(5, 30)
            sample_words = []
            for _ in range(length):
                w = rng.choice(word_pool_upper if rng.random() < 0.4 else word_pool_lower)
                p = rng.choice(puncts)
                sample_words.append(w + p)
            sample_text = ' '.join(sample_words)
            self.assertEqual(
                news_auto.strip_glued_headlines(sample_text),
                legacy(sample_text),
                f'Divergência na iteração {test_idx}'
            )

    def test_gather_candidates_feed_logging(self):
        logs = []
        with patch('news_auto.read_feed', return_value=FEED):
            cands = news_auto.gather_candidates('tecnologia', log=logs.append)
        self.assertTrue(any(l.startswith('Lendo feed ') for l in logs))
        self.assertTrue(any(': ' in l and 'itens' in l for l in logs))
        self.assertTrue(any('Google News: ' in l and 'itens' in l for l in logs))

    def test_gather_candidates_timeout_ignores_slow_feeds_and_shuts_down(self):
        import time

        def rich_entry(title, source, link):
            return {
                'title': f'{title} - {source}',
                'source': {'title': source},
                'link': link,
                'published': 'Sun, 08 Nov 2026 02:00:00 GMT',
                'summary': 'Tecnologia avança com internet e novos sistemas no Brasil. ' * 20
            }

        def slow_read_feed(url):
            if 'google' in url:
                time.sleep(1.0)
                return []
            return [rich_entry('Governo anuncia tecnologia', 'G1', 'https://g1/1')]

        t0 = time.perf_counter()
        with patch('news_auto.read_feed', side_effect=slow_read_feed):
            cands = news_auto.gather_candidates('tecnologia', timeout=0.25)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.8)
        self.assertIsInstance(cands, list)
        self.assertGreater(len(cands), 0)

    def test_item10_relink_automatic_fact_index(self):
        facts = [
            "O telescópio James Webb da Nasa observou galáxias distantes.",
            "A missão espacial do James durou cerca de três anos.",
            "O Webb detectou 25 novos exoplanetas e 277 asteroides.",
            "Os cientistas analisaram amostras de poeira cósmica.",
            "A temperatura no vácuo espacial atingiu níveis extremos.",
            "O projeto envolveu vinte países parceiros.",
        ]
        story = {
            'title': 'Descobertas do Telescópio Webb',
            'body': 'O telescópio espacial James Webb da Nasa fez grandes descobertas científicas no espaço profundo. '
                    'A missão espacial do James durou cerca de três anos e analisou amostras cósmicas. '
                    'O Webb detectou 25 novos exoplanetas e 277 asteroides na pesquisa recente. '
                    'A temperatura no vácuo espacial atingiu níveis extremos e vinte países parceiros participaram.',
            'source_type': 'rss',
        }
        # 1. Inferência automática ignora índice "fato" antigo e encontra fato 2 e fato 0
        lines_inferred = {
            'lines': [
                {'speaker': 0, 'text': 'O que o telescópio Webb detectou no espaço?', 'fato': 0},
                {'speaker': 1, 'text': 'O Webb detectou 25 novos exoplanetas e 277 asteroides!', 'fato': 4},
                {'speaker': 0, 'text': 'A Nasa observou galáxias distantes e a missão durou três anos.'},
                {'speaker': 1, 'text': 'Os cientistas analisaram amostras de poeira cósmica!'},
            ]
        }
        logs = []
        valid_lines, prob = news_auto.validate_lines(lines_inferred, story, budget=80, min_lines=4, facts=facts, log=logs.append)
        self.assertEqual(prob, '')
        self.assertEqual(valid_lines[1]['fato'], 2)
        self.assertIn('fala 2: inferida como fato 2', logs)

        # 2. Número ausente em todos os fatos e na matéria continua rejeitado
        lines_invalid = {
            'lines': [
                {'speaker': 0, 'text': 'O que o telescópio Webb detectou no espaço?'},
                {'speaker': 1, 'text': 'O Webb analisou dados por 99 dias seguidos!'},
                {'speaker': 0, 'text': 'A missão do James durou três anos no vácuo.'},
                {'speaker': 1, 'text': 'Os cientistas analisaram amostras de poeira cósmica.'},
            ]
        }
        with self.assertRaisesRegex(ValueError, r'Números ausentes na matéria: 99\. nenhum fato contém estes números'):
            news_auto.validate_lines(lines_invalid, story, budget=80, min_lines=4, facts=facts)

    def test_item11_feedback_useful_between_retries_and_attempt_logging(self):
        import json
        facts = [
            "O telescópio James Webb da Nasa observou galáxias distantes.",
            "A missão espacial do James durou cerca de três anos.",
            "O Webb detectou 25 novos exoplanetas e 277 asteroides.",
            "Os cientistas analisaram amostras de poeira cósmica.",
            "A temperatura no vácuo espacial atingiu níveis extremos.",
            "O projeto envolveu vinte países parceiros.",
        ]
        story = {
            'title': 'Descobertas do Telescópio Webb',
            'body': 'O telescópio espacial James Webb da Nasa fez grandes descobertas científicas no espaço profundo. '
                    'A missão espacial do James durou cerca de três anos e analisou amostras cósmicas. '
                    'O Webb detectou 25 novos exoplanetas e 277 asteroides na pesquisa recente. '
                    'A temperatura no vácuo espacial atingiu níveis extremos e vinte países parceiros participaram.',
            'source_type': 'rss',
            'extracted_facts': facts,
        }

        corrections_none = []
        logs_none = []

        def mock_writer_no_fact(inst, payload_str):
            payload = json.loads(payload_str)
            if 'correcao' in payload:
                corrections_none.append(payload['correcao'])
                return {'lines': [
                    {'speaker': 0, 'text': 'O que o telescópio Webb detectou no espaço?'},
                    {'speaker': 1, 'text': 'O Webb detectou 25 novos exoplanetas e 277 asteroides!'},
                    {'speaker': 0, 'text': 'A Nasa observou galáxias e a missão durou três anos.'},
                    {'speaker': 1, 'text': 'Os cientistas analisaram amostras de poeira cósmica.'},
                    {'speaker': 0, 'text': 'Vinte países parceiros integraram o projeto espacial.'},
                    {'speaker': 1, 'text': 'E a temperatura no vácuo atingiu extremos.'},
                ]}
            return {'lines': [
                {'speaker': 0, 'text': 'O que o telescópio Webb detectou no espaço?'},
                {'speaker': 1, 'text': 'O telescópio analisou dados durante 99 dias seguidos!'},
                {'speaker': 0, 'text': 'A missão do James durou três anos no vácuo.'},
                {'speaker': 1, 'text': 'Os cientistas analisaram amostras de poeira cósmica.'},
                {'speaker': 0, 'text': 'O projeto envolveu vinte países parceiros.'},
                {'speaker': 1, 'text': 'Será que acham vida nesses novos exoplanetas?'},
            ]}

        mock_judge = lambda i, p, model=None: {'falas': [{'i': idx, 'suportada': True, 'motivo': ''} for idx in range(6)]} if 'checador de fatos' in i.lower() else {'nota': 8, 'responde_perguntas': True, 'contradiz_materia': False, 'problemas': []}

        res2 = news_auto.write_script(story, max_words=80, ask=mock_writer_no_fact, judge_ask=mock_judge, facts=facts, log=logs_none.append)
        self.assertEqual(len(res2), 6)
        self.assertEqual(len(corrections_none), 1)
        corr2 = corrections_none[0]
        self.assertIn('Fala 2', corr2)
        self.assertIn('99 dias seguidos', corr2)
        self.assertIn('nenhum fato contém estes números: não os use', corr2)
        self.assertTrue(any('Tentativa 1/8: rejeitada' in l for l in logs_none))
        self.assertTrue(any('Tentativa 2/8: aceita' in l for l in logs_none))

    def test_item12_failure_log_file_and_runtime_error(self):
        import json
        import re
        from pathlib import Path
        import engine

        facts = [
            "O telescópio James Webb da Nasa observou galáxias distantes.",
            "A missão espacial do James durou cerca de três anos.",
            "O Webb detectou 25 novos exoplanetas e 277 asteroides.",
            "Os cientistas analisaram amostras de poeira cósmica.",
            "A temperatura no vácuo espacial atingiu níveis extremos.",
            "O projeto envolveu vinte países parceiros.",
        ]
        story = {
            'id': 'teste_item12_id_123',
            'title': 'Descobertas do Telescópio Webb',
            'body': 'O telescópio espacial James Webb da Nasa fez grandes descobertas científicas no espaço profundo. '
                    'A missão espacial do James durou cerca de três anos e analisou amostras cósmicas em 90 dias. '
                    'O Webb detectou 25 novos exoplanetas e 277 asteroides na pesquisa recente. '
                    'A temperatura no vácuo espacial atingiu níveis extremos e vinte países parceiros participaram.',
            'source_type': 'rss',
            'extracted_facts': facts,
            'link': 'https://exemplo.com/materia_teste_falha_12',
        }

        # Mock writer que sempre falha e retorna resposta longa (> 4000 caracteres)
        attempts_called = []

        def mock_failing_writer(inst, payload_str):
            att_idx = len(attempts_called)
            attempts_called.append(payload_str)
            num_lines = [1, 2, 3, 9, 10, 11, 12, 13][att_idx % 8]
            return {
                'lines': [{'speaker': i % 2, 'text': 'O telescópio Webb observou galáxias distantes na região.', 'fato': 0} for i in range(num_lines)],
                'extra_debug_payload': 'x' * 5000,
            }

        with self.assertRaises(RuntimeError) as ctx:
            news_auto.write_script(story, max_words=80, ask=mock_failing_writer, facts=facts)

        err_msg = str(ctx.exception)
        m = re.match(r'^Não foi possível gerar um roteiro válido em 8 tentativas\. Detalhes em (.+falha_roteiro_.+\.json)$', err_msg)
        self.assertIsNotNone(m, f'Mensagem de erro não confere com o padrão: {err_msg}')
        log_path = Path(m.group(1))
        self.assertTrue(log_path.exists(), f'Arquivo de falha não foi gravado: {log_path}')

        try:
            data = json.loads(log_path.read_text(encoding='utf-8'))
            self.assertEqual(data.get('titulo'), story['title'])
            self.assertEqual(data.get('fatos'), facts)
            self.assertEqual(len(data.get('tentativas', [])), 8)
            for att in data['tentativas']:
                self.assertLessEqual(len(att['resposta_bruta']), 4000)
                self.assertTrue(att.get('erro'))
                self.assertTrue(att.get('tentativa'))
        finally:
            try:
                log_path.unlink()
            except Exception:
                pass

        # Garante que a pauta NÃO foi marcada como usada
        db = engine.conn()
        try:
            row = db.execute("SELECT 1 FROM used WHERE id=?", (story['id'],)).fetchone()
            self.assertIsNone(row)
        finally:
            db.close()

    def test_item13_automatic_retries_next_candidate_on_script_failure(self):
        cands = [
            {
                'id': f'cand_{i}',
                'title': f'Pauta {i} sobre Tecnologia',
                'body': f'Corpo da matéria {i} sobre inovação tecnológica no Brasil com detalhes científicos relevantes. ' * 10,
                'source': 'G1',
                'source_type': 'rss',
                'summary': 'Resumo da matéria',
                'selection_reason': 'Muito relevante',
            }
            for i in range(1, 4)
        ]

        # Caso 1: Primeira pauta falha em write_script, segunda pauta sucede
        logs = []
        def mock_write(story, *args, **kwargs):
            if story['id'] == 'cand_1':
                raise RuntimeError("Não foi possível gerar um roteiro válido em 8 tentativas. Detalhes em out/falha.json")
            return [
                {'speaker': 0, 'text': 'O que aconteceu na pauta dois?', 'fato': 0},
                {'speaker': 1, 'text': 'Inovação avançou com sucesso!', 'fato': 1},
                {'speaker': 0, 'text': 'A tecnologia cresceu no Brasil.', 'fato': 0},
                {'speaker': 1, 'text': 'Será que o avanço continua?', 'fato': 1},
            ]

        with patch('news_auto.gather_candidates', return_value=list(cands)), \
             patch('news_auto.pick_story', side_effect=lambda c, *a, **k: dict(c[0])), \
             patch('news_auto.extract_facts', return_value=['Fato 1', 'Fato 2']), \
             patch('news_auto.enrich', return_value=[]), \
             patch('news_auto.write_script', side_effect=mock_write):
            story_res, lines_res = news_auto.automatic('tecnologia', log=logs.append)

        self.assertEqual(story_res['id'], 'cand_2')
        self.assertEqual(len(lines_res), 4)
        self.assertIn('Pauta falhou na geração; tentando outra', logs)

        # Caso 2: Todas as pautas falham em write_script (até 2 extras, total 3) -> lança erro final
        logs_all_fail = []
        with patch('news_auto.gather_candidates', return_value=list(cands)), \
             patch('news_auto.pick_story', side_effect=lambda c, *a, **k: dict(c[0])), \
             patch('news_auto.extract_facts', return_value=['Fato 1', 'Fato 2']), \
             patch('news_auto.enrich', return_value=[]), \
             patch('news_auto.write_script', side_effect=RuntimeError("Não foi possível gerar um roteiro válido em 8 tentativas. Detalhes em out/falha.json")):
            with self.assertRaisesRegex(RuntimeError, 'Não foi possível gerar um roteiro válido em 8 tentativas'):
                news_auto.automatic('tecnologia', log=logs_all_fail.append)

        self.assertEqual(logs_all_fail.count('Pauta falhou na geração; tentando outra'), 2)

    def test_item14_deterministic_repair_long_reactions(self):
        import re
        # 1. Reação de 7 palavras com pontuação forte vira <= 6 palavras cortada
        raw_7_words = {
            'lines': [
                {'speaker': 0, 'text': 'Você viu as notícias espaciais hoje?', 'fato': 0},
                {'speaker': 1, 'text': 'Caramba, eu não esperava por essa agora!', 'reacao': True},
                {'speaker': 0, 'text': 'O Webb descobriu novos planetas.', 'fato': 1},
                {'speaker': 1, 'text': 'Será que encontramos vida fora?', 'fato': 1},
            ]
        }
        logs_1 = []
        news_auto.repair_reactions(raw_7_words, log=logs_1.append)
        repaired_text = raw_7_words['lines'][1]['text']
        self.assertLessEqual(len(repaired_text.split()), 6)
        self.assertEqual(repaired_text, 'Caramba!')
        self.assertIn('fala 2: reação reparada', logs_1)

        # 2. Reação com número é substituída por reação fixa sem dígitos
        raw_digits = {
            'lines': [
                {'speaker': 0, 'text': 'Você viu as notícias espaciais hoje?', 'fato': 0},
                {'speaker': 1, 'text': 'Caramba, foram 10 recordes quebrados hoje!', 'reacao': True},
                {'speaker': 0, 'text': 'O Webb descobriu novos planetas.', 'fato': 1},
                {'speaker': 1, 'text': 'Será que encontramos vida fora?', 'fato': 1},
            ]
        }
        logs_2 = []
        news_auto.repair_reactions(raw_digits, log=logs_2.append)
        repaired_digit_text = raw_digits['lines'][1]['text']
        self.assertLessEqual(len(repaired_digit_text.split()), 6)
        self.assertFalse(bool(re.search(r'\d', repaired_digit_text)))
        self.assertIn(repaired_digit_text, news_auto.FIXED_REACTIONS)
        self.assertIn('fala 2: reação reparada', logs_2)

        # 3. Reparo NÃO altera falas marcadas com "fato"
        raw_fact = {
            'lines': [
                {'speaker': 0, 'text': 'Esta fala longa tem 10 palavras e o número 25 no texto.', 'fato': 0},
                {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
            ]
        }
        news_auto.repair_reactions(raw_fact)
        self.assertEqual(raw_fact['lines'][0]['text'], 'Esta fala longa tem 10 palavras e o número 25 no texto.')

        # 4. write_script auto-repara reação de 7 palavras antes de validar
        story = {
            'title': 'Descobertas do Telescópio Webb',
            'body': 'O telescópio espacial James Webb da Nasa fez grandes descobertas científicas no espaço profundo. '
                    'A missão espacial do James durou cerca de três anos e analisou amostras cósmicas em 90 dias. '
                    'O Webb detectou 25 novos exoplanetas e 277 asteroides na pesquisa recente. '
                    'A temperatura no vácuo espacial atingiu níveis extremos e vinte países parceiros participaram.',
            'source_type': 'rss',
            'extracted_facts': [
                'O telescópio espacial James Webb da Nasa observou galáxias distantes.',
                'A missão espacial durou cerca de três anos.',
                'O Webb detectou 25 novos exoplanetas e 277 asteroides.',
                'Os cientistas analisaram amostras de poeira cósmica.',
            ],
        }
        logs_ws = []
        def mock_writer_long_reaction(inst, payload_str):
            return {
                'lines': [
                    {'speaker': 0, 'text': 'O que o telescópio James Webb detectou no espaço?', 'fato': 0},
                    {'speaker': 1, 'text': 'Caramba, eu não esperava por essa agora!', 'reacao': True},
                    {'speaker': 0, 'text': 'O Webb detectou 25 novos exoplanetas e 277 asteroides.', 'fato': 2},
                    {'speaker': 1, 'text': 'A Nasa fez a missão espacial por três anos!', 'fato': 1},
                    {'speaker': 0, 'text': 'Os cientistas analisaram amostras de poeira cósmica.', 'fato': 3},
                    {'speaker': 1, 'text': 'A Nasa observou galáxias distantes com detalhes!', 'fato': 0},
                ]
            }

        mock_judge = lambda i, p, model=None: {'falas': [{'i': idx, 'suportada': True, 'motivo': ''} for idx in range(6)]} if 'checador de fatos' in i.lower() else {'nota': 8, 'responde_perguntas': True, 'contradiz_materia': False, 'problemas': []}
        out_lines = news_auto.write_script(story, max_words=80, ask=mock_writer_long_reaction, judge_ask=mock_judge, facts=story['extracted_facts'], log=logs_ws.append)
        self.assertEqual(len(out_lines), 6)
        self.assertIn('fala 2: reação reparada', logs_ws)
        self.assertEqual(out_lines[1]['text'], 'Caramba!')

    def test_item15_retries_diversity_concrete_correction_and_early_abort(self):
        import json
        story = {
            'title': 'Descobertas do Telescópio Webb',
            'body': 'O telescópio espacial James Webb da Nasa fez grandes descobertas científicas no espaço profundo. '
                    'A missão espacial do James durou cerca de três anos e analisou amostras cósmicas em 90 dias. '
                    'O Webb detectou 25 novos exoplanetas e 277 asteroides na pesquisa recente. '
                    'A temperatura no vácuo espacial atingiu níveis extremos e vinte países parceiros participaram.',
            'source_type': 'rss',
            'extracted_facts': [
                'O telescópio espacial James Webb da Nasa observou galáxias distantes.',
                'A missão espacial durou cerca de três anos.',
                'O Webb detectou 25 novos exoplanetas e 277 asteroides.',
                'Os cientistas analisaram amostras de poeira cósmica.',
            ],
        }

        # 1. Verifica temperaturas passadas por tentativa e exemplo concreto na correção
        temperatures_received = []
        corrections_received = []

        def mock_writer_temp(inst, payload_str, temperature=None):
            temperatures_received.append(temperature)
            payload = json.loads(payload_str)
            if 'correcao' in payload:
                corrections_received.append(payload['correcao'])
            if len(temperatures_received) < 4:
                filler = ' ' + ' '.join(['extra'] * (len(temperatures_received) + 1))
                return {'lines': [
                    {'speaker': 0, 'text': f'Quais exoplanetas o telescópio James Webb detectou nesta missão{filler}?', 'fato': 0},
                    {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
                    {'speaker': 0, 'text': 'O Webb detectou 25 novos exoplanetas e 277 asteroides.', 'fato': 2},
                    {'speaker': 1, 'text': 'A missão espacial fez descobertas por três anos!', 'fato': 1},
                    {'speaker': 0, 'text': 'Os cientistas analisaram amostras de poeira cósmica.', 'fato': 3},
                    {'speaker': 1, 'text': 'A Nasa observou galáxias distantes.', 'fato': 0},
                ]}
            return {'lines': [
                {'speaker': 0, 'text': 'Quais exoplanetas o telescópio James Webb detectou?', 'fato': 0},
                {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
                {'speaker': 0, 'text': 'O Webb detectou 25 novos exoplanetas e 277 asteroides.', 'fato': 2},
                {'speaker': 1, 'text': 'A missão espacial fez descobertas por três anos!', 'fato': 1},
                {'speaker': 0, 'text': 'Os cientistas analisaram amostras de poeira cósmica.', 'fato': 3},
                {'speaker': 1, 'text': 'A Nasa observou galáxias distantes.', 'fato': 0},
            ]}

        mock_judge = lambda i, p, model=None: {'falas': [{'i': idx, 'suportada': True, 'motivo': ''} for idx in range(6)]} if 'checador de fatos' in i.lower() else {'nota': 8, 'responde_perguntas': True, 'contradiz_materia': False, 'problemas': []}

        news_auto.write_script(story, max_words=80, ask=mock_writer_temp, judge_ask=mock_judge, facts=story['extracted_facts'])
        self.assertEqual(len(temperatures_received), 4)
        self.assertEqual(temperatures_received[0], 0.55)
        self.assertEqual(temperatures_received[1], 0.60)
        self.assertEqual(temperatures_received[2], 0.65)
        self.assertEqual(temperatures_received[3], 0.70)
        self.assertTrue(len(corrections_received) >= 1)
        self.assertIn('Exemplo', corrections_received[0])

        # 2. Abandono antecipado se o MESMO erro de validação se repetir 3 vezes seguidas
        calls_abort = []
        logs_abort = []

        def mock_writer_repeat(inst, payload_str, temperature=None):
            calls_abort.append(temperature)
            return {'lines': [
                {'speaker': 0, 'text': 'Quais exoplanetas o telescópio James Webb detectou nesta missão espacial agora?', 'fato': 0},
                {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
                {'speaker': 0, 'text': 'O Webb detectou 25 novos exoplanetas e 277 asteroides.', 'fato': 2},
                {'speaker': 1, 'text': 'A missão espacial fez descobertas por três anos!', 'fato': 1},
            ]}

        with self.assertRaises(RuntimeError) as ctx:
            news_auto.write_script(story, max_words=80, ask=mock_writer_repeat, judge_ask=mock_judge, facts=story['extracted_facts'], log=logs_abort.append)

        # Abandona logo na 3ª tentativa em vez de gastar 8 tentativas
        self.assertEqual(len(calls_abort), 3)
        self.assertTrue(any('erro repetido 3x:' in l for l in logs_abort))
        self.assertIn('erro repetido 3x:', str(ctx.exception))

    def test_item16_fact_check_failure_reasons_and_single_rejected_file(self):
        lines = [
            {'speaker': 0, 'text': 'Quais exoplanetas o telescópio James Webb detectou?', 'fato': 0},
            {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
            {'speaker': 0, 'text': 'O Webb detectou 25 novos exoplanetas e 277 asteroides.', 'fato': 2},
            {'speaker': 1, 'text': 'A missão espacial fez descobertas por três anos!', 'fato': 1},
            {'speaker': 0, 'text': 'Os cientistas analisaram amostras de poeira cósmica.', 'fato': 3},
            {'speaker': 1, 'text': 'A Nasa observou galáxias distantes.', 'fato': 0},
        ]
        facts = [
            'O telescópio espacial James Webb da Nasa observou galáxias distantes.',
            'A missão espacial durou cerca de três anos.',
            'O Webb detectou 25 novos exoplanetas e 277 asteroides.',
            'Os cientistas analisaram amostras de poeira cósmica.',
        ]
        story = {
            'title': 'Descobertas do Telescópio Webb',
            'source': 'Canaltech',
            'body': 'O telescópio espacial James Webb da Nasa fez grandes descobertas científicas no espaço profundo. '
                    'A missão espacial do James durou cerca de três anos e analisou amostras cósmicas em 90 dias. '
                    'O Webb detectou 25 novos exoplanetas e 277 asteroides na pesquisa recente.',
            'extracted_facts': facts,
        }

        # 1. Fallback UMA VEZ no HTTP 404 para o modelo do redator
        logs_404 = []
        story_fallback = dict(story)
        def ask_404_fallback(inst, payload, model=None):
            if model == 'qwen3:8b':
                raise RuntimeError("Ollama local respondeu HTTP 404: modelo 'qwen3:8b' não instalado.")
            return {'falas': [{'i': idx, 'suportada': True, 'motivo': ''} for idx in range(6)]}

        with patch.dict('os.environ', {'OLLAMA_MODEL': 'qwen3.5:4b', 'OLLAMA_JUDGE_MODEL': 'qwen3:8b'}):
            res_fb = news_auto.verificar_fatos(lines, facts, ask=ask_404_fallback, story=story_fallback, log=logs_404.append)
        self.assertIn('falas', res_fb)
        self.assertTrue(story_fallback.get('juiz_fraco'))
        self.assertTrue(any('juiz padrão indisponível' in l for l in logs_404))

        # 2. HTTP 404 sem sucesso registra causa real
        story_404_fail = dict(story)
        def ask_404_all(inst, payload, model=None):
            raise RuntimeError("Ollama local respondeu HTTP 404.")

        res_404 = news_auto.verificar_fatos(lines, facts, ask=ask_404_all, story=story_404_fail)
        self.assertTrue(res_404.get('indisponivel'))
        self.assertIn('404:', res_404.get('motivo', ''))
        self.assertIn('não instalado', res_404.get('motivo', ''))

        # 3. Timeout registra causa real
        def ask_timeout(inst, payload, model=None):
            raise RuntimeError("Connection timed out after 5 seconds")

        res_timeout = news_auto.verificar_fatos(lines, facts, ask=ask_timeout, story=dict(story))
        self.assertTrue(res_timeout.get('indisponivel'))
        self.assertEqual(res_timeout.get('motivo'), 'timeout na chamada ao Ollama')

        # 4. JSON inválido registra primeiros 300 caracteres
        raw_garbage = ''.join(f'tok{i:04d}_' for i in range(100))
        def ask_invalid_json(inst, payload, model=None):
            return raw_garbage

        res_invalid_json = news_auto.verificar_fatos(lines, facts, ask=ask_invalid_json, story=dict(story))
        self.assertTrue(res_invalid_json.get('indisponivel'))
        self.assertIn('JSON inválido', res_invalid_json.get('motivo', ''))
        self.assertIn(raw_garbage[:300], res_invalid_json.get('motivo', ''))
        self.assertNotIn(raw_garbage[350:], res_invalid_json.get('motivo', ''))

        # 5. Resposta sem a chave 'falas'
        def ask_missing_falas(inst, payload, model=None):
            return {'status': 'ok', 'avaliacao': 'boa'}

        res_missing = news_auto.verificar_fatos(lines, facts, ask=ask_missing_falas, story=dict(story))
        self.assertTrue(res_missing.get('indisponivel'))
        self.assertEqual(res_missing.get('motivo'), "ausência da chave 'falas'")

        # 6. write_script gera apenas 1 rejected_*.json (sem 3 arquivos ~3ms apart) e inclui motivo_checagem, source e modelos
        out_dir = Path(self.temp.name) / 'out_rej_item16'
        logs_ws = []
        writer_mock = lambda i, p, temperature=None: {'lines': lines}

        with patch.object(engine, 'OUTPUT', out_dir), \
             patch.dict('os.environ', {'REELFORGE_ALLOW_NO_FACT_CHECK': '0'}):
            with self.assertRaises(RuntimeError) as ctx:
                news_auto.write_script(story, max_words=80, ask=writer_mock, judge_ask=ask_missing_falas,
                                       facts=facts, log=logs_ws.append)

        self.assertIn('checagem de fatos indisponível', str(ctx.exception))
        self.assertTrue(any("motivo da checagem: ausência da chave 'falas'" in l for l in logs_ws))
        rejected_files = list(out_dir.glob('rejected_*.json'))
        # Garante que seja salvo exatamente 1 arquivo de rejeição por pauta
        self.assertEqual(len(rejected_files), 1)
        data = json.loads(rejected_files[0].read_text(encoding='utf-8'))
        self.assertEqual(data.get('motivo_checagem'), "ausência da chave 'falas'")
        self.assertEqual(data.get('source'), 'Canaltech')
        self.assertIn('redator', data.get('modelos', {}))
        self.assertIn('juiz', data.get('modelos', {}))
        self.assertIn('juiz_fraco', data.get('modelos', {}))

    def test_item17_minimum_size_and_script_ending(self):
        story = {
            'title': 'Descobertas do Telescópio Webb no Espaço Profundo',
            'body': 'O telescópio James Webb fez grandes descobertas astronômicas no espaço profundo. '
                    'O telescópio James Webb detectou 25 novos exoplanetas e 277 asteroides pelo universo. '
                    'O projeto custou bilhões de dólares e envolveu vinte países parceiros.',
        }
        facts = [
            'O telescópio James Webb fez grandes descobertas astronômicas no espaço profundo.',
            'O telescópio James Webb detectou 25 novos exoplanetas e 277 asteroides pelo universo.',
            'O projeto custou bilhões de dólares e envolveu vinte países parceiros.',
        ]

        # 1. Roteiro com menos de 6 falas (5 falas) é rejeitado pelo padrão min_lines=6; 7 falas passa
        script_5 = {'lines': [
            {'speaker': 0, 'text': 'Você viu as descobertas do James Webb no espaço profundo?', 'fato': 0},
            {'speaker': 1, 'text': 'Caramba, notícia sensacional!', 'reacao': True},
            {'speaker': 0, 'text': 'O telescópio detectou 25 novos exoplanetas e 277 asteroides.', 'fato': 1},
            {'speaker': 1, 'text': 'O telescópio James Webb teve apoio de vinte países parceiros.', 'fato': 2},
            {'speaker': 0, 'text': 'Pra mim, esse projeto espacial é o futuro da ciência.', 'fato': 0},
        ]}
        with self.assertRaisesRegex(ValueError, r'Entregue entre 6 e 12 falas em "lines" \(vieram 5\)'):
            news_auto.validate_lines(script_5, story, budget=80, facts=facts)

        script_7 = {'lines': [
            {'speaker': 0, 'text': 'Você viu as descobertas do James Webb no espaço profundo?', 'fato': 0},
            {'speaker': 1, 'text': 'Caramba, notícia sensacional!', 'reacao': True},
            {'speaker': 0, 'text': 'O telescópio detectou 25 novos exoplanetas e 277 asteroides.', 'fato': 1},
            {'speaker': 1, 'text': 'Sério mesmo?', 'reacao': True},
            {'speaker': 0, 'text': 'O telescópio James Webb teve apoio de vinte países parceiros.', 'fato': 2},
            {'speaker': 1, 'text': 'O telescópio James Webb fez grandes descobertas astronômicas!', 'fato': 0},
            {'speaker': 0, 'text': 'Pra mim, esse projeto espacial é o futuro da ciência.', 'fato': 0},
        ]}
        res7, _ = news_auto.validate_lines(script_7, story, budget=80, facts=facts)
        self.assertEqual(len(res7), 7)

        # 2. Roteiro com 8 falas mas média < 5 palavras sobre falas não-reação é rejeitado
        script_short_words = {'lines': [
            {'speaker': 0, 'text': 'Viu o Webb no espaço?', 'fato': 0},
            {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
            {'speaker': 0, 'text': 'O Webb achou 25 exoplanetas.', 'fato': 1},
            {'speaker': 1, 'text': 'Sério mesmo?', 'reacao': True},
            {'speaker': 0, 'text': 'Foram vinte países parceiros envolvidos.', 'fato': 2},
            {'speaker': 1, 'text': 'Tudo isso de parceiros?', 'fato': 2},
            {'speaker': 0, 'text': 'Sim, vinte países ajudaram.', 'fato': 2},
            {'speaker': 1, 'text': 'Achei esse futuro muito bizarro.', 'fato': 0},
        ]}
        with self.assertRaisesRegex(ValueError, r'Média de palavras por fala .* abaixo do mínimo de 5 palavras'):
            news_auto.validate_lines(script_short_words, story, budget=80, facts=facts)

        # 3. Fecho com "Será que ...?" genérico sem opinião é rejeitado
        script_generic_fecho = {'lines': [
            {'speaker': 0, 'text': 'Você viu as descobertas do James Webb no espaço profundo?', 'fato': 0},
            {'speaker': 1, 'text': 'Caramba, que descoberta sensacional!', 'reacao': True},
            {'speaker': 0, 'text': 'O Webb detectou 25 novos exoplanetas e também 277 asteroides.', 'fato': 1},
            {'speaker': 1, 'text': 'Nunca imaginei tantos exoplanetas novos pelo universo!', 'fato': 1},
            {'speaker': 0, 'text': 'O projeto envolveu vinte países parceiros ao redor do mundo.', 'fato': 2},
            {'speaker': 1, 'text': 'Esse projeto envolveu muitos parceiros internacionais!', 'fato': 2},
            {'speaker': 0, 'text': 'O telescópio espacial fez grandes descobertas pelo espaço profundo.', 'fato': 0},
            {'speaker': 1, 'text': 'Será que esse telescópio espacial vai continuar fazendo descobertas?', 'fato': 0},
        ]}
        with self.assertRaisesRegex(ValueError, r'A última fala não pode ser um "Será que \.\.\.\?" genérico'):
            news_auto.validate_lines(script_generic_fecho, story, budget=80, facts=facts)

        # 4. Fecho que repete >= 3 palavras de conteúdo de fala anterior é rejeitado
        script_repeat_fecho = {'lines': [
            {'speaker': 0, 'text': 'Você viu as descobertas do James Webb no espaço profundo?', 'fato': 0},
            {'speaker': 1, 'text': 'Caramba, que descoberta sensacional!', 'reacao': True},
            {'speaker': 0, 'text': 'O Webb detectou 25 novos exoplanetas e também 277 asteroides.', 'fato': 1},
            {'speaker': 1, 'text': 'Nunca imaginei tantos exoplanetas novos pelo universo!', 'fato': 1},
            {'speaker': 0, 'text': 'O projeto envolveu vinte países parceiros ao redor do mundo.', 'fato': 2},
            {'speaker': 1, 'text': 'Esse projeto envolveu muitos parceiros internacionais!', 'fato': 2},
            {'speaker': 0, 'text': 'O telescópio espacial fez grandes descobertas pelo espaço profundo.', 'fato': 0},
            {'speaker': 1, 'text': 'Acho loucura como esse telescópio espacial fez grandes descobertas astronômicas no espaço profundo!', 'fato': 0},
        ]}
        with self.assertRaisesRegex(ValueError, r'A última fala repete dados já ditos no roteiro'):
            news_auto.validate_lines(script_repeat_fecho, story, budget=80, facts=facts)

        # 5. Roteiro válido com 8 falas, média >= 6 palavras e fecho com opinião aceito
        script_valid = {'lines': [
            {'speaker': 0, 'text': 'Você viu as descobertas do James Webb no espaço profundo?', 'fato': 0},
            {'speaker': 1, 'text': 'Caramba, que descoberta sensacional!', 'reacao': True},
            {'speaker': 0, 'text': 'O Webb detectou 25 novos exoplanetas e também 277 asteroides.', 'fato': 1},
            {'speaker': 1, 'text': 'Nunca imaginei tantos exoplanetas novos pelo universo!', 'fato': 1},
            {'speaker': 0, 'text': 'O projeto envolveu vinte países parceiros ao redor do mundo.', 'fato': 2},
            {'speaker': 1, 'text': 'Esse projeto envolveu muitos parceiros internacionais!', 'fato': 2},
            {'speaker': 0, 'text': 'O telescópio espacial fez grandes descobertas pelo espaço profundo.', 'fato': 0},
            {'speaker': 1, 'text': 'Pra mim, esse projeto gastou bilhões de dólares e é loucura total.', 'fato': 2},
        ]}
        res, prob = news_auto.validate_lines(script_valid, story, budget=80, facts=facts)
        self.assertEqual(prob, '')
        self.assertEqual(len(res), 8)

        # 6. Exemplo concreto atualizado para erro de fecho (sem "Pra mim")
        ex = news_auto._add_concrete_example("Problema no fecho da história")
        self.assertIn('Exemplo de fecho válido com opinião: "Esse gasto alto sem garantia nenhuma é um risco grande."', ex)
        self.assertNotIn('Pra mim', ex)

    def test_item18_step1_hook_retry_and_graceful_fallback(self):
        story = {
            'title': 'Descoberta de nova jazida de lítio em Minas Gerais',
            'body': 'A mineradora confirmou uma nova jazida com 50 milhões de toneladas de lítio no Vale do Jequitinhonha. ' * 3,
        }
        facts_initial = [
            'A mineradora confirmou jazida no Vale do Jequitinhonha.',
            'A jazida possui 50 milhões de toneladas de lítio.',
            'A reserva fica no estado de Minas Gerais.',
            'O projeto visa produção sustentável de baterias.',
            'O investimento totaliza bilhões no setor.',
        ]

        # Caso A: contradição e gancho vazios no passo 1 -> retry específico preenche ambos
        calls_a = []
        def ask_a(instruction, payload_str):
            calls_a.append((instruction, json.loads(payload_str)))
            if len(calls_a) == 1:
                return {'fatos': facts_initial, 'contradicao': None, 'gancho': None}
            return {
                'contradicao': 'Grande reserva de lítio, mas infraestrutura regional ainda precária.',
                'gancho': 'Viu a nova reserva de 50 milhões de toneladas?',
            }

        logs_a = []
        res_a = news_auto.extract_facts(story, ask=ask_a, log=logs_a.append)
        self.assertEqual(len(calls_a), 2)
        self.assertIn('fatos_extraidos', calls_a[1][1])
        self.assertEqual(res_a['contradicao'], 'Grande reserva de lítio, mas infraestrutura regional ainda precária.')
        self.assertEqual(res_a['gancho'], 'Viu a nova reserva de 50 milhões de toneladas?')
        self.assertNotIn('sem contradição/gancho', logs_a)

        # Caso B: retry também devolve vazio -> segue sem gancho forçado e loga "sem contradição/gancho"
        calls_b = []
        def ask_b(instruction, payload_str):
            calls_b.append(payload_str)
            if len(calls_b) == 1:
                return {'fatos': facts_initial, 'contradicao': '', 'gancho': ''}
            return {'contradicao': None, 'gancho': None}

        logs_b = []
        res_b = news_auto.extract_facts(story, ask=ask_b, log=logs_b.append)
        self.assertEqual(len(calls_b), 2)
        self.assertIsNone(res_b['gancho'])
        self.assertIsNone(res_b['contradicao'])
        self.assertIn('sem contradição/gancho', logs_b)

        # Caso C: retry devolve gancho inválido (> 10 palavras) -> validate_hook rejeita e loga fallback
        calls_c = []
        def ask_c(instruction, payload_str):
            calls_c.append(payload_str)
            if len(calls_c) == 1:
                return {'fatos': facts_initial, 'contradicao': 'Contradição válida aqui', 'gancho': None}
            return {'gancho': 'Você sabia que foram encontradas muitas toneladas de lítio em minas gerais para baterias elétricas agora?'}

        logs_c = []
        res_c = news_auto.extract_facts(story, ask=ask_c, log=logs_c.append)
        self.assertEqual(len(calls_c), 2)
        self.assertIsNone(res_c['gancho'])
        self.assertIn('sem contradição/gancho', logs_c)

    def test_item19_writer_prompt_reinforcement_and_fictional_examples(self):
        # 1. Verifica termos fictícios de todos os 3 exemplos rastreados em FICTIONAL_EXAMPLE_TERMS
        expected_terms = {
            'zorbax', 'valoria', 'neoquimera', 'luminopolis', '742', '819', '2089', 'hoverboards', 'hoverboard',
            'kryptonix', 'zyphoria', 'mecazoids', 'mecazoid', '412', '953',
        }
        for term in expected_terms:
            self.assertIn(term, news_auto.FICTIONAL_EXAMPLE_TERMS)

        # 2. Termos do Exemplo 3 vazados no roteiro são rejeitados se ausentes na matéria
        story = {
            'title': 'Brasil avança em pesquisas de inteligência artificial',
            'body': 'Pesquisadores brasileiros anunciaram novos avanços no desenvolvimento de sistemas neurais no país. ' * 4,
        }
        leaked_ex3 = {'lines': [
            {'speaker': 0, 'text': 'Viu a nova tecnologia da Kryptonix em Zyphoria?', 'fato': 0},
            {'speaker': 1, 'text': 'Mano, isso é loucura pura!', 'reacao': True},
            {'speaker': 0, 'text': 'Eles colocaram 412 mecazoids para vigiar tudo.', 'fato': 0},
            {'speaker': 1, 'text': 'Pra mim, encher a cidade de robôs é uma furada total.', 'fato': 0},
        ]}
        with self.assertRaisesRegex(ValueError, 'exemplos fictícios'):
            news_auto.validate_lines(leaked_ex3, story, 80, min_lines=4)

        # 3. Prompt de write_script inclui reforço de estilo falado, qualificadores e os 3 exemplos
        prompts = []
        story_coffee = {
            'title': 'Exportação de café bate recorde e gera US$ 1,786 bilhão no Brasil',
            'body': (
                'O Brasil exportou 34,478 milhões de sacas de café e faturou US$ 1,786 bilhão no período histórico. '
                'Os Estados Unidos foram os maiores compradores do café brasileiro no mercado exterior. '
            ) * 4,
        }
        facts_resp = {
            'fatos': [
                'O Brasil exportou mais de 34 milhões de sacas de café.',
                'O faturamento bateu US$ 1,786 bilhão no período histórico.',
                'Os Estados Unidos foram os maiores compradores no exterior.',
                'A safra bateu recorde histórico no comércio exterior.',
                'O setor de café comemorou o faturamento no país.',
            ],
            'contradicao': 'Volume alto de sacas mas cotação média em queda.',
            'gancho': 'Quem vai pagar caro pelo café brasileiro?',
        }
        good_script = {'lines': [
            {'speaker': 0, 'text': 'Quem vai pagar caro pelo café brasileiro?', 'fato': 0},
            {'speaker': 1, 'text': 'O Brasil exportou mais de 34 milhões de sacas de café.', 'fato': 0},
            {'speaker': 0, 'text': 'O faturamento bateu US$ 1,786 bilhão no período histórico.', 'fato': 1},
            {'speaker': 1, 'text': 'Os Estados Unidos foram os maiores compradores no exterior.', 'fato': 2},
            {'speaker': 0, 'text': 'Caramba, que volume gigantesco!', 'reacao': True},
            {'speaker': 1, 'text': 'A safra bateu recorde histórico no comércio exterior.', 'fato': 3},
            {'speaker': 0, 'text': 'O comércio exterior celebrou bastante todo esse faturamento.', 'fato': 3},
            {'speaker': 1, 'text': 'Pra mim, esse setor comemorar faturamento assim é loucura total.', 'fato': 4},
        ]}

        seq = iter([facts_resp, good_script, {'nota': 9, 'problemas': []}])
        def capture_ask(inst, payload, **kwargs):
            prompts.append(inst)
            return next(seq)

        news_auto.write_script(story_coffee, max_words=100, ask=capture_ask, loop=False)
        self.assertGreaterEqual(len(prompts), 2)
        step2_inst = prompts[1]

        # Checa presença dos 3 exemplos fictícios
        self.assertIn('Exemplo 1:', step2_inst)
        self.assertIn('Exemplo 2:', step2_inst)
        self.assertIn('Exemplo 3:', step2_inst)
        self.assertIn('Kryptonix', step2_inst)
        self.assertIn('Zyphoria', step2_inst)
        self.assertIn('mecazoids', step2_inst)

        # Checa reforço de estilo falado e informal
        self.assertIn('Estilo falado e informal', step2_inst)
        self.assertIn('O Brasil mandou mais de 34 milhões de sacas pra fora', step2_inst)
        self.assertIn('faturou quase 1,8 bilhão de dólares', step2_inst)

        # Checa regra de arredondamento e qualificadores
        self.assertIn('Arredonde números com naturalidade', step2_inst)
        self.assertIn('mais de X', step2_inst)
        self.assertIn('quase X', step2_inst)
        self.assertIn('cerca de X', step2_inst)

        # Checa moderação de vocativos
        self.assertIn('vocativos (mano, cara) com moderação', step2_inst)

    def test_item20_isolated_output_guard(self):
        # 1. Verifica que o engine.OUTPUT ativo neste teste está isolado do diretório output/ real
        real_output = (engine.ROOT / 'output').resolve()
        self.assertNotEqual(engine.OUTPUT.resolve(), real_output)

        # 2. Verifica que todas as classes TestCase de ambos os módulos herdam IsolatedOutputMixin
        import inspect
        import tests.test_integration as ti
        for mod in (sys.modules[__name__], ti):
            for _, cls in inspect.getmembers(mod, inspect.isclass):
                if issubclass(cls, unittest.TestCase) and cls.__module__ == mod.__name__:
                    self.assertTrue(
                        issubclass(cls, IsolatedOutputMixin),
                        f'{mod.__name__}.{cls.__name__} deve herdar IsolatedOutputMixin',
                    )

        # 3. Verifica que _save_rejected_script e _save_failure_log usam engine.OUTPUT em tempo de chamada
        dir_a = Path(self.temp.name) / 'out_a'
        dir_b = Path(self.temp.name) / 'out_b'
        with patch.object(engine, 'OUTPUT', dir_a):
            p_rej_a = news_auto._save_rejected_script({'title': 'T1'}, [{'speaker': 0, 'text': 'Oi'}], {'nota': 4, 'problemas': []})
            p_fail_a = news_auto._save_failure_log({'title': 'T1'}, ['f1'], [])
        with patch.object(engine, 'OUTPUT', dir_b):
            p_rej_b = news_auto._save_rejected_script({'title': 'T2'}, [{'speaker': 0, 'text': 'Oi'}], {'nota': 4, 'problemas': []})
            p_fail_b = news_auto._save_failure_log({'title': 'T2'}, ['f2'], [])

        self.assertEqual(p_rej_a.parent, dir_a)
        self.assertEqual(p_fail_a.parent, dir_a)
        self.assertEqual(p_rej_b.parent, dir_b)
        self.assertEqual(p_fail_b.parent, dir_b)
        self.assertTrue(p_rej_a.is_file() and p_fail_a.is_file())
        self.assertTrue(p_rej_b.is_file() and p_fail_b.is_file())

        # 4. Guarda em subprocesso: rodar toda a suíte não cria nenhum arquivo novo no output/ real
        if os.environ.get('REELFORGE_SKIP_OUTPUT_GUARD') != '1':
            before = set(p.name for p in real_output.iterdir()) if real_output.exists() else set()
            env = dict(os.environ, REELFORGE_SKIP_OUTPUT_GUARD='1')
            proc = subprocess.run(
                [sys.executable, '-m', 'unittest', 'tests.test_news_auto', 'tests.test_integration'],
                cwd=str(engine.ROOT),
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, f'Subprocesso falhou:\n{proc.stdout}\n{proc.stderr}')
            after = set(p.name for p in real_output.iterdir()) if real_output.exists() else set()
            created = sorted(after - before)
            self.assertEqual(created, [], f'Suíte criou arquivos no output/ real: {created}')

    def test_item21_deterministic_pick_story_and_llm_fallback(self):
        # 1. Fluxo padrão não chama ask e escolhe a pauta não usada com maior complexity_score e corpo mínimo válido
        used_cand = {
            'id': 'u1',
            'title': 'Notícia já usada no banco',
            'source': 'G1',
            'url': 'https://a/u1',
            'body': 'A Petrobras anunciou R$ 50 bilhões em novos investimentos no Brasil nesta segunda-feira. ' * 5,
            'complexity': 99.0,
        }
        engine.mark_used(dict(used_cand, source_type='rss'))
        no_body_cand = {
            'id': 'nb1',
            'title': 'Manchete sem corpo com score alto',
            'source': 'G1',
            'url': 'https://a/nb1',
            'body': '',
            'complexity': 90.0,
        }
        low_score_cand = {
            'id': 'l1',
            'title': 'Pauta válida mais simples',
            'source': 'UOL',
            'url': 'https://a/l1',
            'body': 'Empresa apresentou novo recurso de sistema digital esta semana para clientes locais. ' * 5,
            'complexity': 10.5,
        }
        high_score_cand = {
            'id': 'h1',
            'title': 'Pauta válida com maior complexidade',
            'source': 'CNN Brasil',
            'url': 'https://a/h1',
            'body': 'O Banco Central registrou alta de 12,5% e reserva de US$ 350 bilhões no país em 2026. ' * 5,
            'complexity': 25.0,
        }
        cands = [used_cand, no_body_cand, low_score_cand, high_score_cand]
        ask_mock = Mock(side_effect=AssertionError('Fluxo padrão não deve chamar LLM em pick_story'))
        with patch.object(engine, 'load_settings', return_value={}):
            chosen = news_auto.pick_story(cands, ask=ask_mock)
        ask_mock.assert_not_called()
        self.assertEqual(chosen['id'], 'h1')

        # 2. Com escolha_pauta_llm: true -> envia no máx 12 pautas (só título + 1 frase, < 1500 tokens)
        many_cands = []
        for idx in range(15):
            many_cands.append({
                'id': f'c{idx}',
                'title': f'Título notícia {idx} sobre economia e tecnologia',
                'source': 'G1',
                'url': f'https://a/{idx}',
                'body': (
                    f'Primeira frase objetiva da notícia {idx} com R$ {100 + idx} milhões envolvidos. '
                    + ('Segundo parágrafo longo que não deve ir para o prompt de seleção. ' * 20)
                ),
                'complexity': 30.0 - idx,
            })
        captured = []
        def llm_ok(inst, payload_str):
            captured.append((inst, json.loads(payload_str)))
            return {'index': 2, 'reason': 'bom gancho'}

        with patch.object(engine, 'load_settings', return_value={'escolha_pauta_llm': True}):
            chosen_llm = news_auto.pick_story(many_cands, ask=llm_ok)
        self.assertEqual(chosen_llm['id'], 'c2')
        self.assertEqual(len(captured), 1)
        sent_inst, sent_payload = captured[0]
        sent_list = sent_payload['candidatos']
        self.assertLessEqual(len(sent_list), 12)
        self.assertEqual(set(sent_list[0].keys()), {'index', 'titulo', 'frase'})
        self.assertNotIn('Segundo parágrafo', json.dumps(sent_payload))
        est_tokens = (len(sent_inst) + len(json.dumps(sent_payload, ensure_ascii=False))) / 3.5
        self.assertLess(est_tokens, 1500)

        # 3. Fallback em timeout/falha do LLM: loga mensagem e retorna pauta topo do ranking
        logs = []
        def llm_timeout(inst, payload_str):
            raise TimeoutError('read timed out')

        with patch.object(engine, 'load_settings', return_value={'escolha_pauta_llm': True}):
            chosen_fb = news_auto.pick_story(many_cands, ask=llm_timeout, log=logs.append)
        self.assertEqual(chosen_fb['id'], 'c0')
        self.assertIn('escolha da pauta por LLM falhou; usando ranking', logs)

    def test_item22_ollama_timeout_think_and_logging(self):
        import requests

        # 1. Timeout configurável via settings.json, payload "think": false, limpeza de <think>/"thinking" e logs
        mock_resp = Mock(status_code=200)
        mock_resp.json.return_value = {
            'thinking': 'cadeia de raciocinio na raiz',
            'message': {
                'thinking': 'raciocinio interno na mensagem',
                'content': '<think>pensando passo a passo</think>\n{"status": "ok", "valor": 42}',
            },
        }
        logs = []
        inst = 'Instrucao teste ollama'
        payload = '{"dados": "abcde"}'
        expected_tokens = max(1, int(round((len(inst) + len(payload)) / 3.5)))

        fake_session = Mock()
        fake_session.__enter__ = Mock(return_value=fake_session)
        fake_session.__exit__ = Mock(return_value=False)
        fake_session.post.return_value = mock_resp

        with patch('requests.Session', return_value=fake_session), \
             patch.object(engine, 'load_settings', return_value={'ollama_model': 'qwen3:8b', 'ollama_timeout_s': 120}):
            res = engine.request_json(inst, payload, log=logs.append)

        self.assertEqual(res, {'status': 'ok', 'valor': 42})
        post_kwargs = fake_session.post.call_args.kwargs
        self.assertIs(post_kwargs['json']['think'], False)
        self.assertEqual(post_kwargs['timeout'], (5, 120.0))
        self.assertTrue(any(f'Ollama: modelo qwen3:8b, {expected_tokens} tokens de prompt estimados' in m for m in logs))
        self.assertTrue(any('Ollama: modelo qwen3:8b respondeu em ' in m for m in logs))

        # 2. Timeout padrão = 300 quando omitido de settings.json
        fake_session.post.reset_mock()
        with patch('requests.Session', return_value=fake_session), \
             patch.object(engine, 'load_settings', return_value={}):
            engine.request_json('a', 'b', log=lambda *a: None)
        self.assertEqual(fake_session.post.call_args.kwargs['timeout'], (5, 300.0))

        # 3. ReadTimeout e ConnectionError levantam RuntimeError amigável em português e gravam em output/erros.log
        for exc_cls in (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError):
            fake_session.post.side_effect = exc_cls('falha simulada de rede/tempo')
            with patch('requests.Session', return_value=fake_session), \
                 patch.object(engine, 'load_settings', return_value={'ollama_model': 'qwen3:8b', 'ollama_timeout_s': 120}):
                with self.assertRaises(RuntimeError) as ctx:
                    engine.request_json('inst', 'pay', log=lambda *a: None)
            msg = str(ctx.exception)
            self.assertIn('O Ollama não respondeu em 120 s com o modelo qwen3:8b.', msg)
            self.assertIn("Verifique se o modelo está carregado inteiro na GPU ('ollama ps')", msg)
            self.assertIn('tente um modelo menor ou diminua num_ctx', msg)
            self.assertIsNone(ctx.exception.__cause__)

        err_log = engine.OUTPUT / 'erros.log'
        self.assertTrue(err_log.is_file())
        log_txt = err_log.read_text(encoding='utf-8')
        self.assertIn('falha simulada de rede/tempo', log_txt)

    def test_item25_infer_facts_reactions_and_opinions(self):
        facts = [
            'A empresa Alpha construiu 12 usinas solares no Ceará.',
            'O investimento total chegou a R$ 4,5 bilhões neste projeto.',
            'As usinas vão abastecer 800 mil casas na região.',
            'A obra gerou 3 mil empregos diretos durante a construção.',
            'O governo estadual reduziu impostos para energia limpa.',
        ]
        story = {
            'title': 'Alpha investe R$ 4,5 bilhões em 12 usinas solares no Ceará',
            'body': ' '.join(facts) + ' ' + ('Energia renovável ganha força no Nordeste. ' * 25),
            'extracted_facts': facts,
        }

        # 1. annotate_lines infere fato (combinando 2 fatos), reação e opinião
        sample_lines = [
            {'speaker': 0, 'text': 'Por que construir 12 usinas solares custou R$ 4,5 bilhões?'},
            {'speaker': 1, 'text': 'Caramba, quanta grana!'},
            {'speaker': 0, 'text': 'A Alpha investiu R$ 4,5 bilhões e abastece 800 mil casas.'},
            {'speaker': 1, 'text': 'Esse projeto todo muda bastante o futuro energético.'},
            {'speaker': 0, 'text': 'Também gerou 3 mil empregos diretos no Ceará.'},
            {'speaker': 1, 'text': 'O governo estadual reduziu impostos para energia limpa.'},
            {'speaker': 0, 'text': 'A empresa Alpha acelerou muito a construção inteira.'},
            {'speaker': 1, 'text': 'Acho ótimo apostar em energia limpa no estado.'},
        ]
        logs = []
        ann = news_auto.annotate_lines(sample_lines, facts, story['title'], log=logs.append)
        self.assertEqual(ann[0]['tipo'], 'fato')
        self.assertIn(0, ann[0]['matched_facts'])
        self.assertIn(1, ann[0]['matched_facts'])
        self.assertEqual(ann[1]['tipo'], 'reacao')
        self.assertEqual(ann[2]['tipo'], 'fato')
        self.assertIn(2, ann[2]['matched_facts'])
        self.assertEqual(ann[3]['tipo'], 'opiniao')
        self.assertIn('fala 2: inferida como reação', logs)

        res, prob = news_auto.validate_lines({'lines': sample_lines}, story, budget=140, facts=facts)
        self.assertEqual(prob, '')
        self.assertEqual(len(res), 8)
        self.assertTrue(res[1].get('reacao'))
        self.assertTrue(res[3].get('opiniao'))

        # 2. Número inventado continua rejeitado
        bad_inv = list(sample_lines)
        bad_inv[4] = {'speaker': 0, 'text': 'Também gerou 99 empregos diretos no Ceará.'}
        with self.assertRaisesRegex(ValueError, 'Números ausentes na matéria: 99'):
            news_auto.validate_lines({'lines': bad_inv}, story, budget=140, facts=facts)

        # 3. Número de outro fato é aceito (mesmo se modelo passar "fato" errado)
        cross_num = list(sample_lines)
        cross_num[4] = {'speaker': 0, 'text': 'A Alpha abastece 800 mil casas com os empregos e 3 mil vagas.', 'fato': 0}
        res_cross, prob_cross = news_auto.validate_lines({'lines': cross_num}, story, budget=140, facts=facts)
        self.assertEqual(prob_cross, '')
        self.assertEqual(len(res_cross), 8)

        # 4. Opinião com número sem ligação com fatos é rejeitada
        bad_op_num = [{'speaker': 0, 'text': 'Eu vi quase 77 motivos para duvidar disso.'}]
        with self.assertRaisesRegex(ValueError, 'opiniões não podem conter dígitos'):
            news_auto.annotate_lines(bad_op_num, facts, story['title'])

        # 5. Opinião com entidade nova é rejeitada
        bad_op_ent = [{'speaker': 1, 'text': 'Acho que a Petrobras vai copiar esse projeto logo.'}]
        with self.assertRaisesRegex(ValueError, 'entidade "Petrobras" ausente nos fatos e no título'):
            news_auto.annotate_lines(bad_op_ent, facts, story['title'])

        # 6. Prompt sem "fato"/"reacao" e com fatos numerados no payload
        seen_prompts = []
        mock_judge = lambda i, p, m=None: (
            {'falas': [{'i': idx, 'suportada': True, 'motivo': ''} for idx in range(8)]}
            if 'checador de fatos' in i.lower() else {'nota': 9, 'problemas': []}
        )

        def mock_ask(inst, payload_str, temperature=None):
            seen_prompts.append((inst, json.loads(payload_str)))
            return {'lines': sample_lines}

        news_auto.write_script(story, max_words=140, ask=mock_ask, judge_ask=mock_judge, facts=facts)
        w_inst, w_pay = seen_prompts[0]
        self.assertNotIn('"fato":', w_inst)
        self.assertNotIn('"reacao":', w_inst)
        self.assertTrue(all(f.startswith(f'{idx + 1}. ') for idx, f in enumerate(w_pay['fatos_da_materia'])))

    def test_item26_normalize_repair_and_line_bounds(self):
        facts = [
            'A empresa Alpha construiu 12 usinas solares no Ceará.',
            'O investimento total chegou a R$ 4,5 bilhões neste ano.',
            'As novas usinas solares abastecem 800 mil casas na região.',
            'O projeto elétrico gerou 3 mil empregos diretos locais.',
            'O governo estadual acompanhou todas as obras das usinas.',
        ]
        story = {
            'title': 'Ceará inaugura 12 usinas solares de R$ 4,5 bilhões',
            'body': ' '.join(facts * 3),
            'source_type': 'rss',
            'extracted_facts': facts,
        }

        # 1. speaker string ("0"/"1"), chaves extras ("feito"), reação no fim, > 3 reações e junção <= 22 palavras
        noisy = {
            'lines': [
                {'speaker': '0', 'text': 'Você viu as 12 usinas solares no Ceará?', 'feito': 1},
                {'speaker': '1', 'text': 'Caramba!'},
                {'speaker': '0', 'text': 'A Alpha construiu 12 usinas solares no Ceará.'},
                {'speaker': '1', 'text': 'Sério mesmo?'},
                {'speaker': '0', 'text': 'Investiram R$ 4,5 bilhões nas obras.'},
                {'speaker': '0', 'text': 'Inacreditável!'},
                {'speaker': '1', 'text': 'Elas abastecem 800 mil casas locais.'},
                {'speaker': '0', 'text': 'Que loucura!'},
                {'speaker': '0', 'text': 'Geraram 3 mil empregos diretos.'},
                {'speaker': '0', 'text': 'E o governo acompanhou as obras.'},
                {'speaker': '1', 'text': 'Não acredito.'},
            ]
        }
        logs = []
        norm = news_auto.normalize_lines(noisy, facts=facts, title=story['title'], log=logs.append)
        norm_lines = norm['lines']
        # Reação final removida, >3 reações podadas a 2, e falas consecutivas do speaker 0 unidas
        self.assertTrue(all(set(l.keys()) == {'speaker', 'text'} for l in norm_lines))
        self.assertTrue(all(isinstance(l['speaker'], int) for l in norm_lines))
        self.assertTrue(any('reparo: removida reação final' in m for m in logs))
        self.assertTrue(any('removida reação em excesso' in m for m in logs))
        self.assertTrue(any('falas consecutivas do mesmo speaker (0) unidas' in m for m in logs))
        self.assertTrue(any('removidas chaves extras (feito)' in m for m in logs))
        res, prob = news_auto.validate_lines(norm, story, budget=140, facts=facts)
        self.assertEqual(prob, '')
        self.assertGreaterEqual(len(res), 6)

        # 2. Falas consecutivas do mesmo speaker com > 22 palavras são rejeitadas
        too_long_merge = {
            'lines': [
                {'speaker': 0, 'text': 'A empresa Alpha construiu exatamente doze grandes usinas solares importantes na região do Ceará inteiro neste ano.'},
                {'speaker': 0, 'text': 'Além disso, todo o investimento total dessa empresa chegou a R$ 4,5 bilhões com foco em energia limpa regional.'},
            ]
        }
        with self.assertRaisesRegex(ValueError, 'excedem 22 palavras'):
            news_auto.normalize_lines(too_long_merge, facts=facts, title=story['title'])

        # 3. > 12 falas remove reações/opiniões do meio até 12
        over_12 = {
            'lines': [
                {'speaker': 0, 'text': 'Viu as 12 usinas solares no Ceará?', 'feito': 'x'},
                {'speaker': 1, 'text': 'Caramba!'},
                {'speaker': 0, 'text': 'A empresa Alpha construiu 12 usinas solares no Ceará.'},
                {'speaker': 1, 'text': 'Isso parece uma iniciativa muito interessante para todos nós.'},
                {'speaker': 0, 'text': 'O investimento total chegou a R$ 4,5 bilhões neste ano.'},
                {'speaker': 1, 'text': 'Sério mesmo?'},
                {'speaker': 0, 'text': 'As novas usinas solares abastecem 800 mil casas na região.'},
                {'speaker': 1, 'text': 'Tomara que ajude bastante gente por lá agora.'},
                {'speaker': 0, 'text': 'O projeto elétrico gerou 3 mil empregos diretos locais.'},
                {'speaker': 1, 'text': 'Emprego faz toda diferença para qualquer cidade crescer bem.'},
                {'speaker': 0, 'text': 'O governo estadual acompanhou todas as obras das usinas.'},
                {'speaker': 1, 'text': 'As usinas solares mudam muito a matriz elétrica do Ceará.'},
                {'speaker': 0, 'text': 'O investimento de 4,5 bilhões garante energia limpa.'},
                {'speaker': 1, 'text': 'Sem apoio do governo seria difícil tirar isso do papel.'},
            ]
        }
        norm12 = news_auto.normalize_lines(over_12, facts=facts, title=story['title'], log=lambda *a: None, max_lines=12)
        self.assertLessEqual(len(norm12['lines']), 12)

        # 4. Média sobre não-reação: 5.0–6.0 aceita com aviso; < 5.0 rejeitada
        avg_54 = {
            'lines': [
                {'speaker': 0, 'text': 'Ceará ganhou 12 usinas solares?'},       # 5
                {'speaker': 1, 'text': 'Caramba!'},                              # reacao (ignorada na média)
                {'speaker': 0, 'text': 'Sim, 12 usinas por 4,5 bilhões.'},       # 6 (responde gancho!)
                {'speaker': 1, 'text': 'Usinas abastecem 800 mil casas.'},       # 5
                {'speaker': 0, 'text': 'Geraram 3 mil empregos diretos.'},       # 5
                {'speaker': 1, 'text': 'Governo acompanhou obras das usinas.'},  # 5
                {'speaker': 0, 'text': 'Usinas solares mudaram bastante coisa por lá.'},  # 7 -> 33/6 = 5.50 (opinião >6 palavras)
            ]
        }
        warn_logs = []
        res_54, _ = news_auto.validate_lines(avg_54, story, budget=120, facts=facts, log=warn_logs.append)
        self.assertEqual(len(res_54), 7)
        self.assertTrue(any('entre 5.0 e 6.0' in m for m in warn_logs))

        avg_48 = {
            'lines': [
                {'speaker': 0, 'text': '12 usinas no Ceará?'},                   # 4
                {'speaker': 1, 'text': 'Caramba!'},                              # reacao
                {'speaker': 0, 'text': 'Alpha gastou 4,5 bilhões.'},             # 4
                {'speaker': 1, 'text': 'Abastecem 800 mil casas.'},              # 4
                {'speaker': 0, 'text': '3 mil empregos diretos.'},               # 4
                {'speaker': 1, 'text': 'Governo acompanhou essas obras.'},        # 4
            ]
        }
        with self.assertRaisesRegex(ValueError, 'abaixo do mínimo de 5 palavras'):
            news_auto.validate_lines(avg_48, story, budget=120, facts=facts)

    def test_item27_retry_policy_cap_and_low_temp_on_malformed_json(self):
        facts = [
            'A empresa Alpha construiu 12 usinas solares no Ceará.',
            'O investimento total chegou a R$ 4,5 bilhões neste ano.',
            'As novas usinas solares abastecem 800 mil casas na região.',
            'O projeto elétrico gerou 3 mil empregos diretos locais.',
        ]
        story = {
            'title': 'Ceará inaugura 12 usinas solares de R$ 4,5 bilhões',
            'body': ' '.join(facts * 3),
            'source_type': 'rss',
            'extracted_facts': facts,
        }
        valid_lines = [
            {'speaker': 0, 'text': 'Viu as 12 usinas solares no Ceará?'},
            {'speaker': 1, 'text': 'A Alpha construiu 12 usinas por R$ 4,5 bilhões!'},
            {'speaker': 0, 'text': 'O investimento total chegou a R$ 4,5 bilhões neste ano.'},
            {'speaker': 1, 'text': 'Elas abastecem 800 mil casas lá na região.'},
            {'speaker': 0, 'text': 'O projeto elétrico gerou 3 mil empregos diretos locais.'},
            {'speaker': 1, 'text': 'Isso muda completamente a energia daquela região inteira!'},
        ]
        mock_judge = lambda i, p, m=None: (
            {'falas': [{'i': idx, 'suportada': True, 'motivo': ''} for idx in range(6)]}
            if 'checador de fatos' in i.lower() else {'nota': 9, 'problemas': []}
        )

        # 1. JSON quebrado (JSONDecodeError) e chaves estranhas acionam temp=0.4 e formato exato na correção
        temps_1 = []
        corrs_1 = []
        def mock_ask_malformed(inst, payload_str, temperature=None):
            temps_1.append(temperature)
            payload = json.loads(payload_str)
            if 'correcao' in payload:
                corrs_1.append(payload['correcao'])
            step = len(temps_1)
            if step == 1:
                raise json.JSONDecodeError('Expecting value', '{"lines": [', 10)
            if step == 2:
                return {'resultado': [{'speaker': 0, 'feito': 'Texto com chave errada'}]}
            if step == 3:
                return {'lines': [{'speaker': 0, 'text': f'Viu as 12 usinas solares{" extra" * 8}?'}] + valid_lines[1:]}
            return {'lines': valid_lines}

        res1 = news_auto.write_script(story, max_words=120, ask=mock_ask_malformed, judge_ask=mock_judge, facts=facts)
        self.assertEqual(len(res1), 6)
        # tentativa 1=0.55 (JSON quebrado), 2=0.4 (chaves estranhas), 3=0.4 (erro normal), 4=0.70 (ou <=0.7: min(0.7, 0.55 + 3*0.05) = 0.70)
        self.assertEqual(temps_1, [0.55, 0.4, 0.4, 0.7])
        self.assertIn('{"lines":[{"speaker":0,"text":"..."}]}', corrs_1[0])
        self.assertIn('{"lines":[{"speaker":0,"text":"..."}]}', corrs_1[1])

        # 2. Escalada de temperatura +0.05 por retry com teto máximo em 0.7
        temps_2 = []
        def mock_ask_cap(inst, payload_str, temperature=None):
            temps_2.append(temperature)
            step = len(temps_2)
            if step < 7:
                # Erros distintos de tamanho/número para não acionar erro repetido 3x
                bad_num = 700 + step
                return {'lines': [{'speaker': 0, 'text': f'Viu as {bad_num} usinas no Ceará?'}] + valid_lines[1:]}
            return {'lines': valid_lines}

        res2 = news_auto.write_script(story, max_words=120, ask=mock_ask_cap, judge_ask=mock_judge, facts=facts)
        self.assertEqual(len(res2), 6)
        self.assertEqual(temps_2, [0.55, 0.6, 0.65, 0.7, 0.7, 0.7, 0.7])
        self.assertTrue(all(t <= 0.7 for t in temps_2))

    def test_item28_profanity_example_leak_duplicate_reaction_and_related_commons(self):
        story = {
            'title': 'Banco Central reduz juros para 10% após reunião em Brasília',
            'body': (
                'O Banco Central decidiu reduzir a taxa básica de juros para 10% ao ano em Brasília nesta quarta. '
                'A diretoria apontou desaceleração da inflação no varejo e melhora nas expectativas do mercado financeiro. '
                'Economistas preveem impacto gradual no crédito comercial e nos financiamentos imobiliários nos próximos meses. '
            ) * 3,
            'related': [
                'Hora de trocar o investimento no Brasil pode mudar hoje',
                'Deputado Carlos Maranhão apresenta novo projeto no Congresso',
            ],
        }
        facts = [
            'O Banco Central reduziu a taxa de juros para 10% ao ano em Brasília.',
            'A diretoria apontou desaceleração da inflação no varejo.',
            'Expectativas do mercado financeiro mostraram melhora recente.',
            'Economistas preveem impacto gradual no crédito comercial e nos financiamentos.',
        ]

        # 1. Palavrão é bloqueado ("linguagem imprópria")
        bad_profanity = {'lines': [
            {'speaker': 0, 'text': 'Os juros em Brasília caíram para 10%?'},
            {'speaker': 1, 'text': 'O Banco Central confirmou o corte para 10% ao ano.'},
            {'speaker': 0, 'text': 'Puta que pariu, que mudança rápida!'},
            {'speaker': 1, 'text': 'A diretoria notou desaceleração da inflação no varejo.'},
            {'speaker': 0, 'text': 'O mercado financeiro mostrou melhora nas expectativas recentes.'},
            {'speaker': 1, 'text': 'Economistas aguardam impacto gradual nos financiamentos futuros.'},
        ]}
        with self.assertRaisesRegex(ValueError, r'linguagem imprópria'):
            news_auto.validate_lines(bad_profanity, story, budget=120, facts=facts)

        # 2. Cópia de 5-gram dos exemplos do prompt é bloqueada ("trecho copiado dos exemplos")
        bad_example_leak = {'lines': [
            {'speaker': 0, 'text': 'Os juros em Brasília caíram para 10%?'},
            {'speaker': 1, 'text': 'O Banco Central confirmou o corte para 10% ao ano.'},
            {'speaker': 0, 'text': 'A diretoria notou desaceleração da inflação no varejo.'},
            {'speaker': 1, 'text': 'O mercado financeiro mostrou melhora nas expectativas recentes.'},
            {'speaker': 0, 'text': 'Economistas preveem impacto gradual no crédito comercial.'},
            {'speaker': 1, 'text': 'Pra mim, investir bilhões nisso sem testar antes é loucura total.'},
        ]}
        with self.assertRaisesRegex(ValueError, r'trecho copiado dos exemplos'):
            news_auto.validate_lines(bad_example_leak, story, budget=120, facts=facts)

        # 3. Reação repetida no mesmo roteiro é rejeitada
        bad_dup_react = {'lines': [
            {'speaker': 0, 'text': 'Os juros em Brasília caíram para 10%?'},
            {'speaker': 1, 'text': 'Caramba!'},
            {'speaker': 0, 'text': 'O Banco Central reduziu a taxa para 10% ao ano em Brasília.'},
            {'speaker': 1, 'text': 'A diretoria apontou desaceleração da inflação no varejo.'},
            {'speaker': 0, 'text': 'Caramba!'},
            {'speaker': 1, 'text': 'Economistas preveem impacto gradual nos financiamentos futuros.'},
        ]}
        with self.assertRaisesRegex(ValueError, r'Reação repetida'):
            news_auto.validate_lines(bad_dup_react, story, budget=120, facts=facts)

        # 4. Palavras comuns das relacionadas ("hora", "Brasil") não causam falso positivo
        good_with_common = {'lines': [
            {'speaker': 0, 'text': 'Os juros caíram para 10% na hora certa?'},
            {'speaker': 1, 'text': 'O Banco Central reduziu a taxa de juros para 10% ao ano em Brasília.'},
            {'speaker': 0, 'text': 'A diretoria apontou desaceleração da inflação no varejo.'},
            {'speaker': 1, 'text': 'Expectativas do mercado financeiro mostraram melhora recente.'},
            {'speaker': 0, 'text': 'Economistas preveem impacto gradual no crédito comercial.'},
            {'speaker': 1, 'text': 'Brasil inteiro aguarda alívio real nos financiamentos imobiliários.'},
        ]}
        res_ok, _ = news_auto.validate_lines(good_with_common, story, budget=120, facts=facts)
        self.assertEqual(len(res_ok), 6)

        # 5. Nome próprio inventado das manchetes relacionadas continua reprovando
        bad_rel_proper = {'lines': [
            {'speaker': 0, 'text': 'Os juros em Brasília caíram para 10%?'},
            {'speaker': 1, 'text': 'O Banco Central reduziu a taxa para 10% ao ano.'},
            {'speaker': 0, 'text': 'A diretoria apontou desaceleração da inflação no varejo.'},
            {'speaker': 1, 'text': 'Carlos Maranhão também acompanhou o mercado financeiro hoje.'},
            {'speaker': 0, 'text': 'Expectativas do mercado mostraram melhora nos financiamentos.'},
            {'speaker': 1, 'text': 'Corte gradual nos financiamentos deve movimentar o crédito este ano.'},
        ]}
        with self.assertRaisesRegex(ValueError, r'Contaminação de manchete relacionada'):
            news_auto.validate_lines(bad_rel_proper, story, budget=120, facts=facts)


if __name__ == '__main__':
    unittest.main(verbosity=2)
