"""Modo Cortes do ReelForge: entrada, download com yt-dlp, transcrição e seleção de cortes."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import engine
import media_tools

AUTH_CONFIRM_TEXT = 'Confirmo que tenho autorização para cortar este vídeo'
DEFAULT_YTDLP_META_TIMEOUT_S = 60
DEFAULT_YTDLP_DOWNLOAD_TIMEOUT_S = 600

_whisper_models = {}
_whisper_lock = threading.Lock()


class CancelledError(RuntimeError):
    """Exceção lançada quando o usuário cancela cooperativamente a operação."""

DEFAULT_CORTES_SETTINGS = {
    'idioma': 'pt',
    'duracao_min': 25.0,
    'duracao_max': 55.0,
    'num_cortes': 5,
    'modo_corte': 'ajustar',
    'posicao_legenda': 'juncao',
    'titulo_na_tela': True,
    'credito_na_tela': True,
    'credito': '',
    'musica_ativa': False,
}


def cortes_root() -> Path:
    """Retorna o diretório raiz de cache/saída do modo Cortes usando engine.OUTPUT dinâmico."""
    return engine.OUTPUT / 'cortes'


def require_authorization(authorized: bool) -> None:
    """Exige confirmação explícita de autorização antes de baixar, analisar ou cortar."""
    if not authorized:
        raise ValueError(
            f'Confirmação obrigatória: marque "{AUTH_CONFIRM_TEXT}" '
            '(ou informe --autorizado na linha de comando).'
        )


def build_auth_metadata(
    fonte_url: str,
    canal: str,
    credito: str = '',
    authorized: bool = True,
    data: str | None = None,
) -> dict:
    """Monta os campos obrigatórios de autorização e atribuição para todos os JSONs."""
    require_authorization(authorized)
    canal_clean = str(canal or '').strip() or 'local'
    credito_clean = str(credito or '').strip()
    if not credito_clean and canal_clean and canal_clean.lower() != 'local':
        credito_clean = '@' + canal_clean.lstrip('@')
    return {
        'fonte_url': str(fonte_url or '').strip(),
        'canal': canal_clean,
        'autorizado': True,
        'credito': credito_clean,
        'data': data or datetime.now(timezone.utc).isoformat(),
    }


def _is_url(source: str) -> bool:
    return bool(re.match(r'^https?://', str(source or '').strip(), re.IGNORECASE))


def _sanitize_video_id(raw_id: str) -> str:
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', str(raw_id or '').strip()).strip('._-')
    return cleaned or hashlib.sha256(str(raw_id).encode('utf-8')).hexdigest()[:12]


def _load_url_index() -> dict:
    idx_path = cortes_root() / '_url_index.json'
    if not idx_path.is_file():
        return {}
    try:
        data = json.loads(idx_path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_url_index(url: str, video_id: str) -> None:
    root = cortes_root()
    root.mkdir(parents=True, exist_ok=True)
    idx_path = root / '_url_index.json'
    index = _load_url_index()
    index[url] = video_id
    idx_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding='utf-8')


def _friendly_ytdlp_error(stderr: str) -> str:
    raw = str(stderr or '').strip()
    low = raw.casefold()
    if 'no module named yt_dlp' in low or 'no module named \'yt_dlp\'' in low:
        return (
            'yt-dlp não está instalado no ambiente. '
            'Execute: .\\.venv\\Scripts\\python.exe -m pip install yt-dlp'
        )
    if 'private video' in low or 'video is private' in low:
        return 'Este vídeo é privado e não pode ser baixado.'
    if any(tok in low for tok in ('drm', 'widevine', 'fairplay', 'playready')):
        return 'Este vídeo possui proteção DRM e não é suportado pelo ReelForge.'
    if any(tok in low for tok in (
        'sign in to confirm your age', 'age-restricted', 'age gate',
        'confirm you’re not a bot', "confirm you're not a bot",
        'login', 'sign in', 'cookies', 'members-only', 'join this channel',
    )):
        return (
            'Este vídeo exige login, cookies ou verificação de restrição e não será baixado '
            '(o ReelForge não contorna restrições de acesso).'
        )
    if any(tok in low for tok in (
        'video unavailable', 'unavailable', 'removed by the uploader',
        'has been removed', 'does not exist', 'account associated', '404',
    )):
        return 'Vídeo indisponível, removido ou inexistente.'
    if 'unsupported url' in low or 'is not a valid url' in low:
        return 'Link inválido ou site não suportado pelo yt-dlp.'
    if any(tok in low for tok in ('timed out', 'connection', 'name or service not known', 'temporary failure in name resolution', 'urlopen error')):
        return 'Falha de conexão ao acessar o vídeo. Verifique sua internet e tente novamente.'
    last_line = raw.splitlines()[-1].strip() if raw else ''
    snippet = last_line[:180] if last_line else 'erro desconhecido'
    return f'Não foi possível baixar o vídeo com yt-dlp: {snippet}'


def _run_ytdlp(args: list[str], timeout: float, label: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, '-m', 'yt_dlp', *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        engine.log_error_traceback(exc)
        raise RuntimeError('Python/yt-dlp não encontrado para executar o download.') from None
    except subprocess.TimeoutExpired as exc:
        engine.log_error_traceback(exc)
        raise RuntimeError(
            f'Tempo esgotado ({int(timeout)} s) ao {label} com yt-dlp. '
            'Tente novamente ou baixe o vídeo localmente.'
        ) from None
    if proc.returncode != 0:
        msg = _friendly_ytdlp_error(proc.stderr or proc.stdout)
        engine.log_error_traceback(RuntimeError(f'yt-dlp ({proc.returncode}): {(proc.stderr or "")[-1200:]}'))
        raise RuntimeError(msg)
    return proc


def _measure_duration_safe(path: Path) -> float:
    try:
        return float(media_tools.duration(path))
    except Exception:
        return 0.0


def _refresh_cached_info(info_path: Path, fonte_url: str, credito: str, authorized: bool) -> dict:
    info = json.loads(info_path.read_text(encoding='utf-8'))
    canal = info.get('canal', 'local')
    effective_credito = str(credito or '').strip() or info.get('credito', '')
    auth_fields = build_auth_metadata(
        fonte_url or info.get('fonte_url', ''),
        canal,
        credito=effective_credito,
        authorized=authorized,
    )
    info.update(auth_fields)
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
    return info


def prepare_source(
    source: str | Path,
    authorized: bool = False,
    credito: str = '',
    meta_timeout: float = DEFAULT_YTDLP_META_TIMEOUT_S,
    download_timeout: float = DEFAULT_YTDLP_DOWNLOAD_TIMEOUT_S,
    log=print,
) -> dict:
    """Prepara entrada por URL (yt-dlp até 1080p mp4) ou arquivo local com cache em output/cortes/<video_id>/."""
    require_authorization(authorized)
    source_str = str(source or '').strip()
    if not source_str:
        raise ValueError('Informe um link de vídeo ou selecione um arquivo local.')

    if _is_url(source_str):
        index = _load_url_index()
        cached_id = index.get(source_str)
        if cached_id:
            video_dir = cortes_root() / cached_id
            video_path = video_dir / 'source.mp4'
            info_path = video_dir / 'info.json'
            if video_path.is_file() and video_path.stat().st_size > 0 and info_path.is_file():
                log(f'Cache encontrado ({cached_id}); reutilizando {video_path}')
                return _refresh_cached_info(info_path, source_str, credito, authorized)

        log('Consultando metadados do vídeo via yt-dlp...')
        meta_proc = _run_ytdlp(
            ['--dump-single-json', '--no-playlist', '--no-warnings', source_str],
            timeout=meta_timeout,
            label='consultar metadados do vídeo',
        )
        try:
            meta = json.loads(meta_proc.stdout or '{}')
        except ValueError as exc:
            engine.log_error_traceback(exc)
            raise RuntimeError('Não foi possível ler os metadados retornados pelo yt-dlp.') from None

        if not isinstance(meta, dict) or not meta:
            raise RuntimeError('Metadados vazios retornados pelo yt-dlp.')

        video_id = _sanitize_video_id(meta.get('id') or hashlib.sha256(source_str.encode('utf-8')).hexdigest()[:12])
        title = str(meta.get('title') or video_id).strip()
        canal = str(meta.get('channel') or meta.get('uploader') or meta.get('uploader_id') or '').strip()
        duration = float(meta.get('duration') or 0.0)
        fonte_url = str(meta.get('webpage_url') or meta.get('original_url') or source_str).strip()

        video_dir = cortes_root() / video_id
        video_dir.mkdir(parents=True, exist_ok=True)
        video_path = video_dir / 'source.mp4'
        info_path = video_dir / 'info.json'

        if video_path.is_file() and video_path.stat().st_size > 0 and info_path.is_file():
            log(f'Cache encontrado ({video_id}); reutilizando {video_path}')
            _save_url_index(source_str, video_id)
            return _refresh_cached_info(info_path, fonte_url, credito, authorized)

        log(f'Baixando vídeo "{title}" (até 1080p MP4)...')
        fmt = 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best[height<=1080]'
        _run_ytdlp(
            [
                '-f', fmt,
                '--merge-output-format', 'mp4',
                '--no-playlist',
                '--no-warnings',
                '-o', str(video_path),
                source_str,
            ],
            timeout=download_timeout,
            label='baixar o vídeo',
        )
        if not video_path.is_file() or video_path.stat().st_size == 0:
            raise RuntimeError('O download terminou sem gerar o arquivo source.mp4.')
        if duration <= 0:
            duration = _measure_duration_safe(video_path)

        info = {
            'id': video_id,
            'titulo': title,
            'duracao': duration,
            'source_path': str(video_path),
            'is_local': False,
            **build_auth_metadata(fonte_url, canal, credito=credito, authorized=True),
        }
        info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
        _save_url_index(source_str, video_id)
        log(f'Vídeo pronto em cache: {video_path}')
        return info

    # Entrada por arquivo local
    local_path = Path(source_str)
    if not local_path.is_file():
        raise FileNotFoundError(f'Arquivo de vídeo local não encontrado: {local_path}')

    resolved = local_path.resolve()
    stat = resolved.stat()
    digest = hashlib.sha256(f'{resolved}|{stat.st_size}|{stat.st_mtime_ns}'.encode('utf-8')).hexdigest()[:12]
    video_id = f'local_{digest}'
    video_dir = cortes_root() / video_id
    video_dir.mkdir(parents=True, exist_ok=True)
    video_path = video_dir / 'source.mp4'
    info_path = video_dir / 'info.json'

    if info_path.is_file() and (video_path.is_file() or resolved.is_file()):
        log(f'Cache local encontrado ({video_id}); reutilizando.')
        return _refresh_cached_info(info_path, str(resolved), credito, authorized)

    effective_source = resolved
    if not video_path.exists():
        if resolved.suffix.lower() == '.mp4':
            try:
                os.link(resolved, video_path)
                effective_source = video_path
            except OSError:
                if stat.st_size <= 64 * 1024 * 1024:
                    shutil.copy2(resolved, video_path)
                    effective_source = video_path
        elif stat.st_size <= 64 * 1024 * 1024:
            shutil.copy2(resolved, video_path)
            effective_source = video_path

    duration = _measure_duration_safe(effective_source)
    info = {
        'id': video_id,
        'titulo': local_path.stem,
        'duracao': duration,
        'source_path': str(effective_source),
        'is_local': True,
        **build_auth_metadata(str(resolved), 'local', credito=credito, authorized=True),
    }
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
    log(f'Arquivo local registrado em cache: {video_id}')
    return info


def _is_cancelled(cancel_event) -> bool:
    if cancel_event is None:
        return False
    if callable(cancel_event):
        return bool(cancel_event())
    is_set = getattr(cancel_event, 'is_set', None)
    return bool(is_set()) if callable(is_set) else bool(cancel_event)


def _check_cancelled(cancel_event) -> None:
    if _is_cancelled(cancel_event):
        raise CancelledError('Operação cancelada pelo usuário.')


def _default_whisper_model() -> str:
    cfg = engine.load_settings()
    return str(cfg.get('whisper_model') or os.getenv('WHISPER_MODEL') or 'small').strip() or 'small'


def _load_whisper_model(model_name: str, download_root: Path):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError(
            'Instale as legendas por áudio usando instalar_legendas.bat.'
        ) from None

    download_root_str = str(download_root)
    key = (model_name, download_root_str)
    with _whisper_lock:
        if key not in _whisper_models:
            try:
                _whisper_models[key] = WhisperModel(
                    model_name,
                    device='cpu',
                    compute_type='int8',
                    download_root=download_root_str,
                    local_files_only=True,
                )
            except Exception:
                cmd = (
                    f'.venv\\Scripts\\python.exe -c "from faster_whisper import WhisperModel; '
                    f"WhisperModel('{model_name}', device='cpu', compute_type='int8', "
                    f"download_root='{download_root_str}')\""
                )
                raise RuntimeError(
                    f"O modelo Whisper '{model_name}' não está baixado localmente. "
                    f"Execute instalar_legendas.bat ou rode o comando:\n{cmd}"
                ) from None
        return _whisper_models[key]


def group_words_into_units(
    words: list[dict],
    pause_threshold: float = 0.7,
    max_words_per_unit: int = 35,
) -> list[dict]:
    """Agrupa palavras em unidades (sentenças por pontuação terminal ou pausa > 0,7 s)."""
    cleaned_words = []
    for raw in words or []:
        texto = str(raw.get('texto', raw.get('text', ''))).strip()
        if not texto:
            continue
        inicio = float(raw.get('inicio', raw.get('start', 0.0)))
        fim = float(raw.get('fim', raw.get('end', inicio)))
        if fim < inicio:
            fim = inicio
        cleaned_words.append({'texto': texto, 'inicio': round(inicio, 3), 'fim': round(fim, 3)})

    if not cleaned_words:
        return []

    sentence_end_re = re.compile(r'[.!?…]+["\')\]]*$')
    units = []
    bucket = [cleaned_words[0]]

    for word in cleaned_words[1:]:
        prev = bucket[-1]
        gap = word['inicio'] - prev['fim']
        ends_sentence = bool(sentence_end_re.search(prev['texto']))
        if gap > pause_threshold or ends_sentence or len(bucket) >= max_words_per_unit:
            units.append({
                'indice': len(units),
                'inicio': bucket[0]['inicio'],
                'fim': bucket[-1]['fim'],
                'texto': ' '.join(w['texto'] for w in bucket),
                'palavras': list(bucket),
            })
            bucket = [word]
        else:
            bucket.append(word)

    if bucket:
        units.append({
            'indice': len(units),
            'inicio': bucket[0]['inicio'],
            'fim': bucket[-1]['fim'],
            'texto': ' '.join(w['texto'] for w in bucket),
            'palavras': list(bucket),
        })
    return units


def _extract_audio_slice(source_path: Path, dest_wav: Path, start_s: float, end_s: float | None) -> None:
    cmd = ['ffmpeg', '-y', '-v', 'error', '-ss', f'{start_s:.3f}']
    if end_s is not None:
        cmd += ['-to', f'{end_s:.3f}']
    cmd += ['-i', str(source_path), '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(dest_wav)]
    engine.run(cmd)


def transcribe_video(
    info: dict,
    language: str = 'pt',
    model_name: str | None = None,
    start_min: float | None = None,
    end_min: float | None = None,
    cancel_event=None,
    log=print,
) -> dict:
    """Transcreve o vídeo com faster-whisper (word_timestamps=True, vad_filter=True) e cacheia em transcricao.json."""
    require_authorization(bool(info.get('autorizado')))
    _check_cancelled(cancel_event)

    lang = str(language or 'pt').strip() or 'pt'
    model = str(model_name or _default_whisper_model()).strip() or 'small'
    s_min = None if start_min in (None, '') else float(start_min)
    e_min = None if end_min in (None, '') else float(end_min)
    if s_min is not None and s_min < 0:
        raise ValueError('O minuto inicial deve ser >= 0.')
    if e_min is not None and e_min <= 0:
        raise ValueError('O minuto final deve ser > 0.')
    if s_min is not None and e_min is not None and e_min <= s_min:
        raise ValueError('O minuto final deve ser maior que o minuto inicial.')

    video_id = str(info['id'])
    video_dir = cortes_root() / video_id
    video_dir.mkdir(parents=True, exist_ok=True)
    cache_path = video_dir / 'transcricao.json'

    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding='utf-8'))
            if (
                isinstance(cached, dict)
                and cached.get('modelo') == model
                and cached.get('idioma') == lang
                and cached.get('inicio_min') == s_min
                and cached.get('fim_min') == e_min
                and isinstance(cached.get('unidades'), list)
            ):
                log(f'Transcrição em cache reutilizada ({model}, idioma={lang}).')
                return cached
        except (OSError, ValueError):
            pass

    source_path = Path(info.get('source_path') or (video_dir / 'source.mp4'))
    if not source_path.is_file():
        raise FileNotFoundError(f'Vídeo de origem não encontrado para transcrição: {source_path}')

    total_duration = float(info.get('duracao') or 0.0)
    if total_duration <= 0:
        total_duration = _measure_duration_safe(source_path)

    offset_s = 0.0
    target_duration = total_duration
    audio_input = source_path
    temp_dir_obj = None

    try:
        if (s_min is not None and s_min > 0) or e_min is not None:
            start_s = (s_min or 0.0) * 60.0
            end_s = e_min * 60.0 if e_min is not None else None
            if total_duration > 0 and start_s >= total_duration:
                raise ValueError('O minuto inicial escolhido está além da duração do vídeo.')
            if end_s is not None and total_duration > 0:
                end_s = min(end_s, total_duration)
            temp_dir_obj = tempfile.TemporaryDirectory(dir=str(video_dir))
            audio_input = Path(temp_dir_obj.name) / 'trecho.wav'
            log(f'Extraindo trecho de áudio ({start_s / 60:.1f}–{(end_s / 60) if end_s else (total_duration / 60):.1f} min)...')
            _extract_audio_slice(source_path, audio_input, start_s, end_s)
            offset_s = start_s
            if end_s is not None:
                target_duration = max(0.1, end_s - start_s)
            elif total_duration > 0:
                target_duration = max(0.1, total_duration - start_s)

        _check_cancelled(cancel_event)
        download_root = engine.OUTPUT / 'alignment_cache' / 'models'
        whisper_model = _load_whisper_model(model, download_root)
        _check_cancelled(cancel_event)

        log(f'Transcrevendo áudio com Whisper ({model}, idioma={lang}, CPU int8, vad_filter=True)...')
        segments, seg_info = whisper_model.transcribe(
            str(audio_input),
            language=lang,
            beam_size=5,
            word_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        if target_duration <= 0:
            target_duration = float(getattr(seg_info, 'duration', 0.0) or 0.0)

        t0 = time.monotonic()
        last_log_wall = t0
        last_log_audio = 0.0
        words = []

        for seg in segments:
            _check_cancelled(cancel_event)
            seg_words = getattr(seg, 'words', None) or []
            for w in seg_words:
                txt = str(getattr(w, 'word', '') or '').strip()
                if not txt:
                    continue
                w_start = float(w.start) + offset_s
                w_end = float(w.end) + offset_s
                if w_end <= w_start:
                    w_end = w_start + 0.02
                words.append({
                    'texto': txt,
                    'inicio': round(w_start, 3),
                    'fim': round(w_end, 3),
                })

            now = time.monotonic()
            seg_end = float(getattr(seg, 'end', 0.0) or 0.0)
            if target_duration > 0 and (now - last_log_wall >= 2.0 or seg_end - last_log_audio >= 15.0 or seg_end >= target_duration - 0.5):
                elapsed = max(0.01, now - t0)
                pct = min(100.0, max(0.0, (seg_end / target_duration) * 100.0))
                if seg_end > 0.5 and pct < 100.0:
                    eta = elapsed * (target_duration - seg_end) / seg_end
                    eta_txt = f'{eta:.0f}s'
                else:
                    eta_txt = '0s'
                log(
                    f'Transcrição: {seg_end:.1f}s / {target_duration:.1f}s ({pct:.0f}%) '
                    f'— decorrido {elapsed:.1f}s, estimado ~{eta_txt}'
                )
                last_log_wall = now
                last_log_audio = seg_end

        _check_cancelled(cancel_event)
        units = group_words_into_units(words)
        elapsed_total = time.monotonic() - t0
        log(f'Transcrição concluída em {elapsed_total:.1f}s: {len(words)} palavras, {len(units)} unidades.')

        auth_fields = build_auth_metadata(
            info.get('fonte_url', ''),
            info.get('canal', 'local'),
            credito=info.get('credito', ''),
            authorized=True,
        )
        result = {
            'video_id': video_id,
            'titulo': info.get('titulo', video_id),
            'duracao': total_duration or (words[-1]['fim'] if words else 0.0),
            'modelo': model,
            'idioma': lang,
            'inicio_min': s_min,
            'fim_min': e_min,
            'palavras': words,
            'unidades': units,
            **auth_fields,
        }
        cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return result
    finally:
        if temp_dir_obj is not None:
            temp_dir_obj.cleanup()


def main():
    engine.apply_settings()
    parser = argparse.ArgumentParser(description='ReelForge Cortes: preparar, transcrever e selecionar cortes autorizados')
    parser.add_argument('--fonte', required=True, help='URL do vídeo ou caminho de arquivo local')
    parser.add_argument('--autorizado', action='store_true', help=AUTH_CONFIRM_TEXT)
    parser.add_argument('--credito', default='', help='Texto livre de crédito (ex.: @canal)')
    args = parser.parse_args()
    info = prepare_source(args.fonte, authorized=args.autorizado, credito=args.credito)
    print(json.dumps(info, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('ERRO:', exc, file=sys.stderr)
        sys.exit(1)
