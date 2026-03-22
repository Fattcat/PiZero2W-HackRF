#!/usr/bin/env python3
"""
HackRF One Web Controller — RPi4 verzia
Lokálna sieť, port 8080

Spustenie:  python3 server.py
Prístup:    http://<IP>:8080
"""

import os
import sys
import json
import signal
import subprocess
import threading
import time
import queue
import hashlib
import shutil
from pathlib import Path
from flask import Flask, request, jsonify, Response, send_from_directory

# ─── Cesty ───────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent.resolve()
UPLOAD = BASE / "uploads"
LIBRARY = BASE / "library"
STATIC = BASE / "static"

for _d in (UPLOAD, LIBRARY, STATIC):
    _d.mkdir(exist_ok=True)

MAX_MB = 150
MAX_SEC = 600

# ─── Stav ────────────────────────────────────────────────────────────────────
_hackrf = False
_tx_proc = None
_tx_lock = threading.Lock()
_tx_meta = {}
_shutdown_flag = False

# ─── SSE bus ─────────────────────────────────────────────────────────────────
_subs: list[queue.Queue] = []
_subs_lock = threading.Lock()


def publish(ev: dict):
    with _subs_lock:
        dead = []
        for q in _subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                dead.append(q)
            except Exception:
                dead.append(q)
        for d in dead:
            try:
                _subs.remove(d)
            except ValueError:
                pass


def subscribe() -> queue.Queue:
    q = queue.Queue(maxsize=64)
    with _subs_lock:
        _subs.append(q)
    return q


def log(msg: str, level: str = "info"):
    publish({"type": "log", "msg": msg, "level": level})
    print(f"[{level.upper():5}] {msg}", flush=True)


# ─── HackRF polling ───────────────────────────────────────────────────────────
def _probe_hackrf() -> bool:
    try:
        # RPi4: hackrf_info môže byť pomalý, timeout znížený
        r = subprocess.run(
            ["hackrf_info"],
            capture_output=True,
            timeout=3,
            start_new_session=True  # Izolovať proces
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return False


def _hackrf_watcher():
    global _hackrf
    time.sleep(2)
    while not _shutdown_flag:
        ok = _probe_hackrf()
        if ok != _hackrf:
            _hackrf = ok
            state = "hackrf_ready" if ok else "ap_ready"
            publish({"type": "hackrf", "connected": ok})
            publish({"type": "state", "state": state, "hackrf": ok})
            log("HackRF One pripojený" if ok else "HackRF odpojený!", "ok" if ok else "warn")
        time.sleep(3)  # RPi4: častejšie polling pre rýchlejšiu detekciu


threading.Thread(target=_hackrf_watcher, daemon=True, name="hackrf-watcher").start()


# ─── TX watchdog ─────────────────────────────────────────────────────────────
def _tx_watchdog():
    global _tx_proc
    while not _shutdown_flag:
        time.sleep(0.5)  # RPi4: rýchlejšia reakcia
        with _tx_lock:
            if _tx_proc is None:
                continue
            rc = _tx_proc.poll()
            if rc is not None:
                stderr_out = ""
                try:
                    if _tx_proc.stderr:
                        stderr_out = _tx_proc.stderr.read().decode(errors="replace").strip()
                except:
                    pass
                msg = f"TX skončil (rc={rc})"
                if stderr_out:
                    msg += f" — {stderr_out[-200:]}"
                log(msg, "warn" if rc == 0 else "error")
                publish({"type": "tx", "active": False, "reason": "exit", "rc": rc})
                state = "hackrf_ready" if _hackrf else "ap_ready"
                publish({"type": "state", "state": state, "hackrf": _hackrf})
                _tx_proc = None


threading.Thread(target=_tx_watchdog, daemon=True, name="tx-watchdog").start()


def _kill_tx():
    global _tx_proc
    with _tx_lock:
        if _tx_proc and _tx_proc.poll() is None:
            try:
                _tx_proc.terminate()
                try:
                    _tx_proc.wait(timeout=3)  # RPi4: kratší timeout
                except subprocess.TimeoutExpired:
                    _tx_proc.kill()
                    _tx_proc.wait()
            except ProcessLookupError:
                pass  # Proces už neexistuje
        _tx_proc = None
    # RPi4: USB release môže trvať dlhšie
    time.sleep(2.0)


# ─── Audio knižnica ───────────────────────────────────────────────────────────
def _meta_path(wav: Path) -> Path:
    return wav.with_suffix(".json")


def _save_meta(wav: Path, original: str, dur: float, mb: float, sha: str):
    _meta_path(wav).write_text(json.dumps({
        "original": original,
        "duration_s": dur,
        "size_mb": mb,
        "sha": sha,
        "added": time.strftime("%Y-%m-%d %H:%M"),
    }), encoding="utf-8")


def _load_meta(wav: Path) -> dict:
    try:
        return json.loads(_meta_path(wav).read_text(encoding="utf-8"))
    except:
        return {}


def validate_convert(src: Path, stem: str) -> dict:
    mb = src.stat().st_size / 1_048_576
    if mb > MAX_MB:
        return {"ok": False, "error": f"Súbor príliš veľký ({mb:.0f} MB, max {MAX_MB} MB)"}

    # Zisti dĺžku
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
            capture_output=True, text=True, timeout=15,
            start_new_session=True
        )
        dur = float(probe.stdout.strip())
    except FileNotFoundError:
        return {"ok": False, "error": "ffmpeg nenájdený — sudo apt install ffmpeg"}
    except Exception as e:
        return {"ok": False, "error": f"ffprobe chyba: {e}"}

    if dur > MAX_SEC:
        return {"ok": False, "error": f"Príliš dlhé ({dur:.0f}s, max {MAX_SEC}s)"}
    if dur < 1:
        return {"ok": False, "error": "Príliš krátke (min 1s)"}

    # Bezpečné meno súboru
    safe = "".join(c if c.isalnum() or c in "-_. " else "_" for c in stem).strip()
    if not safe:
        safe = "audio"
    out = LIBRARY / (safe + ".wav")
    n = 1
    while out.exists():
        out = LIBRARY / (f"{safe}_{n}.wav")
        n += 1

    # Konverzia: 44100 Hz, MONO (dôležité!), 16-bit PCM WAV
    # Oprava: sox očakáva mono pre fm_modulator
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ar", "44100", "-ac", "1",  # MONO! fm_modulator očakáva 1 kanál
        "-f", "wav", str(out),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120, start_new_session=True)
        if r.returncode != 0:
            err = r.stderr.decode(errors="replace")[-300:] if r.stderr else "Neznáma chyba"
            return {"ok": False, "error": f"ffmpeg: {err}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Konverzia trvá príliš dlho"}

    sha = hashlib.sha256(out.read_bytes()).hexdigest()[:10]
    _save_meta(out, src.name, round(dur, 1), round(mb, 2), sha)
    return {
        "ok": True,
        "path": str(out),
        "name": out.name,
        "duration_s": round(dur, 1),
        "size_mb": round(mb, 2),
        "sha": sha
    }


# ─── Flask ────────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=str(STATIC))
app.config["MAX_CONTENT_LENGTH"] = (MAX_MB + 10) * 1_048_576


@app.route("/api/events")
def sse():
    q = subscribe()

    def gen():
        init = {
            "type": "state",
            "state": "hackrf_ready" if _hackrf else "ap_ready",
            "hackrf": _hackrf
        }
        yield f"data: {json.dumps(init)}\n\n"
        while not _shutdown_flag:
            try:
                ev = q.get(timeout=25)
                yield f"data: {json.dumps(ev)}\n\n"
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'
            except GeneratorExit:
                # Klient sa odpojil — cleanup
                with _subs_lock:
                    try:
                        _subs.remove(q)
                    except ValueError:
                        pass
                break
    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "X-Clacks-Overhead": "GNU Terry Pratchett"
        }
    )


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Žiadny súbor"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "Prázdny názov súboru"}), 400

    ext = Path(f.filename).suffix.lower()
    if ext not in (".wav", ".mp3", ".flac", ".ogg"):
        return jsonify({"ok": False, "error": "Podporované: .wav, .mp3, .flac, .ogg"}), 400

    # Bezpečný názov pre upload
    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in f.filename)
    src = UPLOAD / safe_name

    try:
        f.save(src)
        result = validate_convert(src, Path(f.filename).stem)
    finally:
        try:
            src.unlink(missing_ok=True)
        except:
            pass

    log(f"Upload: {f.filename} → {'OK' if result['ok'] else result['error']}",
        "ok" if result["ok"] else "error")
    if result["ok"]:
        publish({"type": "library_update"})
    return jsonify(result)


@app.route("/api/library")
def api_library():
    files = []
    for wav in sorted(LIBRARY.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True):
        m = _load_meta(wav)
        files.append({
            "name": wav.name,
            "path": str(wav),
            "duration_s": m.get("duration_s", 0),
            "size_mb": m.get("size_mb", 0),
            "sha": m.get("sha", ""),
            "original": m.get("original", wav.name),
            "added": m.get("added", ""),
        })
    return jsonify({"ok": True, "files": files})


@app.route("/api/library/<filename>", methods=["DELETE"])
def api_library_delete(filename):
    wav = LIBRARY / Path(filename).name
    if not wav.exists() or wav.suffix != ".wav":
        return jsonify({"ok": False, "error": "Nenájdený"}), 404
    try:
        wav.unlink()
        _meta_path(wav).unlink(missing_ok=True)
        log(f"Zmazaný: {filename}", "warn")
        publish({"type": "library_update"})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/tx/start", methods=["POST"])
def api_tx_start():
    global _tx_proc, _tx_meta
    if not _hackrf:
        return jsonify({"ok": False, "error": "HackRF nie je pripojený"}), 409
    if _tx_proc and _tx_proc.poll() is None:
        return jsonify({"ok": False, "error": "TX už beží"}), 409

    d = request.get_json() or {}
    try:
        freq = int(float(d.get("freq", 433.92)) * 1_000_000)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Neplatná frekvencia"}), 400

    mod = d.get("mod", "FM")
    gain = max(0, min(47, int(d.get("gain", 10))))  # RF gain 0-47 dB
    amp = max(0.0, min(1.0, float(d.get("amplitude", 1.0))))  # Baseband amplitude 0.0-1.0
    rate = int(d.get("sampleRate", 8_000_000))
    wav = d.get("wavPath", "")

    if not wav or not Path(wav).exists():
        return jsonify({"ok": False, "error": "WAV súbor nenájdený"}), 400

    # Overenie že HackRF nie je busy
    for attempt in range(3):
        if _probe_hackrf():
            break
        log(f"HackRF busy, čakám... ({attempt + 1}/3)", "warn")
        time.sleep(1.0)
    else:
        return jsonify({"ok": False, "error": "HackRF je obsadený, skús znova"}), 409

    mod_script = str(BASE / "fm_modulator.py")

    # Oprava: Použiť list namiesto shell=True + oprava hackrf_transfer parametrov
    # -a je antenna power (0/1/2), nie amplitude! Amplitude sa rieši v fm_modulator.py
    sox_cmd = [
        "sox", str(wav), "-t", "raw", "-r", str(rate), "-e", "float", "-b", "32", "-c", "1", "-"
    ]
    mod_cmd = [
        "python3", mod_script, "--rate", str(rate), "--amp", str(amp)
    ]
    hackrf_cmd = [
        "hackrf_transfer", "-f", str(freq), "-s", str(rate), "-x", str(gain), "-t", "-"
    ]

    try:
        # Pipeline: sox | python | hackrf_transfer
        # RPi4: použiť preexec_fn pre izoláciu procesov
        with _tx_lock:
            # Vytvorenie pipeline manuálne pre lepšiu kontrolu
            p1 = subprocess.Popen(sox_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            p2 = subprocess.Popen(mod_cmd, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            p3 = subprocess.Popen(hackrf_cmd, stdin=p2.stdout, stderr=subprocess.PIPE, start_new_session=True)

            # Zatvoriť nepotrebné pipe ends v rodičovskom procese
            p1.stdout.close()
            p2.stdout.close()

            _tx_proc = p3  # Sledujeme koncový proces
            _tx_proc._sox = p1  # Uložiť referencie pre cleanup
            _tx_proc._mod = p2
            _tx_meta = {
                "freq": freq / 1e6,
                "mod": mod,
                "gain": gain,
                "amplitude": amp,
                "pid": _tx_proc.pid
            }

        publish({"type": "tx", "active": True, **_tx_meta})
        publish({"type": "state", "state": "transmitting", **_tx_meta, "hackrf": True})
        log(f"TX START {freq / 1e6:.3f} MHz [{mod}] gain={gain}dB amp={amp}", "tx")
        return jsonify({"ok": True, **_tx_meta})

    except FileNotFoundError as e:
        missing = str(e).split("'")[1] if "'" in str(e) else "neznámy príkaz"
        log(f"Chýba príkaz: {missing}", "error")
        return jsonify({"ok": False, "error": f"Chýba '{missing}' — nainštaluj: sudo apt install {missing.split('_')[0]}"}), 500
    except Exception as e:
        log(f"TX chyba: {e}", "error")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/tx/stop", methods=["POST"])
def api_tx_stop():
    _kill_tx()
    state = "hackrf_ready" if _hackrf else "ap_ready"
    publish({"type": "tx", "active": False, "reason": "user"})
    publish({"type": "state", "state": state, "hackrf": _hackrf})
    log("TX STOP", "warn")
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    return jsonify({
        "hackrf": _hackrf,
        "transmitting": bool(_tx_proc and _tx_proc.poll() is None),
        **_tx_meta,
    })


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def spa(path):
    p = STATIC / path
    if path and p.exists() and p.is_file():
        return send_from_directory(str(STATIC), path)
    idx = STATIC / "index.html"
    if idx.exists():
        return send_from_directory(str(STATIC), "index.html")
    return "<h2>Skopíruj index.html do static/</h2>", 200


def _cleanup_processes():
    """Bezpečné ukončenie všetkých subprocessov"""
    global _tx_proc, _shutdown_flag
    _shutdown_flag = True
    log("Ukončovanie procesov...", "warn")
    _kill_tx()
    # Cleanup SSE subscribers
    with _subs_lock:
        _subs.clear()


def _shutdown(sig, frame):
    log(f"Signál {sig}, vypínanie...", "warn")
    _cleanup_processes()
    sys.exit(0)


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)
# RPi4: pridať cleanup pri SIGHUP
try:
    signal.signal(signal.SIGHUP, _shutdown)
except AttributeError:
    pass  # Windows nemá SIGHUP


if __name__ == "__main__":
    import socket

    # Zisti lokálnu IP
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
    except:
        local_ip = "127.0.0.1"

    wavs = list(LIBRARY.glob("*.wav"))

    # RPi4: ASCII banner bez Unicode problémov
    print(f"""
+============================================+
|  HackRF One — Web Controller (RPi4)        |
+============================================+
|  URL:      http://{local_ip:<25}|
|  Port:     8080                            |
|  Knižnica: {len(wavs):>3} súbor(ov) v ./library/       |
|  CPU:      {os.uname().machine:<20}|
+============================================+
""", flush=True)

    log("Server štartuje — http://" + local_ip + ":8080", "ok")

    # RPi4: threaded=True je dostatočný, nepoužívať reloader
    app.run(
        host="0.0.0.0",
        port=8080,
        threaded=True,
        debug=False,
        use_reloader=False
    )
