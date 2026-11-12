"""ReelForge: RSS -> Ollama local -> VoiceStudio -> Faster-Whisper -> FFmpeg Reel."""
import argparse
import asyncio
import hashlib
import html
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import feedparser
from dotenv import load_dotenv
from PIL import Image, ImageFont

ROOT = Path(os.getenv("REELFORGE_HOME", Path(__file__).resolve().parent))
OUTPUT = ROOT / "output"
DB = ROOT / "history.sqlite3"
COLORS = [(45, 191, 170), (250, 169, 71)]
from characters import NAMES


def run(cmd):
    from media_tools import run as media_run
    return media_run(cmd)


def ffprobe_duration(path):
    from media_tools import duration
    return duration(path)


def load_settings():
    try:
        data = json.loads((ROOT / 'settings.json').read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def check():
    from media_tools import ffmpeg
    cfg = load_settings()
    print('FFmpeg: ' + ffmpeg())
    print('Durações medidas com FFmpeg; FFprobe separado não é necessário.')
    try:
        out = run(["ffmpeg", "-hide_banner", "-filters"])
        print("Filtro subtitles:", "OK" if re.search(r"\bsubtitles\b", out) else "AUSENTE")
    except Exception as exc:
        print("Erro FFmpeg:", exc)
    print("Provedor IA:", cfg.get("provider", "ollama"), "| Modelo:", cfg.get("ollama_model", "qwen3.5:4b"))
    print("Serviço de voz:", cfg.get("tts_provider", "voicestudio"))
    print("Gameplays:", len(gameplays()))


def gameplays():
    folder = ROOT / "assets" / "gameplay"
    return sorted((p for p in folder.iterdir() if p.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}),
                  key=lambda p: (p.name != "gameplay_principal.mp4", p.name))


def conn():
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE IF NOT EXISTS used (id TEXT PRIMARY KEY, title TEXT, created_at TEXT)")
    return db


def mark_used(news):
    """Registra a pauta do RSS para não repetir a mesma notícia em outro Reel."""
    if not news or not news.get('id') or news.get('source_type') != 'rss':
        return
    db = conn()
    try:
        db.execute("INSERT OR IGNORE INTO used VALUES (?, ?, ?)",
                   (news['id'], news.get('title', ''), datetime.now(timezone.utc).isoformat()))
        db.commit()
    finally:
        db.close()


def apply_settings():
    """CLI: aplica brilho/gama, provedor, vozes e legendas salvos em settings.json."""
    cfg = load_settings()
    if not cfg:
        return {}
    tts_provider = cfg.get('tts_provider', 'voicestudio')
    voice = (cfg.get('voice_profiles') or {}).get(tts_provider, {})
    values = {
        'AI_PROVIDER': cfg.get('provider', 'ollama'),
        'OLLAMA_MODEL': cfg.get('ollama_model', 'qwen3.5:4b'),
        'OLLAMA_JUDGE_MODEL': cfg.get('judge_model') or cfg.get('ollama_model', 'qwen3.5:4b'),
        'REELFORGE_ACCEPT_LOW_SCORE': None if cfg.get('aceitar_nota_baixa') is None else ('1' if cfg['aceitar_nota_baixa'] else '0'),
        'TTS_PROVIDER': tts_provider,
        'TTS_VOICE_A': voice.get('a'),
        'TTS_VOICE_B': voice.get('b'),
        'TTS_MODEL': voice.get('model'),
        'TTS_RATE_A': voice.get('rate_a'),
        'TTS_RATE_B': voice.get('rate_b'),
        'TTS_PITCH_A': voice.get('pitch_a'),
        'TTS_PITCH_B': voice.get('pitch_b'),
        'CAPTION_TIMING': cfg.get('caption_timing', 'whisper'),
        'WHISPER_MODEL': cfg.get('whisper_model', 'small'),
        'REELFORGE_BG_BRIGHTNESS': cfg.get('gameplay_brightness'),
        'REELFORGE_BG_GAMMA': cfg.get('gameplay_gamma'),
        'REELFORGE_LOOP': None if cfg.get('loop') is None else ('1' if cfg['loop'] else '0'),
        'REELFORGE_SCRIPT_LOOP': None if cfg.get('script_loop') is None else ('1' if cfg['script_loop'] else '0'),
        'REELFORGE_MUSIC': None if cfg.get('music_enabled') is None else ('1' if cfg['music_enabled'] else '0'),
        'REELFORGE_MUSIC_VOLUME': cfg.get('music_volume'),
    }
    for key, value in values.items():
        if value not in (None, '') and key not in os.environ:
            os.environ[key] = str(value)
    return cfg


def clean(text):
    text = re.sub(r"<[^>]*>", " ", html.unescape(str(text)))
    return re.sub(r"\s+", " ", text).strip()


def fetch_news(topic, feed_url, max_stories, allow_repeat, return_candidates=False):
    url = feed_url or f"https://news.google.com/rss/search?q={quote(topic)}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    import requests
    response = requests.get(url, timeout=(5, 20))
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    if not feed.entries:
        raise RuntimeError("Feed RSS vazio ou indisponível. Verifique internet ou --news-url.")
    db = conn()
    candidates = []
    for e in feed.entries[:max_stories]:
        title = clean(e.get("title", ""))
        summary = clean(e.get("summary", ""))
        link = e.get("link", "")
        if not title or not link:
            continue
        uid = hashlib.sha256(link.encode()).hexdigest()
        if not allow_repeat and db.execute("SELECT 1 FROM used WHERE id=?", (uid,)).fetchone():
            continue
        candidates.append({"id": uid, "title": title, "summary": summary[:700], "url": link, "published": e.get("published", "")})
    db.close()
    if not candidates:
        raise RuntimeError("Nenhuma notícia nova. Use outro tema ou --allow-repeat.")
    return candidates if return_candidates else candidates[0]


def validate_duration(seconds):
    seconds = float(seconds)
    if not 10 <= seconds <= 180:
        raise ValueError('A duração máxima deve estar entre 10 e 180 segundos.')
    return seconds


def word_budget(seconds):
    return max(12, int((validate_duration(seconds) - 1.5) * 2.5))


def shorten_script(lines, budget):
    """Keep speaker turns and a closing line within a total word budget."""
    lines = [dict(line, text=clean(line['text']))
             for line in lines if clean(line['text'])]
    if not lines:
        raise ValueError('Roteiro vazio.')
    if len(lines) > max(2, budget // 3):
        lines = lines[:max(2, budget // 3) - 1] + lines[-1:]
    counts = [len(line['text'].split()) for line in lines]
    if sum(counts) <= budget:
        return lines
    quotas = [min(count, 2) for count in counts]
    for _ in range(budget - sum(quotas)):
        candidates = [i for i, count in enumerate(counts) if quotas[i] < count]
        if not candidates:
            break
        index = max(candidates, key=lambda i: counts[i] / (quotas[i] + 1))
        quotas[index] += 1
    for line, count, quota in zip(lines, counts, quotas):
        if quota < count:
            line['text'] = ' '.join(line['text'].split()[:quota]).rstrip('.,;:!?') + '.'
    return lines


def narration_duration(lines):
    return 0.65 + sum(line['duration'] for line in lines) + 0.14 * len(lines)


async def fit_narration(lines, work, max_seconds, direction=None, source=None):
    """Measure actual speech and regenerate shorter narration before rendering."""
    max_seconds = validate_duration(max_seconds)
    imported = any(line.get('audio_source') == 'imported' for line in lines)
    rss = bool(source and source.get('source_type') == 'rss')
    lines = [dict(line) for line in lines] if (direction or imported or rss) else shorten_script(lines, word_budget(max_seconds))
    for attempt in range(4):
        await synthesize(lines, work)
        actual = narration_duration(lines)
        if actual <= max_seconds - 0.1:
            return lines
        if imported:
            raise RuntimeError('As gravações importadas excedem a duração máxima. Aumente o limite; o áudio não foi cortado ou ressintetizado.')
        if attempt == 3:
            break
        count = sum(len(line['text'].split()) for line in lines)
        budget = min(count - 1, int(count * (max_seconds - 1.5) / actual * 0.9))
        if budget < 6:
            break
        if direction:
            import director
            lines = director.rewrite_for_time(source, direction, budget)
        elif source and source.get('source_type') == 'rss':
            import news_auto
            lines = news_auto.shorten(source, lines, budget)
        else:
            lines = shorten_script(lines, budget)
    raise RuntimeError('As falas não couberam no limite. Aumente a duração máxima ou reduza o roteiro.')


def validate_hook(text):
    text = clean(text)
    if len(text) > 70 or len(text.split()) > 8:
        raise ValueError('Use até 8 palavras e 70 caracteres no gancho visual.')
    return text


def request_json(instruction, payload, model=None, temperature=0.55):
    import requests
    cfg = load_settings()
    model = model or os.getenv("OLLAMA_MODEL") or cfg.get("ollama_model") or "qwen3.5:4b"
    temp = 0.55 if temperature is None else float(temperature)
    with requests.Session() as session:
        session.trust_env = False
        base = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        response = session.post(base + "/api/chat", json={
            "model": model, "stream": False, "format": "json", "think": False,
            "messages": [{"role": "system", "content": instruction},
                         {"role": "user", "content": payload}],
            "keep_alive": "20m",
            "options": {"temperature": temp, "num_ctx": 4096},
        }, timeout=(5, 300), allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError(f"Ollama local respondeu HTTP {response.status_code}.")
        content = response.json()["message"]["content"]
    if not content.strip():
        raise RuntimeError("O Ollama devolveu uma resposta vazia.")
    return json.loads(content)


def make_script(news, max_seconds=30, impact_opening=False, hook_text=''):
    budget = word_budget(max_seconds)
    cfg = load_settings()
    provider = os.getenv("AI_PROVIDER") or cfg.get("provider") or "ollama"
    if provider != "ollama":
        raise ValueError(f"Provedor IA não suportado para roteiro local: {provider}.")
    instruction = (
        "Crie diálogo em português brasileiro informal entre Sasuke (speaker 0, direto e irônico) e Naruto (speaker 1, curioso e espontâneo). "
        "Responda APENAS JSON: {\"lines\":[{\"speaker\":0,\"text\":\"...\"}]}. "
        f"Entre 4 e 10 falas curtas, no máximo {budget} palavras no total, para um vídeo de até {max_seconds:g} segundos. "
        "Comece com uma provocação ou pergunta de até 10 palavras. "
        "Use SOMENTE informações no título e resumo recebidos. Não invente fatos, datas ou números e não cite veículos."
    )
    if impact_opening:
        instruction += (
            ' Abertura: primeira fala com no máximo 6 palavras, específica sobre esta notícia, '
            'sem cumprimento ou introdução. Comece com uma pergunta ou provocação comprovada pelo texto. '
        )
        if hook_text:
            instruction += 'Texto visual fornecido pelo usuário (não é fonte de fatos): ' + validate_hook(hook_text)
    obj = request_json(instruction, json.dumps(news, ensure_ascii=False))
    lines = obj.get("lines", [])
    if not 4 <= len(lines) <= 14 or any(not isinstance(l, dict) or not isinstance(l.get("text"), str) or l.get("speaker") not in (0, 1) for l in lines):
        raise RuntimeError("Resposta da IA fora do formato esperado")
    res_lines = []
    for l in lines:
        entry = {"speaker": l["speaker"], "text": clean(l["text"])}
        if l.get("reacao"):
            entry["reacao"] = True
        elif l.get("fato") is not None:
            try:
                entry["fato"] = int(l["fato"])
            except (ValueError, TypeError):
                pass
        res_lines.append(entry)
    return shorten_script(res_lines, budget)


def font(size):
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def character(path, index, mouth_open):
    # Current production sprites, one actor visible per speech turn.
    from characters import ASSETS
    from PIL import ImageFilter
    source = ROOT / 'assets' / 'characters' / ASSETS[index]
    if not source.is_file():
        raise FileNotFoundError('Asset do personagem ausente: ' + str(source))
    cache_dir = OUTPUT / 'cache'
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f'sprite_v2_{index}_{source.stat().st_mtime_ns}.png'
    if cached.is_file():
        shutil.copyfile(cached, path)
        return
    image = Image.open(source).convert('RGBA')
    image.thumbnail((510, 640), Image.Resampling.LANCZOS)
    canvas = Image.new('RGBA', (540, 670))
    ox, oy = (540 - image.width) // 2, 662 - image.height
    # Sombra suave projetada atrás do personagem para destacar sobre qualquer gameplay.
    alpha = image.getchannel('A').filter(ImageFilter.GaussianBlur(9))
    shadow = Image.new('RGBA', image.size, (0, 0, 0, 0))
    shadow.putalpha(alpha.point(lambda p: int(p * 0.62)))
    canvas.alpha_composite(shadow, (ox, min(670 - image.height, oy + 6)))
    canvas.alpha_composite(image, (ox, oy))
    canvas.save(cached)
    shutil.copyfile(cached, path)


def ass_time(t):
    cs = round(t * 100)
    return f"{cs // 360000}:{(cs // 6000) % 60:02d}:{(cs // 100) % 60:02d}.{cs % 100:02d}"


def ass_escape(s):
    return s.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def write_ass(lines, path, hook_text='', direction=None):
    from captions import write_ass as write_captions
    return write_captions(lines, path, hook_text)


async def synthesize(lines, work):
    from speech import synthesize_lines
    await synthesize_lines(lines, OUTPUT / 'cache')


def escape_filter_path(path):
    return str(path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,").replace("[", "\\[").replace("]", "\\]")


def render(news, lines, gameplay, fps=30, max_seconds=45, impact_opening=False, hook_text='', direction=None):
    from render_pipeline import render as render_integrated
    if isinstance(news, dict):
        cfg = load_settings()
        writer_model = os.getenv('OLLAMA_MODEL') or cfg.get('ollama_model') or 'qwen3.5:4b'
        judge_model = os.getenv('OLLAMA_JUDGE_MODEL') or cfg.get('judge_model') or 'qwen3:8b'
        juiz_fraco = bool(news.get('juiz_fraco', False))
        if 'modelos' not in news:
            news['modelos'] = {
                'redator': writer_model,
                'juiz': judge_model,
                'juiz_fraco': juiz_fraco,
            }
    final = render_integrated(sys.modules[__name__], news, lines, gameplay, fps, max_seconds, impact_opening, hook_text, direction)
    json_path = final.with_suffix('.json')
    if json_path.is_file():
        try:
            meta = json.loads(json_path.read_text(encoding='utf-8'))
            changed = False
            if 'modelos' not in meta and isinstance(news, dict) and 'modelos' in news:
                meta['modelos'] = news['modelos']
                changed = True
            if 'lines' in meta and isinstance(meta['lines'], list):
                for i, pline in enumerate(meta['lines']):
                    if i < len(lines):
                        orig = lines[i]
                        if orig.get('reacao') and not pline.get('reacao'):
                            pline['reacao'] = True
                            changed = True
                        elif orig.get('fato') is not None and pline.get('fato') is None:
                            pline['fato'] = orig['fato']
                            changed = True
            if changed:
                json_path.write_text(json.dumps(meta, default=str, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass
    return final


def main():
    load_dotenv(ROOT / ".env")
    cfg = apply_settings()
    default_fps = int(cfg.get("fps", 30)) if str(cfg.get("fps", 30)) in ("30", "60") else 30
    parser = argparse.ArgumentParser(description="ReelForge: gerador automático de Reels")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--preview", action="store_true")
    group.add_argument("--generate", action="store_true")
    parser.add_argument("--topic", default=os.getenv("NEWS_QUERY", cfg.get("topic", "tecnologia")))
    parser.add_argument("--source-file", type=Path, help="Arquivo UTF-8 com o texto de referência")
    parser.add_argument("--gameplay", type=Path)
    parser.add_argument("--fps", type=int, choices=(30, 60), default=default_fps)
    parser.add_argument("--allow-repeat", action="store_true")
    parser.add_argument("--max-seconds", type=validate_duration, default=float(cfg.get("max_seconds", 60)), help="Duração máxima, de 10 a 180 segundos")
    parser.add_argument('--impact-opening', action='store_true', help='Destacar gancho nos primeiros 3 segundos')
    parser.add_argument('--hook-text', type=validate_hook, default='', help='Gancho visual opcional, até 8 palavras/70 caracteres')
    parser.add_argument('--aceitar-nota-baixa', action='store_true', help='Seguir mesmo se a nota de qualidade ficar abaixo de 7')
    args = parser.parse_args()
    if args.aceitar_nota_baixa:
        os.environ['REELFORGE_ACCEPT_LOW_SCORE'] = '1'
    if args.check:
        check()
        return
    if args.source_file:
        summary = args.source_file.read_text(encoding="utf-8").strip()
        if not summary:
            parser.error("O arquivo de referência está vazio.")
        news = {"title": args.topic, "summary": summary, "url": ""}
        print("Notícia:", news["title"], "\nFonte:", news["url"])
        lines = make_script(news, args.max_seconds, args.impact_opening, args.hook_text)
    else:
        import news_auto
        news, lines = news_auto.automatic(args.topic, cfg.get('content_kind', 'Notícia'), args.max_seconds, args.allow_repeat)
        print("Notícia:", news["title"], "\nFonte:", news["url"])
    if args.preview:
        for l in lines:
            print(f"{NAMES[l['speaker']]}: {l['text']}")
        return
    gameplay = args.gameplay or next(iter(gameplays()), None)
    if not gameplay or not gameplay.is_file():
        raise RuntimeError("Adicione um MP4 de gameplay em assets/gameplay/ ou use --gameplay arquivo.mp4")
    render(news, lines, gameplay, args.fps, args.max_seconds, args.impact_opening, args.hook_text)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERRO:", exc, file=sys.stderr)
        sys.exit(1)
