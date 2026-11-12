"""ReelForge GUI for Windows; standard-library Tkinter with modern Dark Studio theme."""
import copy
import ctypes
import json
import os
import queue
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from dotenv import load_dotenv
import engine

HOME = Path(os.environ.get('REELFORGE_HOME', Path(__file__).resolve().parent))
HOME.mkdir(parents=True, exist_ok=True)
load_dotenv(HOME / '.env')
CONFIG = HOME / 'settings.json'

TOPIC_PRESETS = ['tecnologia', 'economia', 'ciência', 'educação', 'mundo', 'cultura', 'esporte']
DEFAULT_OLLAMA_MODELS = ['qwen3.5:4b', 'qwen3:8b', 'qwen2.5:7b']


def list_ollama_models(timeout: float = 1.5) -> list[str]:
    """Lista os modelos instalados no Ollama local (GET /api/tags).
    Fallback para a lista padrão caso o Ollama esteja offline ou a requisição falhe.
    """
    try:
        import requests
        r = requests.get('http://127.0.0.1:11434/api/tags', timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            models = []
            for item in data.get('models', []):
                if isinstance(item, dict) and item.get('name'):
                    name = item['name']
                    if name not in models:
                        models.append(name)
            if models:
                return models
    except Exception:
        pass
    return list(DEFAULT_OLLAMA_MODELS)


def merge_settings(current: dict, from_ui: dict) -> dict:
    """Mescla configurações atuais de settings.json com as alterações da UI sem perder chaves desconhecidas."""
    merged = copy.deepcopy(current) if isinstance(current, dict) else {}
    if not isinstance(from_ui, dict):
        return merged
    for k, v in from_ui.items():
        if k == 'voice_profiles' and isinstance(v, dict):
            existing_profiles = merged.get('voice_profiles')
            if not isinstance(existing_profiles, dict):
                existing_profiles = {}
            new_profiles = copy.deepcopy(existing_profiles)
            for prov, prof in v.items():
                if isinstance(prof, dict):
                    prov_existing = new_profiles.get(prov, {})
                    if isinstance(prov_existing, dict):
                        merged_prof = copy.deepcopy(prov_existing)
                        merged_prof.update(prof)
                        new_profiles[prov] = merged_prof
                    else:
                        new_profiles[prov] = copy.deepcopy(prof)
                else:
                    new_profiles[prov] = prof
            merged['voice_profiles'] = new_profiles
        else:
            merged[k] = v
    return merged


class App(tk.Tk):
    def __init__(self):
        if os.name == 'nt':
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('reelforge.studio.app')
            except Exception:
                pass
        super().__init__()
        self.title('ReelForge Studio — Gerador de Reels')
        self.geometry('1060x860')
        self.minsize(940, 740)
        try:
            import icon_gen
            icon_path = icon_gen.ensure_icon(HOME)
            self.iconbitmap(default=str(icon_path))
        except Exception:
            pass
        self.events = queue.Queue()
        self.news = None
        self.lines = None
        self.direction = None
        self.source_override = None
        self.imported_script = None
        self.preview_signature = None
        self.director_files = []
        self.busy = False
        try:
            saved = json.loads(CONFIG.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            saved = {}
        self._ollama_models = list_ollama_models()
        saved_writer = saved.get('ollama_model', 'qwen3.5:4b')
        if saved_writer and saved_writer not in self._ollama_models:
            self._ollama_models.insert(0, saved_writer)

        saved_judge = saved.get('judge_model')
        if not saved_judge:
            initial_judge = 'igual ao redator'
        else:
            initial_judge = saved_judge

        judge_options = ['igual ao redator']
        for m in self._ollama_models:
            if m not in judge_options:
                judge_options.append(m)
        if saved_judge and saved_judge not in judge_options:
            judge_options.append(saved_judge)
        self._judge_models = judge_options

        self.topic = tk.StringVar(value=saved.get('topic', 'tecnologia'))
        self.provider = tk.StringVar(value=saved.get('provider', 'ollama'))
        self.ollama_model = tk.StringVar(value=saved_writer)
        self.judge_model = tk.StringVar(value=initial_judge)
        self.aceitar_nota_baixa = tk.BooleanVar(value=bool(saved.get('aceitar_nota_baixa', False)))
        self.openai_key = tk.StringVar(value='')
        self.gameplay = tk.StringVar(value=saved.get('gameplay', ''))
        self.bg_brightness = tk.StringVar(value=str(saved.get('gameplay_brightness', -0.06)))
        self.bg_gamma = tk.StringVar(value=str(saved.get('gameplay_gamma', 0.85)))
        self.loop = tk.BooleanVar(value=saved.get('loop', True))
        self.script_loop = tk.BooleanVar(value=saved.get('script_loop', False))
        self.music_enabled = tk.BooleanVar(value=saved.get('music_enabled', True))
        self.music_volume = tk.StringVar(value=str(saved.get('music_volume', 8)))
        self.fps = tk.StringVar(value=str(saved.get('fps', 30)))
        self.max_seconds = tk.StringVar(value=str(saved.get('max_seconds', 60)))
        self.impact_opening = tk.BooleanVar(value=saved.get('impact_opening', True))
        self.hook_text = tk.StringVar()
        self.director_enabled = tk.BooleanVar(value=saved.get('director_enabled', False))
        self.content_kind = tk.StringVar(value=saved.get('content_kind', 'Notícia'))
        self.repeat = tk.BooleanVar(value=False)
        self.voice_profiles = saved.get('voice_profiles', {})
        self.voice_keys = {'edge': '', 'voicestudio': '', 'elevenlabs': os.getenv('ELEVENLABS_API_KEY', ''), 'fish': os.getenv('FISH_API_KEY', '')}
        self.tts_provider = tk.StringVar(value=saved.get('tts_provider', 'voicestudio'))
        self.voice_a = tk.StringVar(); self.voice_b = tk.StringVar(); self.tts_model = tk.StringVar()
        self.rate_a = tk.StringVar(); self.rate_b = tk.StringVar()
        self.pitch_a = tk.StringVar(); self.pitch_b = tk.StringVar()
        self.tts_key = tk.StringVar()
        self.caption_timing = tk.StringVar(value=saved.get('caption_timing', 'whisper'))
        self.whisper_model = tk.StringVar(value=saved.get('whisper_model', 'small'))
        self._shown_voice_provider = None
        self._switch_voice_provider()
        self.status = tk.StringVar(value='Pronto. Escolha um tema ou clique em "Criar plano + roteiro".')
        self._apply_theme()
        self._layout()
        self.after(120, self._poll)

    def _apply_theme(self):
        bg = '#0F1420'
        card = '#161E2E'
        field = '#1E293B'
        fg = '#F1F5F9'
        muted = '#94A3B8'
        accent = '#FF5A1F'
        cyan = '#2DDCFF'
        border = '#2B384E'
        self.configure(bg=bg)
        style = ttk.Style(self)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('.', background=bg, foreground=fg, fieldbackground=field, bordercolor=border,
                        darkcolor=card, lightcolor=card, font=('Segoe UI', 10))
        style.configure('TFrame', background=bg)
        style.configure('Card.TFrame', background=card)
        style.configure('TLabel', background=bg, foreground=fg)
        style.configure('Card.TLabel', background=card, foreground=fg)
        style.configure('Muted.TLabel', background=bg, foreground=muted, font=('Segoe UI', 9))
        style.configure('CardMuted.TLabel', background=card, foreground=muted, font=('Segoe UI', 9))
        style.configure('Title.TLabel', background=bg, foreground='#FFFFFF', font=('Segoe UI Semibold', 21, 'bold'))
        style.configure('Subtitle.TLabel', background=bg, foreground=cyan, font=('Segoe UI', 10))
        style.configure('TLabelframe', background=card, bordercolor=border, relief='solid')
        style.configure('TLabelframe.Label', background=card, foreground=cyan, font=('Segoe UI Semibold', 10, 'bold'))
        style.configure('TNotebook', background=bg, bordercolor=border, tabmargins=(0, 4, 0, 0))
        style.configure('TNotebook.Tab', background='#131A29', foreground=muted, padding=(16, 8),
                        font=('Segoe UI Semibold', 10))
        style.map('TNotebook.Tab',
                  background=[('selected', card), ('active', '#1C263B')],
                  foreground=[('selected', '#FFFFFF'), ('active', fg)])
        style.configure('TEntry', fieldbackground=field, foreground=fg, insertcolor=fg, padding=5)
        style.map('TEntry', fieldbackground=[('readonly', '#131A29')])
        style.configure('TCombobox', fieldbackground=field, background=card, foreground=fg, arrowcolor=cyan, padding=5)
        style.map('TCombobox', fieldbackground=[('readonly', field)], foreground=[('readonly', fg)],
                  selectbackground=[('readonly', field)], selectforeground=[('readonly', fg)])
        style.configure('TSpinbox', fieldbackground=field, background=card, foreground=fg, arrowcolor=cyan, padding=4)
        style.configure('TCheckbutton', background=card, foreground=fg, indicatorbackground=field, indicatorforeground=accent)
        style.map('TCheckbutton', background=[('active', card)], foreground=[('active', '#FFFFFF')])
        style.configure('TButton', background='#243147', foreground=fg, bordercolor='#334360',
                        focusthickness=0, padding=(12, 6), font=('Segoe UI Semibold', 9))
        style.map('TButton',
                  background=[('active', '#2F405D'), ('disabled', '#172030')],
                  foreground=[('disabled', '#526077')])
        style.configure('Accent.TButton', background=accent, foreground='#FFFFFF', bordercolor='#FF7847',
                        padding=(15, 7), font=('Segoe UI Semibold', 10, 'bold'))
        style.map('Accent.TButton',
                  background=[('active', '#FF703D'), ('disabled', '#5A3224')],
                  foreground=[('disabled', '#8E756B')])
        style.configure('Secondary.TButton', background='#1B3A4B', foreground=cyan, bordercolor='#245269',
                        padding=(13, 7), font=('Segoe UI Semibold', 10))
        style.map('Secondary.TButton',
                  background=[('active', '#234B61'), ('disabled', '#16242E')],
                  foreground=[('disabled', '#4B6775')])
        style.configure('Chip.TButton', background='#1B2538', foreground='#CBD5E1', bordercolor='#2B384E',
                        padding=(9, 3), font=('Segoe UI', 9))
        style.map('Chip.TButton', background=[('active', '#2A3956')], foreground=[('active', '#FFFFFF')])
        style.configure('Horizontal.TProgressbar', troughcolor='#131A29', background=cyan, bordercolor=border,
                        lightcolor=cyan, darkcolor=accent, thickness=6)

    def _layout(self):
        frame = ttk.Frame(self, padding=(20, 14))
        frame.pack(fill='both', expand=True)

        header = ttk.Frame(frame)
        header.pack(fill='x', pady=(0, 10))
        title_box = ttk.Frame(header)
        title_box.pack(side='left', fill='x', expand=True)
        ttk.Label(title_box, text='ReelForge Studio', style='Title.TLabel').pack(anchor='w')
        ttk.Label(title_box, text='Sasuke & Naruto · Diálogos naturais em 2 passos · Filtro de qualidade · 1080x1920',
                  style='Subtitle.TLabel').pack(anchor='w', pady=(2, 0))
        header_btns = ttk.Frame(header)
        header_btns.pack(side='right', anchor='ne')
        ttk.Button(header_btns, text='Atalho no Desktop', command=self._create_shortcut).pack(side='right', padx=(6, 0))
        ttk.Button(header_btns, text='Pasta de saída', command=self._open_output).pack(side='right')

        tabs = ttk.Notebook(frame)
        tabs.pack(fill='x')
        content = ttk.Frame(tabs, padding=14, style='Card.TFrame')
        video = ttk.Frame(tabs, padding=14, style='Card.TFrame')
        voices = ttk.Frame(tabs, padding=14, style='Card.TFrame')
        tabs.add(content, text='  Conteúdo e Pauta IA  ')
        tabs.add(video, text='  Vídeo, Loop e Música  ')
        tabs.add(voices, text='  Vozes e Legendas  ')
        for panel in (content, video, voices):
            panel.columnconfigure(1, weight=1)

        def entry(panel, row, label, var, show=None):
            ttk.Label(panel, text=label, style='Card.TLabel').grid(row=row, column=0, sticky='w', pady=4, padx=(0, 12))
            field = ttk.Entry(panel, textvariable=var, show=show)
            field.grid(row=row, column=1, columnspan=2, sticky='ew', pady=4)
            return field

        entry(content, 0, 'Assunto / Tema', self.topic)
        chips = ttk.Frame(content, style='Card.TFrame')
        chips.grid(row=1, column=1, columnspan=2, sticky='w', pady=(0, 6))
        ttk.Label(content, text='Temas rápidos', style='CardMuted.TLabel').grid(row=1, column=0, sticky='w', pady=(0, 6))
        for preset in TOPIC_PRESETS:
            ttk.Button(chips, text=preset.capitalize(), style='Chip.TButton',
                       command=lambda p=preset: self.topic.set(p)).pack(side='left', padx=(0, 5))

        ttk.Label(content, text='Formato do Reel', style='Card.TLabel').grid(row=2, column=0, sticky='w')
        ttk.Combobox(content, textvariable=self.content_kind, values=['Notícia', 'Curiosidade', 'História'],
                     state='readonly').grid(row=2, column=1, sticky='ew', pady=4)
        ttk.Button(content, text='Fonte manual (opcional)', command=self._edit_source).grid(row=2, column=2, padx=(10, 0))

        ttk.Label(content, text='Motor de IA local', style='Card.TLabel').grid(row=3, column=0, sticky='w')
        ai_row = ttk.Frame(content, style='Card.TFrame')
        ai_row.grid(row=3, column=1, columnspan=2, sticky='ew', pady=4)
        ttk.Combobox(ai_row, textvariable=self.provider, values=['ollama'], state='readonly', width=9).pack(side='left')
        ttk.Label(ai_row, text='Modelo:', style='Card.TLabel').pack(side='left', padx=(10, 4))
        ttk.Combobox(ai_row, textvariable=self.ollama_model, values=self._ollama_models, width=14).pack(side='left')
        ttk.Label(ai_row, text='Modelo do juiz:', style='Card.TLabel').pack(side='left', padx=(10, 4))
        ttk.Combobox(ai_row, textvariable=self.judge_model, values=self._judge_models, width=16).pack(side='left')

        ttk.Checkbutton(content, text='Permitir repetir notícia já usada anteriormente',
                        variable=self.repeat).grid(row=4, column=0, columnspan=3, sticky='w', pady=(6, 2))
        ttk.Checkbutton(content, text='Diretor IA (apenas quando você cola um texto manual em "Fonte manual")',
                        variable=self.director_enabled).grid(row=5, column=0, columnspan=3, sticky='w', pady=2)
        ttk.Checkbutton(content, text='Aceitar nota baixa (apenas testes)',
                        variable=self.aceitar_nota_baixa).grid(row=6, column=0, columnspan=3, sticky='w', pady=2)
        ttk.Label(content,
                  text='Fluxo automático em 2 passos: extrai 5–6 fatos verificados da matéria, escreve o diálogo informal e avalia nota >= 7.',
                  style='CardMuted.TLabel').grid(row=7, column=0, columnspan=3, sticky='w', pady=(8, 0))

        entry(video, 0, 'Gameplay de fundo', self.gameplay)
        ttk.Button(video, text='Escolher MP4...', command=self._choose).grid(row=0, column=3, padx=(8, 0), pady=4)
        ttk.Label(video, text='Se vazio, usa automaticamente assets\\gameplay\\gameplay_principal.mp4 em um ponto sorteado.',
                  style='CardMuted.TLabel').grid(row=1, column=0, columnspan=4, sticky='w', pady=(0, 6))

        v_row = ttk.Frame(video, style='Card.TFrame')
        v_row.grid(row=2, column=0, columnspan=4, sticky='ew', pady=4)
        ttk.Label(v_row, text='Duração máx. (s):', style='Card.TLabel').pack(side='left')
        ttk.Spinbox(v_row, from_=10, to=180, increment=5, textvariable=self.max_seconds, width=7).pack(side='left', padx=(6, 18))
        ttk.Label(v_row, text='FPS:', style='Card.TLabel').pack(side='left')
        ttk.Combobox(v_row, textvariable=self.fps, values=['30', '60'], state='readonly', width=6).pack(side='left', padx=(6, 18))
        ttk.Label(v_row, text='Brilho (-0.30 a 0.30):', style='Card.TLabel').pack(side='left')
        ttk.Entry(v_row, textvariable=self.bg_brightness, width=7).pack(side='left', padx=(6, 18))
        ttk.Label(v_row, text='Gama (0.50 a 1.50):', style='Card.TLabel').pack(side='left')
        ttk.Entry(v_row, textvariable=self.bg_gamma, width=7).pack(side='left', padx=(6, 0))

        ttk.Checkbutton(video, text='Crossfade visual e musical de Loop no vídeo',
                        variable=self.loop).grid(row=3, column=0, columnspan=4, sticky='w', pady=(8, 4))

        m_row = ttk.Frame(video, style='Card.TFrame')
        m_row.grid(row=4, column=0, columnspan=4, sticky='ew', pady=4)
        ttk.Checkbutton(m_row, text='Música de fundo baixa', variable=self.music_enabled).pack(side='left')
        ttk.Label(m_row, text='Volume (%):', style='Card.TLabel').pack(side='left', padx=(16, 6))
        ttk.Spinbox(m_row, from_=0, to=40, increment=1, textvariable=self.music_volume, width=6).pack(side='left')
        ttk.Button(m_row, text='Abrir pasta assets\\music', command=self._open_music).pack(side='left', padx=(12, 0))

        ttk.Checkbutton(video, text='Abertura de impacto (modo manual)',
                        variable=self.impact_opening).grid(row=5, column=0, columnspan=4, sticky='w', pady=(6, 2))
        entry(video, 6, 'Gancho no topo (opcional)', self.hook_text)

        ttk.Label(voices, text='Serviço de voz', style='Card.TLabel').grid(row=0, column=0, sticky='w')
        chooser = ttk.Combobox(voices, textvariable=self.tts_provider, values=['voicestudio', 'edge', 'elevenlabs', 'fish'], state='readonly')
        chooser.grid(row=0, column=1, columnspan=2, sticky='ew', pady=4)
        chooser.bind('<<ComboboxSelected>>', lambda event: self._switch_voice_provider())
        entry(voices, 1, 'Perfil de Sasuke (speaker 0)', self.voice_a)
        entry(voices, 2, 'Perfil de Naruto (speaker 1)', self.voice_b)
        entry(voices, 3, 'Modelo do serviço', self.tts_model)
        for row, name, rate, pitch in [(5, 'Sasuke', self.rate_a, self.pitch_a), (6, 'Naruto', self.rate_b, self.pitch_b)]:
            ttk.Label(voices, text=name + ': velocidade (%) / tom (Hz)', style='Card.TLabel').grid(row=row, column=0, sticky='w', pady=4)
            ttk.Spinbox(voices, from_=-30, to=20, textvariable=rate, width=8).grid(row=row, column=1, sticky='w')
            ttk.Spinbox(voices, from_=-30, to=30, textvariable=pitch, width=8).grid(row=row, column=2, sticky='w')
        ttk.Label(voices, text='Sincronização das legendas', style='Card.TLabel').grid(row=7, column=0, sticky='w', pady=(6, 4))
        c_row = ttk.Frame(voices, style='Card.TFrame')
        c_row.grid(row=7, column=1, columnspan=2, sticky='ew', pady=(6, 4))
        ttk.Combobox(c_row, textvariable=self.caption_timing, values=['auto', 'whisper', 'estimated'],
                     state='readonly', width=14).pack(side='left')
        ttk.Label(c_row, text='Modelo Whisper:', style='Card.TLabel').pack(side='left', padx=(16, 6))
        ttk.Combobox(c_row, textvariable=self.whisper_model, values=['small', 'base'], state='readonly', width=10).pack(side='left')
        ttk.Label(voices, text='Whisper padrão: small (veja o README para baixar o modelo small em output/alignment_cache/models).',
                  style='CardMuted.TLabel').grid(row=8, column=0, columnspan=3, sticky='w', pady=(6, 0))

        actions = ttk.Frame(frame)
        actions.pack(fill='x', pady=(12, 6))
        self.buttons = []
        primary_specs = [
            ('Criar plano + roteiro', self._preview, 'Secondary.TButton'),
            ('Editar falas', self._edit_lines, 'TButton'),
            ('Ouvir prévia', self._preview_audio, 'TButton'),
            ('Gerar Reel', self._generate, 'Accent.TButton'),
            ('Verificar ambiente', self._check, 'TButton'),
            ('Exemplo offline', self._load_example, 'TButton'),
            ('Importar JSON', self._import_script, 'TButton'),
        ]
        for label, action, btn_style in primary_specs:
            button = ttk.Button(actions, text=label, style=btn_style, command=action)
            button.pack(side='left', padx=(0, 8))
            self.buttons.append(button)
        ttk.Button(actions, text='Limpar log', command=self._clear_log).pack(side='right')

        status_row = ttk.Frame(frame)
        status_row.pack(fill='x', pady=(2, 6))
        ttk.Label(status_row, textvariable=self.status, wraplength=780).pack(side='left', anchor='w')
        self.progress = ttk.Progressbar(status_row, mode='indeterminate', length=200)
        self.progress.pack(side='right', padx=(10, 0))

        area = ttk.LabelFrame(frame, text=' Console de Pauta, Diálogo (Sasuke & Naruto) e Progresso ', padding=8)
        area.pack(fill='both', expand=True)
        self.log = tk.Text(area, wrap='word', font=('Consolas', 10), state='disabled', height=10,
                           bg='#0B0F19', fg='#E2E8F0', insertbackground='#FFFFFF', relief='flat', padx=10, pady=8)
        self.log.tag_configure('sasuke', foreground='#2DDCFF', font=('Consolas', 10, 'bold'))
        self.log.tag_configure('naruto', foreground='#FF8A3D', font=('Consolas', 10, 'bold'))
        self.log.tag_configure('headline', foreground='#FACC15', font=('Consolas', 10, 'bold'))
        self.log.tag_configure('error', foreground='#F87171')
        scrollbar = ttk.Scrollbar(area, orient='vertical', command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')

    def _clear_log(self):
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')

    def _create_shortcut(self):
        try:
            import icon_gen
            lnk = icon_gen.create_desktop_shortcut(HOME)
            self._write('Atalho atualizado na Área de Trabalho: ' + str(lnk))
            self.status.set('Ícone e atalho criados na sua Área de Trabalho.')
        except Exception as exc:
            messagebox.showerror('Atalho', str(exc))

    def _edit_lines(self):
        if not self.lines:
            messagebox.showinfo('Roteiro', 'Primeiro clique em "Criar plano + roteiro" para gerar as falas, depois edite se quiser.')
            return
        window = tk.Toplevel(self)
        window.title('Editar diálogo — Sasuke e Naruto')
        window.geometry('760x540')
        window.configure(bg='#0F1420')
        frame = ttk.Frame(window, padding=16, style='Card.TFrame')
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Ajuste as falas como preferir (uma fala por linha no formato "Sasuke: ..." ou "Naruto: ..."):',
                  style='Card.TLabel').pack(anchor='w', pady=(0, 8))
        editor = tk.Text(frame, wrap='word', font=('Consolas', 11), bg='#0B0F19', fg='#F1F5F9',
                         insertbackground='#FFFFFF', relief='flat', padx=10, pady=10, height=16)
        editor.pack(fill='both', expand=True)
        content_lines = [f'{engine.NAMES[l["speaker"]]}: {l["text"]}' for l in self.lines]
        editor.insert('1.0', '\n'.join(content_lines))

        def save_edits():
            raw_lines = [ln.strip() for ln in editor.get('1.0', 'end').splitlines() if ln.strip()]
            if len(raw_lines) < 2 or len(raw_lines) > 12:
                messagebox.showerror('Roteiro', 'Mantenha entre 2 e 12 falas.', parent=window)
                return
            updated = []
            for idx, ln in enumerate(raw_lines):
                low = ln.casefold()
                if low.startswith('sasuke:'):
                    spk, txt = 0, ln.split(':', 1)[1].strip()
                elif low.startswith('naruto:'):
                    spk, txt = 1, ln.split(':', 1)[1].strip()
                else:
                    spk, txt = idx % 2, ln
                if not txt:
                    continue
                updated.append({'speaker': spk, 'text': txt})
            self.lines = updated
            self._write('\n--- Roteiro editado manualmente ---')
            for l in self.lines:
                self._write(f'{engine.NAMES[l["speaker"]]}: {l["text"]}')
            window.destroy()

        btn_bar = ttk.Frame(frame, style='Card.TFrame')
        btn_bar.pack(fill='x', pady=(10, 0))
        ttk.Button(btn_bar, text='Salvar alterações no roteiro', style='Accent.TButton', command=save_edits).pack(side='left')
        ttk.Button(btn_bar, text='Cancelar', command=window.destroy).pack(side='left', padx=8)

    def _search_gameplays(self):
        from gameplay_search import SearchWindow
        if getattr(self, '_gameplay_window', None) is not None and self._gameplay_window.winfo_exists():
            self._gameplay_window.lift()
            return
        self._gameplay_window = SearchWindow(self, HOME)

    def _choose(self):
        path = filedialog.askopenfilename(title='Selecionar gameplay', filetypes=[('Vídeos', '*.mp4 *.mov *.mkv *.webm')])
        if path:
            self.gameplay.set(path)

    def _edit_source(self, on_apply=None):
        window = tk.Toplevel(self)
        window.title('Fonte do conteúdo')
        window.geometry('720x520')
        window.configure(bg='#0F1420')
        frame = ttk.Frame(window, padding=16, style='Card.TFrame')
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Opcional. Sem texto fornecido, o ReelForge busca notícias automaticamente via RSS.',
                  style='Card.TLabel').pack(anchor='w')
        current = self.source_override or {}
        title = tk.StringVar(value=current.get('title', self.topic.get()))
        url = tk.StringVar(value=current.get('url', ''))
        ttk.Label(frame, text='Título', style='Card.TLabel').pack(anchor='w', pady=(12, 0))
        ttk.Entry(frame, textvariable=title).pack(fill='x')
        ttk.Label(frame, text='Link da fonte (opcional)', style='Card.TLabel').pack(anchor='w', pady=(8, 0))
        ttk.Entry(frame, textvariable=url).pack(fill='x')
        ttk.Label(frame, text='Texto de referência: fatos, contexto e desfecho. A IA não inventa fatos ausentes.',
                  style='Card.TLabel').pack(anchor='w', pady=(8, 0))
        text = tk.Text(frame, wrap='word', height=12, bg='#0B0F19', fg='#F1F5F9', insertbackground='#FFFFFF')
        text.pack(fill='both', expand=True)
        text.insert('1.0', current.get('summary', ''))

        def apply():
            import hashlib
            body = engine.clean(text.get('1.0', 'end'))
            heading = engine.clean(title.get())
            if len((heading + ' ' + body).split()) < 20 or len(body) > 20000:
                messagebox.showerror('Fonte', 'Forneça ao menos 20 palavras e até 20.000 caracteres de contexto.', parent=window)
                return
            self.source_override = {'id': hashlib.sha256((heading + body).encode()).hexdigest(), 'title': heading, 'summary': body, 'url': url.get().strip(), 'source_type': 'texto fornecido'}
            self.imported_script = None
            self._write('Fonte selecionada: ' + heading)
            window.destroy()
            if on_apply is not None:
                on_apply()

        def automatic():
            self.source_override = None
            self.imported_script = None
            self._write('Fonte removida. A busca automática será usada.')
            window.destroy()

        actions = ttk.Frame(frame, style='Card.TFrame')
        actions.pack(fill='x', pady=(10, 0))
        ttk.Button(actions, text='Usar texto', style='Accent.TButton', command=apply).pack(side='left')
        ttk.Button(actions, text='Usar busca automática', command=automatic).pack(side='left', padx=8)

    def _switch_voice_provider(self):
        from characters import EDGE_VOICES, FISH_VOICES, NAMES
        previous = self._shown_voice_provider
        if previous:
            self.voice_profiles[previous] = self._voice_form()
            self.voice_keys[previous] = self.tts_key.get()
        provider = self.tts_provider.get()
        profile = self.voice_profiles.get(provider, {})
        defaults = EDGE_VOICES if provider == 'edge' else (FISH_VOICES if provider == 'fish' else (NAMES if provider == 'voicestudio' else ['', '']))
        self.voice_a.set(profile.get('a', defaults[0]))
        self.voice_b.set(profile.get('b', defaults[1]))
        self.tts_model.set(profile.get('model', {'edge': '', 'voicestudio': '', 'elevenlabs': 'eleven_multilingual_v2', 'fish': 's2.1-pro'}[provider]))
        self.rate_a.set(str(profile.get('rate_a', 18 if provider == 'voicestudio' else -3)))
        self.rate_b.set(str(profile.get('rate_b', 18 if provider == 'voicestudio' else 7)))
        self.pitch_a.set(str(profile.get('pitch_a', -7 if provider == 'edge' else 0)))
        self.pitch_b.set(str(profile.get('pitch_b', 4 if provider == 'edge' else 0)))
        self.tts_key.set(self.voice_keys.get(provider, ''))
        self._shown_voice_provider = provider

    def _voice_form(self):
        return {'a': self.voice_a.get().strip(), 'b': self.voice_b.get().strip(), 'model': self.tts_model.get().strip(),
                'rate_a': self.rate_a.get(), 'rate_b': self.rate_b.get(),
                'pitch_a': self.pitch_a.get(), 'pitch_b': self.pitch_b.get()}

    def _save(self):
        from render_pipeline import background_color, music_volume
        brightness, gamma = background_color(self.bg_brightness.get(), self.bg_gamma.get())
        self.voice_profiles[self.tts_provider.get()] = self._voice_form()
        judge_choice = self.judge_model.get().strip()
        judge_val = '' if judge_choice.lower() in ('igual ao redator', '') else judge_choice
        ui_cfg = {'topic': self.topic.get(), 'provider': self.provider.get(), 'ollama_model': self.ollama_model.get().strip(),
                  'judge_model': judge_val, 'aceitar_nota_baixa': self.aceitar_nota_baixa.get(),
                  'gameplay': self.gameplay.get(), 'fps': int(self.fps.get()),
                  'gameplay_brightness': brightness, 'gameplay_gamma': gamma,
                  'loop': self.loop.get(), 'script_loop': self.script_loop.get(),
                  'music_enabled': self.music_enabled.get(),
                  'music_volume': round(music_volume(self.music_volume.get()) * 100, 1),
                  'max_seconds': engine.validate_duration(self.max_seconds.get()),
                  'impact_opening': self.impact_opening.get(),
                  'director_enabled': self.director_enabled.get(), 'content_kind': self.content_kind.get(),
                  'tts_provider': self.tts_provider.get(), 'voice_profiles': self.voice_profiles,
                  'caption_timing': self.caption_timing.get(), 'whisper_model': self.whisper_model.get()}
        engine.validate_hook(self.hook_text.get())
        current = {}
        if CONFIG.exists():
            try:
                current = json.loads(CONFIG.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                current = {}
        cfg = merge_settings(current, ui_cfg)
        self.voice_profiles = cfg.get('voice_profiles', self.voice_profiles)
        CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding='utf-8')
        os.environ.update(REELFORGE_BG_BRIGHTNESS=str(brightness), REELFORGE_BG_GAMMA=str(gamma))
        os.environ.update(REELFORGE_LOOP='1' if cfg['loop'] else '0',
                          REELFORGE_SCRIPT_LOOP='1' if cfg['script_loop'] else '0',
                          REELFORGE_MUSIC='1' if cfg['music_enabled'] else '0',
                          REELFORGE_MUSIC_VOLUME=str(cfg['music_volume']))
        os.environ.update({'AI_PROVIDER': self.provider.get(), 'OLLAMA_MODEL': self.ollama_model.get().strip()})
        if judge_val:
            os.environ['OLLAMA_JUDGE_MODEL'] = judge_val
        else:
            os.environ['OLLAMA_JUDGE_MODEL'] = self.ollama_model.get().strip()
        os.environ['REELFORGE_ACCEPT_LOW_SCORE'] = '1' if self.aceitar_nota_baixa.get() else '0'
        os.environ.update({'TTS_PROVIDER': self.tts_provider.get(), 'TTS_VOICE_A': self.voice_a.get().strip(),
                           'TTS_VOICE_B': self.voice_b.get().strip(), 'TTS_MODEL': self.tts_model.get().strip(),
                           'TTS_RATE_A': self.rate_a.get(), 'TTS_RATE_B': self.rate_b.get(),
                           'TTS_PITCH_A': self.pitch_a.get(), 'TTS_PITCH_B': self.pitch_b.get(),
                           'CAPTION_TIMING': self.caption_timing.get(), 'WHISPER_MODEL': self.whisper_model.get()})
        provider = self.tts_provider.get()
        if provider in ('elevenlabs', 'fish'):
            os.environ['ELEVENLABS_API_KEY' if provider == 'elevenlabs' else 'FISH_API_KEY'] = self.tts_key.get().strip()
        return cfg

    def _write(self, text):
        self.log.configure(state='normal')
        msg = str(text)
        tag = None
        if msg.startswith('Sasuke:'):
            tag = 'sasuke'
        elif msg.startswith('Naruto:'):
            tag = 'naruto'
        elif msg.lstrip().startswith('NOTÍCIA:') or msg.startswith('Pauta escolhida:'):
            tag = 'headline'
        elif 'Traceback' in msg or 'Error:' in msg:
            tag = 'error'
        if tag:
            self.log.insert('end', msg + '\n', tag)
        else:
            self.log.insert('end', msg + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def _job(self, fn):
        if self.busy:
            return
        try:
            cfg = self._save()
        except Exception as exc:
            messagebox.showerror('Configuração', str(exc))
            return
        self.busy = True
        self.job_config = {'topic': self.topic.get().strip(), 'repeat': self.repeat.get(),
                           'max_seconds': cfg['max_seconds'],
                           'impact_opening': cfg['impact_opening'], 'hook_text': engine.validate_hook(self.hook_text.get()),
                           'director_enabled': cfg['director_enabled'], 'content_kind': cfg['content_kind'], 'provider': cfg['provider'],
                           'source': dict(self.source_override) if self.source_override else None,
                           'gameplay': self.gameplay.get().strip(), 'fps': int(self.fps.get())}
        import hashlib
        self.job_config['signature'] = hashlib.sha256(json.dumps([cfg, self.job_config], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        for b in self.buttons:
            b.configure(state='disabled')
        self.progress.start(12)
        self.status.set('Processando... aguarde a conclusão da tarefa.')

        def worker():
            try:
                fn()
            except Exception:
                self.events.put(('error', traceback.format_exc()))
            finally:
                self.events.put(('done', None))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'log':
                    self._write(value)
                elif kind == 'error':
                    self._write(value)
                    self.status.set('Falha: veja o erro detalhado no console abaixo.')
                elif kind == 'done':
                    self.busy = False
                    self.progress.stop()
                    for b in self.buttons:
                        b.configure(state='normal')
                    if self.status.get().startswith('Processando'):
                        self.status.set('Concluído com sucesso.')
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _check(self):
        import importlib.util

        def task():
            from media_tools import ffmpeg
            self.events.put(('log', 'FFmpeg disponível: ' + ffmpeg()))
            self.events.put(('log', 'Legendas por áudio: ' + ('instaladas' if importlib.util.find_spec('faster_whisper') else 'execute instalar_legendas.bat')))
            self.events.put(('log', f'Gameplays locais: {len(engine.gameplays())}'))
            judge_info = f', juiz: {self.judge_model.get()}' if self.judge_model.get() else ''
            self.events.put(('log', f'Roteiros: {self.provider.get()} local ({self.ollama_model.get()}{judge_info}).'))

        self._job(task)

    def _get_news(self):
        cfg = self.job_config
        self.preview_signature = None
        if self.imported_script is not None:
            import script_io
            self.news, self.lines, _ = script_io.load(self.imported_script)
            self.direction = None
            self.preview_signature = cfg['signature']
            return self.news, self.lines
        if cfg['source'] is None:
            import news_auto
            story, lines = news_auto.automatic(cfg['topic'], cfg['content_kind'], cfg['max_seconds'], cfg['repeat'],
                                               log=lambda text: self.events.put(('log', text)))
            self.direction = None
            self.news, self.lines = story, lines
            self.preview_signature = cfg['signature']
            self.events.put(('log', '\nNOTÍCIA: ' + story['title'] + '\nFONTE: ' + story['url']))
            for line in lines:
                self.events.put(('log', f'{engine.NAMES[line["speaker"]]}: {line["text"]}'))
            return story, lines
        if cfg['director_enabled']:
            import director
            paths = [Path(cfg['gameplay'])] if cfg['gameplay'] else engine.gameplays()[:1]
            catalog, self.director_files = director.catalog_gameplays(paths)
            if not catalog:
                raise ValueError('Selecione um gameplay local ou adicione vídeos em assets/gameplay antes de criar o plano.')
        story = cfg['source']
        self.direction = None
        if cfg['director_enabled']:
            import director
            self.events.put(('log', 'Diretor IA: criando plano e revisando roteiro contra a fonte...'))
            self.direction = director.create_plan(story, cfg['content_kind'], cfg['max_seconds'], catalog)
            lines = director.narrated_lines(self.direction)
            self.events.put(('log', 'GANCHO: ' + self.direction['hook_text'] + '\nDIREÇÃO: ' + self.direction['rationale']))
            self.events.put(('log', 'Fundo escolhido: ' + self.director_files[self.direction['gameplay_index']].name))
        else:
            lines = engine.make_script(story, cfg['max_seconds'], cfg['impact_opening'], cfg['hook_text'])
        self.news, self.lines = story, lines
        self.preview_signature = cfg['signature']
        engine.OUTPUT.mkdir(exist_ok=True)
        if self.direction:
            (engine.OUTPUT / 'ultimo_plano_ia.json').write_text(json.dumps(self.direction, ensure_ascii=False, indent=2), encoding='utf-8')
        self.events.put(('log', '\nNOTÍCIA: ' + story['title'] + '\nFONTE: ' + story['url']))
        for line in lines:
            self.events.put(('log', f'{engine.NAMES[line["speaker"]]}: {line["text"]}'))
        return story, lines

    def _content_job(self, task):
        if self.busy:
            return
        self._job(task)

    def _preview(self):
        if self.busy:
            return
        self.imported_script = None
        self._content_job(self._get_news)

    def _preview_audio(self):
        def task():
            import speech
            if not (self.lines and self.preview_signature == self.job_config['signature']):
                self._get_news()
            dest = speech.preview_lines(self.lines, engine.OUTPUT)
            self.events.put(('log', 'Prévia salva: ' + str(dest) + '. O áudio é reaproveitado se o texto e a voz não mudarem.'))
            if os.name == 'nt':
                os.startfile(str(dest))

        self._content_job(task)

    def _load_example(self):
        self._load_script(HOME / 'assets' / 'demo_final' / 'dialogue.json')

    def _import_script(self):
        path = filedialog.askopenfilename(title='Roteiro JSON, com gravações opcionais', filetypes=[('Roteiro', '*.json')])
        if path:
            self._load_script(Path(path))

    def _load_script(self, path):
        import script_io
        try:
            story, lines, background = script_io.load(path)
        except Exception as exc:
            messagebox.showerror('Roteiro', str(exc))
            return
        self.source_override = story
        self.imported_script = Path(path).resolve()
        self.director_enabled.set(False)
        if background:
            self.gameplay.set(str(background))

        def task():
            self.news, self.lines, self.direction = story, lines, None
            self.preview_signature = self.job_config['signature']
            self.events.put(('log', 'Roteiro importado. Ouça a prévia e depois gere o Reel. Gravações importadas não são substituídas.'))
            for line in lines:
                self.events.put(('log', engine.NAMES[line['speaker']] + ': ' + line['text']))

        self._job(task)

    def _generate(self):
        def task():
            cfg = self.job_config
            if self.news and self.lines and self.preview_signature == cfg['signature']:
                story, lines = self.news, self.lines
            else:
                story, lines = self._get_news()
            gameplay = Path(cfg['gameplay']) if cfg['gameplay'] else next(iter(engine.gameplays()), None)
            if self.direction:
                gameplay = self.director_files[self.direction['gameplay_index']]
            if gameplay is None or not gameplay.is_file():
                raise RuntimeError('Selecione um gameplay existente em MP4 ou adicione-o a assets/gameplay.')
            self.events.put(('log', 'Renderizando...'))
            engine.render(story, lines, gameplay, cfg['fps'], cfg['max_seconds'], True if self.direction else cfg['impact_opening'], self.direction['hook_text'] if self.direction else cfg['hook_text'], self.direction)
            self.preview_signature = None
            self.events.put(('log', 'Vídeo salvo na pasta output.'))

        self._content_job(task)

    def _open_music(self):
        import music
        path = music.folder(HOME)
        if os.name == 'nt':
            os.startfile(str(path))

    def _open_output(self):
        import subprocess
        engine.OUTPUT.mkdir(exist_ok=True)
        if os.name == 'nt':
            os.startfile(str(engine.OUTPUT))
        else:
            subprocess.Popen(['xdg-open', str(engine.OUTPUT)])


if __name__ == '__main__':
    App().mainloop()
