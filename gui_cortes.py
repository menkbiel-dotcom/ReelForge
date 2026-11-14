"""Aba 'Cortes' da interface gráfica do ReelForge (Tkinter)."""
import os
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import clip_render
import clipper
import engine


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


class CortesTab(ttk.Frame):
    """Painel completo para baixar/carregar vídeo autorizado, analisar e renderizar cortes."""

    def __init__(self, master, app, saved_cortes=None):
        super().__init__(master, padding=12, style='Card.TFrame')
        self.app = app
        cfg = dict(clipper.DEFAULT_CORTES_SETTINGS)
        if isinstance(saved_cortes, dict):
            cfg.update(saved_cortes)

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
        self.musica_ativa = tk.BooleanVar(value=bool(cfg.get('musica_ativa', False)))
        self.inicio_min = tk.StringVar(value='')
        self.fim_min = tk.StringVar(value='')
        self.summary_var = tk.StringVar(
            value='Resumo do vídeo: informe um link ou arquivo autorizado e clique em "Analisar vídeo".'
        )
        self.gameplay_info_var = tk.StringVar(value='')

        self.cancel_event = threading.Event()
        self.info = None
        self.transcription = None
        self.report = None
        self.candidates: list[dict] = []
        self._selected_flags: list[bool] = []
        self._is_busy = False

        self._build_ui()
        self.authorized.trace_add('write', lambda *_: self.refresh_action_states())
        self._sync_gameplay_label()
        if hasattr(self.app, 'gameplay') and isinstance(self.app.gameplay, tk.Variable):
            self.app.gameplay.trace_add('write', lambda *_: self._sync_gameplay_label())
        self.refresh_action_states()

    def _build_ui(self):
        self.columnconfigure(1, weight=1)

        # Linha 0: Link ou arquivo local
        ttk.Label(self, text='Link ou arquivo', style='Card.TLabel').grid(row=0, column=0, sticky='w', pady=3, padx=(0, 10))
        ttk.Entry(self, textvariable=self.source).grid(row=0, column=1, columnspan=2, sticky='ew', pady=3)
        ttk.Button(self, text='Procurar…', command=self._browse_source).grid(row=0, column=3, padx=(8, 0), pady=3)

        # Linha 1: Checkbox obrigatório de autorização + Crédito
        auth_row = ttk.Frame(self, style='Card.TFrame')
        auth_row.grid(row=1, column=0, columnspan=4, sticky='ew', pady=3)
        self.chk_auth = ttk.Checkbutton(
            auth_row,
            text=clipper.AUTH_CONFIRM_TEXT,
            variable=self.authorized,
        )
        self.chk_auth.pack(side='left')
        ttk.Label(auth_row, text='Crédito:', style='Card.TLabel').pack(side='left', padx=(16, 6))
        ttk.Entry(auth_row, textvariable=self.credito, width=20).pack(side='left')

        # Linha 2: Duração, nº cortes, modo e legenda
        cfg_row = ttk.Frame(self, style='Card.TFrame')
        cfg_row.grid(row=2, column=0, columnspan=4, sticky='ew', pady=3)
        ttk.Label(cfg_row, text='Duração (s):', style='Card.TLabel').pack(side='left')
        ttk.Spinbox(cfg_row, from_=5, to=180, increment=5, textvariable=self.duracao_min, width=5).pack(side='left', padx=(4, 2))
        ttk.Label(cfg_row, text='a', style='Card.TLabel').pack(side='left')
        ttk.Spinbox(cfg_row, from_=10, to=180, increment=5, textvariable=self.duracao_max, width=5).pack(side='left', padx=(2, 12))
        ttk.Label(cfg_row, text='Cortes:', style='Card.TLabel').pack(side='left')
        ttk.Spinbox(cfg_row, from_=1, to=15, increment=1, textvariable=self.num_cortes, width=4).pack(side='left', padx=(4, 12))
        ttk.Label(cfg_row, text='Modo:', style='Card.TLabel').pack(side='left')
        ttk.Combobox(cfg_row, textvariable=self.modo_corte, values=['preencher', 'ajustar'], state='readonly', width=10).pack(side='left', padx=(4, 12))
        ttk.Label(cfg_row, text='Enquadramento (X%):', style='Card.TLabel').pack(side='left')
        ttk.Entry(cfg_row, textvariable=self.enquadramento_x, width=5).pack(side='left', padx=(4, 12))
        ttk.Label(cfg_row, text='Legenda:', style='Card.TLabel').pack(side='left')
        ttk.Combobox(cfg_row, textvariable=self.posicao_legenda, values=['juncao', 'rodape'], state='readonly', width=8).pack(side='left', padx=(4, 12))
        ttk.Label(cfg_row, text='Idioma:', style='Card.TLabel').pack(side='left')
        ttk.Combobox(cfg_row, textvariable=self.idioma, values=['pt', 'en', 'es'], width=5).pack(side='left', padx=(4, 0))

        # Linha 3: Opções visuais + trecho min X..Y + gameplay compartilhada
        opt_row = ttk.Frame(self, style='Card.TFrame')
        opt_row.grid(row=3, column=0, columnspan=4, sticky='ew', pady=3)
        ttk.Checkbutton(opt_row, text='Título na tela (3s)', variable=self.titulo_na_tela).pack(side='left')
        ttk.Checkbutton(opt_row, text='Crédito no canto', variable=self.credito_na_tela).pack(side='left', padx=(10, 0))
        ttk.Checkbutton(opt_row, text='Música baixa', variable=self.musica_ativa).pack(side='left', padx=(10, 14))
        ttk.Label(opt_row, text='Trecho min:', style='CardMuted.TLabel').pack(side='left')
        ttk.Entry(opt_row, textvariable=self.inicio_min, width=5).pack(side='left', padx=(4, 2))
        ttk.Label(opt_row, text='ao', style='CardMuted.TLabel').pack(side='left')
        ttk.Entry(opt_row, textvariable=self.fim_min, width=5).pack(side='left', padx=(2, 12))
        ttk.Label(opt_row, textvariable=self.gameplay_info_var, style='CardMuted.TLabel').pack(side='left')

        # Linha 4: Botões principais do fluxo Cortes
        btn_row = ttk.Frame(self, style='Card.TFrame')
        btn_row.grid(row=4, column=0, columnspan=4, sticky='ew', pady=(4, 4))
        self.btn_analyze = ttk.Button(btn_row, text='Analisar vídeo', style='Accent.TButton', command=self.start_analyze)
        self.btn_analyze.pack(side='left', padx=(0, 8))
        self.btn_render = ttk.Button(btn_row, text='Renderizar selecionados', style='Secondary.TButton', command=self.start_render)
        self.btn_render.pack(side='left', padx=(0, 8))
        self.btn_cancel = ttk.Button(btn_row, text='Cancelar', command=self.cancel_operation, state='disabled')
        self.btn_cancel.pack(side='left', padx=(0, 8))
        self.btn_open_folder = ttk.Button(btn_row, text='Abrir pasta', command=self._open_cortes_folder)
        self.btn_open_folder.pack(side='left')

        # Linha 5: Resumo geral do vídeo
        ttk.Label(
            self,
            textvariable=self.summary_var,
            style='CardMuted.TLabel',
            wraplength=920,
            justify='left',
        ).grid(row=5, column=0, columnspan=4, sticky='ew', pady=(2, 4))

        # Linha 6: Treeview de candidatos
        tree_frame = ttk.Frame(self, style='Card.TFrame')
        tree_frame.grid(row=6, column=0, columnspan=4, sticky='nsew', pady=(2, 4))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        cols = ('sel', 'intervalo', 'nota', 'gancho', 'motivo')
        self.tree = ttk.Treeview(tree_frame, columns=cols, show='headings', height=5, selectmode='browse')
        self.tree.heading('sel', text='Sel')
        self.tree.heading('intervalo', text='Intervalo')
        self.tree.heading('nota', text='Nota')
        self.tree.heading('gancho', text='Gancho')
        self.tree.heading('motivo', text='Motivo')
        self.tree.column('sel', width=48, anchor='center', stretch=False)
        self.tree.column('intervalo', width=115, anchor='center', stretch=False)
        self.tree.column('nota', width=60, anchor='center', stretch=False)
        self.tree.column('gancho', width=310, anchor='w', stretch=True)
        self.tree.column('motivo', width=340, anchor='w', stretch=True)
        self.tree.grid(row=0, column=0, sticky='nsew')

        tree_scroll = ttk.Scrollbar(tree_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.grid(row=0, column=1, sticky='ns')

        self.tree.bind('<ButtonRelease-1>', self._on_tree_click)
        self.tree.bind('<<TreeviewSelect>>', lambda _e: self._show_focused_candidate_text())
        self.tree.bind('<space>', lambda _e: self._toggle_focused_selection())

        # Linha 7: Controles ±1s e visualização do texto do corte
        edit_row = ttk.Frame(self, style='Card.TFrame')
        edit_row.grid(row=7, column=0, columnspan=4, sticky='ew', pady=(2, 2))
        ttk.Label(edit_row, text='Ajustar corte selecionado:', style='Card.TLabel').pack(side='left', padx=(0, 8))
        ttk.Button(edit_row, text='-1s Início', style='Chip.TButton', command=lambda: self.adjust_focused(-1.0, 0.0)).pack(side='left', padx=2)
        ttk.Button(edit_row, text='+1s Início', style='Chip.TButton', command=lambda: self.adjust_focused(1.0, 0.0)).pack(side='left', padx=2)
        ttk.Button(edit_row, text='-1s Fim', style='Chip.TButton', command=lambda: self.adjust_focused(0.0, -1.0)).pack(side='left', padx=(10, 2))
        ttk.Button(edit_row, text='+1s Fim', style='Chip.TButton', command=lambda: self.adjust_focused(0.0, 1.0)).pack(side='left', padx=2)

        self.preview_text = tk.Text(
            self,
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
        self.preview_text.grid(row=8, column=0, columnspan=4, sticky='ew', pady=(2, 0))
        self.rowconfigure(6, weight=1)

    def _sync_gameplay_label(self):
        gp = self.app.gameplay.get().strip() if hasattr(self.app, 'gameplay') else ''
        name = Path(gp).name if gp else 'gameplay_principal.mp4 (padrão)'
        self.gameplay_info_var.set(f'Gameplay (aba Vídeo): {name}')

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
            'musica_ativa': bool(self.musica_ativa.get()),
        }

    def refresh_action_states(self):
        can_run = bool(self.authorized.get()) and not self._is_busy
        state = 'normal' if can_run else 'disabled'
        self.btn_analyze.configure(state=state)
        self.btn_render.configure(state=state)
        self.btn_cancel.configure(state='normal' if self._is_busy else 'disabled')

    def set_busy(self, busy: bool):
        self._is_busy = bool(busy)
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
        sel_mark = '[x]' if self._selected_flags[idx] else '[ ]'
        intervalo = f"{clipper.format_timestamp(cand['inicio'])}–{clipper.format_timestamp(cand['fim'])}"
        nota = f"{float(cand.get('nota', 0.0)):.1f}"
        self.tree.item(
            str(idx),
            values=(sel_mark, intervalo, nota, cand.get('gancho', ''), cand.get('motivo', '')),
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
        text = self.candidates[idx].get('texto', '') if idx is not None else ''
        self.preview_text.configure(state='normal')
        self.preview_text.delete('1.0', 'end')
        self.preview_text.insert('1.0', text)
        self.preview_text.configure(state='disabled')

    def adjust_focused(self, delta_start: float, delta_end: float):
        idx = self._focused_index()
        if idx is None:
            return
        cand = self.candidates[idx]
        max_dur = float((self.info or {}).get('duracao') or 0.0)
        new_start = max(0.0, float(cand['inicio']) + float(delta_start))
        new_end = float(cand['fim']) + float(delta_end)
        if max_dur > 0:
            new_end = min(max_dur, new_end)
        if new_end - new_start < 1.0:
            return
        cand['inicio'] = round(new_start, 3)
        cand['fim'] = round(new_end, 3)
        cand['duracao'] = round(new_end - new_start, 3)
        if self.transcription:
            words = clip_render.slice_transcription_words(self.transcription, cand['inicio'], cand['fim'])
            if words:
                cand['texto'] = ' '.join(w['text'] for w in words)
        self._refresh_tree_row(idx)
        self._show_focused_candidate_text()

    def set_analysis_result(self, info: dict, transcription: dict, report: dict):
        """Popula a UI com o resultado da análise (chamado na thread principal)."""
        self.info = info
        self.transcription = transcription
        self.report = report
        self.candidates = [dict(c) for c in (report.get('candidatos') or [])]
        self._selected_flags = [True] * len(self.candidates)

        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        for idx, cand in enumerate(self.candidates):
            sel_mark = '[x]'
            intervalo = f"{clipper.format_timestamp(cand['inicio'])}–{clipper.format_timestamp(cand['fim'])}"
            nota = f"{float(cand.get('nota', 0.0)):.1f}"
            self.tree.insert(
                '',
                'end',
                iid=str(idx),
                values=(sel_mark, intervalo, nota, cand.get('gancho', ''), cand.get('motivo', '')),
            )
        if self.candidates:
            self.tree.selection_set('0')
        self._show_focused_candidate_text()

        pot = str(report.get('potencial_cortes', '')).upper()
        just = report.get('justificativa_potencial', '')
        bullets = ' | '.join(report.get('resumo') or [])
        self.summary_var.set(f'Potencial para cortes: {pot} — {just}\nResumo: {bullets}')

    def get_selected_candidates(self) -> list[dict]:
        return [
            dict(cand)
            for cand, flag in zip(self.candidates, self._selected_flags)
            if flag
        ]

    def handle_event(self, kind: str, value) -> bool:
        if kind == 'cortes_analyzed':
            info, transcription, report = value
            self.set_analysis_result(info, transcription, report)
            return True
        return False

    def start_analyze(self):
        if not self.authorized.get():
            messagebox.showerror('Autorização', f'Marque "{clipper.AUTH_CONFIRM_TEXT}" para continuar.')
            return
        src = self.source.get().strip()
        if not src:
            messagebox.showerror('Cortes', 'Informe um link de vídeo ou escolha um arquivo local.')
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
            messagebox.showinfo('Cortes', 'Marque ao menos um candidato na lista ([x]) para renderizar.')
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

        def task():
            log_fn = lambda msg: self.app.events.put(('log', msg))
            for idx, cand in enumerate(chosen, start=1):
                if self.cancel_event.is_set():
                    raise clipper.CancelledError('Renderização cancelada pelo usuário.')
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
                    crop_x=crop_x_val,
                    enquadramento=cfg.get('enquadramento', 'auto'),
                    modelos=modelos,
                    log=log_fn,
                )
            log_fn(f'Renderização dos {len(chosen)} corte(s) concluída em output/cortes/{info_copy["id"]}.')

        self.app._job(task)

    def destroy(self):
        for attr, val in list(self.__dict__.items()):
            if isinstance(val, tk.Variable):
                try:
                    delattr(self, attr)
                except Exception:
                    pass
        super().destroy()
