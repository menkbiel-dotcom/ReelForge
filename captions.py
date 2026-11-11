"""Audio-derived word timing and transparent kinetic ASS captions.

The Whisper pipeline and damped spring animation adapt ideas from
MoneyPrinterTurbo (Harry, MIT); see THIRD_PARTY_NOTICES.md.
"""
import difflib
import hashlib
import json
import math
import os
import re
import threading
import unicodedata
from pathlib import Path

_models = {}
_lock = threading.Lock()


def normalized(word):
    return ''.join(c for c in unicodedata.normalize('NFKD', word).casefold() if c.isalnum())


def estimated(text, duration):
    words = text.split(); weights = [max(2, len(w)) for w in words]
    total = sum(weights); cursor = 0; result = []
    for word, weight in zip(words, weights):
        result.append({'text': word, 'start': duration * cursor / total,
                       'end': duration * (cursor + weight) / total, 'estimated': True})
        cursor += weight
    return result


def _clean_recognized(recognized, duration):
    """Remove eco de prompt no silêncio final (tokens com start == end) e valida tempos."""
    cleaned = []
    for raw in recognized:
        start, end = float(raw['start']), float(raw['end'])
        if not math.isfinite(start + end) or start < 0 or end < start or start > duration + .5:
            raise ValueError('Tempos por palavra inválidos no áudio.')
        if end <= start:
            # O Whisper às vezes repete o initial_prompt no silêncio final com duração zero:
            # corta o eco inteiro (inclusive as 1-2 palavras que iniciaram a repetição).
            for back in range(1, min(4, len(cleaned)) + 1):
                prefix = [normalized(x['text']) for x in cleaned[:back]]
                tail = [normalized(x['text']) for x in cleaned[-back:]]
                if prefix == tail and len(cleaned) >= back * 2:
                    cleaned = cleaned[:-back]
                    break
            break
        if cleaned and re.fullmatch(r'[,.]\d+\W*', raw['text'].strip()) and re.search(r'\d$', cleaned[-1]['text']):
            cleaned[-1]['text'] += raw['text'].strip()
            cleaned[-1]['end'] = min(max(cleaned[-1]['start'] + .01, end), duration)
            continue
        cleaned.append({'text': raw['text'], 'start': min(start, duration),
                        'end': min(max(start + .01, end), duration)})
    return cleaned


def _merge_split_words(expected_norm, recognized):
    """Une pares de tokens que o Whisper separou (ex.: 'gel'+'política' -> 'geopolítica', 'Por'+'que' -> 'Porque')."""
    merged = []
    i = 0
    while i < len(recognized):
        if i + 1 < len(recognized):
            w1, w2 = normalized(recognized[i]['text']), normalized(recognized[i + 1]['text'])
            pair = w1 + w2
            if pair in expected_norm and (w1 not in expected_norm or w2 not in expected_norm):
                merged.append({'text': pair, 'start': recognized[i]['start'], 'end': recognized[i + 1]['end']})
                i += 2
                continue
            if pair and w1 not in expected_norm and w2 not in expected_norm:
                best = max((w for w in expected_norm if len(w) >= 4),
                           key=lambda w: difflib.SequenceMatcher(a=w, b=pair).ratio(), default='')
                if best and difflib.SequenceMatcher(a=best, b=pair).ratio() >= .74:
                    merged.append({'text': best, 'start': recognized[i]['start'], 'end': recognized[i + 1]['end']})
                    i += 2
                    continue
        merged.append(recognized[i])
        i += 1
    return merged


def match_words(text, recognized, duration, min_ratio=.65):
    """Preserve the approved script; explicitly mark any interpolated words."""
    expected = text.split()
    if recognized and all(float(r['end']) <= float(r['start']) for r in recognized if math.isfinite(float(r['start']) + float(r['end']))):
        raise ValueError('Tempos por palavra inválidos no áudio.')
    recognized = _clean_recognized(recognized, duration)
    a = [normalized(x) for x in expected]
    recognized = _merge_split_words(a, recognized)
    b = [normalized(x['text']) for x in recognized]
    # Trata palavras muito parecidas (ex.: "croissant" vs "croissão") como equivalentes no alinhamento.
    for j, word_b in enumerate(b):
        if word_b and word_b not in a:
            best = max((w for w in a if w), key=lambda w: difflib.SequenceMatcher(a=w, b=word_b).ratio(), default='')
            if best and difflib.SequenceMatcher(a=best, b=word_b).ratio() >= .65:
                b[j] = best
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    matched = {}
    for block in matcher.get_matching_blocks():
        for i in range(block.size):
            raw = recognized[block.b + i]
            start, end = float(raw['start']), float(raw['end'])
            if not math.isfinite(start + end) or not 0 <= start < end <= duration + .25:
                raise ValueError('Tempos por palavra inválidos no áudio.')
            matched[block.a + i] = {'text': expected[block.a + i], 'start': min(start, duration),
                                      'end': min(end, duration), 'estimated': False}
    if not expected or len(matched) / len(expected) < min_ratio:
        raise ValueError('O áudio não corresponde suficientemente ao roteiro. Revise a gravação ou escolha legendas aproximadas explicitamente.')
    result = []
    i = 0
    while i < len(expected):
        if i in matched:
            result.append(matched[i]); i += 1; continue
        first = i
        while i < len(expected) and i not in matched:
            i += 1
        start = result[-1]['end'] if result else 0
        end = matched[i]['start'] if i in matched else duration
        # Collapsed gaps cannot be claimed as measured word timings.
        gap = max(.001, end - start)
        for j, item in enumerate(estimated(' '.join(expected[first:i]), gap)):
            item.update(start=min(duration, start + item['start']), end=min(duration, start + item['end']))
            result.append(item)
    previous = 0
    for word in result:
        word['start'] = max(previous, word['start'])
        word['end'] = max(word['start'], word['end'])
        previous = word['end']
    return result


def whisper_words(audio, text, model_name, cache):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError('Instale as legendas por áudio usando instalar_legendas.bat, ou selecione o modo Aproximado.') from None
    cache = Path(cache); cache.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(Path(audio).read_bytes() + (model_name + ':v2').encode()).hexdigest()
    dest = cache / (digest + '.json')
    if dest.exists():
        return json.loads(dest.read_text(encoding='utf-8'))
    with _lock:
        if model_name not in _models:
            _models[model_name] = WhisperModel(model_name, device='cpu', compute_type='int8',
                                             download_root=str(cache / 'models'), local_files_only=True)
        segments, _ = _models[model_name].transcribe(str(audio), language='pt',
                                                    beam_size=5, word_timestamps=True,
                                                    vad_filter=False, condition_on_previous_text=False)
        words = [{'text': w.word.strip(), 'start': w.start, 'end': w.end}
                 for segment in segments for w in (segment.words or []) if w.word.strip() and w.end > w.start]
    dest.write_text(json.dumps(words, ensure_ascii=False), encoding='utf-8')
    return words


def align_lines(lines, cache, mode=None, model_name=None):
    mode = mode or os.getenv('CAPTION_TIMING', 'auto')
    model_name = model_name or os.getenv('WHISPER_MODEL', 'small')
    if mode not in ('auto', 'whisper', 'estimated'):
        raise ValueError('Modo de sincronização desconhecido.')
    for line in lines:
        if mode == 'estimated':
            line['words'] = estimated(line['text'], line['duration'])
            line['timing_source'] = 'estimated'
            continue
        if mode == 'auto' and line.get('timing_source') == 'edge_word_boundary' and line.get('words'):
            raw = line['words']; source = 'edge_word_boundary'
        else:
            raw = whisper_words(line['audio'], line['text'], model_name, cache)
            source = 'faster_whisper:' + model_name
        min_ratio = .65 if line.get('audio_source') == 'imported' else 0.0
        line['words'] = match_words(line['text'], raw, line['duration'], min_ratio=min_ratio)
        count = sum(bool(w.get('estimated')) for w in line['words'])
        line['timing_source'] = source + ('+interpolated' if count else '')
        line['interpolated_words'] = count
        print(f"Legendas: {source}; {count} palavra(s) interpolada(s).")


def ass_time(t):
    cs = round(t * 100)
    return f'{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}'


def ass_escape(text):
    # Never allow a script or model output to insert ASS override tags.
    return text.replace('\\', '／').replace('{', '(').replace('}', ')').replace('\n', ' ')


def spring_scale(time_seconds, duration=.18):
    if time_seconds >= duration:
        return 1.0
    progress = max(0, time_seconds / duration)
    return max(.70, min(1.18, 1 - math.exp(-6 * progress) * math.cos(2.5 * math.pi * progress)))


def _groups(words):
    # Width is measured with the same font family used for ASS rendering.
    from PIL import ImageFont
    candidates = ['C:/Windows/Fonts/arialbd.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf']
    font_path = next((x for x in candidates if Path(x).exists()), None)
    font = ImageFont.truetype(font_path, 77) if font_path else ImageFont.load_default()
    group = []
    for word in words:
        candidate = group + [word]
        text = ' '.join(w['text'].upper() for w in candidate)
        if group and (len(candidate) > 3 or font.getlength(text) > 890):
            yield group; group = []
        group.append(word)
    if group:
        yield group


def write_ass(lines, path, hook_text='', simulated=False, voice_notice=False):
    header = '''[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,77,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,5,80,80,0,1
Style: Hook,Arial,53,&H00FFFFFF,&H00FFFFFF,&H00120C08,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,8,80,80,80,1
Style: Test,Arial,27,&H00D0E15D,&H00D0E15D,&H00120C08,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,50,50,100,1
Style: Notice,Arial,27,&H00D0E15D,&H00D0E15D,&H00120C08,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,8,50,50,175,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    events = []
    final = max(l['end'] for l in lines) + .5
    if hook_text:
        events.append(f'Dialogue: 1,0:00:00.00,0:00:03.00,Hook,,0,0,0,,{{\\fad(60,160)}}{ass_escape(hook_text.upper())}')
    if simulated:
        events.append(f'Dialogue: 2,0:00:00.00,{ass_time(final)},Test,,0,0,0,,FUNDO DE TESTE · PARKOUR SIMULADO')
    if voice_notice:
        events.append(f'Dialogue: 3,0:00:00.00,{ass_time(final)},Notice,,0,0,0,,PRÉVIA · VOZES PROVISÓRIAS')
    for line in lines:
        for group in _groups(line['words']):
            # Separate events permit real highlight timing without ASS karaoke estimates.
            phrase_start = line['start'] + group[0]['start']
            phrase_end = line['start'] + group[-1]['end']
            width_chars = max(len(w['text']) for w in group)
            size_tag = r'\fs58' if width_chars > 16 else ''
            boundaries = sorted({phrase_start, phrase_end} | {
                line['start'] + w[key] for w in group for key in ('start', 'end')})
            for start, end in zip(boundaries, boundaries[1:]):
                if end - start < .009:
                    continue
                mid = (start + end) / 2 - line['start']
                text = ' '.join((r'{\c&H002DDCFF&}' if w['start'] <= mid < w['end'] else r'{\c&H00FFFFFF&}') + ass_escape(w['text'].upper()) for w in group)
                # Spring's elapsed time is relative to phrase onset, not each word.
                scale = round(spring_scale(start - phrase_start) * 100)
                remaining = round(max(0, .18 - (start - phrase_start)) * 1000)
                tags = r'{\an5\pos(540,1240)' + size_tag
                if remaining:
                    tags += f'\\fscx{scale}\\fscy{scale}\\t(0,{remaining},\\fscx100\\fscy100)'
                tags += '}'
                events.append(f'Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{tags}{text}')
    Path(path).write_text(header + '\n'.join(events) + '\n', encoding='utf-8')
