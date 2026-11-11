"""Música de fundo: usa faixas do usuário em assets/music ou gera uma faixa lo-fi local.

A faixa gerada é sintetizada aqui mesmo (numpy), sem internet e sem direitos de
terceiros, e já sai com loop perfeito: o final emenda no começo.
"""
import random
import wave
from pathlib import Path

import numpy as np

EXTENSIONS = {'.mp3', '.wav', '.m4a', '.ogg', '.flac', '.aac'}
GENERATED = 'reelforge_lofi_gerada.wav'
RATE = 44100


def folder(root):
    path = Path(root) / 'assets' / 'music'
    path.mkdir(parents=True, exist_ok=True)
    return path


def pick(root):
    """Sorteia uma faixa do usuário; sem nenhuma, gera (uma vez) a faixa lo-fi local."""
    music = folder(root)
    tracks = [p for p in music.iterdir() if p.suffix.lower() in EXTENSIONS and p.name != GENERATED]
    if tracks:
        return random.choice(tracks)
    generated = music / GENERATED
    if not generated.is_file():
        generate(generated)
    return generated


def _midi(note):
    return 440.0 * 2 ** ((note - 69) / 12)


def _lowpass(signal, cutoff):
    alpha = 1 - np.exp(-2 * np.pi * cutoff / RATE)
    out = np.empty_like(signal)
    acc = 0.0
    # Filtro de um polo em blocos vetorizados seria complexo; o laço é rápido o bastante para ~40 s.
    for i, x in enumerate(signal):
        acc += alpha * (x - acc)
        out[i] = acc
    return out


def generate(dest, bpm=78, repeats=2, seed=7):
    """Lo-fi suave: pad, arpejo dedilhado, baixo e chimbal discreto. Loop exato."""
    rng = np.random.default_rng(seed)
    beat = 60 / bpm
    bar = 4 * beat
    chords = [([57, 60, 64, 67], 45), ([53, 57, 60, 64], 41), ([55, 59, 60, 64], 48), ([55, 59, 62, 64], 40)]
    length = int(round(len(chords) * 2 * bar * repeats * RATE))
    mix = np.zeros(length + RATE * 4)
    t_note = lambda seconds: np.arange(int(seconds * RATE)) / RATE

    def add(start, wave_data):
        i = int(start * RATE)
        mix[i:i + len(wave_data)] += wave_data

    for r in range(repeats):
        for c, (notes, root) in enumerate(chords):
            start = (r * len(chords) + c) * 2 * bar
            # Pad: senos levemente desafinados, ataque e soltura longos.
            dur = 2 * bar + 1.2
            t = t_note(dur)
            env = np.minimum(1, t / 0.9) * np.minimum(1, np.maximum(0, (dur - t) / 1.2))
            pad = sum(np.sin(2 * np.pi * _midi(n) * d * t) for n in notes for d in (0.998, 1.002))
            add(start, 0.05 * env * pad)
            # Baixo nos tempos 1 e 3.
            for b in range(0, 8, 2):
                t = t_note(beat * 1.8)
                add(start + b * beat, 0.22 * np.exp(-2.2 * t) * np.sin(2 * np.pi * _midi(root) * t))
            # Arpejo em colcheias, uma oitava acima, com leve swing.
            pattern = [0, 1, 2, 3, 2, 1, 2, 3]
            for k in range(16):
                if rng.random() < 0.18:
                    continue
                note = notes[pattern[k % 8]] + 12
                t = t_note(0.9)
                tone = np.sin(2 * np.pi * _midi(note) * t) + 0.25 * np.sin(2 * np.pi * _midi(note) * 3 * t)
                swing = 0.06 * beat if k % 2 else 0
                add(start + k * beat / 2 + swing, 0.07 * np.exp(-4.5 * t) * tone)
            # Chimbal: ruído curto nos contratempos.
            for k in range(8):
                t = t_note(0.06)
                add(start + k * beat + beat / 2, 0.025 * np.exp(-60 * t) * rng.standard_normal(len(t)))
    # Loop perfeito: o que passa do fim (caudas das notas) volta para o começo.
    tail = mix[length:]
    mix = mix[:length].copy()
    mix[:len(tail)] += tail[:length]
    mix = _lowpass(mix, 3200)
    mix += 0.004 * _lowpass(rng.standard_normal(length), 1800)  # chiado de vinil bem leve
    mix *= 0.85 / max(1e-6, np.max(np.abs(mix)))
    left = mix
    right = np.roll(mix, int(0.012 * RATE))  # leve abertura estéreo, mantendo o loop
    data = (np.stack([left, right], axis=1) * 32767).astype('<i2')
    dest = Path(dest)
    tmp = dest.with_suffix('.tmp.wav')
    with wave.open(str(tmp), 'wb') as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(data.tobytes())
    tmp.replace(dest)
    return dest
