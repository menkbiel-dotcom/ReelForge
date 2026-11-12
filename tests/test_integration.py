import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import captions
import speech
import script_io
import engine
from tests._isolation import IsolatedOutputMixin

ROOT = Path(__file__).resolve().parents[1]
HAS_TKINTER = importlib.util.find_spec('tkinter') is not None
HAS_DEMO_GAMEPLAY = (ROOT / 'assets/gameplay/parkour_teste.mp4').is_file()

def _make_silent_wav(duration=1.0, sample_rate=24000, channels=1, sampwidth=2):
    import io, wave
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        wf.writeframes(b'\x00' * int(duration * sample_rate * channels * sampwidth))
    return buf.getvalue()

DEMO_AUDIO_FILE = ROOT / 'assets/demo_ninja/voice_0.wav'
if not DEMO_AUDIO_FILE.is_file():
    try:
        import wave
        DEMO_AUDIO_FILE.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(DEMO_AUDIO_FILE), 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(b'\x00' * int(1.0 * 24000 * 2))
    except Exception:
        pass

HAS_DEMO_AUDIO = DEMO_AUDIO_FILE.is_file()
AUDIO = DEMO_AUDIO_FILE.read_bytes() if DEMO_AUDIO_FILE.is_file() else _make_silent_wav(1.0)


def profile(provider='elevenlabs'):
    return {'provider':provider,'voice':'voice1','model':'eleven_multilingual_v2' if provider=='elevenlabs' else 's2.1-pro','rate':0,'pitch':0}


def response(status=200):
    r=Mock(status_code=status)
    r.iter_content.return_value=[AUDIO]
    return r


class SpeechTests(IsolatedOutputMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'ELEVENLABS_API_KEY':'test-secret','FISH_API_KEY':'fish-secret'})
        self.env.start()
    def tearDown(self): self.env.stop();self.temp.cleanup();super().tearDown()
    def test_cloud_cache_reuses_audio_without_second_request(self):
        with patch('requests.post',return_value=response()) as request:
            a=asyncio.run(speech.synthesize_one('Olá!',profile(),self.root))
            b=asyncio.run(speech.synthesize_one('Olá!',profile(),self.root))
        self.assertEqual(request.call_count,1);self.assertTrue(b['cached'])
        self.assertEqual(a['audio'],b['audio'])
        self.assertNotIn('test-secret',next(self.root.glob('*.json')).read_text())
    def test_changed_voice_invalidates_cache(self):
        p=profile()
        with patch('requests.post',side_effect=[response(),response()]) as request:
            asyncio.run(speech.synthesize_one('Olá!',p,self.root))
            p['voice']='other';asyncio.run(speech.synthesize_one('Olá!',p,self.root))
        self.assertEqual(request.call_count,2)
    def test_http_failure_has_no_retry_no_cache_no_key_leak(self):
        with patch('requests.post',return_value=response(401)) as request:
            with self.assertRaises(RuntimeError) as exc:asyncio.run(speech.synthesize_one('Olá!',profile(),self.root))
        self.assertEqual(request.call_count,1);self.assertFalse(list(self.root.iterdir()))
        self.assertNotIn('test-secret',str(exc.exception))
    def test_redirect_is_rejected_without_replay(self):
        with patch('requests.post',return_value=response(307)) as request:
            with self.assertRaises(RuntimeError):asyncio.run(speech.synthesize_one('Oi',profile(),self.root))
        self.assertEqual(request.call_count,1);self.assertFalse(request.call_args.kwargs['allow_redirects'])
    def test_missing_key_never_calls_network(self):
        with patch.dict(os.environ,{'ELEVENLABS_API_KEY':''}),patch('requests.post') as request:
            with self.assertRaises(ValueError):asyncio.run(speech.synthesize_one('Olá!',profile(),self.root))
        request.assert_not_called()
    def test_fish_request_uses_voice_and_model(self):
        p=profile('fish')
        with patch('requests.post',return_value=response()) as request:
            asyncio.run(speech.synthesize_one('Olá!',p,self.root))
        args=request.call_args
        self.assertEqual(args.args[0],'https://api.fish.audio/v1/tts')
        self.assertEqual(args.kwargs['json']['reference_id'],'voice1')
        self.assertEqual(args.kwargs['headers']['model'],'s2.1-pro')
    def test_invalid_audio_not_published(self):
        r=response();r.iter_content.return_value=[b'not audio']
        with patch('requests.post',return_value=r):
            with self.assertRaises(ValueError):asyncio.run(speech.synthesize_one('Olá!',profile(),self.root))
        self.assertFalse(list(self.root.iterdir()))
    @unittest.skipUnless(HAS_DEMO_AUDIO, 'Requer áudio de demonstração local')
    def test_imported_audio_never_synthesized(self):
        lines=[{'text':'A gravação original','speaker':0,'audio':ROOT/'assets/demo_ninja/voice_0.wav','audio_source':'imported'}]
        with patch('speech.synthesize_one') as synth:
            asyncio.run(speech.synthesize_lines(lines,self.root))
        synth.assert_not_called();self.assertGreater(lines[0]['duration'],0)
    def test_provider_pitch_does_not_silently_get_ignored(self):
        p=profile();p['pitch']=5
        with self.assertRaises(ValueError):speech.validate_profile(p)
    def test_voice_profiles_override_director_suggestions(self):
        lines=[{'speaker':0,'text':'Olá!','voice':'wrong-director-voice'}]
        with patch.dict(os.environ,{'TTS_PROVIDER':'elevenlabs','TTS_VOICE_A':'selected-voice','TTS_MODEL':'eleven_multilingual_v2'}),patch('speech.synthesize_one',return_value={'audio':'test','duration':1}) as synth:
            asyncio.run(speech.synthesize_lines(lines,self.root))
        self.assertEqual(synth.call_args.args[1]['voice'],'selected-voice')


class CaptionTests(IsolatedOutputMixin, unittest.TestCase):
    def test_normalization_preserves_script_punctuation(self):
        words=captions.match_words('Olá, você!', [{'text':'Olá','start':.1,'end':.4},{'text':'voce','start':.5,'end':.9}],1)
        self.assertEqual(words[0]['text'],'Olá,');self.assertFalse(any(w['estimated'] for w in words))
    def test_missing_word_explicitly_marked(self):
        words=captions.match_words('um dois três', [{'text':'um','start':.1,'end':.2},{'text':'três','start':.7,'end':.9}],1)
        self.assertTrue(words[1]['estimated']);self.assertAlmostEqual(words[1]['start'],.2)
    def test_mismatched_recording_fails(self):
        with self.assertRaises(ValueError):captions.match_words('água comida lanterna', [{'text':'nada','start':0,'end':.5}],1)
    def test_nonfinite_word_time_rejected(self):
        with self.assertRaises(ValueError):captions.match_words('água', [{'text':'água','start':float('nan'),'end':.5}],1)
    def test_ass_has_transparent_style_and_measured_highlight(self):
        line={'start':.25,'end':1.25,'words':[{'text':'OLÁ','start':.1,'end':.3},{'text':'MUNDO','start':.4,'end':.8}]}
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.ass';captions.write_ass([line],p,simulated=True,voice_notice=True);text=p.read_text(encoding='utf-8')
        self.assertIn('0:00:00.35',text);self.assertIn(r'\c&H002DDCFF&',text)
        self.assertNotIn(r'\p1',text);self.assertIn('PARKOUR SIMULADO',text)
        self.assertIn('VOZES PROVISÓRIAS',text)
    def test_ass_text_cannot_inject_override_tags(self):
        self.assertNotIn('{',captions.ass_escape(r'{\pos(0,0)}oi'))
        self.assertNotIn('\\',captions.ass_escape(r'{\pos(0,0)}oi'))
    def test_estimated_only_when_explicitly_selected(self):
        line={'text':'Um teste','duration':1,'audio':'missing'}
        with patch('captions.whisper_words',side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):captions.align_lines([copy.deepcopy(line)],'.',mode='auto')
        captions.align_lines([line],'.',mode='estimated');self.assertEqual(line['timing_source'],'estimated')


class AppTests(IsolatedOutputMixin, unittest.TestCase):
    @unittest.skipUnless(HAS_DEMO_GAMEPLAY and HAS_DEMO_AUDIO, 'Requer gameplay e áudios de demonstração locais')
    def test_import_resolves_recordings_relative_to_manifest(self):
        _,lines,bg=script_io.load(ROOT/'assets/demo/dialogue.json')
        self.assertEqual(len(lines),6);self.assertTrue(Path(lines[0]['audio']).is_absolute());self.assertTrue(bg.is_file())
    def test_import_rejects_boolean_speaker(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'script.json';p.write_text(json.dumps({'lines':[{'speaker':True,'text':'Oi'}]}))
            with self.assertRaises(ValueError):script_io.load(p)
    @unittest.skipUnless(HAS_DEMO_GAMEPLAY and HAS_DEMO_AUDIO, 'Requer gameplay e áudios de demonstração locais')
    def test_imported_audio_is_not_truncated_to_duration_limit(self):
        _,lines,_=script_io.load(ROOT/'assets/demo/dialogue.json')
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(RuntimeError,'gravações importadas'):
                asyncio.run(engine.fit_narration(lines,Path(d),10))
    @unittest.skipUnless(HAS_TKINTER, 'Requer tkinter instalado')
    def test_gui_configuration_does_not_save_credentials(self):
        import gui
        with tempfile.TemporaryDirectory() as d,patch.object(gui,'CONFIG',Path(d)/'settings.json'):
            app=gui.App();app.withdraw()
            try:
                app.tts_provider.set('elevenlabs');app._switch_voice_provider()
                app.tts_key.set('private-voice-key');app.openai_key.set('private-llm-key')
                app._save();saved=(Path(d)/'settings.json').read_text()
                self.assertNotIn('private-voice-key',saved);self.assertNotIn('private-llm-key',saved)
                self.assertEqual(app.voice_a.get(),'')
            finally:app.destroy()
    @unittest.skipUnless(HAS_TKINTER and HAS_DEMO_GAMEPLAY and HAS_DEMO_AUDIO, 'Requer tkinter e assets de demonstração')
    def test_imported_script_survives_changed_render_settings(self):
        import gui
        app=gui.App();app.withdraw()
        try:
            app.imported_script=ROOT/'assets/demo/dialogue.json'
            app.job_config={'signature':'changed-caption-mode'}
            with patch('engine.make_script') as generate:
                _,lines=app._get_news()
            generate.assert_not_called()
            self.assertEqual(len(lines),6)
            self.assertEqual(lines[0]['audio_source'],'imported')
            self.assertEqual(app.preview_signature,'changed-caption-mode')
        finally:app.destroy()


class Item6Tests(IsolatedOutputMixin, unittest.TestCase):
    def test_captions_interpolated_words_expand_minimum_duration(self):
        text = "Isso com o preço caiu no Brasil"
        recognized = [
            {'text': 'o', 'start': 0.0005, 'end': 0.3},
            {'text': 'preço', 'start': 0.3, 'end': 0.75},
            {'text': 'caiu', 'start': 0.75, 'end': 1.15},
            {'text': 'no', 'start': 1.15, 'end': 1.35},
            {'text': 'Brasil', 'start': 1.35, 'end': 1.8},
        ]
        duration = 1.8
        res = captions.match_words(text, recognized, duration)
        self.assertEqual([w['text'] for w in res], text.split())
        isso = res[0]
        com = res[1]
        self.assertTrue(isso.get('estimated'))
        self.assertTrue(com.get('estimated'))
        self.assertGreaterEqual(isso['end'] - isso['start'], 0.08 - 1e-6)
        self.assertGreaterEqual(com['end'] - com['start'], 0.08 - 1e-6)
        self.assertLessEqual(res[-1]['end'], duration + 1e-6)
        for i in range(len(res) - 1):
            self.assertLessEqual(res[i]['end'], res[i + 1]['start'] + 1e-6)

    def test_whisper_words_missing_model_raises_runtime_error_without_hf_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            dummy_wav = Path(td) / 'test.wav'
            dummy_wav.write_bytes(AUDIO)
            with patch('captions.WhisperModel', side_effect=Exception('HuggingFace offline error'), create=True):
                with patch('captions._models', {}):
                    with self.assertRaises(RuntimeError) as ctx:
                        captions.whisper_words(dummy_wav, 'teste', 'small', Path(td) / 'cache')
                    err = str(ctx.exception)
                    self.assertIn("não está baixado localmente", err)
                    self.assertIn("instalar_legendas.bat", err)
                    self.assertIn(".venv\\Scripts\\python.exe", err)
                    self.assertIsNone(ctx.exception.__cause__)

    def test_engine_character_thumbnail_size(self):
        from PIL import Image
        captured_sizes = []
        orig_thumbnail = Image.Image.thumbnail

        def fake_thumbnail(self, size, resample=3):
            captured_sizes.append(size)
            return orig_thumbnail(self, size, resample)

        with tempfile.TemporaryDirectory() as td:
            out_png = Path(td) / 'char.png'
            with patch.object(engine, 'OUTPUT', Path(td)):
                with patch.object(Image.Image, 'thumbnail', fake_thumbnail):
                    engine.character(out_png, 0, mouth_open=False)
            self.assertIn((510, 640), captured_sizes)


class Item8Tests(IsolatedOutputMixin, unittest.TestCase):
    def test_merge_settings_preserves_unknown_keys_and_other_providers(self):
        import gui
        current = {
            'topic': 'economia',
            'ollama_model': 'qwen3.5:4b',
            'judge_model': 'qwen3:8b',
            'permitir_sem_checagem_de_fatos': True,
            'aceitar_nota_baixa': True,
            'custom_unrecognized_key': 42,
            'voice_profiles': {
                'voicestudio': {
                    'a': 'old_a',
                    'b': 'old_b',
                    'extra_param': 'secret',
                },
                'custom_cloud_provider': {
                    'endpoint': 'https://custom.tts',
                }
            }
        }
        from_ui = {
            'topic': 'tecnologia',
            'ollama_model': 'qwen3.5:4b',
            'voice_profiles': {
                'voicestudio': {
                    'a': 'new_a',
                    'b': 'new_b',
                    'model': 'm1',
                    'rate_a': '18',
                    'rate_b': '18',
                    'pitch_a': '0',
                    'pitch_b': '0',
                }
            }
        }
        merged = gui.merge_settings(current, from_ui)
        self.assertEqual(merged['topic'], 'tecnologia')
        self.assertEqual(merged['judge_model'], 'qwen3:8b')
        self.assertTrue(merged['permitir_sem_checagem_de_fatos'])
        self.assertTrue(merged['aceitar_nota_baixa'])
        self.assertEqual(merged['custom_unrecognized_key'], 42)
        self.assertIn('custom_cloud_provider', merged['voice_profiles'])
        self.assertEqual(merged['voice_profiles']['custom_cloud_provider']['endpoint'], 'https://custom.tts')
        vs = merged['voice_profiles']['voicestudio']
        self.assertEqual(vs['a'], 'new_a')
        self.assertEqual(vs['b'], 'new_b')
        self.assertEqual(vs['extra_param'], 'secret')

    @unittest.skipUnless(HAS_TKINTER, 'Tkinter necessário para instanciar GUI')
    def test_gui_save_preserves_unknown_settings_on_disk(self):
        import gui
        with tempfile.TemporaryDirectory() as td:
            cfg_file = Path(td) / 'settings.json'
            initial_data = {
                'topic': 'esporte',
                'ollama_model': 'qwen3.5:4b',
                'judge_model': 'qwen3:8b',
                'permitir_sem_checagem_de_fatos': False,
                'aceitar_nota_baixa': True,
                'custom_setting_123': 'dont_delete_me',
                'voice_profiles': {
                    'other_provider': {'voice': 'narrator'},
                    'voicestudio': {'a': 'old_v', 'hidden_meta': 999}
                }
            }
            cfg_file.write_text(json.dumps(initial_data, indent=2), encoding='utf-8')
            with patch.object(gui, 'CONFIG', cfg_file):
                app = gui.App()
                try:
                    app.topic.set('cultura')
                    saved_cfg = app._save()
                    self.assertEqual(saved_cfg['topic'], 'cultura')
                    self.assertEqual(saved_cfg['judge_model'], 'qwen3:8b')
                    self.assertFalse(saved_cfg['permitir_sem_checagem_de_fatos'])
                    self.assertTrue(saved_cfg['aceitar_nota_baixa'])
                    self.assertEqual(saved_cfg['custom_setting_123'], 'dont_delete_me')
                    self.assertIn('other_provider', saved_cfg['voice_profiles'])
                    self.assertEqual(saved_cfg['voice_profiles']['voicestudio']['hidden_meta'], 999)

                    disk_cfg = json.loads(cfg_file.read_text(encoding='utf-8'))
                    self.assertEqual(disk_cfg['topic'], 'cultura')
                    self.assertEqual(disk_cfg['custom_setting_123'], 'dont_delete_me')
                    self.assertEqual(disk_cfg['voice_profiles']['voicestudio']['hidden_meta'], 999)
                finally:
                    app.destroy()


class Item9Tests(IsolatedOutputMixin, unittest.TestCase):
    def test_list_ollama_models_fallback_when_offline(self):
        import gui
        with patch('requests.get', side_effect=Exception('Ollama offline')):
            models = gui.list_ollama_models(timeout=1.5)
            self.assertEqual(models, ['qwen3.5:4b', 'qwen3:8b', 'qwen2.5:7b'])

    def test_list_ollama_models_success_from_api(self):
        import gui
        mock_resp = Mock(status_code=200)
        mock_resp.json.return_value = {
            'models': [
                {'name': 'llama3.1:8b', 'size': 12345},
                {'name': 'qwen2.5:7b', 'size': 67890},
            ]
        }
        with patch('requests.get', return_value=mock_resp) as mock_get:
            models = gui.list_ollama_models(timeout=1.5)
            self.assertEqual(models, ['llama3.1:8b', 'qwen2.5:7b'])
            mock_get.assert_called_once_with('http://127.0.0.1:11434/api/tags', timeout=1.5)

    @unittest.skipUnless(HAS_TKINTER, 'Tkinter necessário para instanciar GUI')
    def test_gui_model_fields_and_accept_low_score_persistence(self):
        import gui
        with patch.dict(os.environ, {}, clear=False):
            with tempfile.TemporaryDirectory() as td:
                cfg_file = Path(td) / 'settings.json'
                initial_data = {
                    'topic': 'tecnologia',
                    'ollama_model': 'qwen3.5:4b',
                    'judge_model': 'qwen3:8b',
                    'aceitar_nota_baixa': False,
                }
                cfg_file.write_text(json.dumps(initial_data, indent=2), encoding='utf-8')
                with patch.object(gui, 'CONFIG', cfg_file):
                    with patch('requests.get', side_effect=Exception('offline')):
                        app = gui.App()
                        try:
                            self.assertEqual(app.ollama_model.get(), 'qwen3.5:4b')
                            self.assertEqual(app.judge_model.get(), 'qwen3:8b')
                            self.assertFalse(app.aceitar_nota_baixa.get())
                            self.assertIn('igual ao redator', app._judge_models)
                            self.assertIn('qwen3.5:4b', app._ollama_models)

                            app.ollama_model.set('qwen2.5:7b')
                            app.judge_model.set('qwen3:8b')
                            app.aceitar_nota_baixa.set(True)
                            saved_cfg = app._save()

                            self.assertEqual(saved_cfg['ollama_model'], 'qwen2.5:7b')
                            self.assertEqual(saved_cfg['judge_model'], 'qwen3:8b')
                            self.assertTrue(saved_cfg['aceitar_nota_baixa'])
                            self.assertEqual(os.environ.get('OLLAMA_MODEL'), 'qwen2.5:7b')
                            self.assertEqual(os.environ.get('OLLAMA_JUDGE_MODEL'), 'qwen3:8b')
                            self.assertEqual(os.environ.get('REELFORGE_ACCEPT_LOW_SCORE'), '1')

                            app.judge_model.set('igual ao redator')
                            app.aceitar_nota_baixa.set(False)
                            saved_cfg2 = app._save()

                            self.assertEqual(saved_cfg2['judge_model'], '')
                            self.assertFalse(saved_cfg2['aceitar_nota_baixa'])
                            self.assertEqual(os.environ.get('OLLAMA_JUDGE_MODEL'), 'qwen2.5:7b')
                            self.assertEqual(os.environ.get('REELFORGE_ACCEPT_LOW_SCORE'), '0')

                            disk_cfg = json.loads(cfg_file.read_text(encoding='utf-8'))
                            self.assertEqual(disk_cfg['ollama_model'], 'qwen2.5:7b')
                            self.assertEqual(disk_cfg['judge_model'], '')
                            self.assertFalse(disk_cfg['aceitar_nota_baixa'])
                        finally:
                            app.destroy()


if __name__=='__main__':unittest.main(verbosity=2)
