#!/usr/bin/env python3
"""talkback: hear Claude Code's replies (Kokoro-82M), talk back with Hex, and auto-Enter.

Hooks (stdin is the hook's JSON):
  talkback.py hook stop | prompt | session-start
Commands (from /talkback):
  talkback.py [on|off|status|full|length <n|full>|voice <name>|speed <x>|autoenter on|off|default on|off|say <text>]

Stdlib only, Python 3.8+, runs on macOS, Linux and Windows. Hooks launch it through bin/run.sh, which uses
`uv run --no-project` (so no system Python is needed) and installs uv itself if it's missing. State lives in ~/.talkback.
"""
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(PLUGIN, "engine")
HELPER_SRC = os.path.join(PLUGIN, "helper")
STATE = os.environ.get("TALKBACK_HOME") or os.path.join(os.path.expanduser("~"), ".talkback")
ENDPOINT = os.path.join(STATE, "voiced.json")
CONFIG = os.path.join(STATE, "config.json")
SESSIONS = os.path.join(STATE, "sessions")
VENV = os.path.join(STATE, "venv")
HELPER_BIN = os.path.join(STATE, "bin", "talkback-helper")
HELPER_PID = os.path.join(STATE, "helper.pid")
DAEMON_LOCK = os.path.join(STATE, "daemon.lock")
STARTING = os.path.join(STATE, "starting.json")  # written while a starter waits for the daemon (first run: installs)
INSTALLED = os.path.join(STATE, "installed")  # the daemon has come up at least once
START_FAILED = os.path.join(STATE, "start-failed")
UV_DIR = os.path.join(STATE, "uv")  # uv's own installer puts the binary here when it installs it for us
UV_LOCK = os.path.join(STATE, "uv.lock")
UV_STARTING = os.path.join(STATE, "uv-starting.json")  # written while the uv installer runs
UV_FAILED = os.path.join(STATE, "uv-failed")
UV_LOG = os.path.join(STATE, "uv-install.log")
UV_RETRY_AFTER = 3600  # after a failed uv install, hooks wait this long before trying again on their own
UV_INSTALL_TIMEOUT = 600
UV_URL_SH = "https://astral.sh/uv/install.sh"
UV_URL_PS1 = "https://astral.sh/uv/install.ps1"
UV_DOCS = "https://docs.astral.sh/uv/getting-started/installation/"
HEX_OFFERED = os.path.join(STATE, "hex-offered")  # the first-run mention of /talkback setup was shown
HEX_REPO = "kitlangton/Hex"
MAX_CHARS = 700
TRIM_NOTE = "The rest was trimmed."  # spoken when code, a table, or the length limit drops something
SESSION_TTL = 14 * 24 * 3600  # on/off switches of sessions untouched this long are pruned
VOICES = {  # Kokoro v1.0 English voices: first letter a = American, b = British
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole", "af_nova", "af_river",
    "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx",
    "am_puck", "am_santa", "bf_alice", "bf_emma", "bf_isabella", "bf_lily", "bm_daniel", "bm_fable",
    "bm_george", "bm_lewis",
}
USAGE = ("Usage: /talkback [on|off|status|full|length <n|full>|voice <name>|speed <0.5-2>|"
         "autoenter on|off|default on|off|say <text>|setup [yes|skip]]")
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"


# ---------- config and per-session switch ----------

def load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(STATE, exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)


def plugin_version():
    try:
        with open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json")) as f:
            return json.load(f).get("version", "dev")
    except (OSError, ValueError):
        return "dev"


def valid_session(session):
    return bool(re.fullmatch(r"[\w-]{1,128}", session or ""))


def session_file(session, suffix=""):
    return os.path.join(SESSIONS, session + suffix)


def prune_sessions():
    cutoff = time.time() - SESSION_TTL
    for path in glob.glob(os.path.join(SESSIONS, "*")):
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
        except OSError:
            pass


def set_session(session, value):
    os.makedirs(SESSIONS, exist_ok=True)
    with open(session_file(session), "w") as f:
        f.write(value)


def is_on(session):
    if not valid_session(session):
        return False
    try:
        with open(session_file(session)) as f:
            return f.read().strip() == "on"
    except OSError:
        return bool(load_config().get("default_on", False))


# ---------- the TTS daemon ----------

def send(req, timeout=2.0):
    """One JSON request to the daemon; raises OSError if it isn't running."""
    try:
        with open(ENDPOINT) as f:
            ep = json.load(f)
    except (OSError, ValueError):
        raise OSError("daemon not running")
    s = socket.create_connection(("127.0.0.1", ep["port"]), timeout=timeout)
    try:
        s.sendall((json.dumps(dict(req, token=ep["token"])) + "\n").encode())
        return s.makefile().readline().strip()
    finally:
        s.close()


def ping():
    """(engine, version) of the running daemon, or None."""
    try:
        parts = send({"cmd": "ping"}, timeout=1).split()
    except OSError:
        return None
    if not parts or parts[0] != "pong":
        return None
    remove(START_FAILED)  # it answered, so a start that timed out earlier is no longer the latest word
    return (parts[1] if len(parts) > 1 else "?", parts[2] if len(parts) > 2 else "?")


def engine_status():
    p = ping()
    if p:
        return f"running ({p[0]})"
    if not find_uv():
        if uv_installing():
            return "installing uv, first run (Kokoro follows; usually a few minutes in all)"
        if os.path.exists(UV_FAILED):
            return (f"needs uv: the automatic install failed (see {UV_LOG}). Install it yourself "
                    f"({UV_DOCS}), then restart Claude Code")
        return "waiting for uv (it installs by itself at the next session start or /talkback on)"
    info = starting()
    if info:
        mins = max(0, int(time.time() - info.get("since", time.time())) // 60)
        if info.get("first"):
            return (f"installing Kokoro, first run ({mins} min so far; usually a minute or two, "
                    "longer on a slow connection)")
        return "starting"
    if os.path.exists(START_FAILED):
        return f"stopped (the last start failed; see {os.path.join(STATE, 'voiced.log')})"
    return "stopped"


def daemon_alive():
    """True if a daemon from this plugin version is running; one from another version is told to quit."""
    p = ping()
    if p and p[1] != plugin_version():
        try:
            send({"cmd": "quit"}, timeout=2)
        except OSError:
            pass
        time.sleep(0.5)
        return False
    return p is not None


def find_uv():
    home = os.path.expanduser("~")
    exe = "uv.exe" if IS_WIN else "uv"
    for cand in (shutil.which("uv"), os.path.join(UV_DIR, exe), os.path.join(home, ".local", "bin", exe),
                 "/opt/homebrew/bin/uv",
                 "/usr/local/bin/uv", os.path.join(home, ".cargo", "bin", exe)):
        if cand and os.path.exists(cand):
            return cand
    return None


def uv_install_argv(win=None, which=shutil.which):
    """The argv of Astral's official standalone installer for this OS, or None if nothing can fetch it."""
    if win is None:
        win = IS_WIN
    if win:
        ps = which("powershell") or which("pwsh") or "powershell"
        return [ps, "-NoProfile", "-ExecutionPolicy", "ByPass", "-Command", f"irm {UV_URL_PS1} | iex"]
    if which("curl"):
        return ["sh", "-c", f"curl -LsSf {UV_URL_SH} | sh"]
    if which("wget"):
        return ["sh", "-c", f"wget -qO- {UV_URL_SH} | sh"]
    return None


def uv_install_env(env=None):
    """The installer drops uv into ~/.talkback/uv and leaves PATH and shell profiles alone."""
    return dict(os.environ if env is None else env, UV_UNMANAGED_INSTALL=UV_DIR, UV_NO_MODIFY_PATH="1")


def uv_installing():
    try:
        with open(UV_STARTING) as f:
            json.load(f)
    except (OSError, ValueError):
        return False
    return lock_held(UV_LOCK)  # the file is stale unless the installer still holds the lock


def ensure_uv(run=None):
    """Install uv with Astral's installer if it's missing. No sudo, no PATH changes. Returns the uv path or None."""
    run = run or subprocess.run
    uv = find_uv()
    if uv:
        return uv
    try:
        if time.time() - os.path.getmtime(UV_FAILED) < UV_RETRY_AFTER:
            return None  # failed a moment ago: don't hammer the network on every session
    except OSError:
        pass
    os.makedirs(STATE, exist_ok=True)
    with FileLock(UV_LOCK):  # one installer at a time; the rest then find uv in place
        uv = find_uv()
        if uv:
            return uv
        with open(UV_STARTING, "w") as f:
            json.dump({"since": time.time()}, f)
        try:
            argv = uv_install_argv()
            ok = False
            with open(UV_LOG, "w") as log:
                if argv is None:
                    log.write("talkback: neither curl nor wget found, so uv can't be downloaded.\n")
                else:
                    log.write("talkback: running " + " ".join(argv) + "\n")
                    log.flush()
                    try:
                        run(argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=uv_install_env(),
                            timeout=UV_INSTALL_TIMEOUT)
                    except (OSError, subprocess.SubprocessError) as e:
                        log.write(f"talkback: installer failed to run: {e}\n")
            uv = find_uv()
            ok = bool(uv)
        finally:
            remove(UV_STARTING)
        if ok:
            remove(UV_FAILED)
            return uv
        open(UV_FAILED, "w").close()
        return None


def spawn_detached(argv, log_path=None, env=None, cwd=None):
    """Start a process that outlives this one (and the hook that started it)."""
    out = open(log_path, "a") if log_path else subprocess.DEVNULL  # noqa: SIM115 - closed below, after Popen
    try:
        if not IS_WIN:
            return subprocess.Popen(argv, stdout=out, stderr=out, stdin=subprocess.DEVNULL, env=env, cwd=cwd,
                                    start_new_session=True)
        # CREATE_NO_WINDOW (not DETACHED_PROCESS) gives a hidden console that uv's python child inherits, so no
        # window pops up. Break away from Claude Code's job object so the daemon survives the session; fall back
        # if the job doesn't allow it.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            return subprocess.Popen(argv, stdout=out, stderr=out, stdin=subprocess.DEVNULL, env=env, cwd=cwd,
                                    creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB)
        except OSError:
            return subprocess.Popen(argv, stdout=out, stderr=out, stdin=subprocess.DEVNULL, env=env, cwd=cwd,
                                    creationflags=flags)
    finally:
        if log_path:
            out.close()


class FileLock:
    """Exclusive lock on a file, on POSIX and Windows."""

    def __init__(self, path):
        self.f = open(path, "a+")  # noqa: SIM115 - held for the object's lifetime; __exit__ closes it

    def __enter__(self):
        if IS_WIN:
            import msvcrt
            while True:
                try:
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:  # LK_LOCK gives up after ~10 s; keep waiting
                    continue
        else:
            import fcntl
            fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self.f.close()  # closing releases the lock on both


def lock_held(path):
    """True if another process holds the FileLock on path (probes without waiting)."""
    try:
        f = open(path, "a+")  # noqa: SIM115 - used as a context manager just below
    except OSError:
        return False
    with f:
        try:
            if IS_WIN:
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
    return False


def starting():
    """{"since": t, "first": bool} while the daemon is being started (or installed), else None."""
    try:
        with open(STARTING) as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    # The file is only meaningful while its starter still holds the lock (it may have been killed).
    return info if isinstance(info, dict) and lock_held(DAEMON_LOCK) else None


def ensure_daemon(wait=900):
    """Start the daemon if needed (the first start installs Kokoro, so it can take minutes)."""
    if daemon_alive():
        if not os.path.exists(INSTALLED):  # installed before this marker existed
            open(INSTALLED, "w").close()
        remove(START_FAILED)
        return True
    uv = find_uv()
    if not uv:
        return False
    os.makedirs(STATE, exist_ok=True)
    with FileLock(DAEMON_LOCK):  # one starter at a time; the rest then find it alive
        if daemon_alive():
            remove(START_FAILED)
            return True
        with open(STARTING, "w") as f:
            json.dump({"since": time.time(), "first": not os.path.exists(INSTALLED)}, f)
        try:
            ok = start_daemon(uv, wait)
        finally:
            remove(STARTING)
        if ok:
            open(INSTALLED, "w").close()
            remove(START_FAILED)
        else:
            open(START_FAILED, "w").close()
        return ok


def stop_spawned(proc):
    """Stop a child from spawn_detached, and the process group it leads (uv's python grandchild)."""
    if proc.poll() is not None:
        return
    if IS_WIN:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if proc.poll() is None:
            proc.kill()
        return
    # start_new_session makes proc.pid the process-group id, so this reaches grandchildren too.
    for sig in (15, 9):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            try:
                proc.kill()
            except OSError:
                return
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue


def start_daemon(uv, wait):
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=VENV, TALKBACK_HOME=STATE, PYTHONUNBUFFERED="1",
               TALKBACK_VERSION=plugin_version())
    proc = spawn_detached([uv, "run", "--project", ENGINE, "--frozen", "python", os.path.join(ENGINE, "voiced.py")],
                          log_path=os.path.join(STATE, "voiced.log"), env=env, cwd=STATE)
    deadline = time.time() + wait
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if daemon_alive():
            return True
        time.sleep(0.5)
    if daemon_alive():  # came up as the clock ran out: keep it, don't start a second one later
        return True
    stop_spawned(proc)  # otherwise the late child stays up and a retry launches another
    return False


def remove(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def argv_from_args_file(path):
    """Words for /talkback, saved by run.sh. Whitespace separates them; nothing is expanded."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    finally:
        remove(path)
    return text.split()


def say_detached(text, session):
    """Hand off to a background child so a hook returns at once, even on a cold start."""
    # Text goes through a file: Windows caps a command line at 32k characters.
    os.makedirs(STATE, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="say-", suffix=".txt", dir=STATE)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    spawn_detached([sys.executable, os.path.abspath(__file__), "_say", session or "", path])


def warm_detached():
    spawn_detached([sys.executable, os.path.abspath(__file__), "_warm"])


# ---------- the macOS helper (mic-stop + auto-Enter) ----------

def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def helper_pid():
    """PID of the running helper (macOS), or None. helper.pid holds "<pid> <binary mtime>"."""
    try:
        with open(HELPER_PID) as f:
            pid = int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    if not pid_alive(pid):
        return None
    out = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True,
                         check=False).stdout
    return pid if out.strip().endswith("talkback-helper") else None


def helper_running():
    return helper_pid() is not None


def build_helper():
    src = os.path.join(HELPER_SRC, "main.swift")
    if os.path.exists(HELPER_BIN) and os.path.getmtime(HELPER_BIN) >= os.path.getmtime(src):
        return True
    os.makedirs(os.path.dirname(HELPER_BIN), exist_ok=True)
    if shutil.which("swiftc"):
        r = subprocess.run([os.path.join(HELPER_SRC, "build.sh"), HELPER_BIN], capture_output=True, text=True,
                           check=False)
        if r.returncode == 0:
            return True
    prebuilt = os.path.join(HELPER_SRC, "talkback-helper-arm64")
    if os.uname().machine == "arm64" and os.path.exists(prebuilt):
        shutil.copy2(prebuilt, HELPER_BIN)
        os.utime(HELPER_BIN)
        return True
    return False


def ensure_helper():
    """Run the macOS helper, restarting it if the plugin update rebuilt its binary."""
    if not IS_MAC:
        return
    os.makedirs(STATE, exist_ok=True)
    with FileLock(os.path.join(STATE, "helper-start.lock")):
        if not build_helper():
            return
        built = str(int(os.path.getmtime(HELPER_BIN)))
        pid = helper_pid()
        if pid:
            with open(HELPER_PID) as f:
                fields = f.read().split()
            if fields[1:2] == [built]:
                return
            os.kill(pid, 15)  # an older binary: replace it
            time.sleep(0.3)
        proc = spawn_detached([HELPER_BIN], log_path=os.path.join(STATE, "helper.log"),
                              env=dict(os.environ, TALKBACK_HOME=STATE))
        with open(HELPER_PID, "w") as f:
            f.write(f"{proc.pid} {built}")


# ---------- turning a reply into speech ----------

def find_transcript(session):
    hits = glob.glob(os.path.join(os.path.expanduser("~"), ".claude", "projects", "*", session + ".jsonl"))
    return hits[0] if hits else ""


def last_reply(transcript_path, tail=4 << 20):
    """Text of the last assistant message that has any text in it (looks at the transcript's last 4 MB)."""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - tail))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") != "assistant":
            continue
        content = entry.get("message", {}).get("content", [])
        if isinstance(content, str):
            return content
        text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
        if text:
            return text
    return ""


def speakable(md, limit=MAX_CHARS):
    """Markdown reply -> plain sentences worth hearing; limit=0 keeps all of it.

    Fenced code (backticks or tildes), table rows, long inline code, and anything past limit are
    dropped. When that removes something, one sentence says so, so a listener can tell.
    """
    dropped = False

    def drop(text, pattern, repl=" ", flags=0):
        nonlocal dropped
        new = re.sub(pattern, repl, text, flags=flags)
        if new != text:
            dropped = True
        return new

    t = drop(md, r"```.*?```", flags=re.DOTALL)                    # backtick fences
    t = drop(t, r"~~~.*?~~~", flags=re.DOTALL)                     # tilde fences
    t = drop(t, r"^\s*\|.*\|\s*$", flags=re.MULTILINE)                # tables
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)               # links -> label
    t = re.sub(r"https?://\S+", "a link", t)
    t = re.sub(r"`([^`]{1,40})`", r"\1", t)                        # short inline code: keep
    t = drop(t, r"`[^`]*`", repl="")                               # long inline code: drop
    t = re.sub(r"~?/?(?:[\w.-]+/){2,}([\w.-]+)", r"\1", t)        # paths -> basename
    t = re.sub(r"(?:[A-Za-z]:)?\\?(?:[\w.-]+\\){2,}([\w.-]+)", r"\1", t)  # Windows paths too
    t = re.sub(r"^\s*#+\s*", "", t, flags=re.MULTILINE)           # headings
    t = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+(.*?)\s*$",                 # list items -> sentences
               lambda m: m.group(1) + ("" if m.group(1)[-1:] in ".!?:" else "."), t, flags=re.MULTILINE)
    t = re.sub(r"(?<!\w)([*_]{1,3})([^*_\n]+?)\1(?!\w)", r"\2", t)    # bold/italic, not snake_case
    t = re.sub(r"\s*(?:->|→)\s*", " to ", t)
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", t)]
    out = ""
    cut = False
    for p in filter(None, paras):
        if limit and out and len(out) + len(p) > limit:
            cut = True
            break
        out += (" " if out else "") + (p if p[-1] in ".!?:" else p + ".")
    if limit and len(out) > limit:
        cut = True
        cut_at = out[:limit]
        end = max(cut_at.rfind(". "), cut_at.rfind("? "), cut_at.rfind("! "))
        out = cut_at[: end + 1] if end > 0 else cut_at
    if dropped or cut:
        out = (out + " " + TRIM_NOTE).strip()
    return out


# ---------- Hex (macOS push-to-talk dictation), offered with consent ----------

def hex_app():
    """Path of an installed Hex.app, or None."""
    for d in ("/Applications", os.path.join(os.path.expanduser("~"), "Applications")):
        if os.path.isdir(os.path.join(d, "Hex.app")):
            return os.path.join(d, "Hex.app")
    return None


def first_run_note():
    """One-time hint shown at the first session start after install; None afterwards."""
    if os.path.exists(HEX_OFFERED):
        return None
    os.makedirs(STATE, exist_ok=True)
    open(HEX_OFFERED, "w").close()
    note = ("talkback is setting up its voice in the background (uv, then Kokoro, a few hundred MB). "
            "/talkback status shows progress; /talkback on turns it on.")
    if IS_MAC and not hex_app() and not load_config().get("hex_declined"):
        note += " To talk back with push-to-talk dictation, run /talkback setup (optional)."
    return note


PANES = ("x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
         "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")


def hex_download_url(opener=urllib.request.urlopen):
    """URL of the .dmg on Hex's latest GitHub release (Hex has no Homebrew cask)."""
    req = urllib.request.Request(f"https://api.github.com/repos/{HEX_REPO}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "talkback"})
    with opener(req, timeout=20) as r:
        data = json.load(r)
    for asset in data.get("assets", []):
        if asset.get("name", "").lower().endswith(".dmg"):
            return asset["browser_download_url"]
    raise OSError("no .dmg on the latest Hex release")


def install_hex(run=None, url_fn=hex_download_url, fetch=urllib.request.urlretrieve):
    """Download Hex's DMG and copy Hex.app to /Applications (or ~/Applications). Returns (ok, message)."""
    run = run or subprocess.run
    work = tempfile.mkdtemp(prefix="talkback-hex-")
    mount = os.path.join(work, "mnt")
    dmg = os.path.join(work, "Hex.dmg")
    attached = False
    try:
        fetch(url_fn(), dmg)
        os.makedirs(mount)
        run(["hdiutil", "attach", "-nobrowse", "-readonly", "-quiet", "-mountpoint", mount, dmg], check=True)
        attached = True
        app = os.path.join(mount, "Hex.app")
        if not os.path.isdir(app):
            return False, "the Hex download didn't contain Hex.app"
        dest = "/Applications"
        if not os.access(dest, os.W_OK):
            dest = os.path.join(os.path.expanduser("~"), "Applications")
            os.makedirs(dest, exist_ok=True)
        run(["ditto", app, os.path.join(dest, "Hex.app")], check=True)
        return True, f"Hex installed in {dest}."
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return False, f"couldn't install Hex ({e}). Get it from https://github.com/{HEX_REPO}/releases"
    finally:
        if attached:
            run(["hdiutil", "detach", "-quiet", mount], check=False)
        shutil.rmtree(work, ignore_errors=True)


def setup(args, run=None):
    run = run or subprocess.run
    if not IS_MAC:
        return ("Nothing to set up here: Hex is macOS only. On Linux and Windows talkback's voice already works; "
                "to talk back, use Claude Code's /voice.")
    cfg = load_config()
    arg = args[0] if args else ""
    if arg == "skip":
        cfg["hex_declined"] = True
        save_config(cfg)
        return "Skipped. talkback won't mention Hex again; use Claude Code's /voice to talk back."
    if arg != "yes":
        what = ("Hex is already installed. `/talkback setup yes` opens the Microphone and Accessibility panes "
                "so you can allow it." if hex_app() else
                f"Hex (https://github.com/{HEX_REPO}) is a free macOS app: hold a hotkey, speak, release, and talkback "
                "presses Enter. `/talkback setup yes` downloads its latest release (about 15 MB) into "
                "/Applications and opens the Microphone and Accessibility permission panes. "
                "`/talkback setup skip` declines (use Claude Code's /voice instead).")
        return what
    lines = []
    if hex_app():
        lines.append("Hex is already installed.")
    else:
        ok, msg = install_hex(run=run)
        lines.append(msg)
        if not ok:
            return msg
    for pane in PANES:
        run(["open", pane], check=False)
    lines.append("Opened System Settings: allow Hex under Microphone and Accessibility, and allow your terminal "
                 "(Terminal or iTerm2) under Accessibility so talkback can press Enter. Then open Hex and set "
                 "its hotkey.")
    cfg.pop("hex_declined", None)
    save_config(cfg)
    return " ".join(lines)


# ---------- hooks ----------

def hook(event):
    data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    session = data.get("session_id", "")
    if event == "stop":
        skip = session_file(session, ".skipnext")
        if valid_session(session) and os.path.exists(skip):
            os.unlink(skip)  # the reply to `/talkback full`: let the full read-out keep playing
            return
        if not is_on(session):
            return
        text = data.get("last_assistant_message") or last_reply(data.get("transcript_path", ""))
        text = speakable(text if isinstance(text, str) else "", load_config().get("max_chars", MAX_CHARS))
        if text:
            say_detached(text, session)
            info = starting()
            if info and info.get("first"):
                # Shown under the reply, so a silent first reply isn't a mystery.
                print(json.dumps({"systemMessage": "talkback is still installing Kokoro (first run). This reply "
                                  "will be spoken when it's ready; /talkback status shows progress."}))
    elif event == "prompt":
        # An interrupted turn never reaches Stop, so a .skipnext it left behind would silence the
        # next reply. This hook runs before a slash command writes a fresh marker for this turn.
        if valid_session(session):
            remove(session_file(session, ".skipnext"))
        if is_on(session):
            try:
                send({"cmd": "stop", "session": session}, timeout=0.5)
            except OSError:
                pass
    elif event == "session-start":
        prune_sessions()
        if is_on(session):
            if os.path.exists(session_file(session)):
                os.utime(session_file(session))  # keep a resumed session's switch from being pruned
            warm_detached()
        elif not os.path.exists(INSTALLED) and not os.path.exists(START_FAILED):
            warm_detached()  # first run after the plugin install: set up uv, then Kokoro, in the background
        note = first_run_note()
        if note:
            print(json.dumps({"systemMessage": note}))


# ---------- /talkback ----------

def command(args):
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    cfg = load_config()
    cmd = args[0] if args else ("off" if is_on(session) else "on")
    if cmd in ("on", "off", "full", "say") and not valid_session(session):
        print("talkback couldn't find this session's ID (CLAUDE_CODE_SESSION_ID); please update Claude Code.")
        return
    if cmd == "on":
        set_session(session, "on")
        first = not os.path.exists(INSTALLED)
        have_uv = bool(find_uv())
        if not have_uv:
            remove(UV_FAILED)  # an explicit request: try the installer again now
        warm_detached()
        print("Talkback is on for this session."
              + ((" First-time setup is installing " + ("" if have_uv else "uv, then ")
                  + "Kokoro in the background (a few hundred MB); replies start speaking in a few minutes. "
                  "/talkback status shows progress.") if first else ""))
    elif cmd == "off":
        set_session(session, "off")
        try:
            send({"cmd": "stop", "session": session})
        except OSError:
            pass
        print("Talkback is off for this session.")
    elif cmd == "full":
        # Re-read the previous reply in full, and don't let the reply to this command cut it off.
        text = speakable(last_reply(find_transcript(session)), limit=0)
        if not text:
            print("No previous reply to read.")
            return
        os.makedirs(SESSIONS, exist_ok=True)
        open(session_file(session, ".skipnext"), "w").close()
        say_detached(text, session)
        print(f"Reading the last reply in full ({len(text)} characters).")
    elif cmd == "length" and len(args) > 1:
        if args[1] in ("full", "all", "0"):
            cfg["max_chars"] = 0
        elif args[1].isdigit() and int(args[1]) >= 50:
            cfg["max_chars"] = int(args[1])
        else:
            print("Length must be 'full' or a number of characters (50 or more).")
            return
        save_config(cfg)
        print("Replies will be read in full." if not cfg["max_chars"]
              else f"Replies will be read up to {cfg['max_chars']} characters.")
    elif cmd == "voice" and len(args) > 1:
        if args[1] not in VOICES:
            print("Unknown voice. Pick one of: " + ", ".join(sorted(VOICES)) + ".")
            return
        cfg["voice"] = args[1]
        save_config(cfg)
        print(f"Voice set to {args[1]}.")
    elif cmd == "speed" and len(args) > 1:
        try:
            speed = float(args[1])
        except ValueError:
            speed = 0
        if not 0.5 <= speed <= 2:
            print("Speed must be a number from 0.5 to 2.")
            return
        cfg["speed"] = speed
        save_config(cfg)
        print(f"Speed set to {speed}.")
    elif cmd in ("autoenter", "default") and len(args) > 1 and args[1] in ("on", "off"):
        key = "autoenter" if cmd == "autoenter" else "default_on"
        cfg[key] = args[1] == "on"
        save_config(cfg)
        print({"autoenter": f"Auto-Enter after Hex dictation is {args[1]}.",
               "default_on": f"Talkback is now {args[1]} by default in new sessions."}[key])
    elif cmd == "say":
        text = speakable(" ".join(args[1:]), limit=0)
        if not text:
            print("Nothing to say. Usage: /talkback say <text>")
            return
        # The reply to this command is "Speaking."; skip it or speak() cuts off the requested line.
        os.makedirs(SESSIONS, exist_ok=True)
        open(session_file(session, ".skipnext"), "w").close()
        say_detached(text, session)
        print("Speaking.")
    elif cmd == "setup":
        print(setup(args[1:]))
    elif cmd != "status":
        print(USAGE)
    else:
        print(f"Talkback is {'on' if is_on(session) else 'off'} for this session. "
              f"voice={cfg.get('voice', 'af_heart')}, speed={cfg.get('speed', 1.1)}, "
              f"length={cfg.get('max_chars', MAX_CHARS) or 'full'}, "
              f"autoenter={'on' if cfg.get('autoenter', True) else 'off'}, "
              f"default={'on' if cfg.get('default_on') else 'off'}, "
              f"engine={engine_status()}"
              + (f", helper={'running' if helper_running() else 'stopped'}" if IS_MAC else "") + ".")


def main():
    argv = sys.argv[1:]
    # Words saved for /talkback are a user command. They go straight to command() so a
    # file containing hook, _say or _warm cannot reach those internal entry points.
    if argv[:1] == ["--args-file"] and len(argv) > 1:
        command(argv_from_args_file(argv[1]))
        return
    if argv[:1] == ["hook"]:
        hook(argv[1] if len(argv) > 1 else "")
    elif argv[:1] == ["_warm"]:
        ensure_uv()
        ensure_helper()
        ensure_daemon()
    elif argv[:1] == ["_say"]:
        with open(argv[2], encoding="utf-8") as f:
            text = f.read()
        os.unlink(argv[2])
        ensure_uv()
        ensure_helper()
        if ensure_daemon():
            send({"cmd": "say", "session": argv[1] or None, "text": text}, timeout=30)
    elif argv[:1] == ["speak"]:
        # Pipe-friendly: `some command | talkback.py speak`
        text = speakable(" ".join(argv[1:]) or sys.stdin.read(), limit=0)
        if text and ensure_uv() and ensure_daemon():
            send({"cmd": "say", "text": text}, timeout=30)
    else:
        command(argv)


if __name__ == "__main__":
    main()
