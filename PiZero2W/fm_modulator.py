#!/usr/bin/env python3
"""
FM IQ modulátor pre HackRF One
Optimalizovaný pre Raspberry Pi Zero 2W (512 MB RAM, quad A53 @1GHz)

Použitie:
  python3 fm_modulator.py --file audio.wav --rate 8000000 --amp 1.0 \
    | hackrf_transfer -f 433920000 -s 8000000 -x 20 -t /dev/stdin

Závislosti:
  pip3 install numpy scipy --break-system-packages
"""

import sys
import argparse
import wave
import numpy as np
from scipy import signal as sp

# Zero 2W — menší chunk znižuje špičkové využitie RAM
# 256 k vzoriek × 2 (I+Q) × 1 byte = 512 kB na chunk → bezpečné pri 512 MB
CHUNK_SAMPLES = 262_144   # 256 k


def read_wav(path: str):
    """Načítaj WAV → float32 mono."""
    with wave.open(path, "rb") as wf:
        n_ch   = wf.getnchannels()
        sw     = wf.getsampwidth()
        rate   = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    if sw == 2:
        raw = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 1:
        raw = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sw == 4:
        raw = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2_147_483_648.0
    else:
        raise ValueError(f"Nepodporovaná hĺbka: {sw*8} bit")

    if n_ch == 2:
        raw = raw.reshape(-1, 2).mean(axis=1)
    elif n_ch > 2:
        raw = raw.reshape(-1, n_ch)[:, 0]

    return raw, rate


def fm_modulate(wav_path: str, sdr_rate: int, amplitude: float,
                audio_rate: int = 44_100,
                deviation: float = 75_000.0) -> None:
    """
    FM modulácia → int8 IQ stream na stdout.
    Spracúva v blokoch CHUNK_SAMPLES — šetrí RAM na Zero 2W.
    """
    audio_raw, src_rate = read_wav(wav_path)

    # 1. Resample na audio_rate (ak sa líši od zdrojovej)
    if src_rate != audio_rate:
        audio_raw = sp.resample_poly(audio_raw, audio_rate, src_rate)

    # 2. Pre-emphasis (τ = 50 µs — európska norma)
    alpha = np.exp(-1.0 / (audio_rate * 50e-6))
    audio_raw = sp.lfilter([1.0 - alpha], [1.0, -alpha], audio_raw)

    # 3. Normalizácia + clip
    peak = np.max(np.abs(audio_raw))
    if peak > 0:
        audio_raw = np.clip(audio_raw / peak, -1.0, 1.0)

    # 4. Resample na SDR rate pomocou polyphase (efektívnejšie na pomalšom CPU)
    audio_up = sp.resample_poly(audio_raw, sdr_rate, audio_rate)
    n_total  = len(audio_up)

    # 5. FM modulácia v blokoch
    kf           = 2.0 * np.pi * deviation / sdr_rate
    phase_carry  = 0.0
    pos          = 0
    amp          = max(0.0, min(1.0, amplitude))

    while pos < n_total:
        chunk = audio_up[pos: pos + CHUNK_SAMPLES]
        pos  += CHUNK_SAMPLES

        # Fázová integrácia
        pd = chunk * kf
        pd[0] += phase_carry
        phase = np.cumsum(pd)
        phase_carry = float(phase[-1])

        # IQ generácia — float64 → int8 (2 operácie, nie complex128)
        cos_p = np.cos(phase)
        sin_p = np.sin(phase)

        i8 = np.clip(cos_p * (amp * 127.0), -128, 127).astype(np.int8)
        q8 = np.clip(sin_p * (amp * 127.0), -128, 127).astype(np.int8)

        # Interleave I/Q
        out = np.empty(len(i8) * 2, dtype=np.int8)
        out[0::2] = i8
        out[1::2] = q8

        sys.stdout.buffer.write(out.tobytes())
        sys.stdout.buffer.flush()

        # Uvoľni pamäť explicitne — Zero 2W má len 512 MB
        del cos_p, sin_p, i8, q8, out, phase, pd, chunk


def main():
    p = argparse.ArgumentParser(description="FM IQ modulátor — RPi Zero 2W")
    p.add_argument("--file",  required=True,            help="Vstupný WAV súbor")
    p.add_argument("--rate",  type=int,   default=8_000_000, help="SDR sample rate (Hz)")
    p.add_argument("--amp",   type=float, default=1.0,   help="Amplitúda 0.0–1.0")
    p.add_argument("--dev",   type=float, default=75_000.0,  help="FM deviácia (Hz)")
    p.add_argument("--arate", type=int,   default=44_100,    help="Audio sample rate")
    args = p.parse_args()

    try:
        fm_modulate(
            wav_path   = args.file,
            sdr_rate   = args.rate,
            amplitude  = args.amp,
            audio_rate = args.arate,
            deviation  = args.dev,
        )
    except BrokenPipeError:
        sys.exit(0)        # hackrf_transfer zatvoril stdin (TX stop)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        print(f"[FM_MOD ERROR] {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
