"""Synthesize from saved VoiceStudio references over its local API (with reference caching and auto-start)."""
import hashlib
import os
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

import requests

from captions import whisper_words
from media_tools import run

BASE = 'http://127.0.0.1:3900'
_ref_cache = {}
_prepared_cache = {}
_CACHE_TTL = 120.0
_autostart_attempted = False


def _ensure_voicestudio_running(session):
    """Se o servidor local do VoiceStudio (127.0.0.1:3900) estiver fechado, inicia-o automaticamente."""
    global _autostart_attempted
    if _autostart_attempted or os.name != 'nt':
        return False
    _autostart_attempted = True
    py = Path(os.getenv('APPDATA', '')) / 'VoiceStudio' / 'runtime' / 'project' / '.venv' / 'Scripts' / 'python.exe'
    main_py = Path(os.getenv('LOCALAPPDATA', '')) / 'Programs' / 'VoiceStudio' / 'resources' / 'backend' / 'main.py'
    exe = Path(os.getenv('LOCALAPPDATA', '')) / 'Programs' / 'VoiceStudio' / 'VoiceStudio.exe'
    print('Iniciando o servidor local do VoiceStudio automaticamente (127.0.0.1:3900)...')
    try:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'DETACHED_PROCESS', 0)
        if py.is_file() and main_py.is_file():
            subprocess.Popen([str(py), str(main_py)], cwd=str(main_py.parent),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags)
        elif exe.is_file():
            subprocess.Popen([str(exe)], cwd=str(exe.parent))
        else:
            return False
    except OSError:
        return False
    for _ in range(45):
        time.sleep(2.0)
        try:
            r = session.get(BASE + '/v1/audio/voices', timeout=3, allow_redirects=False)
            if r.status_code == 200:
                print('VoiceStudio local pronto!')
                return True
        except requests.RequestException:
            pass
    return False


def reference(profile_id):
    key = profile_id.strip().casefold()
    cached = _ref_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    try:
        with requests.Session() as session:
            session.trust_env = False
            try:
                listing = session.get(BASE + '/v1/audio/voices', timeout=10, allow_redirects=False)
                if listing.status_code == 503:
                    raise requests.ConnectionError('VoiceStudio ainda inicializando')
            except requests.RequestException:
                if not _ensure_voicestudio_running(session):
                    raise
                listing = session.get(BASE + '/v1/audio/voices', timeout=10, allow_redirects=False)
            listing.raise_for_status()
            profiles = [voice for voice in listing.json()['voices'] if voice.get('type') == 'profile']
            matches = [voice for voice in profiles if key in
                       (str(voice.get('name', '')).casefold(), str(voice.get('voice_id', '')).casefold())]
            if len(matches) != 1:
                raise ValueError(f'Selecione um nome ou ID único para o perfil {profile_id} no VoiceStudio.')
            voice_id = matches[0]['voice_id']
            response = session.get(BASE + '/profiles/' + quote(voice_id, safe='') + '/audio',
                                   timeout=15, allow_redirects=False)
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError('Resposta inesperada do VoiceStudio.')
            data = response.content
            _ref_cache[key] = (now, data)
            return data
    except requests.RequestException as exc:
        raise RuntimeError(f'Abra o VoiceStudio e confira o ID do perfil {profile_id}.') from exc


def _prepared_reference(ref, cache):
    """Prepara e transcreve o áudio de referência uma única vez por voz (em disco e memória)."""
    ref_hash = hashlib.sha256(ref).hexdigest()
    if ref_hash in _prepared_cache:
        short_path, transcript = _prepared_cache[ref_hash]
        if short_path.is_file():
            return short_path, transcript
    ref_dir = Path(cache) / 'ref_cache'
    ref_dir.mkdir(parents=True, exist_ok=True)
    short_path = ref_dir / f'{ref_hash[:24]}.wav'
    txt_path = ref_dir / f'{ref_hash[:24]}.txt'
    if not short_path.is_file() or not txt_path.is_file():
        source = ref_dir / f'{ref_hash[:24]}_raw.wav'
        source.write_bytes(ref)
        try:
            run(['ffmpeg', '-y', '-v', 'error', '-i', source, '-t', '10',
                 '-ar', '24000', '-ac', '1', short_path])
        finally:
            source.unlink(missing_ok=True)
        words = whisper_words(short_path, '', 'base', Path(cache).parent / 'alignment_cache')
        transcript = ' '.join(word['text'] for word in words).strip()
        if len(transcript.split()) < 3:
            short_path.unlink(missing_ok=True)
            raise ValueError('A referência do VoiceStudio precisa de fala reconhecível.')
        txt_path.write_text(transcript, encoding='utf-8')
    else:
        transcript = txt_path.read_text(encoding='utf-8').strip()
    _prepared_cache[ref_hash] = (short_path, transcript)
    return short_path, transcript


def synthesize(text, profile, dest, ref, cache):
    short, transcript = _prepared_reference(ref, cache)
    with TemporaryDirectory(dir=cache) as directory:
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
