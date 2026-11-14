import gc
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gui
from tests._isolation import IsolatedOutputMixin

HAS_TKINTER = importlib.util.find_spec('tkinter') is not None


class CortesGuiTests(IsolatedOutputMixin, unittest.TestCase):
    def test_merge_settings_preserves_cortes_keys_and_unknown_subkeys(self):
        current = {
            'topic': 'tecnologia',
            'cortes': {
                'idioma': 'pt',
                'duracao_min': 25.0,
                'custom_cortes_flag': 'keep_me',
            },
        }
        from_ui = {
            'cortes': {
                'idioma': 'en',
                'duracao_min': 30.0,
                'modo_corte': 'preencher',
            },
        }
        merged = gui.merge_settings(current, from_ui)
        self.assertEqual(merged['cortes']['idioma'], 'en')
        self.assertEqual(merged['cortes']['duracao_min'], 30.0)
        self.assertEqual(merged['cortes']['modo_corte'], 'preencher')
        self.assertEqual(merged['cortes']['custom_cortes_flag'], 'keep_me')

    @unittest.skipUnless(HAS_TKINTER, 'Tkinter necessário para instanciar GUI')
    def test_cortes_tab_auth_gate_candidates_adjust_and_save(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_file = Path(td) / 'settings.json'
            cfg_file.write_text(
                json.dumps({
                    'topic': 'ciência',
                    'ollama_model': 'qwen3.5:4b',
                    'cortes': {
                        'idioma': 'pt',
                        'duracao_min': 20.0,
                        'duracao_max': 50.0,
                        'num_cortes': 4,
                        'modo_corte': 'ajustar',
                        'posicao_legenda': 'juncao',
                        'titulo_na_tela': True,
                        'credito_na_tela': True,
                        'credito': '@canal_orig',
                        'extra_cortes_key': 42,
                    },
                }, indent=2),
                encoding='utf-8',
            )
            with patch.object(gui, 'CONFIG', cfg_file), \
                 patch('requests.get', side_effect=Exception('offline')):
                app = gui.App()
                app.withdraw()
                try:
                    tab = app.cortes_tab
                    # Sem autorização marcada, botões devem ficar desabilitados
                    self.assertFalse(tab.authorized.get())
                    self.assertEqual(str(tab.btn_analyze['state']), 'disabled')
                    self.assertEqual(str(tab.btn_render['state']), 'disabled')

                    # Ao marcar autorização, botões ficam habilitados
                    tab.authorized.set(True)
                    self.assertEqual(str(tab.btn_analyze['state']), 'normal')
                    self.assertEqual(str(tab.btn_render['state']), 'normal')

                    # Popula resultado da análise
                    info = {'id': 'v1', 'duracao': 120.0, 'autorizado': True}
                    trans = {
                        'palavras': [
                            {'texto': 'Primeira', 'inicio': 9.0, 'fim': 9.5},
                            {'texto': 'Segunda', 'inicio': 10.2, 'fim': 10.8},
                            {'texto': 'palavra', 'inicio': 11.0, 'fim': 11.5},
                            {'texto': 'final.', 'inicio': 35.2, 'fim': 35.8},
                        ]
                    }
                    report = {
                        'resumo': ['B1', 'B2', 'B3'],
                        'potencial_cortes': 'alto',
                        'justificativa_potencial': 'Tema forte.',
                        'candidatos': [
                            {
                                'indice': 1,
                                'inicio': 10.0,
                                'fim': 35.0,
                                'duracao': 25.0,
                                'nota': 9.1,
                                'gancho': 'Gancho teste',
                                'motivo': 'Motivo teste',
                                'texto': 'Segunda palavra',
                            },
                            {
                                'indice': 2,
                                'inicio': 50.0,
                                'fim': 80.0,
                                'duracao': 30.0,
                                'nota': 7.5,
                                'gancho': 'Gancho dois',
                                'motivo': 'Motivo dois',
                                'texto': 'Outro corte',
                            },
                        ],
                    }
                    tab.set_analysis_result(info, trans, report)
                    self.assertEqual(len(tab.tree.get_children()), 2)
                    self.assertEqual(len(tab.get_selected_candidates()), 2)
                    self.assertIn('ALTO', tab.summary_var.get())
                    self.assertIn('Segunda palavra', tab.preview_text.get('1.0', 'end'))

                    # Desmarca segundo candidato e ajusta primeiro em -1s início / +1s fim
                    tab._toggle_row_by_id('1')
                    selected = tab.get_selected_candidates()
                    self.assertEqual(len(selected), 1)
                    self.assertEqual(selected[0]['gancho'], 'Gancho teste')

                    tab.tree.selection_set('0')
                    tab.adjust_focused(-1.0, 1.0)
                    self.assertAlmostEqual(tab.candidates[0]['inicio'], 9.0, places=2)
                    self.assertAlmostEqual(tab.candidates[0]['fim'], 36.0, places=2)
                    self.assertIn('Primeira', tab.preview_text.get('1.0', 'end'))
                    self.assertIn('final.', tab.preview_text.get('1.0', 'end'))

                    # Salva configurações e valida preservação de chaves
                    tab.modo_corte.set('preencher')
                    saved = app._save()
                    self.assertEqual(saved['cortes']['modo_corte'], 'preencher')
                    self.assertEqual(saved['cortes']['extra_cortes_key'], 42)
                    disk = json.loads(cfg_file.read_text(encoding='utf-8'))
                    self.assertEqual(disk['cortes']['extra_cortes_key'], 42)
                finally:
                    app.destroy()
                    app = None
                    gc.collect()


if __name__ == '__main__':
    unittest.main(verbosity=2)
