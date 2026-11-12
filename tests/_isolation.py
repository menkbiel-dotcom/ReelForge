"""Mixin compartilhado para isolar engine.OUTPUT em diretório temporário durante os testes."""
import tempfile
from pathlib import Path
from unittest.mock import patch

import engine


class IsolatedOutputMixin:
    """Garante que nenhum teste escreva no diretório output/ real do projeto."""

    def setUp(self):
        super().setUp()
        self._isolated_output_temp = tempfile.TemporaryDirectory()
        self._isolated_output_dir = Path(self._isolated_output_temp.name)
        self._isolated_output_patch = patch.object(engine, 'OUTPUT', self._isolated_output_dir)
        self._isolated_output_patch.start()
        self.addCleanup(self._cleanup_isolated_output)

    def _cleanup_isolated_output(self):
        self._isolated_output_patch.stop()
        self._isolated_output_temp.cleanup()
