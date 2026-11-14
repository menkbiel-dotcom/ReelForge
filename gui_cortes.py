"""Aba 'Cortes' da interface gráfica do ReelForge (Tkinter) — layout em 3 cartões, etapas, miniatura e prévia."""
import os
import re
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import clip_render
import clipper
import engine
import media_tools

GLYPH_CHECKED = '✔'
GLYPH_UNCHECKED = '☐'

STAGE_NAMES = (
    '① Baixar',
    '② Transcrever',
    '③ Avaliar',
    '④ Renderizar',
)


def _parse_optional_float(raw: str, label: str) -> float | None:
    txt = str(raw or '').strip().replace(',', '.')
    if not txt:
        return None
    try:
        return float(txt)
    except ValueError:
        raise ValueError(f'Valor numérico inválido para {label}: "{raw}".')


def _parse_optional_crop_x(raw: str) -> float | None:
    txt = str(raw or '').strip().replace('%', '').replace(',', '.').casefold()
    if not txt or txt == 'auto':
        return None
    if txt == 'centro':
        return 0.5
    try:
        val = float(txt)
    except ValueError:
        raise ValueError(f'Valor inválido para Enquadramento (X%): "{raw}". Use 0–100 (ou deixe em branco para automático).')
    if 0.0 < val <= 1.0 and '.' in txt:
        return max(0.0, min(1.0, val))
    return max(0.0, min(1.0, val / 100.0))


def score_badge_colors(score: float) -> tuple[str, str]:
    """Retorna (bg, fg) para o selo de nota: verde >=8, amarelo 6-8, vermelho <6."""
    val = float(score or 0.0)
    if val >= 8.0:
        return '#133829', '#34D399'
    if val >= 6.0:
        return '#3B2F13', '#FACC15'
    return '#3B1A1E', '#F87171'


def attach_tooltip(widget, text: str):
    """Adiciona um tooltip leve ao passar o mouse sobre o widget."""
    tip_state = {'win': None}

    def show_tip(_event=None):
        if tip_state['win'] is not None:
            return
        try:
            x = widget.winfo_rootx() + 14
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            tw = tk.Toplevel(widget)
            tw.wm_overrideredirect(True)
            tw.wm_geometry(f'+{x}+{y}')
            lbl = tk.Label(
                tw,
                text=text,
                justify='left',
                bg='#0B0F19',
                fg='#F1F5F9',
                relief='solid',
                borderwidth=1,
                font=('Segoe UI', 8),
                padx=6,
                pady=3,
            )
            lbl.pack()
            tip_state['win'] = tw
        except Exception:
            tip_state['win'] = None

    def hide_tip(_event=None):
        tw = tip_state.get('win')
        tip_state['win'] = None
        if tw is not None:
            try:
                tw.destroy()
            except Exception:
                pass

    widget.bind('<Enter>', show_tip, add='+')
    widget.bind('<Leave>', hide_tip, add='+')


def extract_cut_thumbnail(
    source_path: str | Path,
    video_id: str,
    cut_index: int,
    start_s: float,
) -> Path | None:
    """Extrai miniatura PNG (160x90) do frame inicial do corte com ffmpeg e cacheia em output/cortes/<id>/thumbs/."""
    src = Path(source_path) if source_path else None
    if src is None or not src.is_file():
        return None
    thumbs_dir = clipper.cortes_root() / str(video_id or 'local') / 'thumbs'
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(round(max(0.0, float(start_s)) * 10))
    dest = thumbs_dir / f'thumb_{int(cut_index):02d}_{stamp}.png'
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    cmd = [
        media_tools.ffmpeg(), '-y', '-v', 'error', '-nostdin',
        '-ss', f'{max(0.0, float(start_s)):.3f}',
        '-i', str(src),
        '-frames:v', '1',
        '-vf', 'scale=160:90:force_original_aspect_ratio=decrease',
        str(dest),
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=5)
    except Exception:
        return None
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    return None


def render_quick_preview(
    info: dict,
    candidate: dict,
    gameplay: str | Path | None = None,
    modo_corte: str = 'preencher',
    log=print,
) -> Path:
    """Renderiza prévia rápida 540x960 ultrafast do corte e devolve o caminho do MP4."""
    video_id = str(info.get('id') or 'preview')
    out_dir = clipper.cortes_root() / video_id / 'thumbs'
    out_dir.mkdir(parents=True, exist_ok=True)
    source_path = Path(info.get('source_path') or (clipper.cortes_root() / video_id / 'source.mp4'))
    if not source_path.is_file():
        raise FileNotFoundError(f'Vídeo de origem não encontrado: {source_path}')
    gp_path = Path(gameplay) if gameplay else next(iter(engine.gameplays()), None)
    if gp_path is None or not gp_path.is_file():
        raise ValueError('Selecione uma gameplay válida para gerar a prévia.')

    start = max(0.0, float(candidate.get('inicio', 0.0)))
    end = float(candidate.get('fim', start + 8.0))
    dur = max(1.0, min(15.0, end - start))
    idx = int(candidate.get('indice', 1))
    dest = out_dir / f'previa_{idx:02d}.mp4'
    mode = str(modo_corte or 'preencher').strip().casefold()
    if mode == 'ajustar':
        top_fg = (
            '[0:v]setpts=PTS-STARTPTS,fps=24,split=2[ta][tb];'
            '[ta]scale=540:480:force_original_aspect_ratio=increase,crop=540:480,boxblur=12:3,setsar=1[tbg];'
            '[tb]scale=540:480:force_original_aspect_ratio=decrease,setsar=1[tfg];'
            '[tbg][tfg]overlay=(W-w)/2:(H-h)/2[top]'
        )
    else:
        top_fg = '[0:v]setpts=PTS-STARTPTS,fps=24,scale=540:480:force_original_aspect_ratio=increase,crop=540:480,setsar=1[top]'
    fc = (
        f'{top_fg};'
        f'[1:v]scale=540:480:force_original_aspect_ratio=increase,crop=540:480,setsar=1,fps=24,'
        f'trim=start=0:end={dur:.3f},setpts=PTS-STARTPTS[bottom];'
        '[top][bottom]vstack=inputs=2[vout]'
    )
    cmd = [
        'ffmpeg', '-y', '-v', 'error', '-threads', '0',
        '-ss', f'{start:.3f}', '-t', f'{dur:.3f}', '-i', source_path,
        '-stream_loop', '-1', '-t', f'{dur + 0.5:.3f}', '-i', gp_path,
        '-filter_complex', fc,
        '-map', '[vout]', '-map', '0:a?',
        '-t', f'{dur:.3f}',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '26', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '128k',
        dest,
    ]
    log(f'Gerando prévia rápida 540x960 do corte {idx:02d}...')
    engine.run(cmd)
    log(f'Prévia pronta: {dest}')
    return dest


class CortesTab(ttk.Frame):
    """Painel completo para baixar/carregar vídeo autorizado, analisar e renderizar cortes."""

    def __init__(self, master, app, saved_cortes=None):
        super().__init__(master, padding=12, style='Card.TFrame')
        self.app = app
        cfg = clipper.migrate_cortes_settings(saved_cortes)

        self.source = tk.StringVar(value='')
        self.authorized = tk.BooleanVar(value=False)
        self.credito = tk.StringVar(value=str(cfg.get('credito', '')))
        self.idioma = tk.StringVar(value=str(cfg.get('idioma', 'pt')))
        self.duracao_min = tk.StringVar(value=str(int(float(cfg.get('duracao_min', 25)))))
        self.duracao_max = tk.StringVar(value=str(int(float(cfg.get('duracao_max', 55)))))
        self.num_cortes = tk.StringVar(value=str(int(cfg.get('num_cortes', 5))))
        self.modo_corte = tk.StringVar(value=str(cfg.get('modo_corte', 'preencher')))
        self.enquadramento = tk.StringVar(value=str(cfg.get('enquadramento', 'auto')))
        self.enquadramento_x = tk.StringVar(value=str(cfg.get('enquadramento_x', '')))
        self.posicao_legenda = tk.StringVar(value=str(cfg.get('posicao_legenda', 'juncao')))
        self.titulo_na_tela = tk.BooleanVar(value=bool(cfg.get('titulo_na_tela', True)))
        self.credito_na_tela = tk.BooleanVar(value=bool(cfg.get('credito_na_tela', True)))
        self.musica_modo = tk.StringVar(value=str(cfg.get('musica_modo', 'impacto')))
        self.musica_volume = tk.StringVar(value=str(int(cfg.get('musica_volume', 22))))
        self.impacto_inicial = tk.StringVar(value=str(cfg.get('impacto_inicial', 'impacto')))
        self.impacto_volume = tk.StringVar(value=str(int(cfg.get('impacto_volume', 70))))
        self.musica_ativa = tk.BooleanVar(value=bool(cfg.get('musica_ativa', True)))
        self.inicio_min = tk.StringVar(value='')
        self.fim_min = tk.StringVar(value='')
        self.summary_var = tk.StringVar(
            value='Resumo do vídeo: informe um link ou arquivo autorizado e clique em "Analisar vídeo".'
        )
        self.gameplay_info_var = tk.StringVar(value='')
        self.detail_hook_var = tk.StringVar(value='Gancho: —')
        self.detail_reason_var = tk.StringVar(value='Motivo: selecione um candidato na lista.')

        self.cancel_event = threading.Event()
        self.info = None
        self.transcription = None
        self.report = None
        self.candidates: list[dict] = []
        self._selected_flags: list[bool] = []
        self._is_busy = False
        self._thumb_photo = None
        self.current_stage = 0

        self._build_ui()
        self.authorized.trace_add('write', lambda *_: self.refresh_action_states())
        self._sync_gameplay_label()
        if hasattr(self.app, 'gameplay') and isinstance(self.app.gameplay, tk.Variable):
            self.app.gameplay.trace_add('write', lambda *_: self._sync_gameplay_label())
        self.refresh_action_states()

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        # Topo: Indicador de etapas ① Baixar → ② Transcrever → ③ Avaliar → ④ Renderizar + Barra + Pílula
        stage_bar = ttk.Frame(self, style='Card.TFrame')
        stage_bar.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        self.stage_labels: list[tk.Label] = []
        for idx, title in enumerate(STAGE_NAMES, start=1):
            lbl = tk.Label(
                stage_bar,
                text=title,
                bg='#131A29',
                fg='#94A3B8',
                font=('Segoe UI Semibold', 9),
                padx=10,
                pady=3,
            )
            lbl.pack(side='left', padx=(0, 4))
            self.stage_labels.append(lbl)
            if idx < len(STAGE_NAMES):
                ttk.Label(stage_bar, text='→', style='CardMuted.TLabel').pack(side='left', padx=(0, 4))

        self.status_pill = tk.Label(
            stage_bar,
            text=' Pronto ',
            bg='#1B3A4B',
            fg='#2DDCFF',
            font=('Segoe UI Semibold', 9, 'bold'),
            padx=10,
            pady=3,
        )
        self.status_pill.pack(side='right')
        self.progress_bar = ttk.Progressbar(stage_bar, orient='horizontal', mode='determinate', maximum=100, value=0, length=180)
        self.progress_bar.pack(side='right', padx=(12, 8))

        # Cartão 1: 1 · Fonte
        card_src = ttk.LabelFrame(self, text=' 1 · Fonte ', padding=12)
        card_src.grid(row=1, column=0, sticky='ew', pady=(0, 8))
        card_src.columnconfigure(1, weight=1)

        ttk.Label(card_src, text='Link ou arquivo:', style='Card.TLabel').grid(row=0, column=0, sticky='w', padx=(0, 8), pady=2)
        ent_src = ttk.Entry(card_src, textvariable=self.source)
        ent_src.grid(row=0, column=1, sticky='ew', pady=2)
        ttk.Button(card_src, text='Procurar…', command=self._browse_source).grid(row=0, column=2, padx=(8, 0), pady=2)

        src_row2 = ttk.Frame(card_src, style='Card.TFrame')
        src_row2.grid(row=1, column=0, columnspan=3, sticky='ew', pady=(6, 0))
        self.chk_auth = ttk.Checkbutton(src_row2, text=clipper.AUTH_CONFIRM_TEXT, variable=self.authorized)
        self.chk_auth.pack(side='left')
        ttk.Label(src_row2, text='Crédito:', style='Card.TLabel').pack(side='left', padx=(16, 4))
        ent_cred = ttk.Entry(src_row2, textvariable=self.credito, width=18)
        ent_cred.pack(side='left', padx=(0, 12))
        attach_tooltip(ent_cred, 'Ex.: @NomeDoCanal (exibido nos metadados e opcionalmente no canto do vídeo).')
        ttk.Label(src_row2, text='Trecho (min):', style='CardMuted.TLabel').pack(side='left')
        ent_smin = ttk.Entry(src_row2, textvariable=self.inicio_min, width=5)
        ent_smin.pack(side='left', padx=(4, 2))
        ttk.Label(src_row2, text='a', style='CardMuted.TLabel').pack(side='left')
        ent_emin = ttk.Entry(src_row2, textvariable=self.fim_min, width=5)
        ent_emin.pack(side='left', padx=(2, 12))
        attach_tooltip(ent_smin, 'Minuto inicial opcional para transcrever apenas parte do vídeo.')
        attach_tooltip(ent_emin, 'Minuto final opcional para transcrever apenas parte do vídeo.')
        ttk.Label(src_row2, textvariable=self.gameplay_info_var, style='CardMuted.TLabel').pack(side='right')

        # Cartão 2: 2 · Ajustes (duas colunas alinhadas com unidades)
        card_cfg = ttk.LabelFrame(self, text=' 2 · Ajustes ', padding=12)
        card_cfg.grid(row=2, column=0, sticky='ew', pady=(0, 8))
        card_cfg.columnconfigure(1, weight=1)
        card_cfg.columnconfigure(3, weight=1)

        # Coluna esquerda
        col_left = ttk.Frame(card_cfg, style='Card.TFrame')
        col_left.grid(row=0, column=0, columnspan=2, sticky='ew', padx=(0, 12))
        ttk.Label(col_left, text='Duração (s):', style='Card.TLabel').pack(side='left')
        ttk.Spinbox(col_left, from_=5, to=180, increment=5, textvariable=self.duracao_min, width=4).pack(side='left', padx=(4, 2))
        ttk.Label(col_left, text='a', style='Card.TLabel').pack(side='left')
        ttk.Spinbox(col_left, from_=10, to=180, increment=5, textvariable=self.duracao_max, width=4).pack(side='left', padx=(2, 10))
        ttk.Label(col_left, text='Nº cortes:', style='Card.TLabel').pack(side='left')
        ttk.Spinbox(col_left, from_=1, to=15, increment=1, textvariable=self.num_cortes, width=4).pack(side='left', padx=(4, 10))
        ttk.Label(col_left, text='Modo:', style='Card.TLabel').pack(side='left')
        cb_modo = ttk.Combobox(col_left, textvariable=self.modo_corte, values=['preencher', 'ajustar'], state='readonly', width=9)
        cb_modo.pack(side='left', padx=(4, 10))
        attach_tooltip(cb_modo, 'preencher: ocupa 1080x960 com enquadramento facial/horizontal; ajustar: vídeo inteiro com fundo desfocado.')
        ttk.Label(col_left, text='Enquadramento (X%):', style='Card.TLabel').pack(side='left')
        ent_crop = ttk.Entry(col_left, textvariable=self.enquadramento_x, width=5)
        ent_crop.pack(side='left', padx=(4, 10))
        attach_tooltip(ent_crop, '0 a 100% para deslocar o recorte horizontal. Vazio = detecção automática de rosto.')
        ttk.Label(col_left, text='Idioma:', style='Card.TLabel').pack(side='left')
        ttk.Combobox(col_left, textvariable=self.idioma, values=['pt', 'en', 'es'], width=4).pack(side='left', padx=(4, 0))

        # Coluna direita (Legenda, títulos, música e impacto)
        col_right = ttk.Frame(card_cfg, style='Card.TFrame')
        col_right.grid(row=1, column=0, columnspan=4, sticky='ew', pady=(8, 0))
        ttk.Label(col_right, text='Legenda:', style='Card.TLabel').pack(side='left')
        ttk.Combobox(col_right, textvariable=self.posicao_legenda, values=['juncao', 'rodape'], state='readonly', width=8).pack(side='left', padx=(4, 10))
        ttk.Checkbutton(col_right, text='Título na tela (3 s)', variable=self.titulo_na_tela).pack(side='left', padx=(0, 8))
        ttk.Checkbutton(col_right, text='Crédito no canto', variable=self.credito_na_tela).pack(side='left', padx=(0, 12))
        ttk.Label(col_right, text='Música:', style='Card.TLabel').pack(side='left')
        cb_mus = ttk.Combobox(col_right, textvariable=self.musica_modo, values=['desligada', 'baixa', 'impacto'], state='readonly', width=9)
        cb_mus.pack(side='left', padx=(4, 8))
        attach_tooltip(cb_mus, 'impacto: entra no drop com ducking automático pela voz; baixa: trilha suave; desligada: apenas voz.')
        ttk.Label(col_right, text='Vol (%):', style='Card.TLabel').pack(side='left')
        ttk.Spinbox(col_right, from_=0, to=100, increment=2, textvariable=self.musica_volume, width=4).pack(side='left', padx=(4, 10))
        ttk.Label(col_right, text='Impacto inicial:', style='Card.TLabel').pack(side='left')
        cb_imp = ttk.Combobox(col_right, textvariable=self.impacto_inicial, values=['nenhum', 'impacto', 'whoosh'], state='readonly', width=8)
        cb_imp.pack(side='left', padx=(4, 0))
        attach_tooltip(cb_imp, 'Efeito sonoro em t=0s para prender a atenção nos primeiros milissegundos.')

        # Botões de ação
        btn_row = ttk.Frame(card_cfg, style='Card.TFrame')
        btn_row.grid(row=2, column=0, columnspan=4, sticky='ew', pady=(8, 0))
        self.btn_analyze = ttk.Button(btn_row, text='Analisar vídeo', style='Accent.TButton', command=self.start_analyze)
        self.btn_analyze.pack(side='left', padx=(0, 8))
        self.btn_render = ttk.Button(btn_row, text='Renderizar selecionados', style='Secondary.TButton', command=self.start_render)
        self.btn_render.pack(side='left', padx=(0, 8))
        self.btn_cancel = ttk.Button(btn_row, text='Cancelar', command=self.cancel_operation, state='disabled')
        self.btn_cancel.pack(side='left', padx=(0, 8))
        self.btn_open_folder = ttk.Button(btn_row, text='Abrir pasta', command=self._open_cortes_folder)
        self.btn_open_folder.pack(side='left', padx=(0, 8))
        self.btn_open_music = ttk.Button(btn_row, text=r'Abrir pasta assets\music', command=self._open_music_folder)
        self.btn_open_music.pack(side='left')

        # Cartão 3: 3 · Candidatos (lista à esquerda + painel de detalhes à direita)
        card_cands = ttk.LabelFrame(self, text=' 3 · Candidatos ', padding=12)
        card_cands.grid(row=3, column=0, sticky='nsew')
        card_cands.columnconfigure(0, weight=3)
        card_cands.columnconfigure(1, weight=2)
        card_cands.rowconfigure(1, weight=1)

        ttk.Label(
            card_cands,
            textvariable=self.summary_var,
            style='CardMuted.TLabel',
            wraplength=980,
            justify='left',
        ).grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 6))

        # Esquerda: Treeview
        tree_frame = ttk.Frame(card_cands, style='Card.TFrame')
        tree_frame.grid(row=1, column=0, sticky='nsew', padx=(0, 12))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        cols = ('sel', 'intervalo', 'nota', 'gancho', 'motivo')
        self.tree = ttk.Treeview(tree_frame, columns=cols, show='headings', height=5, selectmode='browse')
        self.tree.heading('sel', text='Sel')
        self.tree.heading('intervalo', text='Intervalo')
        self.tree.heading('nota', text='Nota')
        self.tree.heading('gancho', text='Gancho')
        self.tree.heading('motivo', text='Motivo')
        self.tree.column('sel', width=44, anchor='center', stretch=False)
        self.tree.column('intervalo', width=108, anchor='center', stretch=False)
        self.tree.column('nota', width=64, anchor='center', stretch=False)
        self.tree.column('gancho', width=240, anchor='w', stretch=True)
        self.tree.column('motivo', width=240, anchor='w', stretch=True)
        self.tree.tag_configure('even', background='#1E293B', foreground='#F1F5F9')
        self.tree.tag_configure('odd', background='#182234', foreground='#F1F5F9')
        self.tree.grid(row=0, column=0, sticky='nsew')

        tree_scroll = ttk.Scrollbar(tree_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.grid(row=0, column=1, sticky='ns')

        self.tree.bind('<ButtonRelease-1>', self._on_tree_click)
        self.tree.bind('<<TreeviewSelect>>', lambda _e: self._show_focused_candidate_text())
        self.tree.bind('<space>', lambda _e: self._toggle_focused_selection())

        # Direita: Painel de detalhes (miniatura, selo de nota colorido, gancho, motivo, texto, ±1s e Prévia)
        detail = ttk.Frame(card_cands, style='Card.TFrame')
        detail.grid(row=1, column=1, sticky='nsew')
        detail.columnconfigure(1, weight=1)
        detail.rowconfigure(2, weight=1)

        top_detail = ttk.Frame(detail, style='Card.TFrame')
        top_detail.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 6))
        self.thumb_label = tk.Label(
            top_detail,
            text='Miniatura\n160×90',
            width=18,
            height=4,
            bg='#0B0F19',
            fg='#94A3B8',
            font=('Segoe UI', 8),
            relief='flat',
        )
        self.thumb_label.pack(side='left', padx=(0, 10))

        meta_box = ttk.Frame(top_detail, style='Card.TFrame')
        meta_box.pack(side='left', fill='both', expand=True)
        self.score_badge = tk.Label(
            meta_box,
            text=' Nota: — ',
            bg='#1E293B',
            fg='#F1F5F9',
            font=('Segoe UI Semibold', 9, 'bold'),
            padx=8,
            pady=2,
        )
        self.score_badge.pack(anchor='w', pady=(0, 4))
        ttk.Label(meta_box, textvariable=self.detail_hook_var, style='Card.TLabel', wraplength=260, justify='left').pack(anchor='w')
        ttk.Label(meta_box, textvariable=self.detail_reason_var, style='CardMuted.TLabel', wraplength=260, justify='left').pack(anchor='w', pady=(2, 0))

        self.preview_text = tk.Text(
            detail,
            wrap='word',
            height=3,
            font=('Consolas', 9),
            bg='#0B0F19',
            fg='#E2E8F0',
            relief='flat',
            padx=8,
            pady=4,
            state='disabled',
        )
        self.preview_text.grid(row=2, column=0, columnspan=2, sticky='nsew', pady=(2, 6))

        edit_row = ttk.Frame(detail, style='Card.TFrame')
        edit_row.grid(row=3, column=0, columnspan=2, sticky='ew')
        ttk.Label(edit_row, text='Início:', style='CardMuted.TLabel').pack(side='left')
        ttk.Button(edit_row, text='-1 s', style='Chip.TButton', command=lambda: self.adjust_focused(-1.0, 0.0)).pack(side='left', padx=2)
        ttk.Button(edit_row, text='+1 s', style='Chip.TButton', command=lambda: self.adjust_focused(1.0, 0.0)).pack(side='left', padx=(2, 8))
        ttk.Label(edit_row, text='Fim:', style='CardMuted.TLabel').pack(side='left')
        ttk.Button(edit_row, text='-1 s', style='Chip.TButton', command=lambda: self.adjust_focused(0.0, -1.0)).pack(side='left', padx=2)
        ttk.Button(edit_row, text='+1 s', style='Chip.TButton', command=lambda: self.adjust_focused(0.0, 1.0)).pack(side='left', padx=(2, 8))
        self.btn_preview_cut = ttk.Button(edit_row, text='Prévia', style='Secondary.TButton', command=self.preview_focused_cut)
        self.btn_preview_cut.pack(side='right')

    def set_stage(self, stage_idx: int):
        """Destaca a etapa atual (1=Baixar, 2=Transcrever, 3=Avaliar, 4=Renderizar, 0=nenhuma)."""
        self.current_stage = int(stage_idx)
        for i, lbl in enumerate(self.stage_labels, start=1):
            if i == self.current_stage:
                lbl.configure(bg='#FF5A1F', fg='#FFFFFF', font=('Segoe UI Semibold', 9, 'bold'))
            elif 0 < i < self.current_stage:
                lbl.configure(bg='#133829', fg='#34D399', font=('Segoe UI Semibold', 9))
            else:
                lbl.configure(bg='#131A29', fg='#94A3B8', font=('Segoe UI Semibold', 9))

    def set_status_pill(self, state: str):
        palette = {
            'Pronto': ('#1B3A4B', '#2DDCFF'),
            'Processando': ('#3B2F13', '#FACC15'),
            'Erro': ('#3B1A1E', '#F87171'),
            'Concluído': ('#133829', '#34D399'),
        }
        bg_c, fg_c = palette.get(state, ('#1B3A4B', '#2DDCFF'))
        self.status_pill.configure(text=f' {state} ', bg=bg_c, fg=fg_c)

    def set_progress(self, percent: float):
        val = max(0.0, min(100.0, float(percent)))
        self.progress_bar.configure(mode='determinate', value=val)

    def on_log_message(self, msg: str):
        """Atualiza etapa e barra de progresso determinada a partir das linhas de log."""
        text = str(msg or '')
        if 'Baixando vídeo' in text or 'Consultando metadados' in text or 'Arquivo local preparado' in text:
            self.set_stage(1)
        elif 'Transcrevendo áudio' in text or text.startswith('Transcrição:'):
            self.set_stage(2)
            m = re.search(r'\((\d+(?:\.\d+)?)%\)', text)
            if m:
                self.set_progress(float(m.group(1)))
        elif 'Avaliando conteúdo' in text or 'Analisando janela' in text:
            self.set_stage(3)
            m = re.search(r'janela\s+(\d+)/(\d+)', text, flags=re.IGNORECASE)
            if m:
                cur, tot = int(m.group(1)), max(1, int(m.group(2)))
                self.set_progress((cur / tot) * 100.0)
        elif 'Renderizando corte' in text:
            self.set_stage(4)

    def _sync_gameplay_label(self):
        gp = self.app.gameplay.get().strip() if hasattr(self.app, 'gameplay') else ''
        name = Path(gp).name if gp else 'gameplay_principal.mp4 (padrão)'
        self.gameplay_info_var.set(f'Gameplay: {name}')

    def _browse_source(self):
        path = filedialog.askopenfilename(
            title='Selecionar vídeo local',
            filetypes=[('Vídeos', '*.mp4 *.mov *.mkv *.webm *.avi'), ('Todos os arquivos', '*.*')],
        )
        if path:
            self.source.set(path)

    def get_settings(self) -> dict:
        """Retorna valores da aba Cortes para persistência em settings.json['cortes']."""
        try:
            dmin = float(str(self.duracao_min.get()).replace(',', '.'))
        except ValueError:
            dmin = 25.0
        try:
            dmax = float(str(self.duracao_max.get()).replace(',', '.'))
        except ValueError:
            dmax = 55.0
        try:
            ncuts = int(float(self.num_cortes.get()))
        except ValueError:
            ncuts = 5
        try:
            mvol = max(0, min(100, int(round(float(str(self.musica_volume.get()).replace(',', '.'))))))
        except ValueError:
            mvol = 22
        try:
            ivol = max(0, min(100, int(round(float(str(self.impacto_volume.get()).replace(',', '.'))))))
        except ValueError:
            ivol = 70
        mmodo = self.musica_modo.get().strip().casefold() or 'impacto'
        if mmodo not in ('desligada', 'baixa', 'impacto'):
            mmodo = 'impacto'
        imp_ini = self.impacto_inicial.get().strip().casefold() or 'impacto'
        if imp_ini not in ('nenhum', 'impacto', 'whoosh'):
            imp_ini = 'impacto'
        return {
            'idioma': self.idioma.get().strip() or 'pt',
            'duracao_min': dmin,
            'duracao_max': dmax,
            'num_cortes': max(1, ncuts),
            'modo_corte': self.modo_corte.get().strip() or 'preencher',
            'enquadramento': self.enquadramento.get().strip() or 'auto',
            'posicao_legenda': self.posicao_legenda.get().strip() or 'juncao',
            'titulo_na_tela': bool(self.titulo_na_tela.get()),
            'credito_na_tela': bool(self.credito_na_tela.get()),
            'credito': self.credito.get().strip(),
            'musica_ativa': mmodo != 'desligada',
            'musica_modo': mmodo,
            'musica_volume': mvol,
            'impacto_inicial': imp_ini,
            'impacto_volume': ivol,
        }

    def refresh_action_states(self):
        can_run = bool(self.authorized.get()) and not self._is_busy
        state = 'normal' if can_run else 'disabled'
        self.btn_analyze.configure(state=state)
        self.btn_render.configure(state=state)
        self.btn_cancel.configure(state='normal' if self._is_busy else 'disabled')

    def set_busy(self, busy: bool):
        self._is_busy = bool(busy)
        self.set_status_pill('Processando' if busy else 'Pronto')
        self.refresh_action_states()

    def cancel_operation(self):
        self.cancel_event.set()
        if hasattr(self.app, 'events'):
            self.app.events.put(('log', 'Cancelamento solicitado; aguardando término da etapa atual...'))

    def _open_cortes_folder(self):
        target = clipper.cortes_root()
        if self.info and self.info.get('id'):
            candidate = target / str(self.info['id'])
            if candidate.exists():
                target = candidate
        target.mkdir(parents=True, exist_ok=True)
        if os.name == 'nt':
            os.startfile(str(target))
        else:
            subprocess.Popen(['xdg-open', str(target)])

    def _open_music_folder(self):
        target = engine.ROOT / 'assets' / 'music'
        target.mkdir(parents=True, exist_ok=True)
        if os.name == 'nt':
            os.startfile(str(target))
        else:
            subprocess.Popen(['xdg-open', str(target)])

    def _on_tree_click(self, event):
        region = self.tree.identify('region', event.x, event.y)
        col = self.tree.identify_column(event.x)
        row_id = self.tree.identify_row(event.y)
        if region == 'cell' and row_id and col == '#1':
            self._toggle_row_by_id(row_id)

    def _toggle_focused_selection(self):
        sel = self.tree.selection()
        if sel:
            self._toggle_row_by_id(sel[0])

    def _toggle_row_by_id(self, row_id: str):
        try:
            idx = int(row_id)
        except ValueError:
            return
        if 0 <= idx < len(self._selected_flags):
            self._selected_flags[idx] = not self._selected_flags[idx]
            self._refresh_tree_row(idx)

    def _refresh_tree_row(self, idx: int):
        cand = self.candidates[idx]
        sel_mark = GLYPH_CHECKED if self._selected_flags[idx] else GLYPH_UNCHECKED
        intervalo = f"{clipper.format_timestamp(cand['inicio'])}–{clipper.format_timestamp(cand['fim'])}"
        nota = f"{float(cand.get('nota', 0.0)):.1f}"
        gancho = str(cand.get('gancho', ''))
        if cand.get('possivel_publi'):
            gancho = f"[PUBLI?] {gancho}"
        row_tag = 'even' if idx % 2 == 0 else 'odd'
        self.tree.item(
            str(idx),
            values=(sel_mark, intervalo, nota, gancho, cand.get('motivo', '')),
            tags=(row_tag,),
        )

    def _focused_index(self) -> int | None:
        sel = self.tree.selection()
        if not sel:
            return 0 if self.candidates else None
        try:
            idx = int(sel[0])
            return idx if 0 <= idx < len(self.candidates) else None
        except ValueError:
            return None

    def _show_focused_candidate_text(self):
        idx = self._focused_index()
        if idx is None or not (0 <= idx < len(self.candidates)):
            self.preview_text.configure(state='normal')
            self.preview_text.delete('1.0', 'end')
            self.preview_text.configure(state='disabled')
            return

        cand = self.candidates[idx]
        text = cand.get('texto', '')
        self.preview_text.configure(state='normal')
        self.preview_text.delete('1.0', 'end')
        self.preview_text.insert('1.0', text)
        self.preview_text.configure(state='disabled')

        nota_val = float(cand.get('nota', 0.0))
        bg_c, fg_c = score_badge_colors(nota_val)
        publi_tag = ' · PUBLI?' if cand.get('possivel_publi') else ''
        self.score_badge.configure(text=f' Nota: {nota_val:.1f}{publi_tag} ', bg=bg_c, fg=fg_c)
        self.detail_hook_var.set(f"Gancho: {cand.get('gancho', '—')}")
        self.detail_reason_var.set(f"Motivo: {cand.get('motivo', '—')}")

        # Atualiza miniatura se houver source_path válido
        src_path = (self.info or {}).get('source_path')
        vid_id = (self.info or {}).get('id', 'local')
        if src_path and Path(src_path).is_file():
            thumb = extract_cut_thumbnail(src_path, vid_id, cand.get('indice', idx + 1), cand.get('inicio', 0.0))
            if thumb is not None and thumb.is_file():
                try:
                    self._thumb_photo = tk.PhotoImage(file=str(thumb))
                    self.thumb_label.configure(image=self._thumb_photo, text='')
                    return
                except Exception:
                    pass
        self._thumb_photo = None
        self.thumb_label.configure(image='', text=f"{clipper.format_timestamp(cand.get('inicio', 0.0))}\n160×90")

    def adjust_focused(self, delta_start: float, delta_end: float):
        """Ajusta início/fim do candidato focado em ±1s e atualiza o texto exibido."""
        idx = self._focused_index()
        if idx is None:
            return
        cand = self.candidates[idx]
        vid_dur = float((self.info or {}).get('duracao') or (self.transcription or {}).get('duracao') or 0.0)
        new_start = max(0.0, round(float(cand['inicio']) + float(delta_start), 3))
        new_end = round(float(cand['fim']) + float(delta_end), 3)
        if vid_dur > 0:
            new_end = min(vid_dur, new_end)
        if new_end <= new_start + 1.0:
            return
        cand['inicio'] = new_start
        cand['fim'] = new_end
        cand['duracao'] = round(new_end - new_start, 3)

        if self.transcription and isinstance(self.transcription.get('palavras'), list):
            words = [
                w['texto']
                for w in self.transcription['palavras']
                if float(w.get('fim', 0.0)) > new_start and float(w.get('inicio', 0.0)) < new_end
            ]
            if words:
                cand['texto'] = ' '.join(words)

        self._refresh_tree_row(idx)
        self._show_focused_candidate_text()

    def preview_focused_cut(self):
        if not self.info or not self.candidates:
            messagebox.showinfo('Prévia', 'Analise o vídeo e selecione um candidato primeiro.')
            return
        idx = self._focused_index()
        if idx is None:
            return
        cand = self.candidates[idx]
        cfg = self.get_settings()
        gp_raw = self.app.gameplay.get().strip() if hasattr(self.app, 'gameplay') else ''
        gp_path = Path(gp_raw) if gp_raw else next(iter(engine.gameplays()), None)
        info_copy = dict(self.info)

        def task():
            log_fn = lambda msg: self.app.events.put(('log', msg))
            dest = render_quick_preview(info_copy, cand, gameplay=gp_path, modo_corte=cfg['modo_corte'], log=log_fn)
            if os.name == 'nt' and dest.is_file():
                os.startfile(str(dest))

        self.app._job(task)

    def set_analysis_result(self, info: dict, transcription: dict, report: dict):
        """Popula a tabela de candidatos e o resumo geral após a análise."""
        self.info = info
        self.transcription = transcription
        self.report = report
        self.candidates = [dict(c) for c in (report.get('candidatos') or [])]
        self._selected_flags = [True for _ in self.candidates]

        bullets = [str(b).strip() for b in (report.get('resumo') or []) if str(b).strip()]
        pot = str(report.get('potencial_cortes', 'médio')).upper()
        just = str(report.get('justificativa_potencial', '')).strip()
        summary_line = ' · '.join(bullets) if bullets else 'Sem resumo disponível.'
        self.summary_var.set(f'Potencial: {pot} ({just}) — {summary_line}')

        for item_id in self.tree.get_children():
            self.tree.delete(item_id)

        for idx, cand in enumerate(self.candidates):
            sel_mark = GLYPH_CHECKED if self._selected_flags[idx] else GLYPH_UNCHECKED
            intervalo = f"{clipper.format_timestamp(cand['inicio'])}–{clipper.format_timestamp(cand['fim'])}"
            nota = f"{float(cand.get('nota', 0.0)):.1f}"
            gancho = str(cand.get('gancho', ''))
            if cand.get('possivel_publi'):
                gancho = f"[PUBLI?] {gancho}"
            row_tag = 'even' if idx % 2 == 0 else 'odd'
            self.tree.insert(
                '',
                'end',
                iid=str(idx),
                values=(sel_mark, intervalo, nota, gancho, cand.get('motivo', '')),
                tags=(row_tag,),
            )

        if self.candidates:
            self.tree.selection_set('0')
            self._show_focused_candidate_text()
        else:
            self._show_focused_candidate_text()
        self.set_progress(100.0)
        self.set_status_pill('Concluído')

    def get_selected_candidates(self) -> list[dict]:
        return [dict(cand) for cand, flag in zip(self.candidates, self._selected_flags) if flag]

    def handle_event(self, kind: str, value) -> bool:
        if kind == 'cortes_analyzed':
            info, transcription, report = value
            self.set_analysis_result(info, transcription, report)
            return True
        if kind == 'cortes_progress':
            stage, pct = value
            self.set_stage(stage)
            self.set_progress(pct)
            return True
        return False

    def start_analyze(self):
        if not self.authorized.get():
            messagebox.showerror('Autorização', f'Marque "{clipper.AUTH_CONFIRM_TEXT}" para continuar.')
            return
        src = self.source.get().strip()
        if not src:
            messagebox.showerror('Cortes', 'Informe um link ou selecione um arquivo de vídeo local.')
            return
        try:
            dmin = float(str(self.duracao_min.get()).replace(',', '.'))
            dmax = float(str(self.duracao_max.get()).replace(',', '.'))
            ncuts = int(float(self.num_cortes.get()))
            s_min = _parse_optional_float(self.inicio_min.get(), 'início (min)')
            e_min = _parse_optional_float(self.fim_min.get(), 'fim (min)')
        except ValueError as exc:
            messagebox.showerror('Configuração', str(exc))
            return

        credito = self.credito.get().strip()
        lang = self.idioma.get().strip() or 'pt'
        eval_model = self.app.ollama_model.get().strip() if hasattr(self.app, 'ollama_model') else None
        whisper_model = self.app.whisper_model.get().strip() if hasattr(self.app, 'whisper_model') else None
        self.cancel_event.clear()
        self.set_stage(1)
        self.set_progress(0.0)

        def task():
            log_fn = lambda msg: self.app.events.put(('log', msg))
            info, transcription, report = clipper.analyze_video(
                src,
                authorized=True,
                credito=credito,
                language=lang,
                whisper_model=whisper_model or None,
                min_duration=dmin,
                max_duration=dmax,
                num_cuts=ncuts,
                start_min=s_min,
                end_min=e_min,
                eval_model=eval_model or None,
                cancel_event=self.cancel_event,
                log=log_fn,
            )
            self.app.events.put(('cortes_analyzed', (info, transcription, report)))
            log_fn(f'Análise concluída: {len(report.get("candidatos") or [])} candidato(s) pronto(s) para seleção.')

        self.app._job(task)

    def start_render(self):
        if not self.authorized.get():
            messagebox.showerror('Autorização', f'Marque "{clipper.AUTH_CONFIRM_TEXT}" para continuar.')
            return
        if not self.info or not self.transcription:
            messagebox.showinfo('Cortes', 'Analise o vídeo primeiro antes de renderizar os cortes.')
            return
        chosen = self.get_selected_candidates()
        if not chosen:
            messagebox.showinfo('Cortes', 'Marque ao menos um candidato na lista (✔) para renderizar.')
            return

        try:
            crop_x_val = _parse_optional_crop_x(self.enquadramento_x.get())
        except ValueError as exc:
            messagebox.showerror('Configuração', str(exc))
            return

        cfg = self.get_settings()
        gp_raw = self.app.gameplay.get().strip() if hasattr(self.app, 'gameplay') else ''
        gp_path = Path(gp_raw) if gp_raw else next(iter(engine.gameplays()), None)
        if gp_path is None or not gp_path.is_file():
            messagebox.showerror('Gameplay', 'Selecione uma gameplay válida na aba Vídeo ou adicione um MP4 em assets/gameplay/.')
            return

        info_copy = dict(self.info)
        if cfg['credito']:
            info_copy['credito'] = cfg['credito']
        transcription_copy = self.transcription
        modelos = (self.report or {}).get('modelos')
        self.cancel_event.clear()
        self.set_stage(4)
        self.set_progress(0.0)

        def task():
            log_fn = lambda msg: self.app.events.put(('log', msg))
            total_cands = max(1, len(chosen))
            for idx, cand in enumerate(chosen, start=1):
                if self.cancel_event.is_set():
                    raise clipper.CancelledError('Renderização cancelada pelo usuário.')
                self.app.events.put(('cortes_progress', (4, ((idx - 1) / total_cands) * 100.0)))
                clip_render.render_clip(
                    info_copy,
                    transcription_copy,
                    cand,
                    gameplay=gp_path,
                    cut_index=cand.get('indice', idx),
                    modo_corte=cfg['modo_corte'],
                    posicao_legenda=cfg['posicao_legenda'],
                    titulo_na_tela=cfg['titulo_na_tela'],
                    credito_na_tela=cfg['credito_na_tela'],
                    musica_ativa=cfg['musica_ativa'],
                    musica_modo=cfg['musica_modo'],
                    musica_volume=cfg['musica_volume'],
                    impacto_inicial=cfg['impacto_inicial'],
                    impacto_volume=cfg['impacto_volume'],
                    crop_x=crop_x_val,
                    enquadramento=cfg.get('enquadramento', 'auto'),
                    modelos=modelos,
                    log=log_fn,
                )
                self.app.events.put(('cortes_progress', (4, (idx / total_cands) * 100.0)))
            log_fn(f'Renderização dos {len(chosen)} corte(s) concluída em output/cortes/{info_copy["id"]}.')

        self.app._job(task)

    def destroy(self):
        self._thumb_photo = None
        for attr, val in list(self.__dict__.items()):
            if isinstance(val, tk.Variable):
                try:
                    delattr(self, attr)
                except Exception:
                    pass
        super().destroy()
