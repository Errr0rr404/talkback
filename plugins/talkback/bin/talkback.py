#!/usr/bin/env python3
"""talkback: hear Claude Code's replies (Kokoro-82M), talk back with Hex, and auto-Enter.

Hooks (stdin is the hook's JSON):
  talkback.py hook stop | prompt | session-start | session-end
Commands (from /talkback):
  talkback.py [on|off|status|full|length <n|full>|voice <name>|speed <x>|autoenter on|off|default on|off|say <text>]

Stdlib only, Python 3.9+, so hooks start fast on the system python3. State lives in ~/.talkback.
"""
import fcntl
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(PLUGIN, "engine")
HELPER_SRC = os.path.join(PLUGIN, "helper")
STATE = os.environ.get("TALKBACK_HOME") or os.path.expanduser("~/.talkback")
SOCK = os.path.join(STATE, "voiced.sock")
CONFIG = os.path.join(STATE, "config.json")
SESSIONS = os.path.join(STATE, "sessions")
VENV = os.path.join(STATE, "venv")
HELPER_BIN = os.path.join(STATE, "bin", "talkback-helper")
HELPER_PID = os.path.join(STATE, "helper.pid")
MAX_CHARS = 700
IS_MAC = sys.platform == "darwin"


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


def session_file(session, suffix=""):
    return os.path.join(SESSIONS, session + suffix)


def set_session(session, value):
    os.makedirs(SESSIONS, exist_ok=True)
    with open(session_file(session), "w") as f:
        f.write(value)


def is_on(session):
    if not session:
        return False
    try:
        with open(session_file(session)) as f:
            return f.read().strip() == "on"
    except OSError:
        return bool(load_config().get("default_on", False))


# ---------- the TTS daemon ----------

def send(req, timeout=2.0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCK)
        s.sendall((json.dumps(req) + "\n").encode())
        return s.makefile().readline().strip()
    finally:
        s.close()


def daemon_alive():
    try:
        return send({"cmd": "ping"}, timeout=1) == "pong"
    except OSError:
        return False


def find_uv():
    for cand in (shutil.which("uv"), os.path.expanduser("~/.local/bin/uv"), "/opt/homebrew/bin/uv",
                 "/usr/local/bin/uv", os.path.expanduser("~/.cargo/bin/uv")):
        if cand and os.path.exists(cand):
            return cand
    return None


def ensure_daemon(wait=900):
    """Start the daemon if needed (the first start installs Kokoro, so it can take minutes)."""
    if daemon_alive():
        return True
    uv = find_uv()
    if not uv:
        return False
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, "daemon.lock"), "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)  # one starter at a time; others wait here and then find it alive
        if daemon_alive():
            return True
        env = dict(os.environ, UV_PROJECT_ENVIRONMENT=VENV, TALKBACK_HOME=STATE)
        with open(os.path.join(STATE, "voiced.log"), "a") as log:
            proc = subprocess.Popen([uv, "run", "--project", ENGINE, "--frozen", "python",
                                     os.path.join(ENGINE, "voiced.py")],
                                    stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=env,
                                    cwd=STATE, start_new_session=True)
        deadline = time.time() + wait
        while time.time() < deadline:
            if proc.poll() is not None:
                return False
            if daemon_alive():
                return True
            time.sleep(0.5)
    return False


def say_detached(text, session):
    """Hand off to a background child so a hook returns at once, even on a cold start."""
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "_say", session or "", text],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def warm_detached():
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "_warm"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


# ---------- the macOS helper (mic-stop + auto-Enter) ----------

def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def helper_running():
    try:
        with open(HELPER_PID) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    if not pid_alive(pid):
        return False
    out = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True).stdout
    return out.strip().endswith("talkback-helper")


def build_helper():
    src = os.path.join(HELPER_SRC, "main.swift")
    if os.path.exists(HELPER_BIN) and os.path.getmtime(HELPER_BIN) >= os.path.getmtime(src):
        return True
    os.makedirs(os.path.dirname(HELPER_BIN), exist_ok=True)
    if shutil.which("swiftc"):
        r = subprocess.run([os.path.join(HELPER_SRC, "build.sh"), HELPER_BIN], capture_output=True, text=True)
        if r.returncode == 0:
            return True
    prebuilt = os.path.join(HELPER_SRC, "talkback-helper-arm64")
    if os.uname().machine == "arm64" and os.path.exists(prebuilt):
        shutil.copy2(prebuilt, HELPER_BIN)
        os.utime(HELPER_BIN)
        return True
    return False


def ensure_helper():
    if not IS_MAC or helper_running() or not build_helper():
        return
    with open(os.path.join(STATE, "helper.log"), "a") as log:
        proc = subprocess.Popen([HELPER_BIN], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                env=dict(os.environ, TALKBACK_HOME=STATE), start_new_session=True)
    with open(HELPER_PID, "w") as f:
        f.write(str(proc.pid))


# ---------- turning a reply into speech ----------

def find_transcript(session):
    hits = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session}.jsonl"))
    return hits[0] if hits else ""


def last_reply(transcript_path):
    """Text of the last assistant message that has any text in it."""
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
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
    """Markdown reply -> plain sentences worth hearing; limit=0 keeps all of it."""
    t = re.sub(r"```.*?```", " ", md, flags=re.S)                  # code blocks
    t = re.sub(r"^\s*\|.*\|\s*$", " ", t, flags=re.M)              # tables
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)               # links -> label
    t = re.sub(r"https?://\S+", "a link", t)
    t = re.sub(r"`([^`]{1,40})`", r"\1", t)                        # short inline code: keep
    t = re.sub(r"`[^`]*`", "", t)                                  # long inline code: drop
    t = re.sub(r"~?/?(?:[\w.-]+/){2,}([\w.-]+)", r"\1", t)        # paths -> basename
    t = re.sub(r"^\s*#+\s*", "", t, flags=re.M)                    # headings
    t = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+(.*?)\s*$",                 # list items -> sentences
               lambda m: m.group(1) + ("" if m.group(1)[-1:] in ".!?:" else "."), t, flags=re.M)
    t = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", t)            # bold/italic
    t = re.sub(r"\s*(?:->|→)\s*", " to ", t)
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", t)]
    out = ""
    for p in filter(None, paras):
        if limit and out and len(out) + len(p) > limit:
            break
        out += (" " if out else "") + (p if p[-1] in ".!?:" else p + ".")
    if limit and len(out) > limit:
        cut = out[:limit]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        out = cut[: end + 1] if end > 0 else cut
    return out


# ---------- hooks ----------

def hook(event):
    data = json.load(sys.stdin)
    session = data.get("session_id", "")
    if event == "stop":
        if not is_on(session):
            return
        skip = session_file(session, ".skipnext")
        if os.path.exists(skip):
            os.unlink(skip)
            return
        text = data.get("last_assistant_message") or last_reply(data.get("transcript_path", ""))
        text = speakable(text if isinstance(text, str) else "", load_config().get("max_chars", MAX_CHARS))
        if text:
            say_detached(text, session)
    elif event == "prompt":
        if is_on(session):
            try:
                send({"cmd": "stop", "session": session}, timeout=0.5)
            except OSError:
                pass
    elif event == "session-start":
        if is_on(session):
            warm_detached()
    elif event == "session-end":
        for path in (session_file(session), session_file(session, ".skipnext")):
            if os.path.exists(path):
                os.unlink(path)


# ---------- /talkback ----------

def command(args):
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    cfg = load_config()
    cmd = args[0] if args else ("off" if is_on(session) else "on")
    if cmd == "on":
        if not find_uv():
            print("talkback needs uv to install Kokoro: brew install uv  (or: curl -LsSf https://astral.sh/uv/install.sh | sh)")
            return
        set_session(session, "on")
        first = not os.path.isdir(VENV)
        warm_detached()
        print("Talkback is on for this session."
              + (" First-time setup is installing Kokoro (about 600 MB) in the background;"
                 " replies start speaking in a minute or two." if first else ""))
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
        cfg["max_chars"] = 0 if args[1] in ("full", "all", "0") else int(args[1])
        save_config(cfg)
        print("Replies will be read in full." if not cfg["max_chars"]
              else f"Replies will be read up to {cfg['max_chars']} characters.")
    elif cmd in ("voice", "speed") and len(args) > 1:
        cfg[cmd] = args[1] if cmd == "voice" else float(args[1])
        save_config(cfg)
        print(f"{cmd.capitalize()} set to {args[1]}.")
    elif cmd in ("autoenter", "default") and len(args) > 1 and args[1] in ("on", "off"):
        key = "autoenter" if cmd == "autoenter" else "default_on"
        cfg[key] = args[1] == "on"
        save_config(cfg)
        print({"autoenter": f"Auto-Enter after Hex dictation is {args[1]}.",
               "default_on": f"Talkback is now {args[1]} by default in new sessions."}[key])
    elif cmd == "say":
        say_detached(speakable(" ".join(args[1:]), limit=0), session)
        print("Speaking.")
    else:
        print(f"Talkback is {'on' if is_on(session) else 'off'} for this session. "
              f"voice={cfg.get('voice', 'af_heart')}, speed={cfg.get('speed', 1.1)}, "
              f"length={cfg.get('max_chars', MAX_CHARS) or 'full'}, "
              f"autoenter={'on' if cfg.get('autoenter', True) else 'off'}, "
              f"default={'on' if cfg.get('default_on') else 'off'}, "
              f"engine={'running' if daemon_alive() else 'stopped'}, "
              f"helper={'running' if helper_running() else 'stopped'}.")


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["hook"]:
        hook(argv[1] if len(argv) > 1 else "")
    elif argv[:1] == ["_warm"]:
        ensure_helper()
        ensure_daemon()
    elif argv[:1] == ["_say"]:
        ensure_helper()
        if ensure_daemon():
            send({"cmd": "say", "session": argv[1] or None, "text": argv[2]}, timeout=30)
    elif argv[:1] == ["speak"]:
        # Pipe-friendly: `some command | talkback.py speak`
        text = speakable(" ".join(argv[1:]) or sys.stdin.read(), limit=0)
        if text and ensure_daemon():
            send({"cmd": "say", "text": text}, timeout=30)
    else:
        command(argv)


if __name__ == "__main__":
    main()
