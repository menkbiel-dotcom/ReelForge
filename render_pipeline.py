"""The app's render pipeline: voices, audio-derived captions and one actor at a time."""
import asyncio
import copy
import json
import math
import random
import os
from datetime import datetime
from pathlib import Path

import captions


def background_color(brightness, gamma):
    brightness = float(str(brightness).replace(',', '.'))
    gamma = float(str(gamma).replace(',', '.'))
    if not -0.30 <= brightness <= 0.30 or not 0.50 <= gamma <= 1.50:
        raise ValueError('Use brilho entre -0,30 e 0,30 e gama entre 0,50 e 1,50.')
    return brightness, gamma


def music_volume(percent):
    percent = float(str(percent).replace(',', '.').replace('%', ''))
    if not 0 <= percent <= 40:
        raise ValueError('Use volume da música entre 0% e 40%.')
    return percent / 100


def turns(prepared, total, speaker):
    """Janelas em que cada personagem aparece: do início da sua fala até a próxima troca.

    Falas consecutivas do mesmo personagem viram uma janela só (sem nova entrada), e a
    janela se estende pela pausa até o próximo turno, evitando quadros sem personagem.
    """
    windows = []
    for index, line in enumerate(prepared):
        end = prepared[index + 1]['start'] if index + 1 < len(prepared) else total
        if windows and windows[-1][0] == line['speaker']:
            windows[-1][2] = end
        else:
            windows.append([line['speaker'], line['start'], end])
    return [(a, b) for who, a, b in windows if who == speaker]


def render(engine, news, lines, gameplay, fps=30, max_seconds=45,
           impact_opening=True, hook_text='', direction=None):
    max_seconds = engine.validate_duration(max_seconds)
    hook_text = engine.validate_hook(hook_text)
    gameplay = Path(gameplay)
    if not gameplay.is_file():
        raise ValueError('Selecione um vídeo de fundo existente.')
    if fps not in (30, 60):
        raise ValueError('Escolha 30 ou 60 FPS.')
    engine.OUTPUT.mkdir(parents=True, exist_ok=True)
    work = engine.OUTPUT / ('work_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    work.mkdir()
    prepared = asyncio.run(engine.fit_narration(copy.deepcopy(lines), work, max_seconds, direction, news))
    print('Sincronizando legendas a partir do áudio...')
    captions.align_lines(prepared, engine.OUTPUT / 'alignment_cache')
    loop = os.getenv('REELFORGE_LOOP', '1') != '0'
    lead, tail = (.10, .15) if loop else (.25, .4)
    cursor = lead
    for line in prepared:
        line['start'] = cursor; line['end'] = cursor + line['duration']
        cursor = line['end'] + .14
    total = round((cursor - .14 + tail) * fps) / fps  # duração exata em quadros
    args = []; filters = []
    for i, line in enumerate(prepared):
        args += ['-i', line['audio']]
        delay = round(line['start'] * 1000)
        filters.append(f'[{i}:a]adelay={delay}|{delay}[a{i}]')
    filters.append(''.join(f'[a{i}]' for i in range(len(prepared))) + f'amix=inputs={len(prepared)}:normalize=0,alimiter=limit=0.95:level=false[aout]')
    dialogue = work / 'dialogue.m4a'
    engine.run(['ffmpeg', '-y', '-v', 'error', *args, '-filter_complex', ';'.join(filters),
                '-map', '[aout]', '-c:a', 'aac', '-b:a', '192k', dialogue])
    simulated = False
    info = gameplay.with_suffix(gameplay.suffix + '.json')
    if info.exists():
        simulated = json.loads(info.read_text(encoding='utf-8')).get('kind') == 'simulated'
    ass = work / 'subtitles.ass'
    provisional = any(l.get('voice_provisional') or l.get('voice_provider') == 'edge' for l in prepared)
    captions.write_ass(prepared, ass, hook_text if impact_opening else '', simulated, provisional)
    sprite_args = []
    for i in range(2):
        png = work / f'actor_{i}.png'
        engine.character(png, i, False)
        sprite_args += ['-loop', '1', '-framerate', str(fps), '-i', png]
    brightness, gamma = background_color(os.getenv('REELFORGE_BG_BRIGHTNESS', '-0.06'),
                                         os.getenv('REELFORGE_BG_GAMMA', '0.85'))
    fade = min(.6, total / 4) if loop else 0
    chain = ['[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1'
             f',eq=brightness={brightness}:gamma={gamma},fps={fps},settb=AVTB'
             + ('[bgsrc]' if loop else '[bg]')]
    if loop:
        # Loop do fundo: os primeiros quadros misturam o trecho logo após o fim com o começo,
        # então o último quadro do vídeo emenda exatamente no primeiro.
        chain += ['[bgsrc]split=2[bga][bgb]',
                  f'[bga]trim=start={total:.4f}:end={total + fade:.4f},setpts=PTS-STARTPTS[bgtail]',
                  f'[bgb]trim=start=0:end={total:.4f},setpts=PTS-STARTPTS[bghead]',
                  f'[bgtail][bghead]xfade=transition=fade:duration={fade:.4f}:offset=0[bg]']
    last = 'bg'
    for i in range(2):
        segments = turns(prepared, total, i)
        if loop and segments and segments[0][0] == prepared[0]['start']:
            segments[0] = (0.0, segments[0][1])  # primeiro personagem já entra no quadro 0
        active = '+'.join(f'gte(t,{a:.4f})*lt(t,{b:.4f})' for a, b in segments) or '0'
        side = -1 if i == 0 else 1  # Sasuke entra pela esquerda, Naruto pela direita.
        # Mola amortecida: entra pela lateral, passa ~12% do centro e assenta em ~1 s (bounce leve).
        spring = lambda amp, k, w: '+'.join(
            f'gte(t,{a:.4f})*lt(t,{a + 1:.4f})*{amp}*exp(-{k}*(t-{a:.4f}))*cos({w}*(t-{a:.4f}))'
            for a, _ in segments) or '0'
        hop = '+'.join(f'gte(t,{a:.4f})*lt(t,{a + 1:.4f})*-34*exp(-6*(t-{a:.4f}))*abs(sin(9*(t-{a:.4f})))'
                       for a, _ in segments) or '0'
        tilt = f"0.035*sin(t*{2.4 if i == 0 else 3.1})+({spring(side * 0.16, 6, 9)})"
        chain.append(f"[SPRITE{i}:v]format=rgba,rotate=angle='{tilt}':ow='rotw(0.22)':oh='roth(0.22)':c=none[actor{i}]")
        # Um personagem por vez, central, sem nomes, painéis ou barra.
        chain.append(f"[{last}][actor{i}]overlay=x='(W-w)/2+10*sin(t*2)+({spring(side * 460, 6, 9)})'"
                     f":y='300+8*sin(t*{4 if i == 0 else 6})+({hop})':enable='{active}':shortest=0[layer{i}]")
        last = f'layer{i}'
    chain.append(f"[{last}]subtitles='{engine.escape_filter_path(ass)}',scale=out_color_matrix=bt709:out_range=tv,setsar=1[vout]")
    final = engine.OUTPUT / ('reel_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.mp4')
    available = engine.ffprobe_duration(gameplay)
    latest = max(0.0, available - total - fade - 0.25)
    offset = random.uniform(0, latest) if latest else 0.0
    start = ['-ss', f'{offset:.4f}']
    print(f'Trecho sorteado: {offset:.2f} s de {available:.2f} s.')
    # Áudio: voz + música bem baixa + normalização de loudness (I=-14, TP=-1.5).
    first_sprite = 2
    audio_inputs = ['-i', dialogue]
    voice_index = 1
    chain.append(f'[{voice_index}:a]aformat=sample_rates=48000:channel_layouts=stereo,apad,'
                 f'atrim=0:{total:.4f}[voice]')
    track, music_offset, volume = None, 0.0, 0.0
    if os.getenv('REELFORGE_MUSIC', '1') != '0':
        import music
        track = music.pick(engine.ROOT)
        volume = music_volume(os.getenv('REELFORGE_MUSIC_VOLUME', '8'))
        length = engine.ffprobe_duration(track)
        music_offset = random.uniform(0, max(0.0, length - total - fade - 0.5))
        audio_inputs += ['-stream_loop', '-1', '-ss', f'{music_offset:.4f}', '-i', track]
        first_sprite = 3
        chain.append(f'[2:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={volume:.3f}'
                     + (f',asplit=2[ma][mb];[ma]atrim=start={total:.4f}:end={total + fade:.4f},asetpts=PTS-STARTPTS[mtail];'
                        f'[mb]atrim=start=0:end={total:.4f},asetpts=PTS-STARTPTS[mhead];'
                        f'[mtail][mhead]acrossfade=d={fade:.4f}:c1=tri:c2=tri[music]'
                        if loop else f',atrim=0:{total:.4f},afade=t=in:d=0.4,'
                                     f'afade=t=out:st={max(0, total - .8):.4f}:d=0.8[music]'))
        chain.append('[voice][music]amix=inputs=2:normalize=0:duration=first,loudnorm=I=-14:TP=-1.5:LRA=11[aout]')
    else:
        chain.append('[voice]loudnorm=I=-14:TP=-1.5:LRA=11[aout]')
    # Os sprites vêm depois das entradas de áudio (voz e, se ativa, música).
    chain = [c.replace('[SPRITE0:v]', f'[{first_sprite}:v]').replace('[SPRITE1:v]', f'[{first_sprite + 1}:v]')
             for c in chain]
    print('Renderizando MP4...')
    engine.run(['ffmpeg', '-y', '-v', 'error', '-stream_loop', '-1', *start,
                '-t', f'{total + fade + 0.5:.4f}', '-i', gameplay,
                *audio_inputs, *sprite_args, '-filter_complex', ';'.join(chain),
                '-map', '[vout]', '-map', '[aout]', '-t', f'{total:.4f}', '-r', fps,
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
                '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709',
                '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-movflags', '+faststart', final])
    quality_score = news.get('quality_score') if isinstance(news, dict) else None
    metadata = {'news': news, 'characters': engine.NAMES, 'lines': prepared,
                'nota': quality_score, 'quality_score': quality_score,
                'duration': total, 'fps': fps, 'resolution': [1080, 1920],
                'gameplay': str(gameplay), 'gameplay_start': offset, 'simulated_background': simulated,
                'gameplay_brightness': brightness, 'gameplay_gamma': gamma,
                'loop': loop, 'loop_crossfade': fade,
                'music': str(track) if track else None, 'music_start': music_offset, 'music_volume': volume,
                'caption_style': 'three_words_transparent_highlight', 'director': direction,
                'provisional_voices': provisional}
    final.with_suffix('.json').write_text(json.dumps(metadata, default=str, ensure_ascii=False, indent=2), encoding='utf-8')
    engine.mark_used(news)
    print('Vídeo salvo: ' + str(final))
    return final
