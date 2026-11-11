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

ROOT = Path(__file__).resolve().parents[1]
HAS_TKINTER = importlib.util.find_spec('tkinter') is not None
HAS_DEMO_GAMEPLAY = (ROOT / 'assets/gameplay/parkour_teste.mp4').is_file()
HAS_DEMO_AUDIO = (ROOT / 'assets/demo_ninja/voice_0.wav').is_file()
AUDIO = (ROOT / 'assets/demo_ninja/voice_0.wav').read_bytes() if (ROOT / 'assets/demo_ninja/voice_0.wav').is_file() else b'RIFF$\x00\x00\x00WAVEfmt '


def profile(provider='elevenlabs'):
    return {'provider':provider,'voice':'voice1','model':'eleven_multilingual_v2' if provider=='elevenlabs' else 's2.1-pro','rate':0,'pitch':0}


def response(status=200):
    r=Mock(status_code=status)
    r.iter_content.return_value=[AUDIO]
    return r


class SpeechTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'ELEVENLABS_API_KEY':'test-secret','FISH_API_KEY':'fish-secret'})
        self.env.start()
    def tearDown(self): self.env.stop();self.temp.cleanup()
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


class CaptionTests(unittest.TestCase):
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


class AppTests(unittest.TestCase):
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


if __name__=='__main__':unittest.main(verbosity=2)
