"""PyInstaller entry point: use persistent directory next to the EXE."""
import os
import sys
from pathlib import Path
if getattr(sys, 'frozen', False):
    os.environ['REELFORGE_HOME'] = str(Path(sys.executable).resolve().parent)
import gui
if __name__ == '__main__':
    gui.App().mainloop()
