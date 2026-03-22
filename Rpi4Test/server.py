#!/usr/bin/env python3
"""
HackRF Test Server — lokálna sieť, port 8080
Spustenie: python3 test_server.py
Prístup:   http://<IP_RPi>:8080
"""

import os, sys, json, signal, subprocess, threading, time, queue, hashlib
from pathlib import Path
from flask import Flask, request, jsonify, Response, send_from_directory
from pydub import AudioSegment

# ─── Cesty ───────────────────────────────────────────────────────────────────
BASE    = Path(__file__).parent
UPLOAD  = BASE / "uploads"     # dočasné
LIBRARY = BASE / "library"     # trvalá knižnica
STATIC  = BASE / "static"

for d in (UPLOAD, LIBRARY, STATIC):
    d.mkdir(exist_ok=True)

MAX_MB  = 150
MAX_SEC = 600

# ─── Stav ────────────────────────────────────────────────────────────────────
_hackrf  = False
_tx_proc = None
_tx_lock = threading.Lock()
_tx_meta = {}

# ─── SSE bus ─────────────────────────────────────────────────────────────────
_subs: list[queue.Queue] = []
_subs_lock = threading.Lock()

def publish(ev: dict):
    with _subs_lock:
        dead = []
        for q in _subs:
            try: q.put_nowait(ev)
            except queue.Full: dead.append(q)
        for d in dead: _subs.remove(d)

def subscribe() -> queue.Queue:
    q = queue.Queue(maxsize=64)
    with _subs_lock: _subs.append(q)
    return q

def log(msg: str, level: str = "info"):
    publish({"type": "log", "msg": msg, "level": level})
    print(f"[{level.upper():5}] {msg}", flush=True)

# ─── HackRF polling ───────────────────────────────────────────────────────────
def _probe() -> bool:
    try:
        r = subprocess.run(["hackrf_info"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False

def _hackrf_watcher():
    global _hackrf
    while True:
        ok = _probe()
        if ok != _hackrf:
            _hackrf = ok
            publish({"type": "hackrf", "connected": ok})
            log("HackRF pripojený" if ok else "HackRF odpojený!", "ok" if ok else "warn")
            publish({"type": "state",
                     "state": "hackrf_ready" if ok else "ap_ready",
                     "hackrf": ok})
        time.sleep(3)

threading.Thread(target=_hackrf_watcher, daemon=True).start()

# ─── TX watchdog ─────────────────────────────────────────────────────────────
def _watchdog():
    global _tx_proc
    while True:
        time.sleep(1)
        with _tx_lock:
            if _tx_proc is None: continue
            rc = _tx_proc.poll()
            if rc is not None:
                err = ""
                try: err = _tx_proc.stderr.read().decode(errors="replace").strip()
                except: pass
                log(f"TX skončil nečakane (rc={rc}) {err}", "warn")
                publish({"type": "tx", "active": False, "reason": "exit", "rc": rc})
                publish({"type": "state",
                         "state": "hackrf_ready" if _hackrf else "ap_ready",
                         "hackrf": _hackrf})
                _tx_proc = None

threading.Thread(target=_watchdog, daemon=True).start()

def _kill_tx():
    global _tx_proc
    with _tx_lock:
        if _tx_proc and _tx_proc.poll() is None:
            _tx_proc.terminate()
            try: _tx_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _tx_proc.kill(); _tx_proc.wait()
            _tx_proc = None

# ─── Audio knižnica ───────────────────────────────────────────────────────────
def _meta_path(wav: Path) -> Path:
    return wav.with_suffix(".json")

def _save_meta(wav: Path, original: str, duration_s: float, size_mb: float, sha: str):
    meta = {"original": original, "duration_s": duration_s,
            "size_mb": size_mb, "sha": sha,
            "added": time.strftime("%Y-%m-%d %H:%M")}
    _meta_path(wav).write_text(json.dumps(meta))

def _load_meta(wav: Path) -> dict:
    try: return json.loads(_meta_path(wav).read_text())
    except: return {}

def validate_convert(src: Path, stem: str) -> dict:
    size_mb = src.stat().st_size / 1_048_576
    if size_mb > MAX_MB:
        return {"ok": False, "error": "Súbor príliš veľký"}
    try:
        audio = AudioSegment.from_file(src)
    except Exception as e:
        return {"ok": False, "error": f"Poškodený súbor: {e}"}
    dur = len(audio) / 1000
    if dur > MAX_SEC: return {"ok": False, "error": f"Príliš dlhé ({dur:.0f}s)"}
    if dur < 1:       return {"ok": False, "error": "Príliš krátke"}

    audio = audio.set_frame_rate(44100).set_channels(2).set_sample_width(2).normalize()

    safe = "".join(c if c.isalnum() or c in "-_. " else "_" for c in stem).strip()
    out = LIBRARY / (safe + ".wav")
    counter = 1
    while out.exists():
        out = LIBRARY / (f"{safe}_{counter}.wav")
        counter += 1

    audio.export(out, format="wav")
    sha = hashlib.sha256(out.read_bytes()).hexdigest()[:10]
    _save_meta(out, src.name, round(dur, 1), round(size_mb, 2), sha)
    return {"ok": True, "path": str(out), "name": out.name,
            "duration_s": round(dur, 1), "size_mb": round(size_mb, 2), "sha": sha}

# ─── Flask ────────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=str(STATIC))
app.config["MAX_CONTENT_LENGTH"] = (MAX_MB + 10) * 1_048_576

@app.route("/api/events")
def sse():
    q = subscribe()
    def gen():
        init = {"type":"state","state":"hackrf_ready" if _hackrf else "ap_ready","hackrf":_hackrf}
        yield f"data: {json.dumps(init)}\n\n"
        while True:
            try:
                ev = q.get(timeout=20)
                yield f"data: {json.dumps(ev)}\n\n"
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no","Connection":"keep-alive"})

@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Žiadny súbor"}), 400
    f = request.files["file"]
    ext = Path(f.filename).suffix.lower()
    if ext not in (".wav", ".mp3"):
        return jsonify({"ok": False, "error": "Iba .wav alebo .mp3"}), 400
    src = UPLOAD / f.filename
    f.save(src)
    result = validate_convert(src, Path(f.filename).stem)
    try: src.unlink()
    except: pass
    log(f"Upload: {f.filename} → {'OK, uložené' if result['ok'] else result['error']}",
        "ok" if result["ok"] else "error")
    if result["ok"]:
        publish({"type": "library_update"})
    return jsonify(result)

@app.route("/api/library", methods=["GET"])
def library_list():
    files = []
    for wav in sorted(LIBRARY.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = _load_meta(wav)
        files.append({
            "name":       wav.name,
            "path":       str(wav),
            "duration_s": meta.get("duration_s", 0),
            "size_mb":    meta.get("size_mb", 0),
            "sha":        meta.get("sha", ""),
            "original":   meta.get("original", wav.name),
            "added":      meta.get("added", ""),
        })
    return jsonify({"ok": True, "files": files})

@app.route("/api/library/<filename>", methods=["DELETE"])
def library_delete(filename):
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
def tx_start():
    global _tx_proc, _tx_meta
    if not _hackrf:
        return jsonify({"ok": False, "error": "HackRF nie je pripojený"}), 409
    if _tx_proc and _tx_proc.poll() is None:
        return jsonify({"ok": False, "error": "TX už beží"}), 409

    d = request.get_json() or {}
    freq  = int(float(d.get("freq", 433.92)) * 1_000_000)
    mod   = d.get("mod", "FM")
    gain  = max(0, min(61, int(d.get("gain", 14))))
    amp   = max(0, min(1,  int(d.get("amplitude", 1))))
    rate  = int(d.get("sampleRate", 8_000_000))
    wav   = d.get("wavPath", "")

    if not wav or not Path(wav).exists():
        return jsonify({"ok": False, "error": "WAV nenájdený"}), 400

    mod_script = str(BASE / "fm_modulator.py")
    cmd = (f"python3 '{mod_script}' --file '{wav}' --rate {rate} --amp {float(amp)} | "
           f"hackrf_transfer -f {freq} -s {rate} -x {gain} -t /dev/stdin 2>&1")
    try:
        with _tx_lock:
            _tx_proc = subprocess.Popen(cmd, shell=True, stderr=subprocess.PIPE)
            _tx_meta = {"freq": freq/1e6, "mod": mod, "gain": gain,
                        "amplitude": amp, "pid": _tx_proc.pid}
        publish({"type": "tx", "active": True, **_tx_meta})
        publish({"type": "state", "state": "transmitting", "hackrf": True, **_tx_meta})
        log(f"TX START {freq/1e6} MHz [{mod}] gain={gain}dB amp={amp}", "tx")
        return jsonify({"ok": True, **_tx_meta})
    except Exception as e:
        log(f"TX chyba: {e}", "error")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/tx/stop", methods=["POST"])
def tx_stop():
    _kill_tx()
    publish({"type": "tx", "active": False, "reason": "user"})
    publish({"type": "state",
             "state": "hackrf_ready" if _hackrf else "ap_ready",
             "hackrf": _hackrf})
    log("TX STOP", "warn")
    return jsonify({"ok": True})

@app.route("/api/status")
def status():
    return jsonify({"hackrf": _hackrf,
                    "transmitting": bool(_tx_proc and _tx_proc.poll() is None),
                    **_tx_meta})

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def spa(path):
    p = STATIC / path
    if path and p.exists():
        return send_from_directory(str(STATIC), path)
    idx = STATIC / "index.html"
    if idx.exists():
        return send_from_directory(str(STATIC), "index.html")
    return "<h2>Skopíruj index.html do static/</h2>", 200

def _shutdown(sig, frame):
    log("Vypínanie...", "warn")
    _kill_tx()
    sys.exit(0)

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT,  _shutdown)

if __name__ == "__main__":
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); local_ip = s.getsockname()[0]; s.close()
    except:
        local_ip = "127.0.0.1"

    wavs = list(LIBRARY.glob("*.wav"))
    print(f"""
╔═══════════════════════════════════════════╗
║   HackRF One — Test Server                ║
╠═══════════════════════════════════════════╣
║  URL:      http://{local_ip:<23}║
║  Port:     8080                           ║
║  Knižnica: {len(wavs)} súbor(ov) v ./library/      ║
╚═══════════════════════════════════════════╝
""")
    app.run(host="0.0.0.0", port=8080, threaded=True, debug=False, use_reloader=False)
