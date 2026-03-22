#!/usr/bin/env python3
"""
FM IQ modulátor pre HackRF One (RPi4 optimalizovaný)
Vstup:  raw float32 mono na stdin (z sox)
Výstup: int8 interleaved IQ na stdout (pre hackrf_transfer)

Pipeline:
  sox file.wav -t raw -r {sdr_rate} -e float -b 32 -c 1 - \
    | python3 fm_modulator.py --rate 8000000 --amp 1.0 \
    | hackrf_transfer -f 433920000 -s 8000000 -x 20 -t /dev/stdin
"""

import sys
import argparse
import numpy as np
import math

DEVIATION_HZ = 75_000.0
CHUNK = 65536  # 64k vzoriek (~8ms pri 8Msps) — nižšia latencia pre RPi4


def fm_stream(sdr_rate: int, amplitude: float, deviation: float) -> None:
    kf = np.float32(2.0 * np.pi * deviation / sdr_rate)
    phase_carry = np.float32(0.0)
    amp_val = np.float32(max(0.0, min(1.0, amplitude)) * 127.0)

    # Predalokácia bufferov pre lepšiu výkon na RPi4
    audio_buf = np.empty(CHUNK, dtype=np.float32)
    phase_buf = np.empty(CHUNK, dtype=np.float32)
    iq_buf = np.empty(CHUNK * 2, dtype=np.int8)

    while True:
        raw = sys.stdin.buffer.read(CHUNK * 4)
        if not raw:
            break
        
        n_samples = len(raw) // 4
        if n_samples == 0:
            break

        # Načítanie priamo do predalokovaného bufferu
        audio = np.frombuffer(raw, dtype=np.float32, count=n_samples)
        audio_buf[:n_samples] = audio

        # Normalizácia s ochranou proti deleniu nulou
        peak = np.max(np.abs(audio_buf[:n_samples]))
        if peak > 1e-6:
            audio_buf[:n_samples] = np.clip(audio_buf[:n_samples] / peak, -1.0, 1.0)

        # FM fázová integrácia — optimalizovaná pre ARM
        pd = audio_buf[:n_samples] * kf
        pd[0] += phase_carry
        np.cumsum(pd, out=phase_buf[:n_samples])  # in-place cumsum
        phase_carry = np.float32(math.fmod(float(phase_buf[n_samples - 1]), 2.0 * np.pi))

        # IQ generovanie + konverzia na int8
        iq_buf[:n_samples * 2:2] = np.clip(np.cos(phase_buf[:n_samples]) * amp_val, -128, 127).astype(np.int8)
        iq_buf[1:n_samples * 2:2] = np.clip(np.sin(phase_buf[:n_samples]) * amp_val, -128, 127).astype(np.int8)

        sys.stdout.buffer.write(iq_buf[:n_samples * 2].tobytes())
        sys.stdout.buffer.flush()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--rate", type=int, default=8_000_000, help="Sample rate in Hz")
    p.add_argument("--amp", type=float, default=1.0, help="Amplitude 0.0-1.0")
    p.add_argument("--dev", type=float, default=DEVIATION_HZ, help="Frequency deviation in Hz")
    args = p.parse_args()

    try:
        fm_stream(args.rate, args.amp, args.dev)
    except (BrokenPipeError, KeyboardInterrupt):
        sys.exit(0)
    except Exception as e:
        print(f"[FM_MOD ERROR] {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
