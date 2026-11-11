"""Kokoro TTS daemon: keeps the model warm and speaks text it is sent.

Listens on 127.0.0.1 (port and token in ~/.talkback/voiced.json). Requests are one JSON line with the token:
{"cmd": "say", "text": ..., "session": ...}, {"cmd": "stop", "session": ...}, {"cmd": "ping"} or {"cmd": "quit"}.
A new "say" interrupts whatever is playing; a "stop" without a session stops any speech. "ping" answers
"pong <engine> <version> <player>" so the plugin can replace a daemon left over from an older version.

Engines: MLX on Apple silicon (GPU), Kokoro ONNX on the CPU everywhere else (Linux, Windows, Intel Macs).
On Linux and Windows the daemon also stops speech when a microphone turns on (macOS: talkback-helper).
Exits after IDLE_EXIT seconds unused.
"""
import hashlib
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
# The release files never change; a download that doesn't match is dropped, not loaded.
ONNX_SHA256 = {
    "kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}


def fetch_model(name, dest):
    """Download one ONNX model file, check its SHA-256, and only then move it into place."""
    part = dest + ".part"
    try:
        urllib.request.urlretrieve(ONNX_URL + name, part)
        digest = hashlib.sha256()
        with open(part, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                digest.update(block)
        if digest.hexdigest() != ONNX_SHA256[name]:
            raise RuntimeError(f"{name} failed its SHA-256 check; the download was discarded")
        os.replace(part, dest)
    finally:
        if os.path.exists(part):
            os.unlink(part)


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
                fetch_model(name, path)
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

FAST_FAIL = 0.5  # a player that exits non-zero this soon after it starts is broken, not interrupted
PLAYER_GRACE = 5  # seconds a player may run past its segment's length before it is killed


class ProcPlayer:
    """Plays one WAV with the first working command in `candidates` (a list shared by the whole daemon run).

    A command that exits non-zero within FAST_FAIL seconds is logged once, with its stderr, and dropped for the
    rest of this run when another one is left; the same segment then plays with the next. Each wait is limited
    to the segment's length plus PLAYER_GRACE seconds.
    """
    reported = set()

    def __init__(self, candidates, path, seconds):
        self.candidates, self.path, self.seconds = candidates, path, seconds
        self.proc = self.err = None
        self.killed = False
        self.started = 0.0
        self.begin = time.time()
        self.start()

    def start(self):
        while self.candidates:
            name, argv = self.candidates[0]
            self.close_err()
            self.err = tempfile.TemporaryFile()
            try:
                self.proc = subprocess.Popen(argv + [self.path], stdout=subprocess.DEVNULL, stderr=self.err)
                self.started = time.time()
                if self.killed:  # stopped while this one was starting
                    self.proc.kill()
                return
            except OSError as e:
                self.report(name, f"could not start: {e}", drop=True)
        self.proc = None

    def report(self, name, why, drop):
        if name not in self.reported:
            self.reported.add(name)
            log(f"audio player {name}: {why}")
        if drop and len(self.candidates) > 1:
            self.candidates[:] = [c for c in self.candidates if c[0] != name]

    def stderr_text(self):
        try:
            self.err.seek(0)
            return self.err.read(2000).decode("utf-8", "replace").strip()
        except (OSError, ValueError):
            return ""

    def close_err(self):
        if self.err:
            self.err.close()
            self.err = None

    def wait(self):
        try:
            self.wait_for_player()
        finally:
            self.close_err()
            if self.killed and self.proc:  # reap it, so a stopped reply leaves no zombie
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

    def wait_for_player(self):
        while self.proc and not self.killed:
            name = self.candidates[0][0]
            try:
                rc = self.proc.wait(timeout=max(0.1, self.begin + self.seconds + PLAYER_GRACE - time.time()))
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
                self.report(name, "did not finish in time; killed", drop=False)
                return
            if rc == 0 or self.killed:
                return
            quick = time.time() - self.started < FAST_FAIL
            err = self.stderr_text()
            self.report(name, f"exited with {rc}" + (f": {err}" if err else ""), drop=quick)
            if not quick or self.candidates[0][0] == name:
                return  # a late failure, or nothing left to try, is not retried
            self.start()

    def kill(self):
        self.killed = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


class WinPlayer:
    """winsound plays one WAV asynchronously; we track its length to know when it ends.

    The next PlaySound cuts the previous sound, so a start delay would clip the end of a segment: wait a little
    longer than the WAV is long.
    """
    TAIL = 0.25

    def __init__(self, path, seconds):
        import winsound
        self.winsound = winsound
        self.end = time.time() + seconds + self.TAIL
        self.killed = False
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)

    def wait(self):
        while not self.killed and time.time() < self.end:
            time.sleep(0.05)

    def kill(self):
        self.killed = True
        self.winsound.PlaySound(None, self.winsound.SND_PURGE)


class NullPlayer:
    def __init__(self, path, seconds):
        pass

    def wait(self):
        pass

    def kill(self):
        pass


LINUX_PLAYERS = {
    "pw-play": ["pw-play"], "paplay": ["paplay"], "aplay": ["aplay", "-q"],
    "ffplay": ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
}


def player_factory():
    """(make_player, name): name() is the player in use now; it can change after a fallback."""
    choice = os.environ.get("TALKBACK_PLAYER", "")
    if choice == "none":
        return NullPlayer, lambda: "none"
    if sys.platform == "win32":
        return WinPlayer, lambda: "winsound"
    if sys.platform == "darwin":
        candidates = [("afplay", ["afplay"])]
    else:
        candidates = [(n, a) for n, a in LINUX_PLAYERS.items() if (not choice or choice == n) and shutil.which(n)]
    if not candidates:
        log("no audio player found (install pipewire, pulseaudio-utils, alsa-utils or ffmpeg); staying silent")
        return NullPlayer, lambda: "none"
    return (lambda path, seconds: ProcPlayer(candidates, path, seconds)), lambda: candidates[0][0]


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
make_player, player_name = player_factory()
lock = threading.Lock()
speak_lock = threading.Lock()  # one synthesis at a time: a new reply waits for the old one to notice it was stopped
current = {"gen": 0, "session": None, "player": None}
last_used = time.time()
token = secrets.token_hex(16)


def replace_retry(src, dst, tries=5, delay=0.05):
    """os.replace, retried on Windows, where it fails with PermissionError while a reader has dst open."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(delay)


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
    # Stopping the old reply and claiming a generation for this one is a single step: of two says that arrive
    # together only the later keeps its generation, so the earlier one never speaks.
    with lock:
        current["gen"] += 1
        gen = current["gen"]
        current["session"] = session
        player = current["player"]
    if player:
        player.kill()
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
            with lock:
                if gen != current["gen"]:  # stopped since the check above: no further segment may start
                    return
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
            conn.sendall(f"pong {engine.name} {VERSION} {player_name()}\n".encode())
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
    replace_retry(tmp, ENDPOINT)
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
