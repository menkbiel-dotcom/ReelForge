"""Seleção de música para o modo Cortes, detecção de drop via envelope RMS/fluxo espectral e fallback sintético."""
import json
import random
import subprocess
import wave
from pathlib import Path

import numpy as np

import engine
import media_tools
import music
import sfx

EXTENSIONS = {'.mp3', '.wav', '.m4a', '.ogg', '.flac', '.aac'}
GENERATED_LOFI = 'reelforge_lofi_gerada.wav'
PUNCHY_BED_NAME = 'punchy_bed.wav'
RATE = 44100
ANALYSIS_RATE = 22050


def list_tracks(root: str | Path | None = None, pasta: str = 'impacto') -> list[Path]:
    """Lista faixas em assets/music/<pasta>/; se vazio, usa assets/music/ (exceto a lo-fi gerada e _sfx)."""
    base = Path(root) if root is not None else engine.ROOT
    music_dir = base / 'assets' / 'music'
    music_dir.mkdir(parents=True, exist_ok=True)

    if pasta:
        sub_dir = music_dir / pasta
        if sub_dir.is_dir():
            sub_tracks = sorted(
                p for p in sub_dir.iterdir()
                if p.is_file() and p.suffix.lower() in EXTENSIONS and p.name != GENERATED_LOFI
            )
            if sub_tracks:
                return sub_tracks

    return sorted(
        p for p in music_dir.iterdir()
        if p.is_file() and p.suffix.lower() in EXTENSIONS and p.name != GENERATED_LOFI
    )


def _drop_cache_path(track_path: Path) -> Path:
    return track_path.with_suffix(track_path.suffix + '.drop.json')


def analyze_drop(path: str | Path) -> float:
    """Decodifica até 90 s em mono 22,05 kHz float32 e acha o 'drop' (maior salto positivo precedido de trecho calmo)."""
    track_path = Path(path)
    if not track_path.is_file():
        return 0.0

    cache_file = _drop_cache_path(track_path)
    mtime_ns = track_path.stat().st_mtime_ns
    if cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding='utf-8'))
            if cached.get('mtime_ns') == mtime_ns and isinstance(cached.get('drop'), (int, float)):
                return float(cached['drop'])
        except (OSError, ValueError):
            pass

    cmd = [
        media_tools.ffmpeg(), '-v', 'error', '-nostdin',
        '-t', '90', '-i', str(track_path),
        '-vn', '-ac', '1', '-ar', str(ANALYSIS_RATE), '-f', 'f32le', '-',
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0 or not proc.stdout:
        return 0.0

    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    win = int(round(0.05 * ANALYSIS_RATE))  # ~50 ms (1102 amostras)
    n_frames = len(samples) // win
    if n_frames < 4:
        return 0.0

    frames = samples[:n_frames * win].reshape(n_frames, win).astype(np.float64)
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)

    # Fluxo espectral positivo simples em numpy puro
    hann = np.hanning(win)
    spectra = np.abs(np.fft.rfft(frames * hann, axis=1))
    spec_diff = np.diff(spectra, axis=0, prepend=spectra[:1])
    flux = np.sum(np.maximum(0.0, spec_diff), axis=1)
    if np.max(flux) > 1e-9:
        flux = flux / np.max(flux)
    max_rms = float(np.max(rms)) if rms.size else 0.0
    norm_rms = rms / max_rms if max_rms > 1e-9 else rms

    # Para cada frame k, mede o salto entre trecho calmo anterior (~1 s = 20 janelas) e trecho seguinte (~0,5 s = 10 janelas)
    pre_w = min(20, max(1, n_frames // 6))
    post_w = min(10, max(1, n_frames // 8))
    best_idx = 0
    best_score = -1e9
    for k in range(1, n_frames):
        pre_slice = norm_rms[max(0, k - pre_w):k]
        post_slice = norm_rms[k:min(n_frames, k + post_w)]
        pre_mean = float(np.mean(pre_slice)) if pre_slice.size else 0.0
        post_mean = float(np.mean(post_slice)) if post_slice.size else float(norm_rms[k])
        inst_jump = max(0.0, float(norm_rms[k] - norm_rms[k - 1]))
        contrast = post_mean - pre_mean
        if contrast <= 0.0:
            continue
        score = contrast + 0.65 * inst_jump + 0.35 * float(flux[k])
        if score > best_score:
            best_score = score
            best_idx = k

    dt = win / float(ANALYSIS_RATE)
    drop_sec = round(max(0.0, best_idx * dt - 0.05), 3)
    try:
        cache_file.write_text(
            json.dumps({'mtime_ns': mtime_ns, 'drop': drop_sec}, indent=2),
            encoding='utf-8',
        )
    except OSError:
        pass
    return drop_sec


def _midi(note: float) -> float:
    return 440.0 * (2.0 ** ((float(note) - 69.0) / 12.0))


def generate_punchy_bed(dest: str | Path, bpm: int = 128, seed: int = 4403) -> Path:
    """Gera base rítmica sintética de ~24 s (~128 BPM: bumbo 4x4, clap, chimbal, baixo e arpejo menor, loop exato)."""
    rng = np.random.default_rng(seed)
    beat = 60.0 / float(bpm)  # 0.46875 s em 128 BPM
    # 51.2 batidas = 24.0 s; 12 compassos (48 batidas) = 22.5 s (~24 s com loop perfeito de compasso 4/4)
    bars = 12
    total_beats = bars * 4
    duration = total_beats * beat  # 22.5 s (~24 s, loop 4/4 exato)
    length = int(round(duration * RATE))
    mix = np.zeros(length + RATE * 2, dtype=np.float64)

    def add(start_s: float, wave_data: np.ndarray):
        idx = int(round(start_s * RATE))
        if idx < 0 or idx >= len(mix):
            return
        end_idx = min(len(mix), idx + len(wave_data))
        mix[idx:end_idx] += wave_data[:end_idx - idx]

    # Progressão menor (Lá menor -> Fá -> Ré menor -> Mi)
    chords = [
        ([57, 60, 64], 45),
        ([53, 57, 60], 41),
        ([50, 53, 57], 38),
        ([52, 56, 59], 40),
    ]

    for b in range(total_beats):
        t_beat = b * beat
        bar_idx = (b // 4) % len(chords)
        notes, bass_note = chords[bar_idx]

        # Intro mais contida nos primeiros 4 compassos (~7,5 s) para destacar o drop no compasso 5
        in_drop = b >= 8
        energy_scale = 1.0 if in_drop else 0.25

        # 1. Bumbo 4x4
        tk = np.arange(int(0.22 * RATE), dtype=np.float64) / RATE
        kf = 48.0 + 110.0 * np.exp(-32.0 * tk)
        kphase = 2.0 * np.pi * np.cumsum(kf) / RATE
        kick = 0.48 * energy_scale * np.exp(-11.0 * tk) * np.sin(kphase)
        add(t_beat, kick)

        # 2. Clap nos tempos 2 e 4
        if b % 2 == 1 and in_drop:
            tc = np.arange(int(0.14 * RATE), dtype=np.float64) / RATE
            clap = 0.22 * np.exp(-24.0 * tc) * rng.standard_normal(len(tc))
            add(t_beat, clap)

        # 3. Chimbal nos contratempos
        th = np.arange(int(0.05 * RATE), dtype=np.float64) / RATE
        hat = 0.08 * energy_scale * np.exp(-55.0 * th) * rng.standard_normal(len(th))
        add(t_beat + 0.5 * beat, hat)

        # 4. Baixo pulsante
        tb = np.arange(int(beat * 0.9 * RATE), dtype=np.float64) / RATE
        bf = _midi(bass_note)
        bass = 0.28 * energy_scale * np.exp(-4.5 * tb) * (
            np.sin(2.0 * np.pi * bf * tb) + 0.35 * np.sin(4.0 * np.pi * bf * tb)
        )
        add(t_beat, bass)

        # 5. Arpejo menor em semicolcheias
        for step in range(4):
            ta = np.arange(int(0.12 * RATE), dtype=np.float64) / RATE
            arp_note = notes[(b + step) % len(notes)] + 12
            af = _midi(arp_note)
            arp = 0.09 * energy_scale * np.exp(-14.0 * ta) * np.sin(2.0 * np.pi * af * ta)
            add(t_beat + step * (beat / 4.0), arp)

    tail = mix[length:]
    mix = mix[:length].copy()
    mix[:len(tail)] += tail[:length]
    max_abs = float(np.max(np.abs(mix))) if mix.size else 0.0
    if max_abs > 1e-8:
        mix = mix * (0.82 / max_abs)
    left = mix
    right = np.roll(mix, int(0.008 * RATE))
    pcm = (np.stack([left, right], axis=1) * 32767.0).astype('<i2')
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


def pick_for_clip(
    root: str | Path | None,
    total: float,
    modo: str = 'impacto',
) -> tuple[Path | None, float]:
    """Escolhe faixa e offset inicial conforme o modo ('desligada', 'baixa' ou 'impacto')."""
    base = Path(root) if root is not None else engine.ROOT
    norm = str(modo or 'desligada').strip().casefold()
    if norm in ('', 'desligada', 'none', 'off'):
        return None, 0.0

    if norm == 'baixa':
        track = music.pick(base)
        length = engine.ffprobe_duration(track)
        offset = random.uniform(0.0, max(0.0, length - float(total) - 0.5))
        return track, round(offset, 3)

    if norm == 'impacto':
        tracks = list_tracks(base, pasta='impacto')
        if tracks:
            track = random.choice(tracks)
        else:
            bed = sfx.sfx_folder(base) / PUNCHY_BED_NAME
            if not bed.is_file() or bed.stat().st_size == 0:
                generate_punchy_bed(bed)
            track = bed
        drop = analyze_drop(track)
        start = max(0.0, round(float(drop) - 0.1, 3))
        return track, start

    raise ValueError(f'Modo de música inválido: "{modo}". Use "desligada", "baixa" ou "impacto".')
