"""Provider adapters and reusable voice previews, inspired by MoneyPrinterTurbo.

Cloud requests are never retried automatically or replaced by another provider.
Credentials are read at runtime and are never written into the cache metadata.
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote

from media_tools import validate_audio

PROVIDERS = ('edge', 'elevenlabs', 'fish', 'voicestudio')


def profiles():
    from characters import EDGE_VOICES, FISH_VOICES, NAMES
    import engine
    saved = engine.load_settings()
    provider = os.getenv('TTS_PROVIDER') or saved.get('tts_provider') or 'voicestudio'
    prof = (saved.get('voice_profiles') or {}).get(provider) or {}
    result = []
    for index, (suffix, key) in enumerate((('A', 'a'), ('B', 'b'))):
        default = EDGE_VOICES[index] if provider == 'edge' else (FISH_VOICES[index] if provider == 'fish' else (NAMES[index] if provider == 'voicestudio' else ''))
        default_model = 'eleven_multilingual_v2' if provider == 'elevenlabs' else ('s2.1-pro' if provider == 'fish' else '')
        default_rate = '18' if provider == 'voicestudio' else ('-3' if suffix == 'A' else '7')
        default_pitch = ('-7' if suffix == 'A' else '4') if provider == 'edge' else '0'
        result.append({'provider': provider,
                       'voice': os.getenv('TTS_VOICE_' + suffix) or prof.get(key) or default,
                       'model': os.getenv('TTS_MODEL') or prof.get('model') or default_model,
                       'rate': int(os.getenv('TTS_RATE_' + suffix) or prof.get('rate_' + key, default_rate)),
                       'pitch': int(os.getenv('TTS_PITCH_' + suffix) or prof.get('pitch_' + key, default_pitch))})
    return result


def validate_profile(profile):
    provider = profile['provider']
    if provider not in PROVIDERS:
        raise ValueError('Serviço de voz desconhecido.')
    if not profile.get('voice', '').strip():
        raise ValueError('Preencha o identificador de voz de cada personagem.')
    if not -30 <= profile['rate'] <= 20 or not -30 <= profile['pitch'] <= 30:
        raise ValueError('Ritmo ou tom fora do intervalo permitido.')
    if provider != 'edge' and profile['pitch']:
        raise ValueError('O ajuste de tom é exclusivo do Edge. Use tom 0 neste serviço.')
    if provider == 'elevenlabs' and profile['model'] not in ('eleven_multilingual_v2', 'eleven_v3'):
        raise ValueError('Modelo ElevenLabs suportado: eleven_multilingual_v2 ou eleven_v3.')
    if provider == 'fish' and not re.fullmatch(r'[a-zA-Z0-9._-]+', profile['model']):
        raise ValueError('Identificador de modelo Fish inválido.')


def api_request(profile, text):
    """One paid request at most. Fixed hosts; do not expose provider error bodies."""
    import requests
    provider = profile['provider']
    key_name = 'ELEVENLABS_API_KEY' if provider == 'elevenlabs' else 'FISH_API_KEY'
    key = os.getenv(key_name, '').strip()
    if not key:
        raise ValueError('Configure a chave de ' + ('ElevenLabs' if provider == 'elevenlabs' else 'Fish Audio') + ' na aba Vozes.')
    speed = 1 + profile['rate'] / 100
    if provider == 'elevenlabs':
        url = 'https://api.elevenlabs.io/v1/text-to-speech/' + quote(profile['voice'], safe='')
        headers = {'xi-api-key': key, 'Content-Type': 'application/json'}
        body = {'text': text, 'model_id': profile['model'],
                'voice_settings': {'stability': 0.5, 'similarity_boost': 0.75,
                                   'style': 0.0, 'use_speaker_boost': True, 'speed': speed}}
    else:
        url = 'https://api.fish.audio/v1/tts'
        headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'model': profile['model']}
        body = {'text': text, 'reference_id': profile['voice'], 'format': 'mp3',
                'prosody': {'speed': speed}}
    try:
        response = requests.post(url, headers=headers, json=body, timeout=(10, 180),
                                 stream=True, allow_redirects=False)
    except requests.RequestException:
        raise RuntimeError('Não foi possível confirmar a resposta do serviço de voz. Não houve repetição automática.') from None
    return response


def download_cloud(profile, text, dest):
    response = api_request(profile, text)
    try:
        if response.status_code != 200:
            raise RuntimeError(f'Serviço de voz respondeu HTTP {response.status_code}. Confira chave, crédito, modelo e acesso à voz. Nenhuma voz substituta foi usada.')
        count = 0
        with dest.open('wb') as f:
            for chunk in response.iter_content(65536):
                count += len(chunk)
                if count > 50 * 1024 * 1024:
                    raise RuntimeError('Resposta de áudio acima do limite de 50 MB.')
                f.write(chunk)
        if not count:
            raise RuntimeError('Serviço retornou áudio vazio.')
    except Exception as exc:
        # Avoid request object/headers leaking in GUI traceback.
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError('Transferência de áudio interrompida. A geração não foi repetida.') from None
    finally:
        response.close()


async def synthesize_one(text, profile, cache):
    validate_profile(profile)
    cache = Path(cache); cache.mkdir(parents=True, exist_ok=True)
    ref = None
    if profile['provider'] == 'voicestudio':
        import voice_studio
        ref = await asyncio.to_thread(voice_studio.reference, profile['voice'])
        profile = dict(profile, reference_hash=hashlib.sha256(ref).hexdigest())
    digest = hashlib.sha256(json.dumps({'version': 1, 'text': text, 'profile': profile}, sort_keys=True).encode()).hexdigest()
    dest = cache / (digest + '.mp3'); meta = dest.with_suffix('.json')
    if dest.exists() and meta.exists():
        try:
            result = json.loads(meta.read_text(encoding='utf-8'))
            result['duration'] = validate_audio(dest)
            return dict(result, audio=dest, cached=True)
        except (ValueError, OSError, RuntimeError):
            pass
    fd, tmpname = tempfile.mkstemp(suffix='.mp3', dir=cache)
    os.close(fd); tmp = Path(tmpname)
    words = []
    try:
        if profile['provider'] == 'edge':
            import edge_tts
            talk = edge_tts.Communicate(text, profile['voice'], rate=f"{profile['rate']:+d}%",
                                       pitch=f"{profile['pitch']:+d}Hz", boundary='WordBoundary')
            with tmp.open('wb') as f:
                async for item in talk.stream():
                    if item['type'] == 'audio':
                        f.write(item['data'])
                    elif item['type'] == 'WordBoundary':
                        words.append({'text': item['text'], 'start': item['offset'] / 1e7,
                                      'end': (item['offset'] + item['duration']) / 1e7})
        elif ref is not None:
            await asyncio.to_thread(voice_studio.synthesize, text, profile, tmp, ref, cache)
        else:
            await asyncio.to_thread(download_cloud, profile, text, tmp)
        measured = validate_audio(tmp)
        result = {'duration': measured, 'words': words,
                  'timing_source': 'edge_word_boundary' if words else 'pending_alignment',
                  'profile': profile}
        os.replace(tmp, dest)
        meta.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return dict(result, audio=dest, cached=False)
    finally:
        tmp.unlink(missing_ok=True)


async def synthesize_lines(lines, cache):
    selected = profiles()
    for line in lines:
        # Explicit imported recordings take precedence and never invoke a service.
        if line.get('audio_source') == 'imported':
            path = Path(line['audio'])
            line['duration'] = validate_audio(path)
            line['timing_source'] = 'pending_alignment'
            continue
        result = await synthesize_one(line['text'], selected[line['speaker']], cache)
        line.update(result)
        actual = selected[line['speaker']]
        line.update(voice=actual['voice'], voice_provider=actual['provider'],
                    rate=f"{actual['rate']:+d}%", pitch=f"{actual['pitch']:+d}Hz")


def preview_lines(lines, folder):
    """Preview uses the exact audio cache consumed later by the renderer."""
    import copy
    from media_tools import run
    prepared = copy.deepcopy(lines)
    asyncio.run(synthesize_lines(prepared, Path(folder) / 'cache'))
    args = []; filters = []; cursor = .25
    for i, line in enumerate(prepared):
        args += ['-i', str(line['audio'])]
        delay = round(cursor * 1000)
        filters.append(f'[{i}:a]adelay={delay}|{delay}[a{i}]')
        cursor += line['duration'] + .14
    dest = Path(folder) / 'previa_dialogo.wav'; dest.parent.mkdir(parents=True, exist_ok=True)
    filters.append(''.join(f'[a{i}]' for i in range(len(prepared))) + f'amix=inputs={len(prepared)}:normalize=0[out]')
    run(['ffmpeg', '-y', '-v', 'error', *args, '-filter_complex', ';'.join(filters),
         '-map', '[out]', '-ar', '24000', '-ac', '1', dest])
    return dest
