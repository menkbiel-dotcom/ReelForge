"""Synthesize from saved VoiceStudio references over its local API."""
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

import requests

from captions import whisper_words
from media_tools import run

BASE = 'http://127.0.0.1:3900'


def reference(profile_id):
    try:
        with requests.Session() as session:
            session.trust_env = False
            listing = session.get(BASE + '/v1/audio/voices', timeout=10, allow_redirects=False)
            listing.raise_for_status()
            profiles = [voice for voice in listing.json()['voices'] if voice.get('type') == 'profile']
            matches = [voice for voice in profiles if profile_id.casefold() in
                       (str(voice.get('name', '')).casefold(), str(voice.get('voice_id', '')).casefold())]
            if len(matches) != 1:
                raise ValueError(f'Selecione um nome ou ID único para o perfil {profile_id} no VoiceStudio.')
            profile_id = matches[0]['voice_id']
            response = session.get(BASE + '/profiles/' + quote(profile_id, safe='') + '/audio',
                                   timeout=15, allow_redirects=False)
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError('Resposta inesperada do VoiceStudio.')
            return response.content
    except requests.RequestException as exc:
        raise RuntimeError(f'Abra o VoiceStudio e confira o ID do perfil {profile_id}.') from exc


def synthesize(text, profile, dest, ref, cache):
    with TemporaryDirectory(dir=cache) as directory:
        source = Path(directory) / 'reference.wav'
        source.write_bytes(ref)
        short = Path(directory) / 'short.wav'
        run(['ffmpeg', '-y', '-v', 'error', '-i', source, '-t', '10',
             '-ar', '24000', '-ac', '1', short])
        words = whisper_words(short, '', 'base', cache.parent / 'alignment_cache')
        transcript = ' '.join(word['text'] for word in words)
        if len(transcript.split()) < 3:
            raise ValueError('A referência do VoiceStudio precisa de fala reconhecível.')
        with requests.Session() as session, short.open('rb') as audio:
            session.trust_env = False
            response = session.post(BASE + '/generate', data={
                'text': text, 'language': 'Portuguese', 'ref_text': transcript,
                'num_step': '16', 'hosted': 'false',
            }, files={'ref_audio': ('reference.wav', audio, 'audio/wav')},
                timeout=(5, 600), allow_redirects=False)
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError('Resposta inesperada do VoiceStudio.')
        raw = Path(directory) / 'speech.wav'
        raw.write_bytes(response.content)
        speed = 1 + profile['rate'] / 100
        run(['ffmpeg', '-y', '-v', 'error', '-i', raw, '-af', f'atempo={speed}',
             '-c:a', 'libmp3lame', '-q:a', '2', dest])
