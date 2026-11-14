"""Geração local determinística de efeitos sonoros (SFX) em WAV estéreo 44,1 kHz (numpy puro, offline)."""
from pathlib import Path
import wave

import numpy as np

import engine

RATE = 44100
PEAK_LIMIT = 0.85  # ~ -1.41 dBFS (sempre <= -1 dBFS)


def sfx_folder(root: str | Path | None = None) -> Path:
    base = Path(root) if root is not None else engine.ROOT
    path = base / 'assets' / 'music' / '_sfx'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _lowpass_1pole(signal: np.ndarray, cutoff_hz: float, rate: int = RATE) -> np.ndarray:
    alpha = float(1.0 - np.exp(-2.0 * np.pi * float(cutoff_hz) / float(rate)))
    out = np.empty_like(signal, dtype=np.float64)
    acc = 0.0
    for i, x in enumerate(signal):
        acc += alpha * (float(x) - acc)
        out[i] = acc
    return out


def _write_stereo_wav(dest: Path, left: np.ndarray, right: np.ndarray, peak: float = PEAK_LIMIT) -> Path:
    stereo = np.stack([left, right], axis=1).astype(np.float64)
    max_abs = float(np.max(np.abs(stereo))) if stereo.size else 0.0
    if max_abs > 1e-8:
        stereo = stereo * (float(peak) / max_abs)
    stereo = np.clip(stereo, -float(peak), float(peak))
    pcm = (stereo * 32767.0).astype('<i2')
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix('.tmp.wav')
    with wave.open(str(tmp), 'wb') as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(pcm.tobytes())
    tmp.replace(dest)
    return dest


def generate_impacto(dest: str | Path, seed: int = 4401) -> Path:
    """Gera efeito 'impacto' (~1,2 s): sub-grave 90->35 Hz + transiente forte + cauda de ruído filtrado (pico <= -1 dBFS)."""
    rng = np.random.default_rng(seed)
    duration = 1.2
    n = int(round(duration * RATE))
    t = np.arange(n, dtype=np.float64) / RATE

    # 1. Sub-grave com queda exponencial 90 -> 35 Hz
    freq = 35.0 + (90.0 - 35.0) * np.exp(-5.5 * t)
    phase = 2.0 * np.pi * np.cumsum(freq) / RATE
    sub_env = np.minimum(1.0, t / 0.004) * np.exp(-2.6 * t)
    sub = 0.78 * sub_env * np.sin(phase)

    # 2. Transiente de ataque nos primeiros 100 ms (punch médio-grave + clique)
    punch_freq = 60.0 + 160.0 * np.exp(-40.0 * t)
    punch_phase = 2.0 * np.pi * np.cumsum(punch_freq) / RATE
    punch = 0.55 * np.exp(-24.0 * t) * np.sin(punch_phase)
    click = 0.35 * np.exp(-90.0 * t) * rng.standard_normal(n)

    # 3. Cauda estéreo de ruído filtrado com fade-out suave
    noise_l = _lowpass_1pole(rng.standard_normal(n), 850.0, RATE)
    noise_r = _lowpass_1pole(rng.standard_normal(n), 850.0, RATE)
    tail_env = np.minimum(1.0, t / 0.01) * np.exp(-3.4 * t) * np.minimum(1.0, np.maximum(0.0, (duration - t) / 0.15))

    left = np.tanh(1.35 * (sub + punch + click + 0.28 * tail_env * noise_l))
    right = np.tanh(1.35 * (sub + punch + click + 0.28 * tail_env * noise_r))
    return _write_stereo_wav(Path(dest), left, right, peak=PEAK_LIMIT)


def generate_whoosh(dest: str | Path, seed: int = 4402) -> Path:
    """Gera efeito 'whoosh' (~0,8 s): ruído com varredura passa-banda ascendente + hit curto (pico <= -1 dBFS)."""
    rng = np.random.default_rng(seed)
    duration = 0.8
    n = int(round(duration * RATE))
    t = np.arange(n, dtype=np.float64) / RATE

    # Varredura passa-banda ascendente (350 Hz -> 3400 Hz) via modulação + filtragem
    sweep_f = 350.0 + (3400.0 - 350.0) * ((t / duration) ** 1.35)
    sweep_phase = 2.0 * np.pi * np.cumsum(sweep_f) / RATE
    raw_l = _lowpass_1pole(rng.standard_normal(n), 1400.0, RATE)
    raw_r = _lowpass_1pole(rng.standard_normal(n), 1400.0, RATE)

    # Envelope com energia imediata nos primeiros 100 ms e pico no hit
    whoosh_env = (0.45 * np.exp(-6.0 * t) + 0.75 * np.sin(np.pi * (t / duration) ** 0.75))
    whoosh_env *= np.minimum(1.0, t / 0.005) * np.minimum(1.0, np.maximum(0.0, (duration - t) / 0.08))

    band_l = raw_l * (0.6 + 0.4 * np.cos(sweep_phase))
    band_r = raw_r * (0.6 + 0.4 * np.sin(sweep_phase))

    # Hit curto no ataque (primeiros 100 ms) + acento na crista da varredura
    hit_f = 55.0 + 180.0 * np.exp(-38.0 * t)
    hit_phase = 2.0 * np.pi * np.cumsum(hit_f) / RATE
    early_hit = 0.65 * np.exp(-18.0 * t) * np.sin(hit_phase)

    crest_t = np.maximum(0.0, t - 0.42)
    crest_hit = np.where(t >= 0.42, 0.45 * np.exp(-28.0 * crest_t) * np.sin(2.0 * np.pi * 95.0 * crest_t), 0.0)

    left = np.tanh(1.3 * (early_hit + crest_hit + 0.65 * whoosh_env * band_l))
    right = np.tanh(1.3 * (early_hit + crest_hit + 0.65 * whoosh_env * band_r))
    return _write_stereo_wav(Path(dest), left, right, peak=PEAK_LIMIT)


def ensure_sfx(kind: str, root: str | Path | None = None) -> Path | None:
    """Garante que o arquivo WAV do SFX ('impacto' ou 'whoosh') exista em assets/music/_sfx/ e retorna seu Path."""
    norm = str(kind or 'nenhum').strip().casefold()
    if norm in ('', 'nenhum', 'none', 'desligado'):
        return None
    folder = sfx_folder(root)
    if norm == 'impacto':
        target = folder / 'impacto.wav'
        if not target.is_file() or target.stat().st_size == 0:
            generate_impacto(target)
        return target
    if norm == 'whoosh':
        target = folder / 'whoosh.wav'
        if not target.is_file() or target.stat().st_size == 0:
            generate_whoosh(target)
        return target
    raise ValueError(f'Tipo de impacto inicial inválido: "{kind}". Use "nenhum", "impacto" ou "whoosh".')
