"""Testes offline do fluxo automático RSS -> Ollama (rede e modelo simulados)."""
import importlib.util
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
        facts = ['A Sony relançou o Walkman no Brasil.', 'O aparelho custa R$ 32 mil.',
                 'O foco é áudio de alta definição.', 'O modelo chega este mês.', 'É voltado a profissionais.']
        bad = {'lines': [{'speaker': 0, 'text': 'Pagaria 50 mil reais num Walkman?'}] + [{'speaker': 0, 'text': 'Fala.'}] * 5}
        good = {'lines': [{'speaker': 1, 'text': 'Naruto: Pagaria 32 mil reais num Walkman hoje?'},
                          {'speaker': 1, 'text': 'Cara, R$ 32 mil é preço de carro!'},
                          {'speaker': 0, 'text': 'A Sony lançou esse modelo focado em estúdio.'},
                          {'speaker': 0, 'text': 'Mesmo assim, quem no Brasil vai encarar esse valor?'},
                          {'speaker': 1, 'text': 'Só colecionador muito fanático mesmo!'},
                          {'speaker': 0, 'text': 'Você compraria se tivesse essa grana sobrando?'}]}
        answers = iter([bad, good, {'nota': 9, 'problemas': []}])
        lines = news_auto.write_script(story, max_words=80, ask=lambda i, p: next(answers), loop=False, facts=facts)
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
        vague = {'lines': [{'speaker': 0, 'text': t} for t in
                           ('Sabia que uma empresa fez um acordo?', 'Isso é importante.',
                            'A tecnologia avança.', 'O que você acha disso?')]}
        with self.assertRaisesRegex(ValueError, 'genérico'):
            news_auto.validate_lines(vague, story, 60)
        rich = {'lines': [{'speaker': 0, 'text': t} for t in
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
        odd_complete = {'lines': [{'speaker': 0, 'text': t} for t in (
            'Pagaria R$ 32 mil num Walkman novo?',
            'A Sony trouxe o aparelho de volta ao Brasil.',
            'Ele é feito para audiófilos exigentes.',
            'Mesmo assim o preço assustou todo mundo.',
            'Vale tudo isso só para ouvir música?'
        )]}
        lines, problem = news_auto.validate_lines(odd_complete, story, 80, loop=False, min_lines=4, max_lines=8)
        self.assertEqual(problem, '')
        self.assertEqual(len(lines), 5)
        dangling = {'lines': [{'speaker': 0, 'text': t} for t in (
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
        bad_cpg = {'lines': [{'speaker': 0, 'text': t} for t in (
            'Viu a descoberta de 200 milhões de barris?',
            'A Petrobras achou petróleo na Bacia de Campos.',
            'Dados confirmados pela CPG Click Petróleo e Gás.',
            'Será que a gasolina baixa agora?'
        )]}
        with self.assertRaisesRegex(ValueError, 'veículo|confirmado por'):
            news_auto.validate_lines(bad_cpg, story, 80)
        bad_uol = {'lines': [{'speaker': 0, 'text': t} for t in (
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
        leaked_entity = {'lines': [{'speaker': 0, 'text': t} for t in (
            'Já conferiu as 180 questões do Enem?',
            'O MEC e o Inep liberaram o gabarito no Brasil.',
            'Daniela Toffoli até comentou o tema.',
            'Quantos acertos você conseguiu fazer?'
        )]}
        with self.assertRaisesRegex(ValueError, 'Contaminação|manchete relacionada'):
            news_auto.validate_lines(leaked_entity, story, 80)
        leaked_number = {'lines': [{'speaker': 0, 'text': t} for t in (
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
        script_v1 = {'lines': [{'speaker': 0, 'text': t} for t in (
            'Por que gastar 3,4 bilhões em geradores?',
            'A Anthropic fechou isso com a Caterpillar.',
            'Fica tudo lá no Texas.',
            'A energia vai até 2045 nos data centers.',
            'Isso muda o jogo da IA.',
            'O que você acha desse gasto?'
        )]}
        eval_low = {'nota': 5, 'problemas': ['Falas finais pouco espontâneas']}
        script_v2 = {'lines': [{'speaker': 0, 'text': t} for t in (
            'Por que gastar 3,4 bilhões só em gerador?',
            'Cara, a Anthropic comprou tudo da Caterpillar!',
            'É que a rede elétrica do Texas não aguenta mais.',
            'Eles garantiram energia até 2045 pros data centers.',
            'Mano, é muita energia só pra rodar IA!',
            'Será que vai faltar luz pro resto do estado?'
        )]}
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
