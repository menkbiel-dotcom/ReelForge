"""Search public YouTube metadata without downloading video files."""
import json
import queue
import re
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk, messagebox
from urllib.parse import urlencode

PRESETS = {
    'Minecraft parkour': 'minecraft parkour gameplay no commentary 60fps',
    'GTA corrida': 'GTA 5 stunt race gameplay no commentary 60fps',
    'GTA mega rampa': 'GTA 5 mega ramp gameplay no commentary',
    'Minecraft movimento fluido': 'minecraft speed parkour satisfying gameplay no commentary',
}


def build_query(category, extra=''):
    return (PRESETS[category] + ' ' + extra.strip()).strip()


def rank_entry(entry, category):
    """A title-based match score, not a prediction of attention or dopamine."""
    title = str(entry.get('title') or '')
    lower = title.lower()
    terms = ('minecraft', 'parkour') if category.startswith('Minecraft') else ('gta', 'race', 'racing', 'corrida', 'ramp', 'rampa')
    reasons = [term for term in terms if term in lower]
    bonus = [term for term in ('no commentary', 'sem comentários', '60fps', '60 fps', 'gameplay') if term in lower]
    score = len(reasons) * 3 + len(bonus)
    for term in ('tutorial', 'review', 'reaction'):
        if term in lower:
            score -= 3
    return score, reasons + bonus


def search(category, extra='', minimum=60, limit=15):
    from yt_dlp import YoutubeDL
    query = build_query(category, extra)
    options = {'quiet': True, 'no_warnings': True, 'extract_flat': True,
               'skip_download': True, 'socket_timeout': 15, 'retries': 1,
               'extractor_retries': 1, 'ignoreerrors': False}
    with YoutubeDL(options) as client:
        data = client.extract_info(f'ytsearch30:{query}', download=False)
    results = []
    seen = set()
    for entry in (data or {}).get('entries') or []:
        if not entry or entry.get('live_status') in ('is_live', 'is_upcoming'):
            continue
        video_id = str(entry.get('id') or '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id) or video_id in seen:
            continue
        duration = entry.get('duration')
        if duration is not None and duration < minimum:
            continue
        score, reasons = rank_entry(entry, category)
        results.append({'title': entry.get('title') or video_id,
                        'channel': entry.get('channel') or entry.get('uploader') or 'Não informado',
                        'duration': duration, 'score': score, 'reasons': reasons,
                        'url': f'https://www.youtube.com/watch?v={video_id}'})
        seen.add(video_id)
    return sorted(results, key=lambda item: item['score'], reverse=True)[:limit]


def duration_label(seconds):
    if seconds is None:
        return '?'
    seconds = int(seconds)
    return f'{seconds // 60}:{seconds % 60:02d}'


class SearchWindow(tk.Toplevel):
    def __init__(self, parent, home):
        super().__init__(parent)
        self.title('Buscar gameplays — ReelForge')
        self.geometry('1000x620')
        self.minsize(760, 480)
        self.home = Path(home)
        self.results = []
        self.events = queue.Queue()
        self.category = tk.StringVar(value=next(iter(PRESETS)))
        self.extra = tk.StringVar()
        self.minimum = tk.StringVar(value='60')
        self.status = tk.StringVar(value='Escolha um estilo e clique em Buscar.')
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Gameplays para fundo de Reels', font=('Segoe UI', 17, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='Foco em movimento contínuo, parkour e corridas. A busca não mede dopamina.').pack(anchor='w', pady=(4, 12))
        filters = ttk.Frame(frame)
        filters.pack(fill='x')
        ttk.Combobox(filters, textvariable=self.category, values=list(PRESETS), state='readonly', width=30).pack(side='left')
        ttk.Label(filters, text='  Duração mínima (s): ').pack(side='left')
        ttk.Spinbox(filters, from_=0, to=3600, increment=30, textvariable=self.minimum, width=7).pack(side='left')
        self.button = ttk.Button(filters, text='Buscar', command=self._search)
        self.button.pack(side='left', padx=10)
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=10)
        ttk.Label(row, text='Termos extras: ').pack(side='left')
        ttk.Entry(row, textvariable=self.extra).pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='Pesquisar no YouTube', command=self._browser_search).pack(side='left', padx=(8, 0))
        ttk.Label(frame, textvariable=self.status, wraplength=920).pack(anchor='w', pady=(0, 8))
        table = ttk.Frame(frame)
        table.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(table, columns=('title', 'channel', 'duration', 'score'), show='headings', selectmode='browse')
        for name, label, width in [('title', 'Vídeo', 470), ('channel', 'Canal', 190), ('duration', 'Duração', 80), ('score', 'Afinidade', 75)]:
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, stretch=name in ('title', 'channel'))
        scroll = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        self.tree.bind('<Double-1>', lambda event: self._open())
        self.tree.bind('<<TreeviewSelect>>', self._details)
        self.details = tk.StringVar(value='Afinidade considera apenas palavras no título; duração desconhecida aparece como ?.')
        ttk.Label(frame, textvariable=self.details, wraplength=920).pack(anchor='w', pady=8)
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x')
        ttk.Button(buttons, text='Assistir selecionado', command=self._open).pack(side='left')
        ttk.Button(buttons, text='Copiar link', command=self._copy).pack(side='left', padx=8)
        ttk.Button(buttons, text='Salvar lista', command=self._save).pack(side='left')
        ttk.Button(buttons, text='Usar MP4 do computador', command=parent._choose).pack(side='right')
        ttk.Label(frame, text='Confira a licença ou autorização do autor antes de reutilizar. A busca não confirma permissões.').pack(anchor='w', pady=(10, 0))
        self.after(150, self._poll)

    def _browser_search(self):
        webbrowser.open('https://www.youtube.com/results?' + urlencode({'search_query': build_query(self.category.get(), self.extra.get())}))

    def _search(self):
        try:
            minimum = int(self.minimum.get())
            if not 0 <= minimum <= 3600:
                raise ValueError
        except ValueError:
            messagebox.showerror('Duração', 'Informe um número entre 0 e 3600.', parent=self)
            return
        category, extra = self.category.get(), self.extra.get()
        self.button.configure(state='disabled')
        self.results = []
        self.tree.delete(*self.tree.get_children())
        self.status.set('Buscando no YouTube…')
        events = self.events
        def worker():
            try:
                events.put(('results', search(category, extra, minimum)))
            except Exception as exc:
                events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                self.button.configure(state='normal')
                if kind == 'error':
                    self.status.set('Busca indisponível. Tente novamente ou use Pesquisar no YouTube.')
                    self.details.set(value[:500])
                else:
                    self.results = value
                    for index, result in enumerate(value):
                        self.tree.insert('', 'end', iid=str(index), values=(result['title'], result['channel'], duration_label(result['duration']), result['score']))
                    self.status.set(f'{len(value)} resultados. Ordenados por afinidade do título; não por popularidade ou retenção.' if value else 'Nenhum resultado. Reduza a duração mínima ou altere os termos.')
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def _selected(self):
        selected = self.tree.selection()
        return self.results[int(selected[0])] if selected else None

    def _details(self, event=None):
        result = self._selected()
        if result:
            self.details.set('Termos encontrados: ' + (', '.join(result['reasons']) or 'nenhum') + '. Assista para conferir fluidez, cortes e enquadramento.')

    def _open(self):
        result = self._selected()
        if result:
            webbrowser.open(result['url'])

    def _copy(self):
        result = self._selected()
        if result:
            self.clipboard_clear()
            self.clipboard_append(result['url'])
            self.status.set('Link copiado.')

    def _save(self):
        if not self.results:
            return
        path = self.home / 'gameplays_encontrados.json'
        try:
            path.write_text(json.dumps(self.results, ensure_ascii=False, indent=2), encoding='utf-8')
            self.status.set(f'Lista salva em {path}')
        except OSError as exc:
            messagebox.showerror('Salvar lista', str(exc), parent=self)
