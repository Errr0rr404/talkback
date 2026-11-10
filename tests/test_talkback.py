"""Tests for talkback.py's text cleanup, commands and hooks (no TTS engine needed).

Run: uv run --no-project python -m unittest discover -s tests
"""
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugins", "talkback", "bin"))


class TalkbackTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        os.environ["TALKBACK_HOME"] = self.home.name
        os.environ["CLAUDE_CODE_SESSION_ID"] = "test-session"
        import talkback
        self.tb = importlib.reload(talkback)  # re-read TALKBACK_HOME
        self.said = []
        self.tb.say_detached = lambda text, session: self.said.append((text, session))
        self.tb.warm_detached = lambda: None

    def tearDown(self):
        self.home.cleanup()

    def run_command(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            self.tb.command(list(args))
        return out.getvalue().strip()

    def run_hook(self, event, payload):
        stdin = mock.Mock()
        stdin.buffer.read.return_value = json.dumps(payload).encode()
        with mock.patch.object(sys, "stdin", stdin):
            self.tb.hook(event)


class SpeakableTest(TalkbackTest):
    def test_strips_markdown_and_code(self):
        md = "## Done\n\nI set up **the hook** -> see [docs](https://x.y/z).\n\n```sh\necho hi\n```\n\n- First\n- Second"
        self.assertEqual(self.tb.speakable(md), "Done. I set up the hook to see docs. First. Second.")

    def test_paths_become_file_names(self):
        self.assertEqual(self.tb.speakable("Edit ~/.claude/voice/hook.py now"), "Edit hook.py now.")
        self.assertEqual(self.tb.speakable(r"Edit C:\Users\me\proj\main.py now"), "Edit main.py now.")

    def test_keeps_snake_case(self):
        self.assertEqual(self.tb.speakable("Set max_chars_value here"), "Set max_chars_value here.")
        self.assertEqual(self.tb.speakable("This is _important_ stuff"), "This is important stuff.")

    def test_limit_cuts_at_paragraph_or_sentence(self):
        md = "\n\n".join([f"Para number {i} is here." for i in range(100)])
        short = self.tb.speakable(md, limit=100)
        self.assertLessEqual(len(short), 100)
        self.assertTrue(short.endswith("."))
        self.assertEqual(len(self.tb.speakable(md, limit=0)), len(md.replace("\n\n", " ")))


class CommandTest(TalkbackTest):
    def test_on_off(self):
        with mock.patch.object(self.tb, "find_uv", return_value="/usr/bin/uv"):
            self.assertIn("on for this session", self.run_command("on"))
        self.assertTrue(self.tb.is_on("test-session"))
        with mock.patch.object(self.tb, "send", side_effect=OSError):
            self.assertIn("off", self.run_command("off"))
        self.assertFalse(self.tb.is_on("test-session"))

    def test_rejects_bad_values(self):
        self.assertIn("Unknown voice", self.run_command("voice", "nope"))
        self.assertIn("0.5 to 2", self.run_command("speed", "fast"))
        self.assertIn("0.5 to 2", self.run_command("speed", "9"))
        self.assertIn("50 or more", self.run_command("length", "abc"))
        self.assertIn("Usage", self.run_command("bogus"))
        self.assertEqual(self.tb.load_config(), {})

    def test_accepts_good_values(self):
        self.run_command("voice", "bf_emma")
        self.run_command("speed", "1.3")
        self.run_command("length", "full")
        self.assertEqual(self.tb.load_config(), {"voice": "bf_emma", "speed": 1.3, "max_chars": 0})

    def test_needs_a_session_id(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "../../etc"
        self.assertIn("couldn't find this session", self.run_command("on"))

    def test_toggle_without_arguments(self):
        with mock.patch.object(self.tb, "find_uv", return_value="/usr/bin/uv"):
            out = self.run_command()
            self.assertIn("on for this session", out)
            self.assertTrue(self.tb.is_on("test-session"))
            with mock.patch.object(self.tb, "send", side_effect=OSError):
                out = self.run_command()
                self.assertIn("off for this session", out)
                self.assertFalse(self.tb.is_on("test-session"))

    def test_autoenter_and_default(self):
        self.assertIn("Auto-Enter after Hex dictation is off.", self.run_command("autoenter", "off"))
        self.assertEqual(self.tb.load_config().get("autoenter"), False)
        self.assertIn("Auto-Enter after Hex dictation is on.", self.run_command("autoenter", "on"))
        self.assertEqual(self.tb.load_config().get("autoenter"), True)
        self.assertIn("Talkback is now on by default in new sessions.", self.run_command("default", "on"))
        self.assertEqual(self.tb.load_config().get("default_on"), True)
        self.assertIn("Talkback is now off by default in new sessions.", self.run_command("default", "off"))
        self.assertEqual(self.tb.load_config().get("default_on"), False)

    def test_say_command(self):
        self.assertIn("Nothing to say.", self.run_command("say"))
        out = self.run_command("say", "Hello", "world!")
        self.assertIn("Speaking.", out)
        self.assertEqual(self.said, [("Hello world!", "test-session")])

    def test_full_command_without_transcript(self):
        with mock.patch.object(self.tb, "find_transcript", return_value=""):
            out = self.run_command("full")
            self.assertIn("No previous reply to read.", out)

    def test_full_command_with_transcript(self):
        path = os.path.join(self.home.name, "t.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "This is a full response from assistant."}]}}) + "\n")
        with mock.patch.object(self.tb, "find_transcript", return_value=path):
            out = self.run_command("full")
            self.assertIn("Reading the last reply in full", out)
            self.assertEqual(self.said, [("This is a full response from assistant.", "test-session")])
            self.assertTrue(os.path.exists(self.tb.session_file("test-session", ".skipnext")))

    def test_status_command(self):
        with mock.patch.object(self.tb, "find_uv", return_value="/usr/bin/uv"):
            out = self.run_command("status")
            self.assertIn("Talkback is off for this session.", out)
            self.assertIn("voice=af_heart", out)
            self.assertIn("speed=1.1", out)
            self.assertIn("length=700", out)
            self.assertIn("autoenter=on", out)
            self.assertIn("default=off", out)
            self.assertIn("engine=", out)

    def test_say_does_not_write_skipnext_without_a_valid_session(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "../../etc"
        leaked = os.path.normpath(self.tb.session_file("../../etc", ".skipnext"))
        try:
            out = self.run_command("say", "hello")
            self.assertIn("couldn't find this session", out)
            self.assertEqual(self.said, [])
            self.assertFalse(os.path.exists(leaked))
        finally:
            if os.path.exists(leaked):
                os.remove(leaked)

    def test_say_sets_skipnext_so_the_acknowledgement_is_not_spoken(self):
        out = self.run_command("say", "hello", "there")
        self.assertIn("Speaking.", out)
        self.assertEqual(self.said, [("hello there.", "test-session")])
        skip = self.tb.session_file("test-session", ".skipnext")
        self.assertTrue(os.path.exists(skip))
        self.tb.set_session("test-session", "on")
        self.run_hook("stop", {"session_id": "test-session", "last_assistant_message": "Speaking."})
        self.assertEqual(self.said, [("hello there.", "test-session")])
        self.assertFalse(os.path.exists(skip))

    def test_args_file_is_spoken_literally(self):
        path = os.path.join(self.home.name, "args.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("say it's ready; echo $(HOME)\n")
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["talkback.py", "--args-file", path]), redirect_stdout(out):
            self.tb.main()
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.said, [("it's ready; echo $(HOME).", "test-session")])
        self.assertIn("Speaking.", out.getvalue())
        self.assertTrue(os.path.exists(self.tb.session_file("test-session", ".skipnext")))

    def test_args_file_words_cannot_reach_internal_subcommands(self):
        sentinel = os.path.join(self.home.name, "do-not-read.txt")
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write("secret")
        calls = []
        self.tb.hook = lambda event: calls.append(("hook", event))
        self.tb.ensure_uv = lambda: calls.append("ensure_uv")
        self.tb.ensure_helper = lambda: calls.append("ensure_helper")
        self.tb.ensure_daemon = lambda: calls.append("ensure_daemon") or False
        self.tb.send = lambda *a, **k: calls.append("send")
        for line in (
            "_say test-session " + sentinel,
            "hook session-start",
            "_warm",
        ):
            path = os.path.join(self.home.name, "args.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(line + "\n")
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["talkback.py", "--args-file", path]), redirect_stdout(out):
                self.tb.main()
            self.assertFalse(os.path.exists(path), line)
            self.assertIn("Usage", out.getvalue(), line)
            self.assertEqual(self.said, [], line)
        self.assertEqual(calls, [])
        with open(sentinel, encoding="utf-8") as f:
            self.assertEqual(f.read(), "secret")


class HookTest(TalkbackTest):
    def test_stop_speaks_only_when_on(self):
        self.run_hook("stop", {"session_id": "test-session", "last_assistant_message": "Hello there."})
        self.assertEqual(self.said, [])
        self.tb.set_session("test-session", "on")
        self.run_hook("stop", {"session_id": "test-session", "last_assistant_message": "Hello there."})
        self.assertEqual(self.said, [("Hello there.", "test-session")])

    def test_default_on(self):
        self.tb.save_config({"default_on": True})
        self.run_hook("stop", {"session_id": "other", "last_assistant_message": "Hi."})
        self.assertEqual(len(self.said), 1)

    def test_skipnext_survives_off_and_is_used_once(self):
        os.makedirs(self.tb.SESSIONS)
        open(self.tb.session_file("test-session", ".skipnext"), "w").close()
        self.run_hook("stop", {"session_id": "test-session", "last_assistant_message": "A."})  # off: consumed
        self.tb.set_session("test-session", "on")
        self.run_hook("stop", {"session_id": "test-session", "last_assistant_message": "B."})
        self.assertEqual(self.said, [("B.", "test-session")])

    def test_falls_back_to_transcript(self):
        path = os.path.join(self.home.name, "t.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Old."}]}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "From file."}]}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "x"}]}}) + "\n")
        self.tb.set_session("test-session", "on")
        self.run_hook("stop", {"session_id": "test-session", "transcript_path": path})
        self.assertEqual(self.said, [("From file.", "test-session")])

    def test_prompt_hook_stops_speech(self):
        self.tb.set_session("test-session", "on")
        sent = []
        with mock.patch.object(self.tb, "send", lambda req, timeout=0.5: sent.append(req)):
            self.run_hook("prompt", {"session_id": "test-session"})
        self.assertEqual(sent, [{"cmd": "stop", "session": "test-session"}])

    def test_prompt_hook_ignores_daemon_down(self):
        self.tb.set_session("test-session", "on")
        with mock.patch.object(self.tb, "send", side_effect=OSError("not running")):
            self.run_hook("prompt", {"session_id": "test-session"})

    def test_prompt_clears_stale_skipnext(self):
        os.makedirs(self.tb.SESSIONS)
        skip = self.tb.session_file("test-session", ".skipnext")
        open(skip, "w").close()
        self.tb.set_session("test-session", "on")
        with mock.patch.object(self.tb, "send", side_effect=OSError):
            self.run_hook("prompt", {"session_id": "test-session", "prompt": "go on"})
        self.assertFalse(os.path.exists(skip))
        self.run_hook("stop", {"session_id": "test-session", "last_assistant_message": "Next reply."})
        self.assertEqual(self.said, [("Next reply.", "test-session")])


class InstallStatusTest(TalkbackTest):
    def setUp(self):
        super().setUp()
        # engine_status() reports "waiting for uv" before Kokoro when find_uv() is empty.
        patcher = mock.patch.object(self.tb, "find_uv", return_value="/usr/bin/uv")
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_starting(self, first):
        with open(self.tb.STARTING, "w") as f:
            json.dump({"since": 0, "first": first}, f)

    def test_status_while_installing(self):
        self.write_starting(first=True)
        with mock.patch.object(self.tb, "ping", return_value=None):
            with self.tb.FileLock(self.tb.DAEMON_LOCK):
                self.assertIn("installing Kokoro", self.tb.engine_status())
            self.assertEqual(self.tb.engine_status(), "stopped")  # starter gone: the file is stale

    def test_status_while_starting_not_first(self):
        self.write_starting(first=False)
        with mock.patch.object(self.tb, "ping", return_value=None), self.tb.FileLock(self.tb.DAEMON_LOCK):
            self.assertEqual(self.tb.engine_status(), "starting")

    def test_status_when_daemon_running(self):
        with mock.patch.object(self.tb, "ping", return_value=("127.0.0.1:51123", "0.4.0")):
            self.assertEqual(self.tb.engine_status(), "running (127.0.0.1:51123)")

    def test_status_after_failed_start(self):
        open(self.tb.START_FAILED, "w").close()
        with mock.patch.object(self.tb, "ping", return_value=None):
            self.assertIn("last start failed", self.tb.engine_status())

    def test_stop_hook_says_it_is_still_installing(self):
        self.tb.set_session("test-session", "on")
        self.write_starting(first=True)
        out = io.StringIO()
        with self.tb.FileLock(self.tb.DAEMON_LOCK), redirect_stdout(out):
            self.run_hook("stop", {"session_id": "test-session", "last_assistant_message": "Hi."})
        self.assertEqual(self.said, [("Hi.", "test-session")])
        self.assertIn("still installing", json.loads(out.getvalue())["systemMessage"])


class UvBootstrapTest(TalkbackTest):
    def setUp(self):
        super().setUp()
        # Hide any real uv: only a binary in ~/.talkback/uv (or one a test creates) counts.
        self.fake_home = os.path.join(self.home.name, "fakehome")
        os.makedirs(self.fake_home)
        patcher = mock.patch.dict(os.environ, {"HOME": self.fake_home, "USERPROFILE": self.fake_home})
        patcher.start()
        self.addCleanup(patcher.stop)
        real_which = shutil.which
        w = mock.patch.object(shutil, "which", lambda n, *a, **k: None if n == "uv" else real_which(n, *a, **k))
        w.start()
        self.addCleanup(w.stop)
        e = mock.patch.object(os.path, "exists", side_effect=self._fake_exists)
        e.start()
        self.addCleanup(e.stop)

    def _fake_exists(self, path):
        if path in ("/opt/homebrew/bin/uv", "/usr/local/bin/uv"):
            return False
        return os.path.lexists(path)

    def fake_installer(self, create=True):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            if create:
                os.makedirs(self.tb.UV_DIR, exist_ok=True)
                open(os.path.join(self.tb.UV_DIR, "uv"), "w").close()
        return run, calls

    def test_installer_argv_per_platform(self):
        have = lambda *names: (lambda n: "/bin/" + n if n in names else None)
        mac = self.tb.uv_install_argv(win=False, which=have("curl"))
        self.assertEqual(mac[:2], ["sh", "-c"])
        self.assertIn("curl -LsSf https://astral.sh/uv/install.sh | sh", mac[2])
        linux = self.tb.uv_install_argv(win=False, which=have("wget"))
        self.assertIn("wget -qO- https://astral.sh/uv/install.sh | sh", linux[2])
        self.assertIsNone(self.tb.uv_install_argv(win=False, which=have()))
        win = self.tb.uv_install_argv(win=True, which=have("powershell"))
        self.assertEqual(win[0], "/bin/powershell")
        self.assertIn("ByPass", win)
        self.assertIn("irm https://astral.sh/uv/install.ps1 | iex", win[-1])
        for argv in (mac, linux, win):
            self.assertNotIn("sudo", " ".join(argv))
            self.assertNotIn("brew", " ".join(argv))

    def test_installer_env_keeps_path_alone(self):
        env = self.tb.uv_install_env({"A": "b"})
        self.assertEqual(env["UV_NO_MODIFY_PATH"], "1")
        self.assertEqual(env["UV_UNMANAGED_INSTALL"], self.tb.UV_DIR)
        self.assertEqual(env["A"], "b")

    def test_installs_when_missing_then_finds_it_by_absolute_path(self):
        self.assertIsNone(self.tb.find_uv())
        run, calls = self.fake_installer()
        uv = self.tb.ensure_uv(run=run)
        self.assertEqual(uv, os.path.join(self.tb.UV_DIR, "uv"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["env"]["UV_UNMANAGED_INSTALL"], self.tb.UV_DIR)
        self.assertFalse(os.path.exists(self.tb.UV_STARTING))
        self.assertFalse(os.path.exists(self.tb.UV_FAILED))
        self.tb.ensure_uv(run=run)
        self.assertEqual(len(calls), 1)  # already there: installer not run again

    def test_failed_install_fails_soft_and_status_explains(self):
        run, calls = self.fake_installer(create=False)
        self.assertIsNone(self.tb.ensure_uv(run=run))
        self.assertTrue(os.path.exists(self.tb.UV_FAILED))
        with mock.patch.object(self.tb, "ping", return_value=None):
            status = self.tb.engine_status()
        self.assertIn("needs uv", status)
        self.assertIn("docs.astral.sh", status)
        self.assertIsNone(self.tb.ensure_uv(run=run))  # retry throttled
        self.assertEqual(len(calls), 1)
        os.utime(self.tb.UV_FAILED, (0, 0))
        self.tb.ensure_uv(run=run)  # old failure: tried again
        self.assertEqual(len(calls), 2)

    def test_installer_that_raises_is_a_soft_failure(self):
        def run(argv, **kw):
            raise OSError("no sh")
        self.assertIsNone(self.tb.ensure_uv(run=run))
        with open(self.tb.UV_LOG) as f:
            self.assertIn("no sh", f.read())

    def test_status_while_uv_installs(self):
        with open(self.tb.UV_STARTING, "w") as f:
            json.dump({"since": 0}, f)
        with mock.patch.object(self.tb, "ping", return_value=None):
            with self.tb.FileLock(self.tb.UV_LOCK):
                self.assertIn("installing uv", self.tb.engine_status())
            self.assertIn("waiting for uv", self.tb.engine_status())  # installer gone: stale file

    def test_on_without_uv_starts_setup_and_says_so(self):
        out = self.run_command("on")
        self.assertIn("on for this session", out)
        self.assertIn("uv, then Kokoro", out)
        self.assertTrue(self.tb.is_on("test-session"))

    def test_on_clears_an_old_failure_marker(self):
        open(self.tb.UV_FAILED, "w").close()
        self.run_command("on")
        self.assertFalse(os.path.exists(self.tb.UV_FAILED))

    def test_session_start_warms_on_first_run_only(self):
        warmed = []
        self.tb.warm_detached = lambda: warmed.append(1)
        out = io.StringIO()
        with redirect_stdout(out):
            self.run_hook("session-start", {"session_id": "s1"})
        self.assertEqual(len(warmed), 1)
        self.assertIn("uv, then Kokoro", json.loads(out.getvalue())["systemMessage"])
        out = io.StringIO()
        open(self.tb.INSTALLED, "w").close()
        with redirect_stdout(out):
            self.run_hook("session-start", {"session_id": "s1"})
        self.assertEqual(len(warmed), 1)
        self.assertEqual(out.getvalue(), "")  # the note is shown once

    def test_session_start_does_not_loop_after_a_failed_start(self):
        warmed = []
        self.tb.warm_detached = lambda: warmed.append(1)
        open(self.tb.START_FAILED, "w").close()
        self.run_hook("session-start", {"session_id": "s1"})
        self.assertEqual(warmed, [])

    def test_warm_chains_uv_then_kokoro(self):
        order = []
        with mock.patch.object(self.tb, "ensure_uv", lambda: order.append("uv")), \
                mock.patch.object(self.tb, "ensure_helper", lambda: order.append("helper")), \
                mock.patch.object(self.tb, "ensure_daemon", lambda: order.append("kokoro")), \
                mock.patch.object(sys, "argv", ["talkback.py", "_warm"]):
            self.tb.main()
        self.assertEqual(order, ["uv", "helper", "kokoro"])

    def test_daemon_not_started_without_uv(self):
        with mock.patch.object(self.tb, "ping", return_value=None):
            self.assertFalse(self.tb.ensure_daemon())


class HexSetupTest(TalkbackTest):
    def test_not_offered_off_macos(self):
        with mock.patch.object(self.tb, "IS_MAC", False):
            out = self.run_command("setup", "yes")
            self.assertIn("macOS only", out)
            self.assertIn("/voice", out)

    def test_first_run_note_mentions_setup_only_on_mac(self):
        with mock.patch.object(self.tb, "IS_MAC", False):
            self.assertNotIn("/talkback setup", self.tb.first_run_note())
        os.unlink(self.tb.HEX_OFFERED)
        with mock.patch.object(self.tb, "IS_MAC", True), mock.patch.object(self.tb, "hex_app", return_value=None):
            self.assertIn("/talkback setup", self.tb.first_run_note())
        self.assertIsNone(self.tb.first_run_note())

    def test_declined_users_are_not_nagged(self):
        self.tb.save_config({"hex_declined": True})
        with mock.patch.object(self.tb, "IS_MAC", True), mock.patch.object(self.tb, "hex_app", return_value=None):
            self.assertNotIn("/talkback setup", self.tb.first_run_note())

    def test_setup_without_yes_only_offers(self):
        with mock.patch.object(self.tb, "IS_MAC", True), mock.patch.object(self.tb, "hex_app", return_value=None), \
                mock.patch.object(self.tb, "install_hex") as install, mock.patch.object(self.tb.subprocess, "run") as run:
            out = self.run_command("setup")
        self.assertIn("setup yes", out)
        self.assertIn("kitlangton/Hex", out)
        install.assert_not_called()
        run.assert_not_called()

    def test_setup_skip_is_remembered(self):
        with mock.patch.object(self.tb, "IS_MAC", True):
            self.run_command("setup", "skip")
        self.assertTrue(self.tb.load_config()["hex_declined"])

    def test_setup_yes_installs_and_opens_both_panes(self):
        runs = []
        with mock.patch.object(self.tb, "IS_MAC", True), mock.patch.object(self.tb, "hex_app", return_value=None), \
                mock.patch.object(self.tb, "install_hex", return_value=(True, "Hex installed in /Applications.")), \
                mock.patch.object(self.tb.subprocess, "run", side_effect=lambda argv, **kw: runs.append(argv)):
            out = self.run_command("setup", "yes")
        self.assertIn("Hex installed", out)
        opened = [a[1] for a in runs if a[0] == "open"]
        self.assertEqual(len(opened), 2)
        self.assertTrue(any("Privacy_Microphone" in u for u in opened))
        self.assertTrue(any("Privacy_Accessibility" in u for u in opened))

    def test_setup_yes_stops_when_install_fails(self):
        with mock.patch.object(self.tb, "IS_MAC", True), mock.patch.object(self.tb, "hex_app", return_value=None), \
                mock.patch.object(self.tb, "install_hex", return_value=(False, "couldn't install Hex (offline).")), \
                mock.patch.object(self.tb.subprocess, "run") as run:
            out = self.run_command("setup", "yes")
        self.assertIn("offline", out)
        run.assert_not_called()

    def test_download_url_picks_the_dmg(self):
        payload = {"assets": [{"name": "Hex.zip", "browser_download_url": "z"},
                              {"name": "Hex-1.dmg", "browser_download_url": "https://x/Hex-1.dmg"}]}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass
        url = self.tb.hex_download_url(opener=lambda req, timeout: Resp(json.dumps(payload).encode()))
        self.assertEqual(url, "https://x/Hex-1.dmg")

    def test_install_hex_mounts_copies_and_detaches(self):
        cmds = []

        def run(argv, **kw):
            cmds.append(argv[0])
            if argv[0] == "hdiutil" and argv[1] == "attach":
                os.makedirs(os.path.join(argv[argv.index("-mountpoint") + 1], "Hex.app"))
        with mock.patch.object(self.tb.os, "access", return_value=True):
            ok, msg = self.tb.install_hex(run=run, url_fn=lambda: "u", fetch=lambda url, dst: None)
        self.assertTrue(ok, msg)
        self.assertEqual(cmds, ["hdiutil", "ditto", "hdiutil"])

    def test_install_hex_failure_still_detaches(self):
        cmds = []

        def run(argv, **kw):
            cmds.append(argv[1])
            if argv[1] == "attach":
                os.makedirs(os.path.join(argv[argv.index("-mountpoint") + 1], "other"))
        ok, _ = self.tb.install_hex(run=run, url_fn=lambda: "u", fetch=lambda url, dst: None)
        self.assertFalse(ok)
        self.assertEqual(cmds, ["attach", "detach"])


class ArgumentSafetyTest(unittest.TestCase):
    """/talkback must not let the shell parse the user's words."""

    def command_script(self):
        path = os.path.join(ROOT, "plugins", "talkback", "commands", "talkback.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count("$ARGUMENTS"), 1)
        self.assertIn('allowed-tools: "Bash(exec sh:*)"', text)
        match = re.search(r"^```!\n(.*)^```\s*$", text, re.M | re.S)
        self.assertIsNotNone(match)
        script = match.group(1)
        self.assertTrue(script.lstrip().startswith("exec sh "))
        self.assertIn("<<'TALKBACK_EOF'", script)
        return script

    def test_shell_command_passes_text_literally(self):
        script = self.command_script()
        delim = re.search(r"<<'(\w+)'", script).group(1)
        with tempfile.TemporaryDirectory() as root:
            bindir = os.path.join(root, "bin")
            os.makedirs(bindir)
            capture = os.path.join(root, "capture")
            stub = os.path.join(bindir, "run.sh")
            with open(stub, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$CAPTURE.argv\"\ncat > \"$CAPTURE.stdin\"\n")
            os.chmod(stub, 0o755)
            env = dict(os.environ, CLAUDE_PLUGIN_ROOT=root, CAPTURE=capture,
                       HOME="/talkback-home-should-not-expand")
            payloads = [
                "say it's ready",
                "say cost is $HOME",
                "say hello > " + os.path.join(root, "pwned"),
                "say $(touch " + os.path.join(root, "pwned") + ")",
                "say hello; echo PWNED",
                "say `touch " + os.path.join(root, "pwned") + "`",
                'say hello"; touch ' + os.path.join(root, "pwned") + '; echo "',
                "say hello\n" + delim + "\ntouch " + os.path.join(root, "pwned"),
            ]
            for payload in payloads:
                marker = os.path.join(root, "pwned")
                proc = subprocess.run(["bash", "-c", script.replace("$ARGUMENTS", payload, 1)],
                                      env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertNotIn("PWNED", proc.stdout)
                self.assertFalse(os.path.exists(marker), payload)
                with open(capture + ".argv", encoding="utf-8") as f:
                    self.assertEqual(f.read().split(), ["--from-stdin"])
                with open(capture + ".stdin", encoding="utf-8") as f:
                    got = f.read()
                self.assertNotIn("/talkback-home-should-not-expand", got)
                if payload.startswith("say hello\n" + delim):
                    self.assertEqual(got, "say hello\n")
                else:
                    self.assertEqual(got, payload + "\n")

    def test_run_sh_forwards_stdin_without_parsing_it(self):
        src = os.path.join(ROOT, "plugins", "talkback", "bin", "run.sh")
        with tempfile.TemporaryDirectory() as home:
            bin_dir = os.path.join(home, "bin")
            os.makedirs(bin_dir)
            run_sh = os.path.join(home, "run.sh")
            with open(src, encoding="utf-8") as f:
                text = f.read().replace("/opt/homebrew/bin/uv /usr/local/bin/uv", "")
            with open(run_sh, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(run_sh, 0o755)
            py = os.path.join(bin_dir, "python3")
            with open(py, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\n"
                        "if [ \"$1\" = \"-c\" ]; then exit 0; fi\n"
                        "prev=\n"
                        "for a in \"$@\"; do\n"
                        "  if [ \"$prev\" = \"--args-file\" ]; then cat \"$a\"; rm -f \"$a\"; exit 0; fi\n"
                        "  prev=$a\n"
                        "done\n"
                        "echo missing-args-file >&2\nexit 1\n")
            os.chmod(py, 0o755)
            for tool in ("mktemp", "cat", "rm"):
                os.symlink(shutil.which(tool), os.path.join(bin_dir, tool))
            # talkback.py must sit next to run.sh; the stub python ignores its contents.
            open(os.path.join(home, "talkback.py"), "w").close()
            payload = "say it's ready; echo $(HOME) > /tmp/talkback-should-not-exist\n"
            env = {"HOME": home, "PATH": bin_dir, "TALKBACK_HOME": os.path.join(home, "tb")}
            proc = subprocess.run(["/bin/sh", run_sh, "--from-stdin"], env=env, input=payload,
                                  capture_output=True, text=True, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, payload)
            self.assertFalse(os.path.exists("/tmp/talkback-should-not-exist"))


class LauncherTest(unittest.TestCase):
    """bin/run.sh with no uv and no Python on PATH must not fail a hook."""

    def test_hook_without_uv_or_python_exits_zero(self):
        import subprocess
        src = os.path.join(ROOT, "plugins", "talkback", "bin", "run.sh")
        with tempfile.TemporaryDirectory() as home:
            # A copy without the system-wide uv paths, so a uv installed on this machine can't be found.
            run_sh = os.path.join(home, "run.sh")
            with open(src) as f:
                text = f.read().replace("/opt/homebrew/bin/uv /usr/local/bin/uv", "")
            with open(run_sh, "w") as f:
                f.write(text)
            env = {"HOME": home, "PATH": "/nonexistent", "TALKBACK_HOME": os.path.join(home, "tb")}
            for hook in ("stop", "prompt"):
                r = subprocess.run(["/bin/sh", run_sh, "hook", hook], env=env, capture_output=True, text=True,
                                   timeout=20, stdin=subprocess.DEVNULL, check=False)
                self.assertEqual((r.returncode, r.stdout), (0, ""))
            r = subprocess.run(["/bin/sh", run_sh, "status"], env=env, capture_output=True, text=True, timeout=20,
                               stdin=subprocess.DEVNULL, check=False)
            self.assertIn("installing uv", r.stdout)

    def test_no_runtime_removes_the_argument_file(self):
        src = os.path.join(ROOT, "plugins", "talkback", "bin", "run.sh")
        with tempfile.TemporaryDirectory() as home:
            run_sh = os.path.join(home, "run.sh")
            with open(src, encoding="utf-8") as f:
                text = f.read().replace("/opt/homebrew/bin/uv /usr/local/bin/uv", "")
            with open(run_sh, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(run_sh, 0o755)
            bin_dir = os.path.join(home, "bin")
            os.makedirs(bin_dir)
            for tool in ("mktemp", "cat", "rm"):
                os.symlink(shutil.which(tool), os.path.join(bin_dir, tool))
            tmp = os.path.join(home, "tmp")
            os.makedirs(tmp)
            env = {"HOME": home, "PATH": bin_dir, "TALKBACK_HOME": os.path.join(home, "tb"), "TMPDIR": tmp}
            proc = subprocess.run(["/bin/sh", run_sh, "--from-stdin"], env=env,
                                  input="say hello; echo should-not-run\n",
                                  capture_output=True, text=True, timeout=20)
            self.assertIn("installing uv", proc.stdout)
            self.assertNotIn("should-not-run", proc.stdout)
            self.assertEqual(os.listdir(tmp), [])

    def test_hooks_json_uses_the_launcher(self):
        with open(os.path.join(ROOT, "plugins", "talkback", "hooks", "hooks.json")) as f:
            hooks = json.load(f)["hooks"]
        for groups in hooks.values():
            for h in groups[0]["hooks"]:
                self.assertEqual(h["command"], "sh")
                self.assertTrue(h["args"][0].endswith("bin/run.sh"))


class SessionLifecycleTest(TalkbackTest):
    def test_prune_sessions(self):
        os.makedirs(self.tb.SESSIONS, exist_ok=True)
        old_file = os.path.join(self.tb.SESSIONS, "old-session")
        new_file = os.path.join(self.tb.SESSIONS, "new-session")
        with open(old_file, "w") as f:
            f.write("on")
        with open(new_file, "w") as f:
            f.write("on")
        old_time = time.time() - (self.tb.SESSION_TTL + 3600)
        os.utime(old_file, (old_time, old_time))
        self.tb.prune_sessions()
        self.assertFalse(os.path.exists(old_file))
        self.assertTrue(os.path.exists(new_file))

    def test_daemon_alive_replaces_older_version(self):
        sent = []
        with mock.patch.object(self.tb, "ping", return_value=("127.0.0.1:5000", "0.3.0")), \
             mock.patch.object(self.tb, "plugin_version", return_value="0.4.0"), \
             mock.patch.object(self.tb, "send", lambda req, timeout=2.0: sent.append(req)), \
             mock.patch.object(self.tb.time, "sleep", return_value=None):
            alive = self.tb.daemon_alive()
            self.assertFalse(alive)
            self.assertEqual(sent, [{"cmd": "quit"}])

    def test_daemon_alive_matching_version(self):
        with mock.patch.object(self.tb, "ping", return_value=("127.0.0.1:5000", "0.4.0")), \
             mock.patch.object(self.tb, "plugin_version", return_value="0.4.0"):
            self.assertTrue(self.tb.daemon_alive())


class MainTest(TalkbackTest):
    def test_main_speak(self):
        sent = []
        with mock.patch.object(self.tb, "ensure_uv", return_value="/bin/uv"), \
             mock.patch.object(self.tb, "ensure_daemon", return_value=True), \
             mock.patch.object(self.tb, "send", lambda req, timeout=30: sent.append(req)), \
             mock.patch.object(sys, "argv", ["talkback.py", "speak", "hello", "world"]):
            self.tb.main()
        self.assertEqual(sent, [{"cmd": "say", "text": "hello world."}])

    def test_main_say_worker(self):
        tmp = os.path.join(self.home.name, "say.txt")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("text from file")
        sent = []
        with mock.patch.object(self.tb, "ensure_uv", return_value="/bin/uv"), \
             mock.patch.object(self.tb, "ensure_helper", return_value=None), \
             mock.patch.object(self.tb, "ensure_daemon", return_value=True), \
             mock.patch.object(self.tb, "send", lambda req, timeout=30: sent.append(req)), \
             mock.patch.object(sys, "argv", ["talkback.py", "_say", "test-session", tmp]):
            self.tb.main()
        self.assertFalse(os.path.exists(tmp))
        self.assertEqual(sent, [{"cmd": "say", "session": "test-session", "text": "text from file"}])


if __name__ == "__main__":
    unittest.main()
