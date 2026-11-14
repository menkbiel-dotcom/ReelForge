"""Renderização do Modo Cortes: 1080x1920 @30fps (corte em cima + gameplay embaixo + legenda .ass)."""
import json
import math
import os
import random
import re
import unicodedata
from datetime import datetime
from pathlib import Path

import clipper
import engine


def _background_color(brightness, gamma):
    brightness = float(str(brightness).replace(',', '.'))
    gamma = float(str(gamma).replace(',', '.'))
    if not -0.30 <= brightness <= 0.30 or not 0.50 <= gamma <= 1.50:
        raise ValueError('Use brilho entre -0,30 e 0,30 e gama entre 0,50 e 1,50.')
    return brightness, gamma


def _music_volume(percent, max_percent: float = 100.0):
    percent = float(str(percent).replace(',', '.').replace('%', ''))
    if not 0 <= percent <= max_percent:
        raise ValueError(f'Use volume da música entre 0% e {int(max_percent)}%.')
    return percent / 100.0


def _ass_time(t: float) -> str:
    cs = max(0, round(float(t) * 100))
    return f'{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}'


def _ass_escape(text: str) -> str:
    return str(text).replace('\\', '／').replace('{', '(').replace('}', ')').replace('\n', ' ')


def _spring_scale(time_seconds: float, duration: float = 0.18) -> float:
    if time_seconds >= duration:
        return 1.0
    progress = max(0.0, time_seconds / duration)
    return max(0.70, min(1.18, 1.0 - math.exp(-6 * progress) * math.cos(2.5 * math.pi * progress)))


def _word_groups(words: list[dict]):
    from PIL import ImageFont
    candidates = ['C:/Windows/Fonts/arialbd.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf']
    font_path = next((x for x in candidates if Path(x).exists()), None)
    font = ImageFont.truetype(font_path, 77) if font_path else ImageFont.load_default()
    group = []
    for word in words:
        candidate = group + [word]
        text = ' '.join(w['text'].upper() for w in candidate)
        if group and (len(candidate) > 3 or font.getlength(text) > 890):
            yield group
            group = []
        group.append(word)
    if group:
        yield group


def slugify_title(text: str, max_len: int = 36) -> str:
    norm = unicodedata.normalize('NFKD', str(text or '').casefold())
    ascii_txt = ''.join(c for c in norm if not unicodedata.combining(c))
    slug = re.sub(r'[^a-z0-9]+', '_', ascii_txt).strip('_')
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip('_')
    return slug or 'corte'


def slice_transcription_words(transcription: dict, start: float, end: float) -> list[dict]:
    """Extrai e desloca palavras da transcrição existente para o intervalo [0, end - start] sem rodar Whisper."""
    raw_words = list(transcription.get('palavras') or [])
    if not raw_words:
        for unit in transcription.get('unidades') or []:
            raw_words.extend(unit.get('palavras') or [])

    total = max(0.05, float(end) - float(start))
    shifted = []
    for w in raw_words:
        txt = str(w.get('texto', w.get('text', ''))).strip()
        if not txt:
            continue
        w_start = float(w.get('inicio', w.get('start', 0.0)))
        w_end = float(w.get('fim', w.get('end', w_start)))
        if w_end <= start or w_start >= end:
            continue
        rel_start = max(0.0, min(total - 0.02, w_start - start))
        rel_end = max(rel_start + 0.02, min(total, w_end - start))
        shifted.append({'text': txt, 'start': round(rel_start, 3), 'end': round(rel_end, 3)})

    # Garante ordem cronológica monotônica
    prev = 0.0
    for w in shifted:
        w['start'] = max(prev, w['start'])
        w['end'] = max(w['start'] + 0.02, min(total, w['end']))
        if w['end'] <= w['start']:
            w['end'] = w['start'] + 0.01
        prev = w['end']
    return shifted


def write_clip_ass(
    words: list[dict],
    duration: float,
    dest_path: Path,
    hook_text: str = '',
    credito: str = '',
    posicao_legenda: str = 'juncao',
    titulo_na_tela: bool = True,
    credito_na_tela: bool = True,
    simulated_gameplay: bool = False,
) -> Path:
    """Gera arquivo .ass com estilo do ReelForge (máx. 3 palavras, contorno preto, palavra ativa destacada)."""
    pos_key = str(posicao_legenda or 'juncao').strip().casefold()
    y_pos = 960 if pos_key in ('juncao', 'junção', 'centro') else 1650

    header = '''[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,77,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,5,80,80,0,1
Style: Hook,Arial,52,&H00FFFFFF,&H00FFFFFF,&H00120C08,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,8,70,70,65,1
Style: Credit,Arial,28,&H33FFFFFF,&H33FFFFFF,&H33000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,9,32,36,28,1
Style: Test,Arial,27,&H00D0E15D,&H00D0E15D,&H00120C08,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,50,50,100,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    events = []
    total = max(0.1, float(duration))

    if titulo_na_tela and str(hook_text or '').strip():
        hook_end = min(3.0, total)
        events.append(
            f'Dialogue: 1,0:00:00.00,{_ass_time(hook_end)},Hook,,0,0,0,,'
            f'{{\\fad(60,180)}}{_ass_escape(str(hook_text).strip().upper())}'
        )

    if credito_na_tela and str(credito or '').strip():
        events.append(
            f'Dialogue: 2,0:00:00.00,{_ass_time(total)},Credit,,0,0,0,,'
            f'{_ass_escape(str(credito).strip())}'
        )

    if simulated_gameplay:
        events.append(
            f'Dialogue: 3,0:00:00.00,{_ass_time(total)},Test,,0,0,0,,'
            'FUNDO DE TESTE · PARKOUR SIMULADO'
        )

    for group in _word_groups(words):
        phrase_start = group[0]['start']
        phrase_end = group[-1]['end']
        width_chars = max(len(w['text']) for w in group)
        size_tag = r'\fs58' if width_chars > 16 else ''
        boundaries = sorted({phrase_start, phrase_end} | {w[k] for w in group for k in ('start', 'end')})
        for st, en in zip(boundaries, boundaries[1:]):
            if en - st < 0.009:
                continue
            mid = (st + en) / 2.0
            rendered_words = []
            for w in group:
                color = r'{\c&H002DDCFF&}' if w['start'] <= mid < w['end'] else r'{\c&H00FFFFFF&}'
                rendered_words.append(color + _ass_escape(w['text'].upper()))
            text = ' '.join(rendered_words)
            scale = round(_spring_scale(st - phrase_start) * 100)
            remaining = round(max(0.0, 0.18 - (st - phrase_start)) * 1000)
            tags = f'{{\\an5\\pos(540,{y_pos}){size_tag}'
            if remaining:
                tags += f'\\fscx{scale}\\fscy{scale}\\t(0,{remaining},\\fscx100\\fscy100)'
            tags += '}'
            events.append(f'Dialogue: 0,{_ass_time(st)},{_ass_time(en)},Default,,0,0,0,,{tags}{text}')

    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(header + '\n'.join(events) + '\n', encoding='utf-8')
    return dest_path


def detect_face_crop_x(
    source_path: str | Path,
    start: float,
    end: float,
    num_samples: int = 5,
    log=print,
) -> float:
    """Amostra ~5 frames do corte e retorna a mediana do centro X normalizado (0.0..1.0) dos rostos via OpenCV."""
    try:
        import cv2
    except ImportError:
        log('Aviso: opencv-python-headless não instalado; usando crop_x=0.5 (centro).')
        return 0.5

    try:
        cascade_dir = getattr(getattr(cv2, 'data', None), 'haarcascades', '') or ''
        cascade_file = os.path.join(cascade_dir, 'haarcascade_frontalface_default.xml')
        classifier = cv2.CascadeClassifier(cascade_file)
        if classifier.empty():
            log('Aviso: classificador Haar de rosto do OpenCV indisponível; usando crop_x=0.5.')
            return 0.5

        cap = cv2.VideoCapture(str(source_path))
        if not cap.isOpened():
            log('Aviso: não foi possível abrir o vídeo no OpenCV para enquadramento; usando crop_x=0.5.')
            return 0.5
        try:
            duration = max(0.0, float(end) - float(start))
            n = max(1, int(num_samples))
            centers_x: list[float] = []
            for i in range(n):
                ts = float(start) + (duration * (i + 0.5) / n if duration > 0 else 0.0)
                cap.set(cv2.CAP_PROP_POS_MSEC, ts * 1000.0)
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                shape = getattr(frame, 'shape', None)
                if not shape or len(shape) < 2 or shape[1] <= 0:
                    continue
                frame_w = float(shape[1])
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = classifier.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))
                if faces is not None and len(faces) > 0:
                    for (fx, _fy, fw, _fh) in faces:
                        cx = (float(fx) + float(fw) / 2.0) / frame_w
                        centers_x.append(max(0.0, min(1.0, cx)))
            if not centers_x:
                return 0.5
            centers_x.sort()
            mid = len(centers_x) // 2
            if len(centers_x) % 2 == 1:
                med = centers_x[mid]
            else:
                med = (centers_x[mid - 1] + centers_x[mid]) / 2.0
            return round(max(0.0, min(1.0, float(med))), 4)
        finally:
            cap.release()
    except Exception as exc:
        log(f'Aviso: falha na detecção facial OpenCV ({exc}); usando crop_x=0.5.')
        return 0.5


def render_clip(
    info: dict,
    transcription: dict,
    candidate: dict,
    gameplay: str | Path | None = None,
    cut_index: int = 1,
    modo_corte: str = 'preencher',
    posicao_legenda: str = 'juncao',
    titulo_na_tela: bool = True,
    credito_na_tela: bool = True,
    musica_ativa: bool = False,
    musica_modo: str | None = None,
    musica_volume: float | None = None,
    impacto_inicial: str | None = None,
    impacto_volume: float | None = None,
    crop_x: float | None = None,
    enquadramento: str | None = None,
    modelos: dict | None = None,
    log=print,
) -> Path:
    """Renderiza um corte 1080x1920 @30fps com corte em cima, gameplay muda embaixo e legenda .ass."""
    clipper.require_authorization(bool(info.get('autorizado')))

    mode = str(modo_corte or 'preencher').strip().casefold()
    if mode not in ('ajustar', 'preencher'):
        raise ValueError('Modo de corte inválido. Use "ajustar" ou "preencher".')

    pos_leg = str(posicao_legenda or 'juncao').strip().casefold()
    if pos_leg not in ('juncao', 'junção', 'rodape', 'rodapé'):
        raise ValueError('Posição de legenda inválida. Use "juncao" ou "rodape".')
    pos_leg = 'juncao' if pos_leg in ('juncao', 'junção') else 'rodape'

    video_id = str(info['id'])
    out_dir = clipper.cortes_root() / video_id
    out_dir.mkdir(parents=True, exist_ok=True)

    source_path = Path(info.get('source_path') or (out_dir / 'source.mp4'))
    if not source_path.is_file():
        raise FileNotFoundError(f'Vídeo de origem não encontrado: {source_path}')

    gameplay_path = Path(gameplay) if gameplay else next(iter(engine.gameplays()), None)
    if gameplay_path is None or not gameplay_path.is_file():
        raise ValueError('Selecione um vídeo de gameplay existente ou adicione um MP4 em assets/gameplay/.')

    start = max(0.0, float(candidate['inicio']))
    end = float(candidate['fim'])
    if end <= start:
        raise ValueError('O fim do corte deve ser maior que o início.')

    fps = 30
    total = round((end - start) * fps) / fps
    total = max(0.5, total)

    slug = slugify_title(candidate.get('gancho') or candidate.get('texto') or f'corte_{cut_index}')
    stem = f'corte_{int(cut_index):02d}_{slug}'
    final_mp4 = out_dir / f'{stem}.mp4'
    final_json = out_dir / f'{stem}.json'
    work_dir = out_dir / f'_work_{stem}_{datetime.now().strftime("%H%M%S_%f")}'
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        words = slice_transcription_words(transcription, start, start + total)
        sim_info = gameplay_path.with_suffix(gameplay_path.suffix + '.json')
        simulated = False
        if sim_info.is_file():
            try:
                simulated = json.loads(sim_info.read_text(encoding='utf-8')).get('kind') == 'simulated'
            except (OSError, ValueError):
                simulated = False

        credito_val = str(info.get('credito') or '').strip()
        hook_val = str(candidate.get('gancho') or '').strip()
        ass_path = work_dir / 'subtitles.ass'
        write_clip_ass(
            words,
            total,
            ass_path,
            hook_text=hook_val,
            credito=credito_val,
            posicao_legenda=pos_leg,
            titulo_na_tela=bool(titulo_na_tela),
            credito_na_tela=bool(credito_na_tela),
            simulated_gameplay=simulated,
        )

        # Configura gameplay sem crossfade de loop (corte direto 0..total com -stream_loop -1)
        cfg = engine.load_settings()
        brightness, gamma = _background_color(
            os.getenv('REELFORGE_BG_BRIGHTNESS', str(cfg.get('gameplay_brightness', -0.06))),
            os.getenv('REELFORGE_BG_GAMMA', str(cfg.get('gameplay_gamma', 0.85))),
        )
        available = engine.ffprobe_duration(gameplay_path)
        latest = max(0.0, available - total - 0.25)
        offset = random.uniform(0.0, latest) if latest else 0.0
        log(f'Gameplay {gameplay_path.name}: trecho {offset:.2f}s de {available:.2f}s (áudio mudo).')

        chain = []
        eff_crop_x = 0.5
        if mode == 'ajustar':
            chain.append(
                f'[0:v]setpts=PTS-STARTPTS,fps={fps},split=2[top_a][top_b];'
                '[top_a]scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960,'
                'boxblur=20:5,eq=brightness=-0.12,setsar=1[topbg];'
                '[top_b]scale=1080:960:force_original_aspect_ratio=decrease,setsar=1[topfg];'
                '[topbg][topfg]overlay=(W-w)/2:(H-h)/2[top]'
            )
        else:
            if crop_x is not None:
                eff_crop_x = max(0.0, min(1.0, float(crop_x)))
            else:
                cortes_cfg = cfg.get('cortes') if isinstance(cfg.get('cortes'), dict) else {}
                enq = str(enquadramento if enquadramento is not None else cortes_cfg.get('enquadramento', 'auto')).strip().casefold()
                if enq == 'auto':
                    eff_crop_x = detect_face_crop_x(source_path, start, start + total, log=log)
                else:
                    eff_crop_x = 0.5
            chain.append(
                f'[0:v]setpts=PTS-STARTPTS,fps={fps},'
                f'scale=1080:960:force_original_aspect_ratio=increase,'
                f'crop=1080:960:(in_w-1080)*{eff_crop_x:.4f}:(in_h-960)/2,setsar=1[top]'
            )

        chain.append(
            '[1:v]scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960,setsar=1,'
            f'eq=brightness={brightness}:gamma={gamma},vignette=PI/6,fps={fps},settb=AVTB,'
            f'trim=start=0:end={total:.4f},setpts=PTS-STARTPTS[bottom]'
        )

        chain.append('[top][bottom]vstack=inputs=2[stacked]')
        chain.append(
            f"[stacked]subtitles='{engine.escape_filter_path(ass_path)}',"
            'scale=out_color_matrix=bt709:out_range=tv,setsar=1[vout]'
        )

        # Áudio do corte + música ('desligada'|'baixa'|'impacto') + impacto inicial ('nenhum'|'impacto'|'whoosh')
        if musica_modo is not None and str(musica_modo).strip():
            eff_music_mode = str(musica_modo).strip().casefold()
        else:
            eff_music_mode = 'baixa' if bool(musica_ativa) else 'desligada'
        if eff_music_mode not in ('desligada', 'baixa', 'impacto'):
            raise ValueError('Modo de música inválido. Use "desligada", "baixa" ou "impacto".')

        if impacto_inicial is not None and str(impacto_inicial).strip():
            eff_impacto = str(impacto_inicial).strip().casefold()
        else:
            eff_impacto = 'impacto' if eff_music_mode == 'impacto' else 'nenhum'
        if eff_impacto not in ('nenhum', 'impacto', 'whoosh'):
            raise ValueError('Impacto inicial inválido. Use "nenhum", "impacto" ou "whoosh".')

        cortes_cfg = cfg.get('cortes') if isinstance(cfg.get('cortes'), dict) else {}
        extra_inputs = []
        next_input_idx = 2
        track = None
        music_offset = 0.0
        volume = 0.0
        music_input_idx = None

        if eff_music_mode == 'baixa':
            import music
            track = music.pick(engine.ROOT)
            raw_vol = musica_volume if musica_volume is not None else os.getenv('REELFORGE_MUSIC_VOLUME', str(cfg.get('music_volume', 8)))
            volume = _music_volume(raw_vol, max_percent=100.0)
            length = engine.ffprobe_duration(track)
            music_offset = random.uniform(0.0, max(0.0, length - total - 0.5))
            music_input_idx = next_input_idx
            next_input_idx += 1
            extra_inputs += ['-stream_loop', '-1', '-ss', f'{music_offset:.4f}', '-i', track]
        elif eff_music_mode == 'impacto':
            import music_cortes
            track, music_offset = music_cortes.pick_for_clip(engine.ROOT, total, modo='impacto')
            raw_vol = musica_volume if musica_volume is not None else cortes_cfg.get('musica_volume', 22)
            volume = _music_volume(raw_vol, max_percent=100.0)
            music_input_idx = next_input_idx
            next_input_idx += 1
            extra_inputs += ['-stream_loop', '-1', '-ss', f'{music_offset:.4f}', '-i', track]

        sfx_path = None
        sfx_input_idx = None
        imp_vol_pct = float(impacto_volume if impacto_volume is not None else cortes_cfg.get('impacto_volume', 70))
        imp_gain = _music_volume(imp_vol_pct, max_percent=100.0)
        if eff_impacto in ('impacto', 'whoosh'):
            import sfx
            sfx_path = sfx.ensure_sfx(eff_impacto, root=engine.ROOT)
            if sfx_path is not None:
                sfx_input_idx = next_input_idx
                next_input_idx += 1
                extra_inputs += ['-i', sfx_path]

        if eff_music_mode == 'impacto':
            chain.append(
                f'[0:a]asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,'
                f'apad,atrim=0:{total:.4f},asplit=2[clipaudio][sc]'
            )
            chain.append(
                f'[{music_input_idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={volume:.3f},'
                f'atrim=0:{total:.4f},asetpts=PTS-STARTPTS,adelay=0|0,'
                f'afade=t=in:d=0.05,afade=t=out:st={max(0.0, total - 0.8):.4f}:d=0.8[musicraw];'
                f'[musicraw][sc]sidechaincompress=threshold=0.04:ratio=10:attack=5:release=300[music]'
            )
            if sfx_input_idx is not None:
                chain.append(
                    f'[{sfx_input_idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={imp_gain:.3f},'
                    f'adelay=0|0,apad,atrim=0:{total:.4f}[sfx]'
                )
                chain.append(
                    '[clipaudio][music][sfx]amix=inputs=3:normalize=0:duration=first,'
                    'alimiter=limit=0.95,loudnorm=I=-14:TP=-1.5:LRA=11[aout]'
                )
            else:
                chain.append(
                    '[clipaudio][music]amix=inputs=2:normalize=0:duration=first,'
                    'alimiter=limit=0.95,loudnorm=I=-14:TP=-1.5:LRA=11[aout]'
                )
        elif eff_music_mode == 'baixa':
            chain.append(
                f'[0:a]asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,'
                f'apad,atrim=0:{total:.4f}[clipaudio]'
            )
            chain.append(
                f'[{music_input_idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={volume:.3f},'
                f'atrim=0:{total:.4f},afade=t=in:d=0.4,afade=t=out:st={max(0.0, total - 0.8):.4f}:d=0.8[music]'
            )
            if sfx_input_idx is not None:
                chain.append(
                    f'[{sfx_input_idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={imp_gain:.3f},'
                    f'adelay=0|0,apad,atrim=0:{total:.4f}[sfx]'
                )
                chain.append(
                    '[clipaudio][music][sfx]amix=inputs=3:normalize=0:duration=first,'
                    'alimiter=limit=0.95,loudnorm=I=-14:TP=-1.5:LRA=11[aout]'
                )
            else:
                chain.append('[clipaudio][music]amix=inputs=2:normalize=0:duration=first,loudnorm=I=-14:TP=-1.5:LRA=11[aout]')
        else:
            chain.append(
                f'[0:a]asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,'
                f'apad,atrim=0:{total:.4f}[clipaudio]'
            )
            if sfx_input_idx is not None:
                chain.append(
                    f'[{sfx_input_idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={imp_gain:.3f},'
                    f'adelay=0|0,apad,atrim=0:{total:.4f}[sfx]'
                )
                chain.append(
                    '[clipaudio][sfx]amix=inputs=2:normalize=0:duration=first,'
                    'alimiter=limit=0.95,loudnorm=I=-14:TP=-1.5:LRA=11[aout]'
                )
            else:
                chain.append('[clipaudio]loudnorm=I=-14:TP=-1.5:LRA=11[aout]')

        log(f'Renderizando corte {int(cut_index):02d} ({start:.2f}s–{start + total:.2f}s)...')
        ffmpeg_cmd = [
            'ffmpeg', '-y', '-v', 'error', '-threads', '0',
            '-ss', f'{start:.4f}', '-t', f'{total:.4f}', '-i', source_path,
            '-stream_loop', '-1', '-ss', f'{offset:.4f}', '-t', f'{total + 0.5:.4f}', '-i', gameplay_path,
            *extra_inputs,
            '-filter_complex', ';'.join(chain),
            '-map', '[vout]', '-map', '[aout]',
            '-t', f'{total:.4f}', '-r', str(fps),
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
            '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709',
            '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-movflags', '+faststart',
            final_mp4,
        ]
        engine.run(ffmpeg_cmd)

        auth_fields = clipper.build_auth_metadata(
            info.get('fonte_url', ''),
            info.get('canal', 'local'),
            credito=credito_val,
            authorized=True,
        )
        metadata = {
            'video_id': video_id,
            'titulo': info.get('titulo', video_id),
            'indice': int(cut_index),
            'inicio': start,
            'fim': round(start + total, 3),
            'duracao': total,
            'nota': candidate.get('nota'),
            'nota_llm': candidate.get('nota_llm'),
            'bonus_heuristico': candidate.get('bonus_heuristico'),
            'gancho': hook_val,
            'motivo': candidate.get('motivo', ''),
            'texto': candidate.get('texto', ''),
            'modo_corte': mode,
            'crop_x': eff_crop_x,
            'posicao_legenda': pos_leg,
            'titulo_na_tela': bool(titulo_na_tela),
            'credito_na_tela': bool(credito_na_tela),
            'gameplay': str(gameplay_path),
            'gameplay_start': offset,
            'musica_modo': eff_music_mode,
            'faixa': str(track) if track else None,
            'start': round(float(music_offset), 3),
            'impacto_inicial': eff_impacto,
            'impacto_volume': round(float(imp_vol_pct), 1),
            'music': str(track) if track else None,
            'music_start': music_offset,
            'music_volume': volume,
            'modelos': modelos or {
                'avaliador': engine.load_settings().get('ollama_model', 'qwen3.5:4b'),
                'whisper': transcription.get('modelo', 'small'),
            },
            'arquivo_mp4': str(final_mp4),
            **auth_fields,
        }
        final_json.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        log(f'Corte salvo: {final_mp4}')
        return final_mp4
    finally:
        # Limpa pasta temporária mantendo MP4, JSON e eventuais depurações limpas
        for p in sorted(work_dir.glob('*'), reverse=True):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            work_dir.rmdir()
        except OSError:
            pass
