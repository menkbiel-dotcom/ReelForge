"""Modo Cortes do ReelForge: entrada, download com yt-dlp, transcrição e seleção de cortes."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import engine
import media_tools

AUTH_CONFIRM_TEXT = 'Confirmo que tenho autorização para cortar este vídeo'
DEFAULT_YTDLP_META_TIMEOUT_S = 60
DEFAULT_YTDLP_DOWNLOAD_TIMEOUT_S = 600

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
