"""Testes offline do fluxo automático RSS -> Ollama (rede e modelo simulados)."""
import importlib.util
import json
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

HAS_NUMPY = importlib.util.find_spec('numpy') is not None


def entry(title, source, link):
    return {'title': f'{title} - {source}', 'source': {'title': source}, 'link': link,
            'published': 'Sun, 08 Nov 2026 02:00:00 GMT', 'summary': ''}


FEED = [entry('Governo anuncia novo programa de internet nas escolas públicas', 'G1', 'https://a/1'),
        entry('TECNOLOGIA 🖥️ post viral sobre celulares', 'Instagram', 'https://a/2'),
        entry('Sony relança Walkman com preço de R$ 32 mil no Brasil', 'O Globo', 'https://a/3')]


class NewsAutoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = patch.object(engine, 'DB', Path(self.temp.name) / 'h.sqlite3')
        self.db.start()

    def tearDown(self):
        self.db.stop()
        self.temp.cleanup()

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
        story = news_auto.pick_story(cands, ask=lambda i, p: {'index': '1', 'reason': 'ok'})
        self.assertEqual(story['title'], 'B')

    def test_script_alternates_speakers_and_rejects_invented_numbers(self):
        story = {'title': 'Sony relança Walkman com preço de R$ 32 mil no Brasil',
                 'body': 'A Sony confirmou o novo Walkman por R$ 32 mil no mercado brasileiro com foco em áudio de estúdio. ' * 3}
        facts_resp = {'fatos': ['A Sony relançou o Walkman no Brasil.', 'O aparelho custa R$ 32 mil.',
                                'O foco é áudio de alta definição.', 'O modelo chega este mês.', 'É voltado a profissionais.']}
        bad = {'lines': [{'speaker': 0, 'text': 'Pagaria 50 mil reais num Walkman?', 'fato': 1}] + [{'speaker': 0, 'text': 'Fala.', 'fato': 0}] * 5}
        good = {'lines': [{'speaker': 1, 'text': 'Naruto: Pagaria 32 mil reais num Walkman hoje?', 'fato': 1},
                          {'speaker': 1, 'text': 'Cara, R$ 32 mil é preço de carro!', 'fato': 1},
                          {'speaker': 0, 'text': 'A Sony lançou esse modelo focado em estúdio.', 'fato': 2},
                          {'speaker': 0, 'text': 'Mesmo assim, quem no Brasil vai encarar esse valor?', 'fato': 0},
                          {'speaker': 1, 'text': 'Caramba, só colecionador!', 'reacao': True},
                          {'speaker': 0, 'text': 'Você compraria se tivesse essa grana sobrando?', 'fato': 0}]}
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
        story = {'title': 'Anthropic compra geradores', 'related': [],
                 'body': 'A Anthropic fechou um acordo de US$ 3,4 bilhões com a Caterpillar no Texas. ' * 10}
        vague = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in
                           ('Sabia que uma empresa fez um acordo?', 'Isso é importante.',
                            'A tecnologia avança.', 'O que você acha disso?')]}
        with self.assertRaisesRegex(ValueError, 'genérico'):
            news_auto.validate_lines(vague, story, 60)
        rich = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in
                          ('Por que a Anthropic gastou 3,4 bilhões?', 'O acordo foi direto com a Caterpillar.',
                           'Os geradores vão para o Texas.', 'Será que a rede aguenta os data centers?')]}
        lines, problem = news_auto.validate_lines(rich, story, 60)
        self.assertEqual(problem, '')
        self.assertEqual(len(lines), 4)

    def test_validation_without_bridge_allows_odd_turns_and_rejects_dangling_ending(self):
        self.assertFalse(hasattr(news_auto, 'loop_bridge'))
        self.assertFalse(hasattr(news_auto, 'DEFAULT_BRIDGE'))
        self.assertFalse(news_auto.loop_enabled())
        story = {'title': 'Sony relança Walkman por R$ 32 mil no Brasil',
                 'body': 'A Sony lançou no Brasil o novo Walkman por R$ 32 mil para audiófilos. ' * 3}
        odd_complete = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in (
            'Pagaria R$ 32 mil num Walkman novo?',
            'A Sony trouxe o aparelho de volta ao Brasil.',
            'Ele é feito para audiófilos exigentes.',
            'Mesmo assim o preço assustou todo mundo.',
            'Vale tudo isso só para ouvir música?'
        )]}
        lines, problem = news_auto.validate_lines(odd_complete, story, 80, loop=False, min_lines=4, max_lines=8)
        self.assertEqual(problem, '')
        self.assertEqual(len(lines), 5)
        dangling = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in (
            'Pagaria R$ 32 mil num Walkman novo?',
            'A Sony trouxe o aparelho de volta ao Brasil.',
            'Ele é feito para audiófilos exigentes.',
            'E todo mundo está comentando isso porque'
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
            news_auto.validate_lines(bad_cpg, story, 80)
        bad_uol = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in (
            'Viu a descoberta de 200 milhões de barris?',
            'A Petrobras achou petróleo na Bacia de Campos.',
            'Tudo isso está confirmado pelo Uol nesta segunda-feira.',
            'Será que a gasolina baixa agora?'
        )]}
        with self.assertRaisesRegex(ValueError, 'veículo|confirmado por'):
            news_auto.validate_lines(bad_uol, story, 80)

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
            news_auto.validate_lines(leaked_entity, story, 80)
        leaked_number = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in (
            'Já conferiu as 180 questões do Enem?',
            'O MEC e o Inep liberaram o gabarito no Brasil.',
            'Foram 999 milhões envolvidos no caso.',
            'Quantos acertos você conseguiu fazer?'
        )]}
        with self.assertRaisesRegex(ValueError, 'Números ausentes'):
            news_auto.validate_lines(leaked_number, story, 80)
        with self.assertRaisesRegex(ValueError, 'sem corpo'):
            news_auto.require_minimum_source({'title': 'Só título curto', 'body': ''})

    def test_two_step_extraction_and_quality_filter_regenerates_below_7(self):
        story = {
            'title': 'Anthropic fecha acordo de US$ 3,4 bilhões em geradores no Texas',
            'body': ('A Anthropic assinou um contrato de US$ 3,4 bilhões com a Caterpillar no Texas '
                     'para garantir energia até 2045 em seus data centers de inteligência artificial. ' * 5),
        }
        step1_facts = {'fatos': [
            'A Anthropic fechou contrato de US$ 3,4 bilhões.',
            'Os equipamentos são da Caterpillar.',
            'A instalação fica no Texas.',
            'O acordo garante energia até 2045.',
            'O objetivo é abastecer data centers de IA.',
        ]}
        script_v1 = {'lines': [
            {'speaker': 0, 'text': 'Por que gastar 3,4 bilhões em geradores?', 'fato': 0},
            {'speaker': 0, 'text': 'A Anthropic fechou isso com a Caterpillar.', 'fato': 1},
            {'speaker': 0, 'text': 'Fica tudo lá no Texas.', 'fato': 2},
            {'speaker': 0, 'text': 'A energia vai até 2045 nos data centers.', 'fato': 3},
            {'speaker': 0, 'text': 'Isso muda o jogo da IA.', 'fato': 4},
            {'speaker': 0, 'text': 'O que você acha desse gasto?', 'fato': 0}
        ]}
        eval_low = {'nota': 5, 'problemas': ['Falas finais pouco espontâneas']}
        script_v2 = {'lines': [
            {'speaker': 0, 'text': 'Por que gastar 3,4 bilhões só em gerador?', 'fato': 0},
            {'speaker': 0, 'text': 'Cara, a Anthropic comprou tudo da Caterpillar!', 'fato': 1},
            {'speaker': 0, 'text': 'É que a rede elétrica do Texas não aguenta mais.', 'fato': 2},
            {'speaker': 0, 'text': 'Eles garantiram energia até 2045 pros data centers.', 'fato': 3},
            {'speaker': 0, 'text': 'Mano, é muita energia!', 'reacao': True},
            {'speaker': 0, 'text': 'Será que vai faltar luz pro resto do estado?', 'fato': 2}
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
        self.assertEqual(news_auto.turn_range({'title': 'Curta', 'body': ''}, 120), (4, 8))
        self.assertEqual(news_auto.turn_range({'title': 'Média', 'body': 'palavra ' * 120}, 120), (6, 10))
        self.assertEqual(news_auto.turn_range({'title': 'Rica', 'body': 'palavra ' * 350}, 140), (8, 12))

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
            'Os equipamentos são da Caterpillar.',
            'A instalação fica no Texas.',
            'O acordo garante energia até 2045.',
            'O objetivo é abastecer data centers de IA.',
        ]}
        script = {'lines': [
            {'speaker': 0, 'text': 'Por que gastar 3,4 bilhões só em gerador?', 'fato': 0},
            {'speaker': 0, 'text': 'Cara, a Anthropic comprou tudo da Caterpillar!', 'fato': 1},
            {'speaker': 0, 'text': 'É que a rede elétrica do Texas não aguenta mais.', 'fato': 2},
            {'speaker': 0, 'text': 'Eles garantiram energia até 2045 pros data centers.', 'fato': 3},
            {'speaker': 0, 'text': 'Mano, é muita energia!', 'reacao': True},
            {'speaker': 0, 'text': 'Será que vai faltar luz pro resto do estado?', 'fato': 2}
        ]}
        low_eval = {'nota': 5, 'problemas': ['Diálogo pouco natural']}
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
        self.assertIn('Diálogo pouco natural', saved['problemas'])

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
            news_auto.validate_lines(leaked, story, 80)

        prompts = []
        facts_resp = {'fatos': [
            'A Sony lançou o Walkman no Brasil.',
            'O preço oficial é de R$ 32 mil.',
            'O modelo é focado em áudio de estúdio.',
            'O público-alvo são profissionais de som.',
            'O aparelho chegou ao mercado nacional.',
        ]}
        good = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in (
            'Pagaria R$ 32 mil num Walkman novo?',
            'A Sony trouxe o aparelho de volta ao Brasil.',
            'O foco é som de estúdio profissional.',
            'Vale tudo isso só para ouvir música?'
        )]}
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
            'Será que isso aumenta a produção de energia?'
        )]}
        lines, problem = news_auto.validate_lines(valid_with_digit, story, 80)
        self.assertEqual(problem, '')
        self.assertEqual(len(lines), 4)

        absent_digit = {'lines': [{'speaker': 0, 'text': t, 'fato': 0} for t in (
            'Sabia que a Petrobras terá 7 novas plataformas?',
            'A construção será lá na Bacia de Santos.',
            'É um projeto grande da estatal no Brasil.',
            'Será que isso aumenta a produção de energia?'
        )]}
        with self.assertRaisesRegex(ValueError, 'Números ausentes'):
            news_auto.validate_lines(absent_digit, story, 80)

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
        ]}
        script_resp = {'lines': [
            {'speaker': 0, 'text': 'Pagaria R$ 32 mil num Walkman novo?', 'fato': 1},
            {'speaker': 0, 'text': 'A Sony trouxe o aparelho de volta ao Brasil.', 'fato': 0},
            {'speaker': 0, 'text': 'O foco é áudio de estúdio profissional.', 'fato': 2},
            {'speaker': 0, 'text': 'Mesmo assim o preço assustou todo mundo.', 'fato': 1},
            {'speaker': 0, 'text': 'Caramba, só colecionador!', 'reacao': True},
            {'speaker': 0, 'text': 'Vale tudo isso só para ouvir música?', 'fato': 1}
        ]}
        seq = iter([
            {'index': 0, 'reason': 'primeira sem corpo'},
            {'index': 0, 'reason': 'segunda com corpo'},
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
        # 1. Fala sem "fato" e sem "reacao"
        bad_no_tag = {'lines': [
            {'speaker': 0, 'text': 'Sabia do novo recorde de café?', 'fato': 0},
            {'speaker': 1, 'text': 'Fala sem amarra nenhuma aqui no meio.'},
            {'speaker': 0, 'text': 'A receita foi de US$ 1,786 bilhão.', 'fato': 1},
            {'speaker': 1, 'text': 'Será que o mercado mantém o ritmo?', 'fato': 0},
        ]}
        with self.assertRaisesRegex(ValueError, 'deve indicar "fato": N ou "reacao": true'):
            news_auto.validate_lines(bad_no_tag, story, 80)

        # 2. Reação com mais de 6 palavras
        bad_long_reaction = {'lines': [
            {'speaker': 0, 'text': 'Sabia do novo recorde de café?', 'fato': 0},
            {'speaker': 1, 'text': 'Esta reação tem palavras demais para ser aceita pelo sistema', 'reacao': True},
            {'speaker': 0, 'text': 'A receita foi de US$ 1,786 bilhão.', 'fato': 1},
            {'speaker': 1, 'text': 'Será que o mercado mantém o ritmo?', 'fato': 0},
        ]}
        with self.assertRaisesRegex(ValueError, 'máximo permitido é 6'):
            news_auto.validate_lines(bad_long_reaction, story, 80)

        # 3. Reação com dígitos
        bad_digits_reaction = {'lines': [
            {'speaker': 0, 'text': 'Sabia do novo recorde de café?', 'fato': 0},
            {'speaker': 1, 'text': 'Caramba, 5 sacas!', 'reacao': True},
            {'speaker': 0, 'text': 'A receita foi de US$ 1,786 bilhão.', 'fato': 1},
            {'speaker': 1, 'text': 'Será que o mercado mantém o ritmo?', 'fato': 0},
        ]}
        with self.assertRaisesRegex(ValueError, 'não podem conter números'):
            news_auto.validate_lines(bad_digits_reaction, story, 80)

        # 4. Reações consecutivas
        bad_consecutive_reactions = {'lines': [
            {'speaker': 0, 'text': 'Sabia do novo recorde de café?', 'fato': 0},
            {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
            {'speaker': 0, 'text': 'Mano do céu!', 'reacao': True},
            {'speaker': 1, 'text': 'Será que o mercado mantém o ritmo?', 'fato': 0},
        ]}
        with self.assertRaisesRegex(ValueError, 'não podem ser consecutivas'):
            news_auto.validate_lines(bad_consecutive_reactions, story, 80)

        # 5. Mais de 2 reações
        bad_3_reactions = {'lines': [
            {'speaker': 0, 'text': 'Sabia do novo recorde de café?', 'fato': 0},
            {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
            {'speaker': 0, 'text': 'A receita foi de US$ 1,786 bilhão.', 'fato': 1},
            {'speaker': 1, 'text': 'Pois é!', 'reacao': True},
            {'speaker': 0, 'text': 'Europa comprou 17,8 milhões.', 'fato': 2},
            {'speaker': 1, 'text': 'Inacreditável!', 'reacao': True},
            {'speaker': 0, 'text': 'Será que o mercado mantém o ritmo?', 'fato': 0},
        ]}
        with self.assertRaisesRegex(ValueError, 'máximo 2 por roteiro'):
            news_auto.validate_lines(bad_3_reactions, story, 80)

        # 6. Última fala sendo reação
        bad_last_reaction = {'lines': [
            {'speaker': 0, 'text': 'Sabia do novo recorde de café?', 'fato': 0},
            {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
            {'speaker': 0, 'text': 'A receita foi de US$ 1,786 bilhão.', 'fato': 1},
            {'speaker': 1, 'text': 'Pois é!', 'reacao': True},
        ]}
        with self.assertRaisesRegex(ValueError, 'última fala NÃO pode ser reação'):
            news_auto.validate_lines(bad_last_reaction, story, 80)

        # 7. Fecho de enchimento (FILLER_CLOSE)
        bad_filler = {'lines': [
            {'speaker': 0, 'text': 'Sabia do novo recorde de café?', 'fato': 0},
            {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
            {'speaker': 0, 'text': 'A receita foi de US$ 1,786 bilhão.', 'fato': 1},
            {'speaker': 1, 'text': 'Que legal ver o Brasil fazendo números assim!', 'fato': 0},
        ]}
        with self.assertRaisesRegex(ValueError, 'fecho de enchimento'):
            news_auto.validate_lines(bad_filler, story, 80)

        # 8. Outro fecho com FILLER_CLOSE
        bad_filler2 = {'lines': [
            {'speaker': 0, 'text': 'Sabia do novo recorde de café?', 'fato': 0},
            {'speaker': 1, 'text': 'A receita foi de US$ 1,786 bilhão.', 'fato': 1},
            {'speaker': 0, 'text': 'Os embarques foram gigantes.', 'fato': 0},
            {'speaker': 1, 'text': 'Isso é show demais!', 'fato': 0},
        ]}
        with self.assertRaisesRegex(ValueError, 'fecho de enchimento'):
            news_auto.validate_lines(bad_filler2, story, 80)

        # 9. Número ausente no fato citado (fato 0 não tem 1,786)
        bad_num_mismatch = {'lines': [
            {'speaker': 0, 'text': 'A receita atingiu US$ 1,786 bilhão.', 'fato': 0},  # Fato 0 é 5,3 milhões
            {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
            {'speaker': 0, 'text': 'Foram 5,3 milhões de sacas.', 'fato': 0},
            {'speaker': 1, 'text': 'Será que o mercado mantém o ritmo?', 'fato': 0},
        ]}
        with self.assertRaisesRegex(ValueError, 'números ausentes no fato 0'):
            news_auto.validate_lines(bad_num_mismatch, story, 80)

        # 10. Entidade ausente no fato citado (Europa só está no fato 2)
        bad_ent_mismatch = {'lines': [
            {'speaker': 0, 'text': 'Sabia do novo recorde de café?', 'fato': 0},
            {'speaker': 1, 'text': 'A Europa comprou muito!', 'fato': 1},  # Fato 1 não cita Europa
            {'speaker': 0, 'text': 'A receita foi de US$ 1,786 bilhão.', 'fato': 1},
            {'speaker': 1, 'text': 'Será que o mercado mantém o ritmo?', 'fato': 0},
        ]}
        with self.assertRaisesRegex(ValueError, 'ausente no fato 1'):
            news_auto.validate_lines(bad_ent_mismatch, story, 80)

        # 11. Roteiro válido
        valid = {'lines': [
            {'speaker': 0, 'text': 'Viu o recorde de 5,3 milhões de sacas?', 'fato': 0},
            {'speaker': 1, 'text': 'Caramba, isso tudo?!', 'reacao': True},
            {'speaker': 0, 'text': 'A receita foi de US$ 1,786 bilhão.', 'fato': 1},
            {'speaker': 1, 'text': 'E a Europa comprou 17,8 milhões de sacas!', 'fato': 2},
            {'speaker': 0, 'text': 'Mesmo assim o preço caiu 9,5% no ano.', 'fato': 3},
            {'speaker': 1, 'text': 'Será que esse ritmo se mantém?', 'fato': 0},
        ]}
        lines, prob = news_auto.validate_lines(valid, story, 80)
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
            news_auto.validate_lines(bad_quase_um, story, 80)

        # Caso 2 obrigatório: "mais de dezesseis milhões" com fonte 16,591 milhões -> ACEITA (floor(16.591) == 16)
        valid_mais_de = {'lines': [
            {'speaker': 0, 'text': 'Viu o novo recorde no Brasil?', 'fato': 3},
            {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
            {'speaker': 0, 'text': 'Foram mais de dezesseis milhões de sacas.', 'fato': 0},
            {'speaker': 1, 'text': 'A receita foi de US$ 1,786 bilhão!', 'fato': 1},
            {'speaker': 0, 'text': 'Bateu a marca antiga de 1,657 bilhão.', 'fato': 2},
            {'speaker': 1, 'text': 'Será que o preço do café continua caindo?', 'fato': 3},
        ]}
        lines, prob = news_auto.validate_lines(valid_mais_de, story, 100)
        self.assertEqual(prob, '')
        self.assertEqual(len(lines), 6)

        # Caso 3 obrigatório: "US$ 1,786 bilhão" com fonte 1,786 bilhão -> ACEITA
        valid_exact = {'lines': [
            {'speaker': 0, 'text': 'Viu o novo recorde no Brasil?', 'fato': 3},
            {'speaker': 1, 'text': 'Caramba!', 'reacao': True},
            {'speaker': 0, 'text': 'A receita foi de US$ 1,786 bilhão.', 'fato': 1},
            {'speaker': 1, 'text': 'Foram 16,591 milhões de sacas embarcadas.', 'fato': 0},
            {'speaker': 0, 'text': 'Bateu a marca antiga de 1,657 bilhão.', 'fato': 2},
            {'speaker': 1, 'text': 'Será que o preço do café continua caindo?', 'fato': 3},
        ]}
        lines, prob = news_auto.validate_lines(valid_exact, story, 100)
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
            news_auto.validate_lines(bad_temporal_script, story, 80)

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
            ]}

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
            {'speaker': 0, 'text': 'Viu o novo recorde no Brasil?', 'fato': 4},
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
        valid_hook = 'Por que vendemos mais café e faturamos menos?'
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
                {'speaker': 1, 'text': 'Será que as cotações sobem de novo?', 'fato': 0},
            ]}

        mock_judge = lambda i, p, model=None: {'nota': 9, 'responde_perguntas': True, 'contradiz_materia': False, 'problemas': []}
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
            news_auto.validate_lines(raw_fert, story_fert, budget=150, facts=facts_fert)

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
            news_auto.validate_lines(delayed_lines, story_fert, budget=120, facts=facts_fert)

        # 4. Quando cobre as palavras-chave nos primeiros turnos e no total, é aceito
        valid_lines = {'lines': [
            {'speaker': 0, 'text': 'O Brasil vai investir pesado agora?', 'fato': 0},
            {'speaker': 1, 'text': 'Sim, R$ 460 bilhões para fertilizantes!', 'fato': 0},
            {'speaker': 0, 'text': 'Isso vai levar cerca de 20 anos.', 'fato': 0},
            {'speaker': 1, 'text': 'Será que melhora a segurança alimentar?', 'fato': 5},
        ]}
        res, prob = news_auto.validate_lines(valid_lines, story_fert, budget=80, facts=facts_fert)
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
            'A Caterpillar vende geradores para a empresa.',
            'A rede elétrica do Texas enfrenta limitações.',
            'A energia atenderá data centers até 2045.',
        ]
        res_anth, prob_anth = news_auto.validate_lines(anth_data, story_anth, budget=120, facts=facts_anth)
        self.assertEqual(prob_anth, '')
        self.assertEqual(len(res_anth), 6)


if __name__ == '__main__':
    unittest.main(verbosity=2)
