"""Import approved scripts and optional per-turn recordings as plain data."""
import hashlib
import json
from pathlib import Path


def load(path):
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding='utf-8'))
    lines = data.get('lines')
    if not isinstance(lines, list) or not 1 <= len(lines) <= 30:
        raise ValueError('Forneça de 1 a 30 falas em lines.')
    prepared = []
    for line in lines:
        if type(line.get('speaker')) is not int or line['speaker'] not in (0, 1):
            raise ValueError('speaker deve ser 0 (Sasuke) ou 1 (Naruto).')
        text = line.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError('Cada fala precisa de text com até 2.000 caracteres.')
        item = {'speaker': line['speaker'], 'text': ' '.join(text.split())}
        if line.get('voice_provisional'):
            item['voice_provisional'] = True
        if line.get('audio'):
            audio = (path.parent / line['audio']).resolve()
            if not audio.is_file():
                raise ValueError('Gravação não encontrada: ' + audio.name)
            item.update(audio=audio, audio_source='imported')
        prepared.append(item)
    title = str(data.get('title', 'Roteiro fornecido'))[:200]
    story = {'id': hashlib.sha256(path.read_bytes()).hexdigest(), 'title': title,
             'summary': ' '.join(l['text'] for l in prepared), 'url': str(data.get('url', '')),
             'source_type': 'roteiro importado'}
    background = (path.parent / data['gameplay']).resolve() if data.get('gameplay') else None
    if background and not background.is_file():
        raise ValueError('Fundo não encontrado.')
    return story, prepared, background
