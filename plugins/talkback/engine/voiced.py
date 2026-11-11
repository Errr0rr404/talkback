"""Kokoro TTS daemon: keeps the model warm and speaks text it is sent.

Listens on 127.0.0.1 (port and token in ~/.talkback/voiced.json). Requests are one JSON line with the token:
{"cmd": "say", "text": ..., "session": ...}, {"cmd": "stop", "session": ...}, {"cmd": "ping"} or {"cmd": "quit"}.
A new "say" interrupts whatever is playing; a "stop" without a session stops any speech. "ping" answers
"pong <engine> <version>" so the plugin can replace a daemon left over from an older version.

Engines: MLX on Apple silicon (GPU), Kokoro ONNX on the CPU everywhere else (Linux, Windows, Intel Macs).
On Linux and Windows the daemon also stops speech when a microphone turns on (macOS: talkback-helper).
Exits after IDLE_EXIT seconds unused.
"""
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request

import numpy as np
import soundfile as sf

STATE = os.environ.get("TALKBACK_HOME") or os.path.join(os.path.expanduser("~"), ".talkback")
ENDPOINT = os.path.join(STATE, "voiced.json")
CONFIG = os.path.join(STATE, "config.json")
VERSION = os.environ.get("TALKBACK_VERSION", "dev")
IDLE_EXIT = 60 * 60
SAMPLE_RATE = 24000
ONNX_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


# ---------- engines ----------

class MlxEngine:
    name = "mlx"

    def __init__(self):
        from mlx_audio.tts.utils import load_model
        self.model = load_model("mlx-community/Kokoro-82M-bf16")

    def generate(self, text, voice, speed):
        # Kokoro's pipeline yields one segment per sentence group. The voice's first letter is its language
        # ("a" American, "b" British English).
        for seg in self.model.generate(text=text, voice=voice, speed=speed, lang_code=voice[:1] or "a"):
            yield np.array(seg.audio)


class OnnxEngine:
    name = "onnx"
    files = ("kokoro-v1.0.onnx", "voices-v1.0.bin")

    def __init__(self):
        from kokoro_onnx import Kokoro
        models = os.path.join(STATE, "models")
        os.makedirs(models, exist_ok=True)
        for name in self.files:
            path = os.path.join(models, name)
            if not os.path.exists(path):
                log(f"downloading {name}")
                urllib.request.urlretrieve(ONNX_URL + name, path + ".part")
                os.replace(path + ".part", path)
        self.kokoro = Kokoro(*(os.path.join(models, n) for n in self.files))

    def generate(self, text, voice, speed):
        # Speak sentence groups one at a time so playback starts before the whole reply is synthesized.
        for chunk in sentence_chunks(text):
            samples, _ = self.kokoro.create(chunk, voice=voice, speed=speed,
                                            lang="en-gb" if voice.startswith("b") else "en-us")
            yield samples


def sentence_chunks(text, size=300):
    chunk = ""
    for sentence in re.split(r"(?<=[.!?:])\s+", text.strip()):
        if chunk and len(chunk) + len(sentence) > size:
            yield chunk
            chunk = ""
        chunk = (chunk + " " + sentence).strip()
    if chunk:
        yield chunk


def load_engine():
    want = os.environ.get("TALKBACK_ENGINE", "")
    if want != "onnx" and sys.platform == "darwin" and platform.machine() == "arm64":
        try:
            return MlxEngine()
        except ImportError:
            if want == "mlx":
                raise
    return OnnxEngine()


# ---------- players ----------

class ProcPlayer:
    def __init__(self, argv):
        self.proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait(self):
        self.proc.wait()

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()


class WinPlayer:
    """winsound plays one WAV asynchronously; we track its length to know when it ends."""

    def __init__(self, path, seconds):
        import winsound
        self.winsound = winsound
        self.end = time.time() + seconds
        self.killed = False
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)

    def wait(self):
        while not self.killed and time.time() < self.end:
            time.sleep(0.05)

    def kill(self):
        self.killed = True
        self.winsound.PlaySound(None, 0)


class NullPlayer:
    def __init__(self, path, seconds):
        pass

    def wait(self):
        pass

    def kill(self):
        pass


def player_factory():
    choice = os.environ.get("TALKBACK_PLAYER", "")
    if choice == "none":
        return NullPlayer
    if sys.platform == "win32":
        return WinPlayer
    if sys.platform == "darwin":
        return lambda path, seconds: ProcPlayer(["afplay", path])
    candidates = {
        "pw-play": ["pw-play"], "paplay": ["paplay"], "aplay": ["aplay", "-q"],
        "ffplay": ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
    }
    for name, argv in candidates.items():
        if (not choice or choice == name) and shutil.which(name):
            return lambda path, seconds, argv=argv: ProcPlayer(argv + [path])
    log("no audio player found (install pipewire, pulseaudio-utils, alsa-utils or ffmpeg); staying silent")
    return NullPlayer


# ---------- microphone (Linux, Windows) ----------

def mic_active_linux():
    try:
        out = subprocess.run(["pactl", "list", "short", "source-outputs"], capture_output=True, text=True,
                             timeout=1, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(out.strip())


def mic_active_windows():
    import winreg
    root = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"

    def in_use(path):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                start = winreg.QueryValueEx(k, "LastUsedTimeStart")[0]
                stop = winreg.QueryValueEx(k, "LastUsedTimeStop")[0]
                return start > 0 and stop == 0
        except OSError:
            return False

    def subkeys(path):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                i = 0
                while True:
                    try:
                        yield path + "\\" + winreg.EnumKey(k, i)
                    except OSError:
                        return
                    i += 1
        except OSError:
            return

    return any(in_use(p) for p in list(subkeys(root)) + list(subkeys(root + r"\NonPackaged")))


def mic_probe():
    if sys.platform == "win32":
        return mic_active_windows
    if sys.platform.startswith("linux") and shutil.which("pactl"):
        return mic_active_linux
    return None  # macOS: talkback-helper watches CoreAudio


# ---------- daemon ----------

engine = load_engine()
make_player = player_factory()
lock = threading.Lock()
speak_lock = threading.Lock()  # one synthesis at a time: a new reply waits for the old one to notice it was stopped
current = {"gen": 0, "session": None, "player": None}
last_used = time.time()
token = secrets.token_hex(16)


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
        player = current["player"]
    if player:
        player.kill()


def speak(text, session):
    stop()
    with lock:
        gen = current["gen"]
        current["session"] = session
    with speak_lock:
        if gen == current["gen"]:  # not stopped or replaced while waiting for the previous reply to wind down
            speak_locked(text, gen)


def speak_locked(text, gen):
    cfg = config()
    tmpdir = tempfile.mkdtemp(prefix="talkback-")
    try:
        player = None
        for i, audio in enumerate(engine.generate(text, cfg.get("voice", "af_heart"), float(cfg.get("speed", 1.1)))):
            if gen != current["gen"]:
                return
            path = os.path.join(tmpdir, f"{i}.wav")
            sf.write(path, audio, SAMPLE_RATE)
            if player:
                player.wait()
            if gen != current["gen"]:
                return
            with lock:
                player = current["player"] = make_player(path, len(audio) / SAMPLE_RATE)
        if player:
            player.wait()
        if gen == current["gen"]:
            log(f"spoke {len(text)} chars")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        with lock:
            if gen == current["gen"]:
                current["session"] = current["player"] = None


def handle(conn):
    global last_used
    with conn:
        try:
            req = json.loads(conn.makefile().readline())
        except ValueError:
            return
        if not isinstance(req, dict) or req.get("token") != token:
            return
        last_used = time.time()
        cmd = req.get("cmd")
        if cmd == "say" and req.get("text", "").strip():
            conn.sendall(b"ok\n")
            try:
                speak(req["text"], req.get("session"))
            except Exception:  # noqa: BLE001 - a speaking failure must not kill the daemon
                log("speaking failed:\n" + traceback.format_exc())
        elif cmd == "stop":
            stop(req.get("session"))
            conn.sendall(b"ok\n")
        elif cmd == "ping":
            conn.sendall(f"pong {engine.name} {VERSION}\n".encode())
        elif cmd == "quit":
            stop()
            conn.sendall(b"ok\n")
            shutdown()


def mic_watch(probe):
    """Stop only when the mic goes from off to on during speech. An already-open mic must not cut off a reply.

    The probe runs only while something plays (on Linux it spawns pactl), and its first reading for each
    reply is the baseline, not a press.
    """
    was_on = None  # None: nothing playing, so no baseline yet
    while True:
        time.sleep(0.15)
        if current["player"] is None:
            was_on = None
            continue
        on = probe()
        if was_on is False and on:
            stop()
            log("microphone turned on: stopped speech")
        was_on = on


def shutdown():
    try:
        with open(ENDPOINT) as f:
            mine = json.load(f).get("pid") == os.getpid()
        if mine:
            os.unlink(ENDPOINT)
    except (OSError, ValueError):
        pass
    os._exit(0)


def idle_watch():
    while True:
        time.sleep(60)
        if time.time() - last_used > IDLE_EXIT and current["player"] is None:
            shutdown()


def main():
    os.makedirs(STATE, exist_ok=True)
    # Warm the pipeline so the first real reply is fast.
    for _ in engine.generate("Ready.", "af_heart", 1.0):
        pass
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    tmp = ENDPOINT + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"port": srv.getsockname()[1], "token": token, "pid": os.getpid()}, f)
    if os.name == "posix":
        os.chmod(tmp, 0o600)
    os.replace(tmp, ENDPOINT)
    log(f"ready: engine={engine.name} version={VERSION} port={srv.getsockname()[1]}")
    probe = mic_probe()
    if probe:
        threading.Thread(target=mic_watch, args=(probe,), daemon=True).start()
    threading.Thread(target=idle_watch, daemon=True).start()
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
