"""Render the bundled example through the same engine used by the GUI."""
from pathlib import Path
import engine
import script_io

if __name__ == '__main__':
    source = Path(__file__).resolve().parent / 'assets/demo_final/dialogue.json'
    story, lines, background = script_io.load(source)
    engine.render(story, lines, background, 30, 45, True, 'Kit de emergência: só lámen?')
