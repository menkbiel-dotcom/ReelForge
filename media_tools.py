"""Small FFmpeg boundary shared by speech, preview and rendering."""
import math
import os
import re
import shutil
import subprocess
from pathlib import Path


def ffmpeg():
    configured = os.getenv('REELFORGE_FFMPEG')
    if configured:
        return configured
    found = shutil.which('ffmpeg')
    if found:
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def run(args):
    args = list(map(str, args))
    if args[0] == 'ffmpeg':
        args[0] = ffmpeg()
    p = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if p.returncode:
        raise RuntimeError('Falha no processamento de mídia: ' + p.stderr[-1800:])
    return p.stdout.strip()


def duration(path):
    p = subprocess.run([ffmpeg(), '-hide_banner', '-i', str(path)],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    match = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', p.stderr)
    if not match:
        raise ValueError('Não foi possível medir o áudio/vídeo: ' + Path(path).name)
    hours, minutes, seconds = map(float, match.groups())
    value = hours * 3600 + minutes * 60 + seconds
    if not math.isfinite(value) or value <= 0:
        raise ValueError('Duração inválida.')
    return value


def validate_audio(path):
    value = duration(path)
    run(['ffmpeg', '-v', 'error', '-i', path, '-map', '0:a:0', '-f', 'null', '-'])
    return value
