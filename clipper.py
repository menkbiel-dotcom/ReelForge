"""Modo Cortes do ReelForge: entrada, download com yt-dlp, transcrição e seleção de cortes."""
import argparse
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
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
    'modo_corte': 'preencher',
    'enquadramento': 'auto',
    'posicao_legenda': 'juncao',
    'titulo_na_tela': True,
    'credito_na_tela': True,
    'credito': '',
    'musica_ativa': False,
    'musica_modo': 'impacto',
    'musica_volume': 22,
    'impacto_inicial': 'impacto',
    'impacto_volume': 70,
}


def migrate_cortes_settings(raw: dict | None, has_fallback: bool = True) -> dict:
    """Normaliza configurações de cortes e migra musica_ativa (true->'baixa', false->'desligada') quando musica_modo ausente."""
    default_mode = 'impacto' if has_fallback else 'desligada'
    out = dict(DEFAULT_CORTES_SETTINGS)
    out['musica_modo'] = default_mode
    if not isinstance(raw, dict):
        out['musica_ativa'] = out['musica_modo'] != 'desligada'
        return out

    out.update(raw)
    raw_mode = raw.get('musica_modo')
    if raw_mode is not None and str(raw_mode).strip():
        mode = str(raw_mode).strip().casefold()
        if mode not in ('desligada', 'baixa', 'impacto'):
            mode = default_mode
        out['musica_modo'] = mode
    elif 'musica_ativa' in raw:
        out['musica_modo'] = 'baixa' if bool(raw['musica_ativa']) else 'desligada'
    else:
        out['musica_modo'] = default_mode

    out['musica_ativa'] = out['musica_modo'] != 'desligada'
    try:
        out['musica_volume'] = max(0, min(100, int(round(float(out.get('musica_volume', 22))))))
    except (TypeError, ValueError):
        out['musica_volume'] = 22
    imp = str(out.get('impacto_inicial', 'impacto') or 'impacto').strip().casefold()
    out['impacto_inicial'] = imp if imp in ('nenhum', 'impacto', 'whoosh') else 'impacto'
    try:
        out['impacto_volume'] = max(0, min(100, int(round(float(out.get('impacto_volume', 70))))))
    except (TypeError, ValueError):
        out['impacto_volume'] = 70
    return out


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


def _log_ytdlp_warnings(stderr: str, log=None) -> None:
    if log is None or not stderr:
        return
    for raw_line in str(stderr).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        low = line.casefold()
        if 'warning' in low or 'ffmpeg' in low or 'javascript' in low:
            log(f'yt-dlp: {line}')


def _run_ytdlp(args: list[str], timeout: float, label: str, log=None) -> subprocess.CompletedProcess:
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
    _log_ytdlp_warnings(proc.stderr, log=log)
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


def _ffmpeg_to_mp4(inputs: list[Path], out_path: Path) -> None:
    input_args: list[str] = []
    for inp in inputs:
        input_args.extend(['-i', str(inp)])
    try:
        media_tools.run(['ffmpeg', '-y', *input_args, '-c', 'copy', '-movflags', '+faststart', str(out_path)])
    except RuntimeError:
        media_tools.run([
            'ffmpeg', '-y', *input_args,
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
            '-c:a', 'aac', '-movflags', '+faststart', str(out_path),
        ])


def _finalize_downloaded_source(video_dir: Path, video_path: Path, stderr_text: str = '', log=print) -> Path:
    if video_path.is_file() and video_path.stat().st_size > 0:
        return video_path

    ignored_suffixes = {'.json', '.part', '.ytdl', '.tmp'}
    f_frag_re = re.compile(r'\.f\d+(?:\.[^.]+)?$', re.IGNORECASE)

    other_sources = [
        p for p in sorted(video_dir.iterdir())
        if p.is_file()
        and p != video_path
        and p.name.startswith('source.')
        and p.suffix.lower() not in ignored_suffixes
        and not f_frag_re.search(p.name)
        and p.stat().st_size > 0
    ]
    if other_sources:
        cand = max(other_sources, key=lambda p: p.stat().st_size)
        log(f'Convertendo {cand.name} para {video_path.name} com ffmpeg...')
        _ffmpeg_to_mp4([cand], video_path)
        if video_path.is_file() and video_path.stat().st_size > 0:
            try:
                cand.unlink(missing_ok=True)
            except OSError:
                pass
            return video_path

    f_files = [
        p for p in sorted(video_dir.iterdir())
        if p.is_file()
        and f_frag_re.search(p.name)
        and p.suffix.lower() not in ignored_suffixes
        and p.stat().st_size > 0
    ]
    if f_files:
        inputs = f_files[:2] if len(f_files) >= 2 else [f_files[0]]
        log(f'Unindo/convertendo arquivos temporários ({", ".join(p.name for p in inputs)}) com ffmpeg...')
        _ffmpeg_to_mp4(inputs, video_path)
        if video_path.is_file() and video_path.stat().st_size > 0:
            for p in f_files:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
            return video_path

    files_present = ', '.join(sorted(p.name for p in video_dir.iterdir())) if video_dir.is_dir() else '(pasta inexistente)'
    files_present = files_present or '(nenhum)'
    stderr_tail = (stderr_text or '').strip()[-600:] or 'sem saída no stderr'
    raise RuntimeError(
        f'O download terminou sem gerar o arquivo source.mp4. '
        f'Arquivos encontrados em {video_dir}: [{files_present}]. '
        f'Stderr do yt-dlp: {stderr_tail}'
    )


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
        ffmpeg_bin = str(media_tools.ffmpeg())
        fmt = 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best[height<=1080]'
        dl_proc = _run_ytdlp(
            [
                '-f', fmt,
                '--merge-output-format', 'mp4',
                '--ffmpeg-location', ffmpeg_bin,
                '--no-playlist',
                '-o', str(video_path),
                source_str,
            ],
            timeout=download_timeout,
            label='baixar o vídeo',
            log=log,
        )
        _finalize_downloaded_source(video_dir, video_path, stderr_text=dl_proc.stderr or '', log=log)
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


_STOPWORDS_PT = {
    'a', 'o', 'e', 'de', 'do', 'da', 'dos', 'das', 'em', 'no', 'na', 'nos', 'nas',
    'um', 'uma', 'uns', 'umas', 'para', 'pra', 'por', 'com', 'sem', 'sob', 'sobre',
    'que', 'quem', 'qual', 'quais', 'quando', 'onde', 'como', 'porque', 'por', 'pois',
    'mas', 'porem', 'contudo', 'entretanto', 'ou', 'nem', 'se', 'ja', 'mais', 'menos',
    'muito', 'muita', 'muitos', 'muitas', 'pouco', 'pouca', 'todo', 'toda', 'todos',
    'todas', 'isso', 'isto', 'aquilo', 'esse', 'essa', 'este', 'esta', 'aquele',
    'aquela', 'ele', 'ela', 'eles', 'elas', 'eu', 'tu', 'voces', 'voce', 'nos',
    'meu', 'minha', 'seu', 'sua', 'nosso', 'nossa', 'ao', 'aos', 'as', 'os',
    'ate', 'ate', 'entre', 'foi', 'era', 'sao', 'ser', 'esta', 'estao', 'tem', 'ter',
    'vai', 'vao', 'fazer', 'fez', 'diz', 'disse', 'aqui', 'ali', 'la', 'entao', 'assim',
    'mesmo', 'mesma', 'ainda', 'tambem', 'so', 'bem', 'mal', 'sim', 'nao', 'nada',
}

_NUMBER_WORDS_PT = {
    'zero': '0', 'um': '1', 'uma': '1', 'dois': '2', 'duas': '2', 'tres': '3',
    'quatro': '4', 'cinco': '5', 'seis': '6', 'sete': '7', 'oito': '8', 'nove': '9',
    'dez': '10', 'onze': '11', 'doze': '12', 'treze': '13', 'catorze': '14',
    'quatorze': '14', 'quinze': '15', 'dezesseis': '16', 'dezessete': '17',
    'dezoito': '18', 'dezenove': '19', 'vinte': '20', 'trinta': '30',
    'quarenta': '40', 'cinquenta': '50', 'sessenta': '60', 'setenta': '70',
    'oitenta': '80', 'noventa': '90', 'cem': '100', 'cento': '100',
    'duzentos': '200', 'trezentos': '300', 'quatrocentos': '400', 'quinhentos': '500',
    'mil': '1000', 'milhao': '1000000', 'milhoes': '1000000',
    'bilhao': '1000000000', 'bilhoes': '1000000000',
}

WEAK_START_TRANSITIONS = (
    'e galera',
    'então',
    'bom',
    'outra coisa',
    'agora',
    'mas',
    'aí',
    'olha só',
)


def _strip_accents_lower(text: str) -> str:
    norm = unicodedata.normalize('NFKD', str(text or '').casefold())
    return ''.join(c for c in norm if not unicodedata.combining(c))


def starts_with_weak_transition(text: str) -> bool:
    """Retorna True se o texto começa com transição/conector fraco ('e galera', 'então', 'bom', etc.)."""
    plain = re.sub(r'^[^\w]+', '', _strip_accents_lower(text)).strip()
    if not plain:
        return False
    return bool(re.match(r'^(e galera|entao|bom|outra coisa|agora|mas|ai|olha so)\b', plain))


AD_KEYWORD_PATTERNS: tuple[tuple[str, str], ...] = (
    ('patrocinio', r'\bpatrocin\w*'),
    ('parceria', r'\bparceri\w*'),
    ('parceiro', r'\bparceir\w*'),
    ('cupom', r'\bcupo(?:m|ns)\b'),
    ('desconto', r'\bdescont\w*'),
    ('codigo', r'\bcodig\w*'),
    ('link na descricao', r'\blink\s+na\s+descric\w*'),
    ('acesse o site', r'\bacesse\s+o\s+site\b'),
    ('cadastre', r'\bcadastr\w*'),
    ('baixe o app', r'\bbaixe\s+o\s+(?:app|aplicativo)\b'),
    ('assine', r'\bassin(?:e|ar|atura)\b'),
    ('totalmente gratuito', r'\btotalmente\s+gratuit\w*'),
    ('sem propaganda', r'\bsem\s+propagand\w*'),
)


def detect_advertising(text: str) -> tuple[bool, list[str]]:
    """Detecta indícios de publicidade/patrocínio por palavras-chave em PT-BR (>=2 ocorrências distintas)."""
    norm = _strip_accents_lower(text)
    if not norm:
        return False, []
    matched: list[str] = []
    for label, pattern in AD_KEYWORD_PATTERNS:
        if re.search(pattern, norm):
            matched.append(label)
    return len(matched) >= 2, matched


def _normalize_tokens(text: str) -> list[str]:
    plain = _strip_accents_lower(text)
    return re.findall(r'[a-z0-9]+', plain)


def _short_reason(exc_or_text, limit: int = 220) -> str:
    txt = ' '.join(str(exc_or_text or '').split())
    if len(txt) <= limit:
        return txt
    return txt[:limit - 3].rstrip() + '...'


def format_timestamp(seconds: float) -> str:
    total_s = max(0, int(round(float(seconds or 0.0))))
    h = total_s // 3600
    m = (total_s % 3600) // 60
    s = total_s % 60
    if h > 0:
        return f'{h}:{m:02d}:{s:02d}'
    return f'{m:02d}:{s:02d}'


def build_windows(
    units: list[dict],
    max_words: int = 650,
    overlap_seconds: float = 60.0,
) -> list[list[dict]]:
    """Divide unidades em janelas de até max_words palavras com sobreposição aproximada de overlap_seconds."""
    if not units:
        return []
    windows = []
    n = len(units)
    start_idx = 0
    while start_idx < n:
        win = []
        word_count = 0
        idx = start_idx
        while idx < n:
            u = units[idx]
            u_words = len(str(u.get('texto', '')).split())
            if win and (word_count + u_words > max_words):
                break
            win.append(u)
            word_count += u_words
            idx += 1
        windows.append(win)
        if idx >= n:
            break
        # Recua a partir do fim da janela atual até acumular >= overlap_seconds (~1 min)
        end_time = float(win[-1].get('fim', 0.0))
        next_start = idx - 1
        while next_start > start_idx and (end_time - float(units[next_start].get('inicio', 0.0))) < overlap_seconds:
            next_start -= 1
        if next_start <= start_idx:
            next_start = start_idx + max(1, len(win) // 2)
        start_idx = next_start
    return windows


def _format_window_text(window_units: list[dict]) -> str:
    lines = []
    for u in window_units:
        idx = int(u['indice'])
        ts = format_timestamp(float(u['inicio']))
        txt = str(u.get('texto', '')).strip()
        lines.append(f'[{idx}] {ts} {txt}')
    return '\n'.join(lines)


def _call_ask(ask, instruction: str, payload: str, model: str | None, temperature: float, log):
    kwargs = {'model': model, 'temperature': temperature, 'log': log}
    try:
        sig = inspect.signature(ask)
        if 'num_ctx' in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            kwargs['num_ctx'] = 4096
    except (ValueError, TypeError):
        pass
    return ask(instruction, payload, **kwargs)


def _ask_with_retry(ask, instruction: str, payload: str, model: str | None, log, max_attempts: int = 3):
    last_exc = None
    for attempt in range(max_attempts):
        temp = min(0.7, round(0.55 + 0.05 * attempt, 2))
        try:
            res = _call_ask(ask, instruction, payload, model=model, temperature=temp, log=log)
            if not isinstance(res, dict):
                raise ValueError(f'Resposta fora do formato objeto JSON (recebido {type(res).__name__}).')
            return res
        except Exception as exc:
            last_exc = exc
            log(f'Tentativa {attempt + 1}/{max_attempts} falhou (temp={temp:.2f}): {_short_reason(exc, 220)}')
    raise last_exc


def _span_with_padding(units: list[dict], s_idx: int, e_idx: int, video_duration: float) -> tuple[float, float]:
    raw_start = float(units[s_idx]['inicio']) - 0.15
    raw_end = float(units[e_idx]['fim']) + 0.25
    start = max(0.0, raw_start)
    end = min(video_duration, raw_end) if video_duration > 0 else raw_end
    if end < start:
        end = start
    return round(start, 3), round(end, 3)


def _repair_unit_bounds(
    units: list[dict],
    s_idx: int,
    e_idx: int,
    min_duration: float,
    max_duration: float,
    video_duration: float,
) -> tuple[int, int] | None:
    """Repara limites de unidades para caber em [min_duration, max_duration]. Retorna None se impossível."""
    n = len(units)
    if s_idx > e_idx:
        s_idx, e_idx = e_idx, s_idx
    if s_idx < 0 or e_idx >= n:
        return None

    def dur(a: int, b: int) -> float:
        st, en = _span_with_padding(units, a, b, video_duration)
        return en - st

    # 1. Se curto demais, expande pelas fronteiras de unidade vizinhas
    while dur(s_idx, e_idx) < min_duration:
        can_right = e_idx + 1 < n
        can_left = s_idx - 1 >= 0
        if not can_right and not can_left:
            break
        d_right = dur(s_idx, e_idx + 1) if can_right else float('inf')
        d_left = dur(s_idx - 1, e_idx) if can_left else float('inf')
        if can_right and d_right <= max_duration and (not can_left or d_right <= d_left or d_left > max_duration):
            e_idx += 1
        elif can_left and d_left <= max_duration:
            s_idx -= 1
        elif can_right and d_right < d_left:
            e_idx += 1
        elif can_left:
            s_idx -= 1
        else:
            break

    # 2. Se longo demais, apara pelas bordas (mantendo unidades inteiras)
    while dur(s_idx, e_idx) > max_duration and s_idx < e_idx:
        d_trim_end = dur(s_idx, e_idx - 1)
        d_trim_start = dur(s_idx + 1, e_idx)
        end_ok = min_duration <= d_trim_end <= max_duration
        start_ok = min_duration <= d_trim_start <= max_duration
        if end_ok and not start_ok:
            e_idx -= 1
        elif start_ok and not end_ok:
            s_idx += 1
        elif abs(d_trim_end - max_duration) <= abs(d_trim_start - max_duration) and d_trim_end >= min_duration:
            e_idx -= 1
        elif d_trim_start >= min_duration:
            s_idx += 1
        elif d_trim_end >= d_trim_start:
            e_idx -= 1
        else:
            s_idx += 1

    # 3. Se a primeira unidade começar com transição fraca e a próxima ainda mantiver a duração na faixa, avança o início
    while s_idx < e_idx and starts_with_weak_transition(str(units[s_idx].get('texto', ''))):
        if min_duration <= dur(s_idx + 1, e_idx) <= max_duration:
            s_idx += 1
        elif e_idx + 1 < n and min_duration <= dur(s_idx + 1, e_idx + 1) <= max_duration:
            s_idx += 1
            e_idx += 1
        else:
            break

    final_dur = dur(s_idx, e_idx)
    if min_duration <= final_dur <= max_duration:
        return s_idx, e_idx
    return None


def _extract_numbers_from_text(text: str) -> set[str]:
    plain = _strip_accents_lower(text)
    nums = set()
    for m in re.findall(r'\d+(?:[.,]\d+)?', plain):
        nums.add(m.replace(',', '.').rstrip('0').rstrip('.') if '.' in m.replace(',', '.') else m)
    for tok in re.findall(r'[a-z]+', plain):
        if tok in _NUMBER_WORDS_PT and tok not in ('um', 'uma'):
            nums.add(_NUMBER_WORDS_PT[tok])
    return nums


_PT_VERB_RE = re.compile(
    r'\b('
    r'e|eh|era|eram|foi|foram|ser|sera|seria|sao|esta|estao|estava|estavam|estar|esteve|'
    r'tem|tinha|tinham|teve|tiveram|ter|tera|pode|podem|podia|deve|devem|vai|vao|ia|iam|'
    r'faz|fez|fizeram|fazer|disse|diz|dizem|falou|fala|falam|explica|explicou|mostra|mostrou|'
    r'revela|revelou|acontece|aconteceu|chega|chegou|saiu|sai|entra|entrou|fica|ficou|'
    r'parece|existe|existem|precisa|quer|querem|sabe|sabem|acha|acham|pensa|pensam|ve|viu|'
    r'deixa|deixou|passa|passou|ganha|ganhou|perde|perdeu|compra|comprou|vende|vendeu|'
    r'usa|usou|cria|criou|muda|mudou|funciona|importa|vale|custa|custou|paga|pagou|'
    r'\w+(?:ar|er|ir|ou|aram|eram|iram|ava|avam|ia|iam|ando|endo|indo)'
    r')\b'
)


def _has_verb_or_question(text: str) -> bool:
    if '?' in str(text or ''):
        return True
    norm = _strip_accents_lower(text)
    tokens = re.findall(r'[a-z]+', norm)
    if len(tokens) < 2:
        return False
    return bool(_PT_VERB_RE.search(' '.join(tokens)))


def _is_fragment_sentence(text: str) -> bool:
    """Retorna True se a frase é um fragmento curto sem verbo ou com menos de 5 palavras de conteúdo."""
    words = [w for w in str(text or '').split() if w.strip()]
    if len(words) < 5:
        return True
    tokens = _normalize_tokens(text)
    content_words = [t for t in tokens if t not in _STOPWORDS_PT and not t.isdigit()]
    if len(content_words) < 3 and not _has_verb_or_question(text):
        return True
    if not _has_verb_or_question(text) and len(content_words) < 5:
        return True
    return False


def _truncate_first_sentence(text: str, max_words: int = 10) -> str:
    cleaned = ' '.join(str(text or '').split()).strip()
    if not cleaned:
        return ''
    sentences = [s.strip() for s in re.split(r'(?<=[.!?…])\s+', cleaned) if s.strip()]
    first_sent = None
    # 1. Primeira frase completa (>= 5 palavras, não fragmento) e sem transição fraca
    for s in sentences:
        if not starts_with_weak_transition(s) and not _is_fragment_sentence(s):
            first_sent = s
            break
    # 2. Qualquer frase completa (>= 5 palavras, não fragmento)
    if first_sent is None:
        for s in sentences:
            if not _is_fragment_sentence(s):
                first_sent = s
                break
    # 3. Qualquer frase com >= 5 palavras no corte
    if first_sent is None:
        for s in sentences:
            if len(s.split()) >= 5:
                first_sent = s
                break
    # 4. Fallback final: primeira frase sem transição fraca ou primeira frase disponível
    if first_sent is None:
        first_sent = sentences[0]
        for s in sentences:
            if not starts_with_weak_transition(s):
                first_sent = s
                break
    words = first_sent.split()
    if len(words) <= max_words:
        return first_sent
    return ' '.join(words[:max_words]).rstrip(',;:') + '...'


def validate_or_repair_hook(gancho: str, clip_text: str, log=print) -> str:
    """Garante que o gancho tenha <=10 palavras, >=50% das palavras de conteúdo no corte e zero números inventados."""
    def _fallback() -> str:
        log('gancho: fallback usado')
        return _truncate_first_sentence(clip_text, 10)

    hook = ' '.join(str(gancho or '').split()).strip()
    hook_words = hook.split()
    if not hook or len(hook_words) > 10 or len(hook_words) < 3:
        return _fallback()
    if not _has_verb_or_question(hook) and len(hook_words) < 5:
        return _fallback()

    clip_tokens = set(_normalize_tokens(clip_text))
    hook_tokens = _normalize_tokens(hook)
    if not hook_tokens:
        return _fallback()

    # Todos os números citados no gancho devem constar no texto do corte
    hook_nums = _extract_numbers_from_text(hook)
    clip_nums = _extract_numbers_from_text(clip_text)
    if not hook_nums.issubset(clip_nums):
        return _fallback()

    content_words = [t for t in hook_tokens if t not in _STOPWORDS_PT and not t.isdigit()]
    if content_words:
        matched = sum(1 for t in content_words if t in clip_tokens)
        if matched / len(content_words) < 0.5:
            return _fallback()
    else:
        matched = sum(1 for t in hook_tokens if t in clip_tokens)
        if matched / len(hook_tokens) < 0.5:
            return _fallback()

    return hook


def heuristic_score(clip_text: str, start: float, end: float, clip_units: list[dict]) -> tuple[float, float]:
    """Calcula (bonus_heuristico [0..2], nota_pura_heuristica [1..10])."""
    text = ' '.join(str(clip_text or '').split()).strip()
    dur = max(0.5, float(end) - float(start))
    words = text.split()
    n_words = len(words)
    wps = n_words / dur

    bonus = 0.0
    pure = 4.5

    # 1. Pergunta no início
    first_15 = ' '.join(words[:15]).casefold()
    interrogative_start = bool(re.match(
        r'^(por que|porque|como|qual|quais|quem|o que|voce sabia|você sabia|sabia que|sera que|será que)\b',
        _strip_accents_lower(first_15),
    ))
    if '?' in first_15 or interrogative_start:
        bonus += 0.45
        pure += 1.4

    # 2. Presença de números / dados concretos
    if re.search(r'\d+', text):
        bonus += 0.35
        pure += 1.1

    # 3. Exclamações / ênfase emocional
    if '!' in text:
        bonus += 0.25
        pure += 0.7

    # 4. Densidade de fala saudável (1.8 a 4.2 palavras/s)
    if 1.8 <= wps <= 4.2:
        bonus += 0.4
        pure += 1.2
    elif 1.2 <= wps < 1.8:
        bonus += 0.15
        pure += 0.4

    # 5. Ausência de silêncio interno longo (> 1.5 s)
    max_gap = 0.0
    for u_prev, u_curr in zip(clip_units, clip_units[1:]):
        gap = float(u_curr['inicio']) - float(u_prev['fim'])
        if gap > max_gap:
            max_gap = gap
    if max_gap <= 1.5:
        bonus += 0.35
        pure += 1.0
    elif max_gap > 2.5:
        bonus -= 0.2
        pure -= 0.8

    # 6. Penalidade mole se começar/terminar com clichê de intro/despedida/patrocínio
    plain = _strip_accents_lower(text)
    if re.match(r'^(oi pessoal|ola pessoal|fala galera|bom dia|boa tarde|boa noite|bem vindos|sejam bem vindos)\b', plain):
        bonus -= 0.4
        pure -= 1.2
    if any(k in plain for k in ('se inscreve', 'inscreva-se', 'deixa o like', 'patrocinado por', 'ate a proxima')):
        bonus -= 0.4
        pure -= 1.0

    # 7. Penalidade (-1.0) se começar com transição/conector fraco ("e galera", "então", "bom", etc.)
    if starts_with_weak_transition(text):
        bonus -= 1.0
        pure -= 1.0

    # 8. Penalidade (-3.0) se tiver >= 2 indícios distintos de publicidade/patrocínio
    is_publi, _ = detect_advertising(text)
    if is_publi:
        bonus -= 3.0
        pure -= 3.0

    bonus = round(max(-3.5, min(2.0, bonus)), 2)
    pure = round(max(1.0, min(10.0, pure)), 2)
    return bonus, pure


def _overlap_ratio(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    inter = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    if inter <= 0.0:
        return 0.0
    min_dur = min(max(0.001, a_end - a_start), max(0.001, b_end - b_start))
    return inter / min_dur


def dedupe_candidates(candidates: list[dict], max_overlap: float = 0.5) -> list[dict]:
    """Remove duplicatas/sobreposições (>50% de interseção), mantendo sempre a de maior nota."""
    ordered = sorted(candidates, key=lambda c: (-float(c['nota']), float(c['inicio'])))
    kept = []
    for cand in ordered:
        if any(_overlap_ratio(cand['inicio'], cand['fim'], k['inicio'], k['fim']) > max_overlap for k in kept):
            continue
        kept.append(cand)
    return kept


def select_top_candidates(candidates: list[dict], max_cuts: int = 5, min_distance_s: float = 10.0) -> list[dict]:
    """Seleciona até max_cuts melhores respeitando deduplicação (>50%) e distância >= min_distance_s."""
    ordered = sorted(candidates, key=lambda c: (-float(c['nota']), float(c['inicio'])))
    selected = []
    for cand in ordered:
        if any(_overlap_ratio(cand['inicio'], cand['fim'], s['inicio'], s['fim']) > 0.5 for s in selected):
            continue
        too_close = any(
            not (cand['fim'] <= s['inicio'] - min_distance_s or cand['inicio'] >= s['fim'] + min_distance_s)
            for s in selected
        )
        if too_close:
            continue
        selected.append(dict(cand))
        if len(selected) >= max_cuts:
            break
    for idx, cand in enumerate(selected, start=1):
        cand['indice'] = idx
        texto_cand = str(cand.get('texto', ''))
        cand.setdefault('inicio_fraco', starts_with_weak_transition(texto_cand))
        is_publi, publi_terms = detect_advertising(texto_cand)
        cand.setdefault('possivel_publi', is_publi)
        cand.setdefault('termos_publi', publi_terms)
        if cand.get('possivel_publi'):
            cand.setdefault('selo_publi', 'PUBLI?')
    return selected


def fallback_heuristic_candidates(
    units: list[dict],
    min_duration: float,
    max_duration: float,
    video_duration: float,
    max_cuts: int = 5,
) -> list[dict]:
    """Ranqueamento heurístico determinístico puro para sempre devolver candidatos mesmo se o LLM falhar."""
    if not units:
        return []
    n = len(units)
    target = (min_duration + max_duration) / 2.0
    pool = []

    for s_idx in range(n):
        best_e = None
        best_diff = float('inf')
        for e_idx in range(s_idx, n):
            st, en = _span_with_padding(units, s_idx, e_idx, video_duration)
            dur = en - st
            if dur > max_duration:
                break
            if dur >= min_duration:
                diff = abs(dur - target)
                if diff < best_diff:
                    best_diff = diff
                    best_e = e_idx
        if best_e is not None:
            st, en = _span_with_padding(units, s_idx, best_e, video_duration)
            slice_units = units[s_idx:best_e + 1]
            texto = ' '.join(u['texto'] for u in slice_units)
            bonus, pure = heuristic_score(texto, st, en, slice_units)
            is_publi, publi_terms = detect_advertising(texto)
            pool.append({
                'inicio_unidade': s_idx,
                'fim_unidade': best_e,
                'inicio': st,
                'fim': en,
                'duracao': round(en - st, 3),
                'nota_llm': None,
                'bonus_heuristico': bonus,
                'nota': pure,
                'gancho': _truncate_first_sentence(texto, 10),
                'motivo': 'Selecionado pelo ranqueamento heurístico determinístico (gancho, densidade e ritmo).',
                'texto': texto,
                'inicio_fraco': starts_with_weak_transition(texto),
                'possivel_publi': is_publi,
                'termos_publi': publi_terms,
                **({'selo_publi': 'PUBLI?'} if is_publi else {}),
            })

    if not pool:
        # Vídeo muito curto (< min_duration) ou unidade única longa (> max_duration): devolve o melhor trecho possível
        st, en = _span_with_padding(units, 0, n - 1, video_duration)
        texto = ' '.join(u['texto'] for u in units)
        bonus, pure = heuristic_score(texto, st, en, units)
        is_publi, publi_terms = detect_advertising(texto)
        pool.append({
            'inicio_unidade': 0,
            'fim_unidade': n - 1,
            'inicio': st,
            'fim': en,
            'duracao': round(en - st, 3),
            'nota_llm': None,
            'bonus_heuristico': bonus,
            'nota': pure,
            'gancho': _truncate_first_sentence(texto, 10),
            'motivo': 'Trecho único disponível dentro da duração do vídeo.',
            'texto': texto,
            'inicio_fraco': starts_with_weak_transition(texto),
            'possivel_publi': is_publi,
            'termos_publi': publi_terms,
            **({'selo_publi': 'PUBLI?'} if is_publi else {}),
        })

    return select_top_candidates(pool, max_cuts=max_cuts, min_distance_s=10.0)


def _evaluate_window_cuts(
    window_units: list[dict],
    all_units: list[dict],
    min_duration: float,
    max_duration: float,
    video_duration: float,
    ask,
    model: str | None,
    log,
) -> list[dict]:
    instruction = (
        'Você é um editor de vídeos curtos (Reels/Shorts) em português brasileiro.\n'
        'Analise a transcrição numerada por unidades "[indice] mm:ss texto" e escolha ATÉ 3 trechos autossuficientes '
        f'com duração aproximada entre {int(round(min_duration))} e {int(round(max_duration))} segundos.\n'
        'Critérios:\n'
        '- Ideia completa: começa com gancho forte e termina com conclusão clara.\n'
        '- O corte DEVE COMEÇAR com pergunta, afirmação forte ou dado surpreendente; '
        'NÃO começar com transições/conectores ("e galera", "então", "bom", "outra coisa", "agora", "mas", "aí", "olha só").\n'
        '- Compreensível sozinha fora de contexto.\n'
        '- Surpreendente, útil ou emocional.\n'
        '- Evite introduções ("oi pessoal"), despedidas ("se inscreve") e cortes no meio de frase.\n'
        '- NÃO selecione trechos de publicidade (patrocínio, parceria, divulgação de produto/serviço/site, cupom, "link na descrição").\n'
        '- Nunca invente números ou fatos no "gancho" (<= 10 palavras).\n'
        'Devolva APENAS JSON usando ÍNDICES inteiros das unidades (nunca timestamps):\n'
        '{"cortes":[{"inicio_unidade":int,"fim_unidade":int,"nota":1-10,"gancho":"...","motivo":"..."}]}\n\n'
        'EXEMPLO FICTÍCIO APENAS DO FORMATO (não copie texto nem números):\n'
        'Entrada:\n'
        '[0] 00:00 Oi pessoal, sejam bem-vindos ao canal.\n'
        '[1] 00:05 Por que o planeta fictício Zorblax brilha em azul?\n'
        '[2] 00:18 Pesquisadores descobriram que cristais de gelo refletem 80% da luz solar lá.\n'
        '[3] 00:34 Isso explica o fenômeno inteiro sem mistério.\n'
        'Saída:\n'
        '{"cortes":[{"inicio_unidade":1,"fim_unidade":3,"nota":9,"gancho":"Por que o planeta Zorblax brilha em azul?","motivo":"Começa com pergunta intrigante e fecha com explicação completa."}]}'
    )
    payload = _format_window_text(window_units)
    resp = _ask_with_retry(ask, instruction, payload, model=model, log=log, max_attempts=3)
    raw_cuts = resp.get('cortes')
    if not isinstance(raw_cuts, list):
        raise ValueError('Resposta do modelo sem lista "cortes".')

    parsed = []
    for item in raw_cuts[:3]:
        if not isinstance(item, dict):
            continue
        try:
            s_idx = int(item['inicio_unidade'])
            e_idx = int(item['fim_unidade'])
        except (KeyError, TypeError, ValueError):
            continue
        repaired = _repair_unit_bounds(all_units, s_idx, e_idx, min_duration, max_duration, video_duration)
        if repaired is None:
            continue
        r_start_idx, r_end_idx = repaired
        st, en = _span_with_padding(all_units, r_start_idx, r_end_idx, video_duration)
        slice_units = all_units[r_start_idx:r_end_idx + 1]
        texto = ' '.join(u['texto'] for u in slice_units)
        try:
            nota_llm = float(item.get('nota', 6.0))
        except (TypeError, ValueError):
            nota_llm = 6.0
        nota_llm = max(1.0, min(10.0, nota_llm))
        bonus, _ = heuristic_score(texto, st, en, slice_units)
        nota_final = round(min(10.0, max(1.0, nota_llm + bonus)), 2)
        gancho = validate_or_repair_hook(str(item.get('gancho', '')), texto, log=log)
        motivo = ' '.join(str(item.get('motivo', '')).split()).strip() or 'Trecho com boa unidade e gancho.'
        is_publi, publi_terms = detect_advertising(texto)
        parsed.append({
            'inicio_unidade': r_start_idx,
            'fim_unidade': r_end_idx,
            'inicio': st,
            'fim': en,
            'duracao': round(en - st, 3),
            'nota_llm': round(nota_llm, 2),
            'bonus_heuristico': bonus,
            'nota': nota_final,
            'gancho': gancho,
            'motivo': motivo,
            'texto': texto,
            'inicio_fraco': starts_with_weak_transition(texto),
            'possivel_publi': is_publi,
            'termos_publi': publi_terms,
            **({'selo_publi': 'PUBLI?'} if is_publi else {}),
        })
    return parsed


def summarize_video_map_reduce(
    windows: list[list[dict]],
    all_units: list[dict],
    candidates: list[dict],
    ask,
    model: str | None,
    avisos: list[str],
    cancel_event=None,
    log=print,
) -> dict:
    """Resumo geral map-reduce: 2 frases por janela -> 3 bullets + potencial para cortes."""
    map_instruction = (
        'Resuma em EXATAMENTE 2 frases curtas em português o assunto principal desta parte da transcrição. '
        'Responda APENAS JSON: {"resumo":"..."}'
    )
    window_summaries = []
    for idx, win in enumerate(windows, start=1):
        _check_cancelled(cancel_event)
        payload = _format_window_text(win)
        try:
            res = _ask_with_retry(ask, map_instruction, payload, model=model, log=log, max_attempts=2)
            txt = ' '.join(str(res.get('resumo', '')).split()).strip()
            if not txt:
                raise ValueError('Resumo vazio da janela.')
            window_summaries.append(f'Parte {idx}: {txt}')
        except Exception as exc:
            msg = f'Resumo da janela {idx}/{len(windows)} falhou; usando extrato local ({_short_reason(exc, 160)})'
            avisos.append(msg[:220])
            log(msg[:220])
            fallback_txt = ' '.join(u['texto'] for u in win[:2]).strip()
            if fallback_txt:
                window_summaries.append(f'Parte {idx}: {fallback_txt[:220]}')

    reduce_instruction = (
        'Com base nos resumos das partes de um vídeo, produza uma avaliação geral em português brasileiro. '
        'Responda APENAS JSON: '
        '{"resumo":["bullet 1","bullet 2","bullet 3"],"potencial_cortes":"alto|médio|baixo","justificativa_potencial":"frase curta"}'
    )
    reduce_payload = '\n'.join(window_summaries) or ' '.join(u['texto'] for u in all_units[:6])
    try:
        _check_cancelled(cancel_event)
        red = _ask_with_retry(ask, reduce_instruction, reduce_payload, model=model, log=log, max_attempts=2)
        bullets = [str(b).strip() for b in (red.get('resumo') or []) if str(b).strip()][:3]
        if not bullets:
            raise ValueError('Lista "resumo" vazia no reduce.')
        pot = str(red.get('potencial_cortes', '')).strip().casefold()
        if pot not in ('alto', 'médio', 'medio', 'baixo'):
            pot = 'médio'
        if pot == 'medio':
            pot = 'médio'
        just = ' '.join(str(red.get('justificativa_potencial', '')).split()).strip() or 'Avaliado conforme densidade de ideias e ganchos.'
        return {'resumo': bullets, 'potencial_cortes': pot, 'justificativa_potencial': just}
    except Exception as exc:
        msg = f'Resumo final map-reduce falhou; usando síntese local ({_short_reason(exc, 160)})'
        avisos.append(msg[:220])
        log(msg[:220])
        bullets = [s.split(':', 1)[-1].strip() for s in window_summaries[:3] if s.strip()]
        if not bullets:
            bullets = [' '.join(u['texto'] for u in all_units[:3])[:180]]
        top_score = max((float(c['nota']) for c in candidates), default=0.0)
        if len(candidates) >= 3 and top_score >= 7.5:
            pot = 'alto'
            just = 'Há múltiplos trechos completos com perguntas, números ou ganchos fortes.'
        elif candidates and top_score >= 5.5:
            pot = 'médio'
            just = 'Há trechos aproveitáveis, embora nem todos tenham gancho forte.'
        else:
            pot = 'baixo'
            just = 'Poucos trechos autossuficientes detectados dentro da faixa de duração.'
        return {'resumo': bullets, 'potencial_cortes': pot, 'justificativa_potencial': just}


def select_cuts_and_report(
    info: dict,
    transcription: dict,
    min_duration: float = 25.0,
    max_duration: float = 55.0,
    num_cuts: int = 5,
    ask=None,
    model: str | None = None,
    cancel_event=None,
    log=print,
) -> dict:
    """Avalia a transcrição em janelas <=650 palavras, ranqueia cortes e salva relatorio.json."""
    require_authorization(bool(info.get('autorizado')))
    _check_cancelled(cancel_event)

    min_dur = float(min_duration)
    max_dur = float(max_duration)
    if min_dur <= 0 or max_dur < min_dur:
        raise ValueError('Duração mínima e máxima inválidas para os cortes.')
    n_cuts = max(1, int(num_cuts))

    ask_fn = ask or engine.request_json
    cfg = engine.load_settings()
    eval_model = str(model or os.getenv('OLLAMA_MODEL') or cfg.get('ollama_model') or 'qwen3.5:4b').strip()
    whisper_model = str(transcription.get('modelo') or _default_whisper_model())

    units = list(transcription.get('unidades') or [])
    if not units:
        raise RuntimeError('A transcrição não contém falas reconhecidas para gerar cortes.')

    video_duration = float(transcription.get('duracao') or info.get('duracao') or (units[-1]['fim'] if units else 0.0))
    windows = build_windows(units, max_words=650, overlap_seconds=60.0)
    log(f'Avaliando conteúdo em {len(windows)} janela(s) (≤650 palavras por janela)...')

    avisos: list[str] = []
    all_candidates: list[dict] = []

    for w_idx, win in enumerate(windows, start=1):
        _check_cancelled(cancel_event)
        log(f'Analisando janela {w_idx}/{len(windows)} (unidades {win[0]["indice"]}–{win[-1]["indice"]})...')
        try:
            found = _evaluate_window_cuts(
                win, units, min_dur, max_dur, video_duration,
                ask=ask_fn, model=eval_model, log=log,
            )
            all_candidates.extend(found)
        except CancelledError:
            raise
        except Exception as exc:
            msg = f'Janela {w_idx}/{len(windows)} ignorada após erro: {_short_reason(exc, 175)}'
            avisos.append(msg[:220])
            log(msg[:220])
            continue

    if all_candidates:
        selected = select_top_candidates(all_candidates, max_cuts=n_cuts, min_distance_s=10.0)
    else:
        selected = []

    if not selected:
        msg = 'LLM não retornou cortes válidos; usando ranqueamento heurístico determinístico.'
        avisos.append(msg)
        log(msg)
        selected = fallback_heuristic_candidates(units, min_dur, max_dur, video_duration, max_cuts=n_cuts)

    _check_cancelled(cancel_event)
    summary = summarize_video_map_reduce(
        windows, units, selected, ask=ask_fn, model=eval_model,
        avisos=avisos, cancel_event=cancel_event, log=log,
    )

    auth_fields = build_auth_metadata(
        info.get('fonte_url', ''),
        info.get('canal', 'local'),
        credito=info.get('credito', ''),
        authorized=True,
    )
    report = {
        'video_id': str(info['id']),
        'titulo': info.get('titulo', info['id']),
        'duracao': video_duration,
        'resumo': summary['resumo'],
        'potencial_cortes': summary['potencial_cortes'],
        'justificativa_potencial': summary['justificativa_potencial'],
        'candidatos': selected,
        'modelos': {
            'avaliador': eval_model,
            'whisper': whisper_model,
        },
        'avisos': avisos,
        **auth_fields,
    }

    report_path = cortes_root() / str(info['id']) / 'relatorio.json'
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    log(f'Relatório salvo: {report_path} ({len(selected)} candidato(s) selecionado(s)).')
    return report


def analyze_video(
    source: str | Path,
    authorized: bool = False,
    credito: str = '',
    language: str = 'pt',
    whisper_model: str | None = None,
    min_duration: float = 25.0,
    max_duration: float = 55.0,
    num_cuts: int = 5,
    start_min: float | None = None,
    end_min: float | None = None,
    ask=None,
    eval_model: str | None = None,
    cancel_event=None,
    log=print,
) -> tuple[dict, dict, dict]:
    """Fluxo completo de análise: prepara entrada -> transcreve -> avalia e gera relatório."""
    require_authorization(authorized)
    info = prepare_source(source, authorized=True, credito=credito, log=log)
    _check_cancelled(cancel_event)
    transcription = transcribe_video(
        info,
        language=language,
        model_name=whisper_model,
        start_min=start_min,
        end_min=end_min,
        cancel_event=cancel_event,
        log=log,
    )
    _check_cancelled(cancel_event)
    report = select_cuts_and_report(
        info,
        transcription,
        min_duration=min_duration,
        max_duration=max_duration,
        num_cuts=num_cuts,
        ask=ask,
        model=eval_model,
        cancel_event=cancel_event,
        log=log,
    )
    return info, transcription, report


def main():
    engine.apply_settings()
    parser = argparse.ArgumentParser(description='ReelForge Cortes: preparar, transcrever e selecionar cortes autorizados')
    parser.add_argument('--fonte', required=True, help='URL do vídeo ou caminho de arquivo local')
    parser.add_argument('--autorizado', action='store_true', help=AUTH_CONFIRM_TEXT)
    parser.add_argument('--credito', default='', help='Texto livre de crédito (ex.: @canal)')
    parser.add_argument('--idioma', default='pt', help='Idioma da transcrição (padrão: pt)')
    parser.add_argument('--duracao-min', type=float, default=25.0, help='Duração mínima do corte em segundos')
    parser.add_argument('--duracao-max', type=float, default=55.0, help='Duração máxima do corte em segundos')
    parser.add_argument('--num-cortes', type=int, default=5, help='Quantidade máxima de cortes sugeridos')
    parser.add_argument('--inicio-min', type=float, default=None, help='Analisar a partir do minuto X')
    parser.add_argument('--fim-min', type=float, default=None, help='Analisar até o minuto Y')
    parser.add_argument('--somente-fonte', action='store_true', help='Prepara apenas download/cache e metadados')
    args = parser.parse_args()
    if args.somente_fonte:
        info = prepare_source(args.fonte, authorized=args.autorizado, credito=args.credito)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return
    _, _, report = analyze_video(
        args.fonte,
        authorized=args.autorizado,
        credito=args.credito,
        language=args.idioma,
        min_duration=args.duracao_min,
        max_duration=args.duracao_max,
        num_cuts=args.num_cortes,
        start_min=args.inicio_min,
        end_min=args.fim_min,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('ERRO:', exc, file=sys.stderr)
        sys.exit(1)
