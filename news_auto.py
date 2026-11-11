"""Pauta automática: RSS de notícias -> Ollama local escolhe a pauta, extrai fatos, escreve e avalia o roteiro.

A internet é usada SOMENTE para ler feeds RSS. Vários veículos publicam o texto
completo da matéria no próprio RSS (content:encoded), o que dá ao roteiro fatos
concretos sem abrir páginas web. A escolha da pauta, a extração de fatos em dois
passos, a redação do diálogo e o filtro de qualidade são feitos pelo Ollama local.
"""
import calendar
import hashlib
import html
import json
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote

import feedparser

RSS_SEARCH = 'https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419'
RSS_TOP = 'https://news.google.com/rss?hl=pt-BR&gl=BR&ceid=BR:pt-419'
SUFFIX = {'Notícia': '', 'Curiosidade': ' curiosidades', 'História': ' história'}
NAMES = ('Sasuke', 'Naruto')

# Feeds testados que trazem a matéria completa (400 a 800 palavras por item).
FEEDS = {
    'g1': 'https://g1.globo.com/rss/g1/',
    'g1_tecnologia': 'https://g1.globo.com/rss/g1/tecnologia/',
    'g1_economia': 'https://g1.globo.com/rss/g1/economia/',
    'g1_politica': 'https://g1.globo.com/rss/g1/politica/',
    'g1_ciencia': 'https://g1.globo.com/rss/g1/ciencia-e-saude/',
    'g1_mundo': 'https://g1.globo.com/rss/g1/mundo/',
    'g1_educacao': 'https://g1.globo.com/rss/g1/educacao/',
    'g1_natureza': 'https://g1.globo.com/rss/g1/meio-ambiente/',
    'g1_pop': 'https://g1.globo.com/rss/g1/pop-arte/',
    'cnnbrasil': 'https://www.cnnbrasil.com.br/feed/',
    'agenciabrasil': 'https://agenciabrasil.ebc.com.br/rss/ultimasnoticias/feed.xml',
    'poder360': 'https://www.poder360.com.br/feed/',
    'infomoney': 'https://www.infomoney.com.br/feed/',
    'tecnoblog': 'https://tecnoblog.net/feed/',
    'canaltech': 'https://canaltech.com.br/rss/',
    'olhardigital': 'https://olhardigital.com.br/feed/',
    'ge': 'https://ge.globo.com/rss/ge/',
}
SOURCE_NAMES = {'g1': 'g1', 'cnnbrasil': 'CNN Brasil', 'agenciabrasil': 'Agência Brasil', 'poder360': 'Poder360',
                'infomoney': 'InfoMoney', 'tecnoblog': 'Tecnoblog', 'canaltech': 'Canaltech',
                'olhardigital': 'Olhar Digital', 'ge': 'ge'}
GENERAL = ['g1', 'cnnbrasil', 'agenciabrasil', 'poder360', 'infomoney']
CATEGORIES = {
    'tecnologia': ['g1_tecnologia', 'tecnoblog', 'canaltech', 'olhardigital'],
    'economia': ['g1_economia', 'infomoney'],
    'politica': ['g1_politica', 'poder360', 'cnnbrasil'],
    'ciencia': ['g1_ciencia', 'g1_natureza', 'olhardigital'],
    'educacao': ['g1_educacao', 'agenciabrasil', 'g1'],
    'cultura': ['g1_pop', 'canaltech', 'tecnoblog'],
    'mundo': ['g1_mundo', 'cnnbrasil'],
    'esporte': ['ge'],
}
SYNONYMS = {'tecnologia': 'tecnologia', 'tech': 'tecnologia', 'games': 'tecnologia', 'celular': 'tecnologia',
            'internet': 'tecnologia', 'ia': 'tecnologia', 'ai': 'tecnologia', 'inteligencia': 'tecnologia',
            'jogos': 'tecnologia', 'gta': 'tecnologia', 'playstation': 'tecnologia', 'xbox': 'tecnologia',
            'seguranca': 'tecnologia', 'hacker': 'tecnologia', 'chip': 'tecnologia', 'software': 'tecnologia',
            'economia': 'economia', 'mercado': 'economia', 'financas': 'economia', 'dinheiro': 'economia',
            'cripto': 'economia', 'bitcoin': 'economia', 'negocios': 'economia', 'juros': 'economia',
            'inflacao': 'economia', 'emprego': 'economia', 'politica': 'politica', 'governo': 'politica',
            'justica': 'politica', 'brasil': 'politica', 'congresso': 'politica', 'stf': 'politica',
            'ciencia': 'ciencia', 'saude': 'ciencia', 'espaco': 'ciencia', 'medicina': 'ciencia',
            'clima': 'ciencia', 'ambiente': 'ciencia', 'natureza': 'ciencia', 'energia': 'ciencia',
            'educacao': 'educacao', 'enem': 'educacao', 'escola': 'educacao', 'faculdade': 'educacao',
            'concurso': 'educacao', 'universidade': 'educacao', 'cultura': 'cultura', 'cinema': 'cultura',
            'musica': 'cultura', 'anime': 'cultura', 'serie': 'cultura', 'streaming': 'cultura',
            'mundo': 'mundo', 'internacional': 'mundo', 'guerra': 'mundo', 'geopolitica': 'mundo',
            'comunismo': 'mundo', 'socialismo': 'mundo', 'capitalismo': 'mundo',
            'esporte': 'esporte', 'esportes': 'esporte', 'futebol': 'esporte'}
SHORT_TOKENS = {'ia', 'ai', 'vi', 'iv', 'vr', 'pc', 'tv', 'f1', 'g7', 'ue', 'ti', 'rh', 'br'}
QUERY_STOPWORDS = {'noticia', 'noticias', 'hoje', 'historia', 'historias', 'curiosidade', 'curiosidades',
                   'sobre', 'falando', 'video', 'videos', 'reel', 'reels', 'como', 'qual', 'quais',
                   'porque', 'para', 'pelo', 'pela', 'entre', 'tudo', 'das', 'dos', 'uma', 'uns',
                   'umas', 'isso', 'esse', 'essa', 'afinal', 'veja', 'saiba', 'entenda', 'conheca'}
KEY_ALIASES = {
    'ia': {'ia', 'inteligencia'},
    'ai': {'ai', 'ia', 'inteligencia'},
    'vi': {'vi', '6', 'seis'},
    '6': {'6', 'vi', 'seis'},
    'comunismo': {'comunismo', 'comunista', 'comunistas', 'marx', 'sovietica'},
}
SOCIAL = ('instagram', 'facebook', 'youtube', 'tiktok', 'twitter', 'threads', 'linkedin', 'kwai', 'pinterest', 'reddit')
JUNK = re.compile(r'[\U0001F000-\U0001FFFF\u2600-\u27BF\u2060-\u206F\u200B-\u200F\uFE0F]')
ADS = re.compile(r'\b(ofertas?|cupom|cupons|descontos?|black friday|promo[cç](ao|ão|oes|ões)|achadinhos|'
                 r'compre|mais vendidos|vale a pena comprar|inscrever gratuitamente|aproveite agora)\b', re.I)
BOILERPLATE = re.compile(r'^(leia (também|mais)|veja (também|mais)|assista|siga o|inscreva-se|clique aqui|'
                         r'foto:|imagem:|reprodução|the post .* appeared first|o post .* apareceu primeiro|'
                         r'resumo$|publicidade|newsletter)', re.I)
CONNECTORS = {'que', 'porque', 'e', 'mas', 'entao', 'quando', 'como', 'onde', 'pois', 'de', 'do', 'da',
              'para', 'pra', 'com', 'sobre', 'isso', 'o', 'a', 'os', 'as', 'um', 'uma', 'nisso', 'tipo'}
# Expressões de telejornal e nomes de veículos proibidos dentro das falas dos personagens.
TELEJORNAL_RE = re.compile(
    r'\b(confirmad[oa]s?\s+p(or|elo|ela)|segundo\s+(o|a|os|as)\s+\w+|de\s+acordo\s+com\s+(o|a)\b|'
    r'conforme\s+(informou|publicou|divulgou|apurou)|publicou\s+(hoje|nesta)|'
    r'nesta\s+(segunda|terça|terca|quarta|quinta|sexta|sábado|sabado|domingo)(-feira)?|'
    r'cpg\b|click\s+petr[oó]leo|g1\b|uol\b|cnn(\s+brasil)?\b|folha\s+de\s+s|o\s+globo\b|'
    r'estad[aã]o\b|metr[oó]poles\b|poder360\b|infomoney\b|tecnoblog\b|canaltech\b|'
    r'olhar\s+digital\b|ag[eê]ncia\s+brasil\b)\b', re.I)
HOOK_BAD_START_RE = re.compile(r'^\s*(mas|e|porque|pois|ent[aã]o|por\s+isso)\b', re.I)
HOOK_PROVOCATION_RE = re.compile(
    r'(\?|!|\b(por\s+que|como\s+assim|sabia|imagina|j[aá]\s+pensou|voc[eê]|ser[aá]|quem|qual|quanto|'
    r'acredita|faz\s+sentido|tem\s+no[cç][aã]o|olha|pera[ií]|cara|mano|mentira|poss[ií]vel|absurdo|bizarro)\b)', re.I)
MIN_BODY = 80
MIN_SOURCE_WORDS = 25
MAX_BODY = 750
_feed_cache = {}


def loop_enabled():
    """Ponte falada no roteiro fica desligada por padrão; pode ser reativada em settings.json (script_loop)."""
    env = os.getenv('REELFORGE_SCRIPT_LOOP')
    if env is not None:
        return env == '1'
    try:
        import engine
        cfg = json.loads((engine.ROOT / 'settings.json').read_text(encoding='utf-8'))
        return bool(cfg.get('script_loop', False))
    except Exception:
        return False


def _tokens(text):
    text = unicodedata.normalize('NFKD', str(text)).casefold()
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return [w for w in re.sub(r'[^a-z0-9 ]+', ' ', text).split()
            if len(w) > 2 or w in SHORT_TOKENS or w.isdigit()]


def _query_keys(query):
    raw = _tokens(query)
    filtered = [w for w in raw if w not in QUERY_STOPWORDS]
    return filtered or raw


def _similar(a, b):
    x, y = set(_tokens(a)), set(_tokens(b))
    return len(x & y) / max(1, len(x | y))


def read_feed(url):
    import requests
    now = time.time()
    cached = _feed_cache.get(url)
    if cached and now - cached[0] < 180:
        return cached[1]
    response = requests.get(url, timeout=(5, 20), headers={'User-Agent': 'Mozilla/5.0 ReelForge RSS reader'})
    response.raise_for_status()
    entries = feedparser.parse(response.content).entries
    _feed_cache[url] = (now, entries)
    return entries


def article_text(entry):
    """Texto da matéria publicado no RSS, limpo de créditos, chamadas e propaganda."""
    parts = [entry.get('summary', '') or ''] + [c.get('value', '') for c in entry.get('content', []) or []]
    raw = max(parts, key=len)
    raw = re.sub(r'(?i)<\s*(br|/p|/li|/h\d|/div|/blockquote)[^>]*>', '\n', raw)
    raw = re.sub(r'(?is)<(script|style|figure|figcaption)[^>]*>.*?</\1>', ' ', raw)
    text = html.unescape(re.sub(r'<[^>]+>', ' ', raw))
    text = re.sub(r'\((imagem|foto|crédito|reprodução)[^)]*\)', ' ', text, flags=re.I)
    paragraphs = []
    for paragraph in text.split('\n'):
        paragraph = re.sub(r'\s+', ' ', paragraph).strip()
        if len(paragraph.split()) < 4 or BOILERPLATE.match(paragraph):
            continue
        paragraphs.append(paragraph)
    words = ' '.join(paragraphs).split()
    if len(words) <= MAX_BODY:
        return ' '.join(words)
    cut = ' '.join(words[:MAX_BODY])
    end = max(cut.rfind('. '), cut.rfind('! '), cut.rfind('? '))
    return cut[:end + 1] if end > len(cut) * 0.6 else cut


def _published(entry):
    stamp = entry.get('published_parsed') or entry.get('updated_parsed')
    return calendar.timegm(stamp) if stamp else None


def parse_entry(entry, feed_name=None):
    import engine
    title = engine.clean(entry.get('title', ''))
    source = entry.get('source')
    source = engine.clean(source.get('title', '')) if hasattr(source, 'get') else ''
    if feed_name:
        source = SOURCE_NAMES.get(feed_name.split('_')[0], feed_name)
    elif source and title.endswith(' - ' + source):
        title = title[:-(len(source) + 3)].strip()
    elif not source and ' - ' in title:
        title, source = (part.strip() for part in title.rsplit(' - ', 1))
    body = article_text(entry) if feed_name else ''
    if body and _similar(body[:len(title) + 40], title) > 0.8:
        body = body[len(title):].strip() if body.startswith(title) else body
    related = []
    if not feed_name:
        for match in re.finditer(r'<li>(.*?)</li>', entry.get('summary', '') or '', re.S):
            text = engine.clean(match.group(1))
            if text and _similar(text, title) < 0.9:
                related.append(text)
    link = entry.get('link', '')
    return {'id': hashlib.sha256(link.encode()).hexdigest(), 'title': title, 'source': source,
            'url': link, 'published': entry.get('published', ''), 'timestamp': _published(entry),
            'body': body if len(body.split()) >= MIN_BODY else '', 'related': related[:6]}


def is_news(item):
    source = item['source'].casefold()
    words = len(item['title'].split())
    return (not any(s in source for s in SOCIAL) and 4 <= words <= 30 and not JUNK.search(item['title'])
            and not item['title'].isupper() and not ADS.search(item['title']))


def _relevant(item, keys):
    if not keys:
        return True
    title_tokens = set(_tokens(item['title']))
    body_tokens = set(_tokens(' '.join(item.get('body', '').split()[:250])))
    all_tokens = title_tokens | body_tokens
    lead_tokens = title_tokens | set(_tokens(' '.join(item.get('body', '').split()[:60])))
    primary = [k for k in keys if k not in {'vi', 'iv', '6', '1', '2', '3', '4', '5'}] or list(keys)
    for k in primary:
        aliases = KEY_ALIASES.get(k, {k})
        if not (aliases & all_tokens):
            return False
    if not any(KEY_ALIASES.get(k, {k}) & lead_tokens for k in primary):
        return False
    for k in keys:
        if k not in primary:
            aliases = KEY_ALIASES.get(k, {k})
            if not (aliases & all_tokens):
                return False
    return True


def _feed_plan(query):
    keys = _query_keys(query)
    categories = list(dict.fromkeys(SYNONYMS[k] for k in keys if k in SYNONYMS))
    specific_keys = [k for k in keys if k not in CATEGORIES]
    if categories:
        cat_feeds = list(dict.fromkeys(f for c in categories for f in CATEGORIES[c]))
        return [(name, specific_keys) for name in cat_feeds] + [(name, keys) for name in GENERAL if name not in cat_feeds]
    names = list(dict.fromkeys(GENERAL + [n for group in CATEGORIES.values() for n in group]))
    return [(name, keys) for name in names]


def _numbers(text):
    return {re.sub(r'[.,]', '', n).lstrip('0') or '0' for n in re.findall(r'\d[\d.,]*\d|\d', str(text))}


def _entities(text):
    """Extrai nomes próprios e siglas relevantes do texto."""
    found = []
    seen = set()
    for match in re.finditer(r'\b([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÀ-ú-]{2,}|[A-Z]{2,6})\b', str(text)):
        before = text[:match.start()].rstrip()
        word = match.group(1)
        if not word.isupper() and (not before or before[-1] in '.!?:"“\n'):
            continue
        low = word.casefold()
        if word not in NAMES and low not in CONNECTORS and low not in seen:
            seen.add(low)
            found.append(word)
    return found


def complexity_score(item, keys=()):
    """Pontua a densidade informativa da pauta: números, entidades, profundidade, relevância e frescor."""
    body = item.get('body', '')
    text = item.get('title', '') + ' ' + body
    nums = min(6, len(_numbers(text))) * 2.8
    ents = min(6, len(_entities(text))) * 1.6
    depth = min(5.5, len(body.split()) / 85.0)
    tokens = set(_tokens(text))
    rel = sum(2.5 for k in (keys or ()) if KEY_ALIASES.get(k, {k}) & tokens)
    ts = item.get('timestamp')
    age_h = max(0.0, (time.time() - ts) / 3600.0) if ts else 12.0
    recency = max(0.0, 5.0 * (1.0 - min(age_h, 48.0) / 48.0))
    return round(nums + ents + depth + rel + recency, 2)


def build_dossier(story):
    """Extrai dossiê estruturado EXCLUSIVAMENTE do título e corpo da matéria."""
    full_source = source_text(story)
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n+', full_source) if len(s.split()) >= 4]
    num_facts = [s for s in sentences if re.search(r'\d', s)][:6]
    entities = _entities(full_source)[:12]
    return {
        'fatos_com_numeros': num_facts,
        'nomes_e_entidades': entities,
        'complexidade': complexity_score(story),
    }


def _google_search_query(query):
    keys = _query_keys(query)
    if keys == ['ia'] or keys == ['ai']:
        return 'inteligência artificial'
    return query


def gather_candidates(topic, kind='Notícia', limit=15, allow_repeat=False, max_age_hours=72):
    """Reúne pautas recentes dos RSS dos veículos (priorizando matérias com corpo completo)."""
    import engine
    query = (str(topic or '').strip() + SUFFIX.get(kind, '')).strip()
    search_q = _google_search_query(query)
    plan = _feed_plan(query) if query else [(name, []) for name in GENERAL]
    google = ([RSS_SEARCH.format(q=quote(search_q + ' when:2d'))] if search_q else [RSS_TOP])

    def load(job):
        name, keys = job
        try:
            return [(parse_entry(e, name), keys) for e in read_feed(FEEDS[name])]
        except Exception:
            return []

    def load_google(url):
        try:
            return [(parse_entry(e), []) for e in read_feed(url)]
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=10) as pool:
        rich = [x for batch in pool.map(load, plan) for x in batch]
        headlines = [x for batch in pool.map(load_google, google) for x in batch]
    now = time.time()
    fresh = lambda item: not item['timestamp'] or now - item['timestamp'] <= max_age_hours * 3600
    scored_rich = []
    for item, keys in rich:
        if item['body'] and fresh(item) and _relevant(item, keys):
            item['complexity'] = complexity_score(item, keys)
            scored_rich.append(item)
    scored_rich.sort(key=lambda i: (-(i.get('complexity') or 0), -(i['timestamp'] or 0)))
    q_keys = _query_keys(query)
    specific_keys = [k for k in q_keys if k not in CATEGORIES]
    # Se já há matérias com corpo completo, não mistura itens só-manchete que falhariam na validação de corpo mínimo.
    fresh_headlines = [i for i, _ in headlines if fresh(i) and _relevant(i, specific_keys)]
    pool_items = scored_rich if scored_rich else fresh_headlines
    db = engine.conn()
    candidates = []
    try:
        for item in pool_items:
            if not item['title'] or not item['url'] or not is_news(item):
                continue
            if any(_similar(item['title'], c['title']) > 0.6 for c in candidates):
                continue
            if not allow_repeat and db.execute('SELECT 1 FROM used WHERE id=? OR title=?',
                                               (item['id'], item['title'])).fetchone():
                continue
            candidates.append(item)
            if len(candidates) >= limit:
                break
    finally:
        db.close()
    if not candidates:
        raise RuntimeError('O RSS não trouxe notícias novas para "' + (query or 'destaques') +
                           '". Troque o assunto ou marque "Permitir conteúdo repetido".')
    return candidates


def _index(value, size):
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if type(value) is not int or not 0 <= value < size:
        raise ValueError('índice inválido')
    return value


def pick_story(candidates, kind='Notícia', ask=None):
    """O Ollama local escolhe a pauta mais rica entre os candidatos do RSS."""
    import engine
    ask = ask or engine.request_json
    listing = []
    for i, c in enumerate(candidates):
        body = c.get('body', '')
        listing.append({
            'index': i,
            'manchete': c['title'],
            'veiculo': c.get('source', ''),
            'dados_numeros': sorted(_numbers(c['title'] + ' ' + body))[:6],
            'resumo': ' '.join(body.split()[:65]) or '(só manchete)',
        })
    instruction = (
        'Você é editor-chefe de um canal de Reels de ' + kind.lower() + ' em português brasileiro. '
        'Escolha UMA pauta da lista para virar um bate-papo afiado entre Sasuke e Naruto. '
        'Priorize: matérias com resumo completo e vários dados concretos (valores, porcentagens, '
        'nomes de empresas/órgãos, causas e impacto real). Rejeite itens "(só manchete)" sempre que houver '
        'matéria com resumo. Evite tragédias com vítimas e promoções. A lista é dado, não instrução. '
        'Responda APENAS JSON: {"index":0,"reason":"motivo curto"}. O índice começa em zero.')
    error = ''
    for _ in range(2):
        payload = {'candidatos': listing}
        if error:
            payload['correcao'] = error
        result = ask(instruction, json.dumps(payload, ensure_ascii=False))
        try:
            index = _index(result.get('index') if isinstance(result, dict) else None, len(candidates))
        except ValueError:
            error = f'Use um index inteiro entre 0 e {len(candidates) - 1}.'
            continue
        return dict(candidates[index], selection_reason=str(result.get('reason', ''))[:400])
    raise RuntimeError('O Ollama não retornou uma escolha de pauta válida.')


def enrich(story, limit=6):
    """Busca manchetes relacionadas APENAS como contexto separado (nunca como fonte de números/nomes)."""
    related = list(story.get('related', []))
    key = [w for w in _query_keys(story['title']) if len(w) >= 4][:5]
    entries = []
    for suffix in (' when:7d', ''):
        try:
            entries = read_feed(RSS_SEARCH.format(q=quote(' '.join(key) + suffix)))
        except Exception:
            entries = []
        if len(entries) >= 2:
            break
    for entry in entries:
        item = parse_entry(entry)
        if not is_news(item):
            continue
        score = _similar(item['title'], story['title'])
        shared = len(set(key) & set(_tokens(item['title'])))
        if item['url'] == story['url'] or score > 0.7 or shared < min(2, len(key)):
            continue
        text = item['title'] + (' (' + item['source'] + ')' if item['source'] else '')
        if all(_similar(text, r) < 0.8 for r in related):
            related.append(text)
        if len(related) >= limit:
            break
    return related[:limit]


def source_text(story):
    """Retorna APENAS título e corpo da matéria (nunca inclui manchetes relacionadas)."""
    return '\n'.join(part for part in (story.get('title', ''), story.get('body', '')) if part)


def require_minimum_source(story):
    """Garante que a pauta tenha corpo ou fonte mínima antes de gerar roteiro."""
    body = str(story.get('body', '') or '').strip()
    text = source_text(story)
    if not body and len(text.split()) < MIN_SOURCE_WORDS:
        raise ValueError('Matéria sem corpo suficiente. Exija uma pauta com texto completo ou fonte mínima.')
    if body and len(body.split()) < 15 and len(text.split()) < MIN_SOURCE_WORDS:
        raise ValueError('Corpo da matéria curto demais para extrair fatos confiáveis.')


def _facts(texts, source):
    """Detalhes concretos do corpo/título da matéria presentes no roteiro: números e nomes próprios."""
    found = set(_numbers(' '.join(texts))) & _numbers(source)
    source_words = set(_tokens(source))
    known_entities = {e.casefold() for e in _entities(source)}
    for text in texts:
        for match in re.finditer(r'\b([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÀ-ú-]{2,}|[A-Z]{2,6})\b', text):
            word = match.group(1)
            low = word.casefold()
            if word in NAMES:
                continue
            if low in known_entities:
                found.add(low)
                continue
            before = text[:match.start()].rstrip()
            if not before or before[-1] in '.!?:"“':
                continue  # maiúscula de início de frase não indica nome próprio
            if _tokens(word) and _tokens(word)[0] in source_words:
                found.add(low)
    return found


def _ForbiddenVehicleMatch(text, story):
    if TELEJORNAL_RE.search(text):
        return True
    src = str(story.get('source', '') or '').strip()
    if len(src) >= 2 and re.search(r'\b' + re.escape(src) + r'\b', text, re.I):
        return True
    return False


def extract_facts(story, ask=None):
    """Passo 1: extrai de 5 a 6 fatos curtos e verificáveis EXCLUSIVAMENTE do texto da matéria."""
    import engine
    require_minimum_source(story)
    ask = ask or engine.request_json
    source = source_text(story)
    allowed_nums = _numbers(source)
    instruction = (
        'Passo 1 — Extração de fatos: leia o título e o corpo da matéria e extraia de 5 a 6 fatos curtos '
        'e concretos em português brasileiro. '
        'Regras obrigatórias: '
        '- Cada fato deve ser copiado ou parafraseado DIRETAMENTE do texto da matéria (nunca invente dados e nunca use manchetes externas). '
        '- Priorize números exatos, valores, datas, nomes de órgãos/empresas/pessoas, a causa e a consequência. '
        '- Não inclua o nome do veículo jornalístico nos fatos. '
        'Responda APENAS JSON: {"fatos":["fato 1","fato 2","fato 3","fato 4","fato 5"]}')
    payload = {'manchete': story.get('title', ''), 'materia': story.get('body', '')}
    error = ''
    for _ in range(3):
        req = dict(payload, correcao=error) if error else payload
        raw = ask(instruction, json.dumps(req, ensure_ascii=False))
        if isinstance(raw, dict) and 'lines' in raw and 'fatos' not in raw:
            # Compatibilidade com mocks de teste que devolvem {"lines": ...} direto.
            story['_prefetched_raw'] = raw
            sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', source) if s.strip()]
            return (sentences[:6] or [story.get('title', '')])
        fatos = raw.get('fatos') if isinstance(raw, dict) else None
        if not isinstance(fatos, list) or not 5 <= len(fatos) <= 6:
            error = 'Retorne uma lista "fatos" contendo exatamente de 5 a 6 fatos curtos.'
            continue
        cleaned = [engine.clean(str(f)) for f in fatos if isinstance(f, str) and engine.clean(str(f))]
        if len(cleaned) < 5:
            error = 'Todos os 5 a 6 fatos precisam ser frases preenchidas.'
            continue
        invented = _numbers(' '.join(cleaned)) - allowed_nums
        if invented:
            error = 'Fatos contêm números ausentes na matéria: ' + ', '.join(sorted(invented))
            continue
        return cleaned[:6]
    raise ValueError('O Ollama não extraiu uma lista válida de 5 a 6 fatos da matéria.')


def turn_range(story, max_words, previous=None):
    """Define a faixa de falas (até 12) conforme o tamanho da matéria e o orçamento de palavras."""
    body_words = len(story.get('body', '').split())
    cap = min(12, max(4, (max_words // 7) * 2))
    if previous is not None:
        return 4, cap
    if body_words >= 220 and max_words >= 85:
        return min(8, cap), cap
    if body_words >= 80 and max_words >= 60:
        return min(6, cap), min(10, cap)
    return 4, min(8, cap)


def validate_lines(raw, story, budget, loop=False, min_lines=4, max_lines=12):
    """Valida o roteiro contra o corpo e título da matéria (nunca contra manchetes relacionadas)."""
    if not story.get('body') and len(source_text(story).split()) < MIN_SOURCE_WORDS:
        # Se não tem corpo nem atinge a fonte mínima, só permite se for teste explícito de manchete com >= 6 palavras,
        # mas em pautas com source_type=='rss' exige corpo.
        if story.get('source_type') == 'rss' or len(source_text(story).split()) < 5:
            raise ValueError('Matéria sem corpo suficiente para validação.')
    lines = raw.get('lines') if isinstance(raw, dict) else None
    if not isinstance(lines, list) or not min_lines <= len(lines) <= max_lines:
        raise ValueError(f'Entregue entre {min_lines} e {max_lines} falas em "lines" '
                         f'(vieram {len(lines) if isinstance(lines, list) else 0}).')
    import engine
    texts = []
    for line in lines:
        text = line.get('text') if isinstance(line, dict) else None
        if not isinstance(text, str):
            raise ValueError('Cada fala precisa de "text".')
        text = engine.clean(re.sub(r'^\s*(sasuke|naruto)\s*[:\-–]\s*', '', text, flags=re.I))
        text = re.sub(r'[#*_]|[\U0001F000-\U0001FFFF]', '', text).strip()
        if not text or len(text) > 320:
            raise ValueError('Falas vazias ou longas demais.')
        if _ForbiddenVehicleMatch(text, story):
            raise ValueError('Proibido citar o nome do veículo ou expressões como "confirmado por" / "segundo o" nas falas.')
        texts.append(text)
    hook_words = len(texts[0].split())
    if hook_words > 10:
        raise ValueError(f'A primeira fala tem {hook_words} palavras; o máximo é 10 palavras.')
    if HOOK_BAD_START_RE.search(texts[0]):
        raise ValueError('A primeira fala não pode começar com "Mas", "E" ou conector solto; faça uma provocação ou pergunta.')
    if story.get('title') and _similar(texts[0], story['title']) > 0.72 and '?' not in texts[0] and '!' not in texts[0]:
        raise ValueError('A primeira fala deve ser uma provocação ou pergunta curta, não a manchete repetida.')
    total = sum(len(t.split()) for t in texts)
    if total > budget:
        raise ValueError(f'O roteiro tem {total} palavras; o máximo é {budget}.')
    source = source_text(story)
    invented = _numbers(' '.join(texts)) - _numbers(source)
    if invented:
        raise ValueError('Números ausentes na matéria: ' + ', '.join(sorted(invented)) +
                         '. Use apenas números presentes no corpo e título da matéria.')
    # Rejeita nomes próprios vindos das manchetes relacionadas que não existem no corpo/título da matéria.
    related_text = '\n'.join(story.get('related', []) + story.get('related_context', []))
    if related_text:
        source_tokens = set(_tokens(source))
        related_only_entities = {e.casefold() for e in _entities(related_text) if _tokens(e) and _tokens(e)[0] not in source_tokens}
        script_tokens = set(_tokens(' '.join(texts)))
        leaked = related_only_entities & script_tokens
        if leaked:
            raise ValueError('Contaminação de manchete relacionada detectada: ' + ', '.join(sorted(leaked)) +
                             '. Use apenas fatos do corpo da matéria principal.')
    needed = 4 if story.get('body') else 1
    if len(_facts(texts, source)) < needed:
        raise ValueError(f'Roteiro genérico. Inclua pelo menos {needed} detalhes concretos da matéria '
                         '(números, nomes de pessoas, empresas, lugares, valores).')
    first = lines[0].get('speaker') if lines[0].get('speaker') in (0, 1) else 0
    result = [{'speaker': (first + i) % 2, 'text': t} for i, t in enumerate(texts)]
    problem = ''
    tail = unicodedata.normalize('NFKD', texts[-1].split()[-1].casefold())
    tail = re.sub(r'[^a-z]', '', ''.join(c for c in tail if not unicodedata.combining(c)))
    if loop:
        if tail not in CONNECTORS:
            problem = 'A última fala deve terminar com um conector aberto para o loop.'
        else:
            result[-1]['text'] = re.sub(r'[\s.!?…,;:]+$', '', texts[-1])
    else:
        if tail in {'porque', 'que', 'pois', 'como', 'quando', 'onde'} and not texts[-1].rstrip().endswith('?'):
            raise ValueError('A última fala não pode terminar cortada em uma ponte incompleta; feche com uma frase completa ou pergunta aberta.')
    return result, problem


def accept_low_score_enabled():
    """Controla se roteiros com nota < 7 podem seguir (--aceitar-nota-baixa ou aceitar_nota_baixa em settings.json)."""
    env = os.getenv('REELFORGE_ACCEPT_LOW_SCORE')
    if env is not None:
        return env.strip().lower() in ('1', 'true', 'yes', 'sim')
    try:
        import engine
        cfg = engine.load_settings()
        return bool(cfg.get('aceitar_nota_baixa', False))
    except Exception:
        return False


def judge_model_name():
    """Modelo usado como juiz no filtro de qualidade (padrão = ollama_model)."""
    env = os.getenv('OLLAMA_JUDGE_MODEL')
    if env:
        return env
    try:
        import engine
        cfg = engine.load_settings()
        return cfg.get('judge_model') or os.getenv('OLLAMA_MODEL') or cfg.get('ollama_model') or 'qwen3.5:4b'
    except Exception:
        return os.getenv('OLLAMA_MODEL') or 'qwen3.5:4b'


def deterministic_check(lines):
    """Checagem determinística complementar ao modelo juiz (desconta 2 pontos se falhar)."""
    issues = []
    texts = [str(l.get('text', '')).strip() for l in lines if isinstance(l, dict)]
    if not texts:
        return 2, ['Roteiro sem falas para avaliação determinística.']
    counts = [len(t.split()) for t in texts]
    avg = sum(counts) / len(counts)
    if not (6 <= avg <= 16):
        issues.append(f'Média de palavras por fala ({avg:.1f}) fora da faixa de 6 a 16.')
    if not any('?' in t for t in texts):
        issues.append('O roteiro precisa ter pelo menos uma pergunta.')
    seen_lines = set()
    repeated = False
    for t in texts:
        norm = re.sub(r'\s+', ' ', re.sub(r'[^\wÀ-ú ]+', '', t).strip().casefold())
        if norm in seen_lines:
            repeated = True
        seen_lines.add(norm)
    if repeated:
        issues.append('Há fala repetida no roteiro.')
    starts = []
    repeated_start = False
    for t in texts:
        words = [re.sub(r'^[^\wÀ-ú]+|[^\wÀ-ú]+$', '', w).casefold() for w in t.split()]
        words = [w for w in words if w]
        if not words:
            continue
        first = words[0]
        if starts.count(first) >= 2:
            repeated_start = True
        starts.append(first)
    if repeated_start:
        issues.append('Fala começando igual a duas anteriores.')
    return (2 if issues else 0), issues


def evaluate_script(lines, story, ask=None, log=print):
    """Passo 3 (Filtro de qualidade): avalia naturalidade, gancho e final de 1 a 10 + checagem determinística."""
    import engine
    judge_model = judge_model_name()
    if ask is None:
        ask_fn = lambda inst, pay: engine.request_json(inst, pay, model=judge_model)
    else:
        ask_fn = ask
    instruction = (
        'Avalie a qualidade deste roteiro de Reel entre Sasuke (0) e Naruto (1) de 1 a 10. '
        'Critérios: '
        '1. Naturalidade: soa como diálogo informal brasileiro entre dois amigos/rivais (sem tom de telejornal)? '
        '2. Gancho: a primeira fala é uma provocação ou pergunta curta (até 10 palavras) e não uma manchete? '
        '3. Final: a última fala fecha bem com uma frase completa (pergunta aberta ou conclusão forte), sem frase quebrada? '
        'Se a nota for menor que 7, liste os problemas objetivos para correção. '
        'Responda APENAS JSON: {"nota":8,"problemas":[]}')
    payload = {
        'manchete': story.get('title', ''),
        'dialogo': [{'personagem': NAMES[l['speaker']], 'fala': l['text']} for l in lines],
    }
    nota = None
    problemas = []
    for _ in range(2):
        try:
            res = ask_fn(instruction, json.dumps(payload, ensure_ascii=False))
        except Exception:
            continue
        if not isinstance(res, dict) or 'nota' not in res or res.get('nota') is None or isinstance(res.get('nota'), bool):
            continue
        try:
            parsed = float(res['nota'])
        except (TypeError, ValueError):
            continue
        nota = max(1.0, min(10.0, parsed))
        problemas = [str(p).strip() for p in (res.get('problemas') or []) if str(p).strip()]
        break
    if nota is None:
        log('Filtro de qualidade: avaliação indisponível no Ollama.')
        problemas = ['avaliação indisponível']
    penalty, det_issues = deterministic_check(lines)
    if det_issues:
        problemas.extend(det_issues)
        if nota is not None:
            nota = max(0.0, nota - penalty)
    if nota is not None:
        nota = int(nota) if float(nota).is_integer() else round(nota, 1)
    return {'nota': nota, 'problemas': problemas}


def _save_rejected_script(story, lines, review):
    import engine
    engine.OUTPUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    path = engine.OUTPUT / f'rejected_{stamp}.json'
    payload = {
        'title': story.get('title', ''),
        'source': story.get('source', ''),
        'nota': review.get('nota'),
        'problemas': review.get('problemas', []),
        'lines': lines,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def write_script(story, kind='Notícia', max_words=60, previous=None, ask=None, loop=None,
                 facts=None, accept_low_score=None, log=print):
    """Passo 1 (extrai 5-6 fatos) -> Passo 2 (escreve diálogo usando só os fatos) -> Filtro de qualidade (nota >= 7)."""
    import engine
    ask = ask or engine.request_json
    loop = loop_enabled() if loop is None else loop
    allow_low = accept_low_score_enabled() if accept_low_score is None else bool(accept_low_score)
    if facts is None and previous is None and story.get('body'):
        facts = extract_facts(story, ask=ask)
    min_lines, max_lines = turn_range(story, max_words, previous)
    target = max(12, min(int(max_words * 0.9), max_lines * 12))
    few_shot = (
        'Exemplos de boas falas (português brasileiro informal, reação direta, sem soar como jornal):\n'
        'Exemplo 1:\n'
        '[{"speaker":0,"text":"Pagar 32 mil reais num aparelho de música faz sentido?"},'
        '{"speaker":1,"text":"Cara, 32 mil?! Quem teve essa ideia maluca?"},'
        '{"speaker":0,"text":"A Sony relançou o Walkman focado só em áudio de estúdio."},'
        '{"speaker":1,"text":"Mas com essa grana dava pra comprar um carro usado, mano!"}]\n'
        'Exemplo 2:\n'
        '[{"speaker":0,"text":"Por que gastariam 3,4 bilhões de dólares só em gerador?"},'
        '{"speaker":1,"text":"Peraí, os data centers não usavam energia limpa?"},'
        '{"speaker":0,"text":"Na teoria sim, mas a rede do Texas não aguenta a Anthropic."},'
        '{"speaker":1,"text":"E aí apelaram pra Caterpillar pra não travar tudo até 2045?"}]\n'
    )
    instruction = (
        'Passo 2 — Roteiro de diálogo: escreva um diálogo para Reel vertical de ' + kind.lower() + ' em português brasileiro. '
        'Sasuke (speaker 0, direto e irônico) e Naruto (speaker 1, curioso e espontâneo) conversam ALTERNANDO as falas.\n'
        + few_shot +
        'Regras obrigatórias:\n'
        '- Use SOMENTE os fatos extraídos da matéria recebidos no JSON. Não invente números, datas, nomes ou causas.\n'
        '- A primeira fala DEVE ser uma provocação ou pergunta instigante de até 10 palavras (NUNCA uma manchete e nunca comece com "Mas...").\n'
        '- PROIBIDO citar o nome do veículo (ex.: g1, UOL, CNN, CPG) ou frases de telejornal ("confirmado por", "segundo o", "nesta segunda-feira").\n'
        f'- Use entre {min_lines} e {max_lines} falas curtas e naturais, com total máximo de {target} palavras.\n'
        '- A última fala deve terminar com uma frase completa (uma pergunta aberta pro público pensar ou um fecho forte), nunca cortada no meio.\n'
        'Responda APENAS JSON: {"lines":[{"speaker":0,"text":"..."},{"speaker":1,"text":"..."}]}'
    )
    payload = {
        'manchete_referencia': story.get('title', ''),
        'fatos_da_materia': facts if facts else [source_text(story)],
    }
    if story.get('related_context'):
        payload['contexto_tematico_nao_usar_nomes_nem_numeros'] = story['related_context']
    if previous:
        payload['roteiro_anterior'] = previous
        payload['pedido'] = f'Reescreva mais curto, com no máximo {target} palavras, mantendo a provocação inicial e os fatos.'
    error = ''
    best_lines = None
    last_review = None
    quality_retries = 0
    max_attempts = 8
    for _ in range(max_attempts):
        if error:
            payload['correcao'] = error
        prefetched = story.pop('_prefetched_raw', None)
        raw = prefetched if prefetched is not None else ask(instruction, json.dumps(payload, ensure_ascii=False))
        try:
            lines, problem = validate_lines(raw, story, max_words, loop, min_lines, max_lines)
        except ValueError as exc:
            error = str(exc)
            continue
        if problem:
            error = problem
            best_lines = lines
            continue
        review = evaluate_script(lines, story, ask=ask, log=log)
        last_review = review
        story['quality_score'] = review['nota']
        story['quality_issues'] = review['problemas']
        best_lines = lines
        approved = review['nota'] is not None and review['nota'] >= 7
        if approved:
            return lines
        if quality_retries >= 2:
            if allow_low:
                log(f'Aviso: seguindo com nota {review["nota"]} (--aceitar-nota-baixa / aceitar_nota_baixa ativo).')
                return lines
            rejected_path = _save_rejected_script(story, lines, review)
            issues = '; '.join(review['problemas']) or 'nota abaixo do mínimo'
            raise RuntimeError(
                f'Roteiro reprovado no filtro de qualidade após 2 regenerações '
                f'(nota {review["nota"]}/10; problemas: {issues}). Salvo em {rejected_path}.'
            )
        quality_retries += 1
        issues = '; '.join(review['problemas']) or 'Melhore a naturalidade, o gancho provocativo e o fecho.'
        error = f'Nota de qualidade {review["nota"]}/10 (mínimo 7). Corrija: {issues}'
    if best_lines is not None and last_review is not None:
        if allow_low:
            return best_lines
        rejected_path = _save_rejected_script(story, best_lines, last_review)
        issues = '; '.join(last_review['problemas']) or 'nota abaixo do mínimo'
        raise RuntimeError(
            f'Roteiro reprovado no filtro de qualidade (nota {last_review["nota"]}/10; '
            f'problemas: {issues}). Salvo em {rejected_path}.'
        )
    if best_lines:
        return best_lines
    raise RuntimeError('O Ollama não produziu um roteiro válido: ' + error)


def shorten(story, lines, max_words, ask=None):
    previous = [{'speaker': l['speaker'], 'text': l['text']} for l in lines]
    return write_script(story, story.get('kind', 'Notícia'), max_words, previous, ask, facts=story.get('extracted_facts'))


def automatic(topic, kind='Notícia', max_seconds=45, allow_repeat=False, log=print, ask=None):
    """Fluxo completo em dois passos + filtro de qualidade: RSS -> pauta -> 5-6 fatos -> roteiro -> avaliação."""
    import engine
    log('Lendo os RSS dos veículos e do Google News...')
    candidates = gather_candidates(topic, kind, 15, allow_repeat)
    full = sum(1 for c in candidates if c['body'])
    log(f'{len(candidates)} pautas analisadas ({full} com matéria completa). Ollama escolhendo...')
    story = pick_story(candidates, kind, ask)
    require_minimum_source(story)
    log('Pauta escolhida: ' + story['title'] + (' — ' + story['source'] if story['source'] else '') +
        ('\nMotivo: ' + story['selection_reason'] if story.get('selection_reason') else ''))
    story['related_context'] = enrich(story, limit=4)
    log(f'Passo 1: Extraindo de 5 a 6 fatos verificados da matéria ({len(story["body"].split())} palavras)...')
    facts = extract_facts(story, ask=ask)
    story['extracted_facts'] = facts
    log(f'Passo 2: Escrevendo diálogo informal a partir de {len(facts)} fatos...')
    story.update(kind=kind, source_type='rss', summary=source_text(story))
    lines = write_script(story, kind, engine.word_budget(max_seconds), ask=ask, facts=facts)
    log(f'Filtro de qualidade: nota {story.get("quality_score", "-")}/10.')
    return story, lines
