"""Kokoro TTS daemon: keeps the model warm and speaks text sent over a unix socket.

Requests are one JSON line: {"cmd": "say", "text": ..., "session": ...}, {"cmd": "stop", "session": ...}
or {"cmd": "ping"}. A new "say" interrupts whatever is playing; a "stop" without a session stops any speech.
Exits after IDLE_EXIT seconds unused.
"""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time

import numpy as np
import soundfile as sf
from mlx_audio.tts.utils import load_model

STATE = os.environ.get("TALKBACK_HOME") or os.path.expanduser("~/.talkback")
SOCK = os.path.join(STATE, "voiced.sock")
CONFIG = os.path.join(STATE, "config.json")
MODEL = "mlx-community/Kokoro-82M-bf16"
IDLE_EXIT = 60 * 60

model = load_model(MODEL)
lock = threading.Lock()
current = {"gen": 0, "session": None, "proc": None}
last_used = time.time()


def config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def stop(session=None):
    with lock:
        if session and current["session"] not in (None, session):
            return
        current["gen"] += 1
        proc = current["proc"]
    if proc and proc.poll() is None:
        proc.kill()


def speak(text, session):
    stop()
    with lock:
        gen = current["gen"]
        current["session"] = session
    cfg = config()
    tmpdir = tempfile.mkdtemp(prefix="talkback-")
    try:
        # Kokoro yields one segment per sentence group: play each while the next one generates.
        player = None
        for i, seg in enumerate(model.generate(text=text, voice=cfg.get("voice", "af_heart"),
                                               speed=float(cfg.get("speed", 1.1)), lang_code="a")):
            if gen != current["gen"]:
                return
            path = os.path.join(tmpdir, f"{i}.wav")
            sf.write(path, np.array(seg.audio), 24000)
            if player:
                player.wait()
            if gen != current["gen"]:
                return
            with lock:
                player = current["proc"] = subprocess.Popen(["afplay", path])
        if player:
            player.wait()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        with lock:
            if gen == current["gen"]:
                current["session"] = current["proc"] = None


def handle(conn):
    global last_used
    with conn:
        try:
            req = json.loads(conn.makefile().readline())
        except ValueError:
            return
        last_used = time.time()
        cmd = req.get("cmd")
        if cmd == "say" and req.get("text", "").strip():
            conn.sendall(b"ok\n")
            speak(req["text"], req.get("session"))
        elif cmd == "stop":
            stop(req.get("session"))
            conn.sendall(b"ok\n")
        elif cmd == "ping":
            conn.sendall(b"pong\n")


def idle_watch():
    while True:
        time.sleep(60)
        if time.time() - last_used > IDLE_EXIT and current["proc"] is None:
            os.unlink(SOCK)
            os._exit(0)


def main():
    os.makedirs(STATE, exist_ok=True)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen(8)
    # Warm the pipeline (spaCy/G2P load) so the first real reply is fast.
    for _ in model.generate(text="Ready.", voice="af_heart", lang_code="a"):
        pass
    threading.Thread(target=idle_watch, daemon=True).start()
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
