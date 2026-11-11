"""Tests for talkback.py's text cleanup, commands and hooks (no TTS engine needed).

Run: uv run --no-project python -m unittest discover -s tests
"""
import ast
import importlib
import io
import json
import os
import re
import shutil
import signal
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
        self.assertEqual(self.tb.speakable(md),
                         "Done. I set up the hook to see docs. First. Second. " + self.tb.SKIP_NOTE)

    def test_paths_become_file_names(self):
        self.assertEqual(self.tb.speakable("Edit ~/.claude/voice/hook.py now"), "Edit hook.py now.")
        self.assertEqual(self.tb.speakable(r"Edit C:\Users\me\proj\main.py now"), "Edit main.py now.")

    def test_keeps_snake_case(self):
        self.assertEqual(self.tb.speakable("Set max_chars_value here"), "Set max_chars_value here.")
        self.assertEqual(self.tb.speakable("This is _important_ stuff"), "This is important stuff.")

    def test_limit_cuts_at_paragraph_or_sentence(self):
        md = "\n\n".join([f"Para number {i} is here." for i in range(100)])
        short = self.tb.speakable(md, limit=100)
        self.assertTrue(short.endswith(" " + self.tb.TRIM_NOTE))
        body = short[: -(len(self.tb.TRIM_NOTE) + 1)]
        self.assertLessEqual(len(body), 100)
        self.assertTrue(body.endswith("."))
        full = self.tb.speakable(md, limit=0)
        self.assertNotIn(self.tb.TRIM_NOTE, full)
        self.assertEqual(len(full), len(md.replace("\n\n", " ")))

    def test_tilde_fences_and_tables_say_they_were_trimmed(self):
        tilde = "Look.\n\n~~~js\nconsole.log(1)\n~~~\n\nDone."
        spoken = self.tb.speakable(tilde)
        self.assertNotIn("console", spoken)
        self.assertEqual(spoken, "Look. Done. " + self.tb.SKIP_NOTE)
        table = "Rows.\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\nEnd."
        table_spoken = self.tb.speakable(table)
        self.assertNotIn("|", table_spoken)
        self.assertTrue(table_spoken.endswith(self.tb.SKIP_NOTE))
        self.assertIn("Rows.", table_spoken)
        self.assertEqual(self.tb.speakable("Nothing to cut."), "Nothing to cut.")


    def test_short_first_paragraph_does_not_cost_the_answer(self):
        md = "Sure.\n\n" + ("Word " * 135).strip() + ".\n\n" + "Tail sentence here."
        spoken, cut = self.tb.speakable_parts(md, limit=700)
        self.assertTrue(cut)
        self.assertGreaterEqual(len(spoken), 600)
        self.assertTrue(spoken.endswith(self.tb.TRIM_NOTE))
        self.assertLessEqual(len(spoken), 700 + len(self.tb.TRIM_NOTE) + 1)

    def test_cut_without_a_sentence_end_uses_the_last_space(self):
        spoken = self.tb.speakable("word " * 100, limit=50)
        body = spoken[: -(len(self.tb.TRIM_NOTE) + 1)]
        self.assertLessEqual(len(body), 50)
        self.assertTrue(body.endswith("word"))

    def test_text_under_the_limit_is_unchanged(self):
        self.assertEqual(self.tb.speakable_parts("Short one.\n\nAnother.", limit=700), ("Short one. Another.", False))

    def test_notes_tell_a_code_skip_from_a_length_cut(self):
        self.assertEqual(self.tb.speakable("Done.\n\n```sh\nls\n```"), "Done. " + self.tb.SKIP_NOTE)
        long = "Done. " * 200 + "\n\n```sh\nls\n```"
        spoken = self.tb.speakable(long, limit=100)
        self.assertTrue(spoken.endswith(self.tb.TRIM_NOTE))
        self.assertNotIn(self.tb.SKIP_NOTE, spoken)


class CommandTest(TalkbackTest):
    def test_bare_settings_show_the_current_value(self):
        out = self.run_command("length")
        self.assertIn("up to 700 characters (about 40 seconds at speed 1.1)", out)
        self.assertIn("/talkback length full", out)
        self.assertIn("/talkback full", out)
        self.assertEqual(self.tb.load_config(), {})
        self.assertIn("Voice is af_heart", self.run_command("voice"))
        self.assertIn("Speed is 1.1", self.run_command("speed"))
        self.assertIn("Auto-Enter after Hex dictation is on", self.run_command("autoenter"))
        self.assertIn("off by default", self.run_command("default"))
        self.run_command("length", "full")
        self.run_command("voice", "bf_emma")
        self.run_command("speed", "1.5")
        self.run_command("autoenter", "off")
        self.run_command("default", "on")
        self.assertIn("read in full", self.run_command("length"))
        self.assertIn("Voice is bf_emma", self.run_command("voice"))
        self.assertIn("Speed is 1.5", self.run_command("speed"))
        self.assertIn("Auto-Enter after Hex dictation is off", self.run_command("autoenter"))
        self.assertIn("on by default", self.run_command("default"))
        self.run_command("length", "1500")
        self.assertIn("up to 1500 characters (about 65 seconds at speed 1.5)", self.run_command("length"))

    def test_length_in_seconds_is_stored_as_characters(self):
        out = self.run_command("length", "60s")
        self.assertEqual(self.tb.load_config()["max_chars"], 990)  # 60 s x 15 chars/s x speed 1.1
        self.assertIn("990 characters (about 60 seconds", out)
        self.run_command("length", "2m")
        self.assertEqual(self.tb.load_config()["max_chars"], 1980)
        self.assertIn("Length must be", self.run_command("length", "1s"))
        self.assertEqual(self.tb.load_config()["max_chars"], 1980)

    def test_status_shows_length_in_characters_and_seconds(self):
        self.assertIn("length=700 characters (about 40 s)", self.run_command("status"))
        self.run_command("length", "full")
        self.assertIn("length=full", self.run_command("status"))

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

    def test_first_length_cut_in_a_session_shows_a_message_once(self):
        self.tb.set_session("test-session", "on")
        reply = "Sentence number one is here. " * 60
        for expect in (True, False):
            out = io.StringIO()
            with redirect_stdout(out):
                self.run_hook("stop", {"session_id": "test-session", "last_assistant_message": reply})
            if expect:
                msg = json.loads(out.getvalue())["systemMessage"]
                self.assertIn("700 characters", msg)
                self.assertIn("/talkback full", msg)
                self.assertIn("/talkback length", msg)
            else:
                self.assertEqual(out.getvalue(), "")
        self.assertEqual(len(self.said), 2)

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
        # engine_status asks for uv before it reports Kokoro. Stub it so a machine without uv
        # still exercises the Kokoro lines (same patch test_on_off uses).
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
        # Claude Code (2.1.318) refuses `exec` in a command's shell block, so the guard against a line
        # that closes the heredoc early is the permission rule: it allows only run.sh --from-stdin, and
        # any other command the words smuggle in after that line needs the user's approval.
        prefix = 'sh "${CLAUDE_PLUGIN_ROOT}/bin/run.sh" --from-stdin'
        self.assertIn("\nallowed-tools: Bash(" + prefix + ":*)\n", text)
        match = re.search(r"^```!\n(.*)^```\s*$", text, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match)
        script = match.group(1)
        self.assertEqual(script.splitlines()[0], prefix + " <<'TALKBACK_EOF'")
        return script

    def test_shell_command_passes_text_literally(self):
        script = self.command_script()
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
            ]
            for payload in payloads:
                marker = os.path.join(root, "pwned")
                proc = subprocess.run(["bash", "-c", script.replace("$ARGUMENTS", payload, 1)],
                                      env=env, capture_output=True, text=True, timeout=20, check=False)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertNotIn("PWNED", proc.stdout)
                self.assertFalse(os.path.exists(marker), payload)
                with open(capture + ".argv", encoding="utf-8") as f:
                    self.assertEqual(f.read().split(), ["--from-stdin"])
                with open(capture + ".stdin", encoding="utf-8") as f:
                    got = f.read()
                self.assertNotIn("/talkback-home-should-not-expand", got)
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
                                  capture_output=True, text=True, timeout=20, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, payload)
            self.assertFalse(os.path.exists("/tmp/talkback-should-not-exist"))


class MicWatchTest(unittest.TestCase):
    """voiced.py imports the TTS stack at import, so load mic_watch on its own and drive it."""

    def test_already_open_mic_is_not_a_new_press(self):
        path = os.path.join(ROOT, "plugins", "talkback", "engine", "voiced.py")
        with open(path) as f:
            tree = ast.parse(f.read())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "mic_watch")
        # After each sleep: (mic is on, something is playing). Playback starts while the mic
        # is already open, so that is not a press. Off then on during speech is one press.
        script = [
            (True, False),
            (True, True),
            (False, True),
            (True, True),
        ]
        state = {"player": None, "i": -1, "mic": True}
        stops = []
        probes = []

        def probe():
            probes.append(state["player"] is not None)
            return state["mic"]

        def sleep(_dt):
            state["i"] += 1
            if state["i"] >= len(script):
                raise StopIteration
            mic, playing = script[state["i"]]
            state["mic"] = mic
            state["player"] = object() if playing else None

        ns = {
            "time": type("T", (), {"sleep": staticmethod(sleep)}),
            "current": state,
            "stop": lambda: stops.append(1),
            "log": lambda *a, **k: None,
        }
        exec(compile(ast.Module([fn], type_ignores=[]), path, "exec"), ns)
        with self.assertRaises(StopIteration):
            ns["mic_watch"](probe)
        self.assertEqual(stops, [1])
        self.assertEqual(probes, [True, True, True])  # never probed while idle (pactl is a process spawn)


class DaemonStartTest(TalkbackTest):
    def test_ping_clears_start_failed(self):
        open(self.tb.START_FAILED, "w").close()
        pong = "pong onnx " + self.tb.plugin_version()
        with mock.patch.object(self.tb, "send", return_value=pong):
            self.assertTrue(self.tb.ensure_daemon())
            self.assertFalse(os.path.exists(self.tb.START_FAILED))
            open(self.tb.START_FAILED, "w").close()
            self.assertIn("running", self.tb.engine_status())
        self.assertFalse(os.path.exists(self.tb.START_FAILED))

    def test_start_timeout_kills_the_child(self):
        # A session leader plus a grandchild, the shape of `uv run` spawning python.
        # Killing only the leader leaves the grandchild running.
        child_file = os.path.join(self.home.name, "childpid")
        proc = subprocess.Popen(
            ["/bin/sh", "-c", "sleep 60 & echo $! > \"$1\"; wait", "sh", child_file],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child_pid = None
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not os.path.exists(child_file):
                if proc.poll() is not None:
                    self.fail("helper shell exited before the grandchild started")
                time.sleep(0.02)
            with open(child_file) as f:
                child_pid = int(f.read().strip())
            with mock.patch.object(self.tb, "spawn_detached", return_value=proc), \
                    mock.patch.object(self.tb, "daemon_alive", return_value=False), \
                    mock.patch.object(self.tb.time, "time", side_effect=[0, 999]):
                self.assertFalse(self.tb.start_daemon("uv", 1))
            self.assertIsNotNone(proc.poll())
            self.assertTrue(self._gone(child_pid), "grandchild still running after the start timed out")
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
            if child_pid and self._alive(child_pid):
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except OSError:
                    pass

    def _alive(self, pid):
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _gone(self, pid):
        """True once pid has exited. A zombie counts: nothing is running it."""
        deadline = time.time() + 2
        while time.time() < deadline:
            if not self._alive(pid):
                return True
            r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
            if not r.stdout.strip() or r.stdout.strip().startswith("Z"):
                return True
            time.sleep(0.05)
        return False


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
                                  capture_output=True, text=True, timeout=20, check=False)
            self.assertIn("installing uv", proc.stdout)
            self.assertNotIn("should-not-run", proc.stdout)
            self.assertEqual(os.listdir(tmp), [])

    def test_session_start_locks_install_and_records_failure(self):
        src = os.path.join(ROOT, "plugins", "talkback", "bin", "run.sh")
        with tempfile.TemporaryDirectory() as home:
            state = os.path.join(home, "tb")
            os.makedirs(state)
            run_sh = os.path.join(home, "run.sh")
            with open(src) as f:
                text = f.read().replace("/opt/homebrew/bin/uv /usr/local/bin/uv", "")
            with open(run_sh, "w") as f:
                f.write(text)
            bin_dir = os.path.join(home, "bin")
            os.makedirs(bin_dir)
            locker = "flock" if shutil.which("flock") else "perl"  # macOS has no flock: run.sh falls back to perl
            if not shutil.which(locker):
                self.skipTest("neither flock nor perl is installed")
            for name in (locker, "date", "stat", "mkdir", "rm", "grep", "sleep"):
                os.symlink(shutil.which(name), os.path.join(bin_dir, name))
            curl_log = os.path.join(home, "curl.log")
            stamp = os.path.join(home, "curl.stamp")
            with open(os.path.join(bin_dir, "curl"), "w") as f:
                f.write("#!/bin/sh\n"
                        "if [ -f \"$CURL_STAMP\" ]; then echo overlap >> \"$CURL_LOG\"; fi\n"
                        "echo $$ > \"$CURL_STAMP\"\n"
                        "echo called >> \"$CURL_LOG\"\n"
                        "if [ ! -f \"$TALKBACK_HOME/uv-starting.json\" ] || "
                        "! grep -q since \"$TALKBACK_HOME/uv-starting.json\"; then\n"
                        "  echo bad-starting >> \"$CURL_LOG\"\n"
                        "fi\n"
                        "if [ -n \"$CURL_DELAY\" ]; then sleep \"$CURL_DELAY\"; fi\n"
                        "rm -f \"$CURL_STAMP\"\n"
                        "exit 0\n")
            os.chmod(os.path.join(bin_dir, "curl"), 0o755)
            env = {
                "HOME": home,
                "PATH": bin_dir,  # no python and no uv, so run.sh takes the shell installer
                "TALKBACK_HOME": state,
                "CURL_LOG": curl_log,
                "CURL_STAMP": stamp,
            }

            def log():
                try:
                    with open(curl_log) as f:
                        return f.read()
                except OSError:
                    return ""

            def installers():
                # Only this test's own processes (its temp dir is in their arguments), not other runs' or tools'.
                r = subprocess.run(["ps", "-eo", "args="], capture_output=True, text=True, check=False)
                return "\n".join(line for line in r.stdout.splitlines()
                                 if home in line and ("uv.lock" in line or "_uv_install" in line))

            def launch(extra=None):
                child_env = dict(env)
                if extra:
                    child_env.update(extra)
                proc = subprocess.Popen(["/bin/sh", run_sh, "hook", "session-start"], env=child_env,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
                procs.append(proc)
                return proc

            def wait_for(pred, timeout=20):  # generous: a loaded machine is slow to reap
                end = time.time() + timeout
                while time.time() < end:
                    if pred():
                        return True
                    time.sleep(0.05)
                return False

            procs = []
            os.environ["TALKBACK_HOME"] = state
            import talkback
            tb = importlib.reload(talkback)
            try:
                # Same uv.lock FileLock as ensure_uv: the installer blocks until we release it.
                with tb.FileLock(tb.UV_LOCK):
                    first = launch()
                    first.wait(timeout=5)
                    blocked = wait_for(lambda: "flock" in installers() and log() == "")
                    self.assertTrue(blocked, f"installer did not block on uv.lock\nlog={log()!r}\nps={installers()!r}")
                    self.assertFalse(os.path.exists(tb.UV_STARTING))
                self.assertTrue(wait_for(lambda: installers() == "" and os.path.exists(tb.UV_FAILED)),
                                f"first installer did not finish\nlog={log()!r}\nps={installers()!r}")
                self.assertEqual(log().count("called"), 1)
                self.assertNotIn("bad-starting", log())
                self.assertNotIn("overlap", log())
                self.assertFalse(os.path.exists(tb.UV_STARTING))
                status = subprocess.run(["/bin/sh", run_sh, "status"], env=env, capture_output=True,
                                        text=True, timeout=20, stdin=subprocess.DEVNULL, check=False)
                self.assertEqual(status.returncode, 0)
                self.assertIn(os.path.join(state, "uv-install.log"), status.stdout)
                self.assertIn("failed", status.stdout)
                self.assertNotIn("installing uv", status.stdout)
                # A recent failure is not retried by the next session start.
                again = launch()
                again.wait(timeout=5)
                time.sleep(0.4)
                self.assertEqual(log().count("called"), 1)
                self.assertNotIn("_uv_install", installers())
                # An old failure is retried, and two at once still run one installer.
                os.utime(tb.UV_FAILED, (0, 0))
                pair = [launch({"CURL_DELAY": "0.4"}), launch({"CURL_DELAY": "0.4"})]
                for proc in pair:
                    proc.wait(timeout=5)  # the launcher returns; the locked installer keeps running
                self.assertTrue(wait_for(lambda: installers() == "" and log().count("called") >= 2),
                                f"overlapping installers did not finish\nlog={log()!r}\nps={installers()!r}\n"
                                f"failed-mtime={os.path.getmtime(tb.UV_FAILED)}")
                self.assertEqual(log().count("called"), 2)
                self.assertNotIn("overlap", log())
                self.assertNotIn("bad-starting", log())
                self.assertTrue(os.path.exists(tb.UV_FAILED))
            finally:
                for proc in procs:
                    if proc.poll() is None:
                        try:
                            os.killpg(proc.pid, signal.SIGTERM)
                        except OSError:
                            pass
                for proc in procs:
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except OSError:
                            pass
                        proc.wait(timeout=2)

    def test_hooks_json_uses_the_launcher(self):
        with open(os.path.join(ROOT, "plugins", "talkback", "hooks", "hooks.json")) as f:
            hooks = json.load(f)["hooks"]
        for groups in hooks.values():
            for h in groups[0]["hooks"]:
                # Shell form, no "args": Claude Code runs it in Git Bash on Windows, where plain `sh` may not be on PATH.
                self.assertNotIn("args", h)
                self.assertRegex(h["command"], r'^sh "\$\{CLAUDE_PLUGIN_ROOT\}/bin/run\.sh" hook [a-z-]+$')

    def test_hook_commands_run_from_a_plugin_path_with_a_space(self):
        with open(os.path.join(ROOT, "plugins", "talkback", "hooks", "hooks.json")) as f:
            hooks = json.load(f)["hooks"]
        with tempfile.TemporaryDirectory() as tmp:
            plugin = os.path.join(tmp, "talk back")
            shutil.copytree(os.path.join(ROOT, "plugins", "talkback", "bin"), os.path.join(plugin, "bin"),
                            ignore=shutil.ignore_patterns("__pycache__"))
            bin_dir = os.path.join(tmp, "path")
            os.makedirs(bin_dir)
            marker = os.path.join(tmp, "args")
            with open(os.path.join(bin_dir, "python3"), "w") as f:
                f.write('#!/bin/sh\n[ "$1" = "-c" ] && exit 0\nprintf "%s\\n" "$@" >> "$MARKER"\n')
            os.chmod(os.path.join(bin_dir, "python3"), 0o755)
            env = {"HOME": tmp, "PATH": bin_dir + ":/usr/bin:/bin", "TALKBACK_HOME": os.path.join(tmp, "tb"),
                   "CLAUDE_PLUGIN_ROOT": plugin, "MARKER": marker}
            if shutil.which("uv"):
                self.skipTest("a real uv would run talkback.py instead of the fake python3")
            for ev, groups in hooks.items():
                r = subprocess.run(groups[0]["hooks"][0]["command"], shell=True, env=env, capture_output=True,
                                   text=True, timeout=20, stdin=subprocess.DEVNULL, check=False)
                self.assertEqual(r.returncode, 0, r.stderr)
            with open(marker) as f:
                lines = f.read().splitlines()
            self.assertEqual(lines.count(os.path.join(plugin, "bin", "talkback.py")), 3)

    def test_uv_installer_gets_its_settings(self):
        src = os.path.join(ROOT, "plugins", "talkback", "bin", "run.sh")
        with tempfile.TemporaryDirectory() as home:
            bin_dir = os.path.join(home, "bin")
            os.makedirs(bin_dir)
            with open(os.path.join(bin_dir, "curl"), "w") as f:
                f.write('#!/bin/sh\necho \'echo "$UV_UNMANAGED_INSTALL|$UV_NO_MODIFY_PATH"\'\n')
            os.chmod(os.path.join(bin_dir, "curl"), 0o755)
            state = os.path.join(home, "tb")
            env = {"HOME": home, "PATH": bin_dir + ":/usr/bin:/bin", "TALKBACK_HOME": state}
            subprocess.run(["/bin/sh", src, "_uv_install"], env=env, capture_output=True, text=True, timeout=20,
                           stdin=subprocess.DEVNULL, check=False)
            with open(os.path.join(state, "uv-install.log")) as f:
                self.assertEqual(f.read().strip(), state + "/uv|1")

    @unittest.skipUnless(shutil.which("uv"), "uv is not installed")
    def test_project_python_pin_and_uv_toml_do_not_break_the_launcher(self):
        src = os.path.join(ROOT, "plugins", "talkback", "bin", "run.sh")
        with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as home:
            with open(os.path.join(proj, ".python-version"), "w") as f:
                f.write("3.9.1\n")
            with open(os.path.join(proj, "uv.toml"), "w") as f:
                f.write('required-version = "==0.1.0"\n')
            env = dict(os.environ, HOME=home, TALKBACK_HOME=os.path.join(home, "tb"), UV_PYTHON_DOWNLOADS="never")
            r = subprocess.run(["/bin/sh", src, "status"], cwd=proj, env=env, capture_output=True, text=True,
                               timeout=60, stdin=subprocess.DEVNULL, check=False)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("talkback", r.stdout.lower())


class RepoFilesTest(unittest.TestCase):
    def test_lock_has_an_intel_mac_onnxruntime_wheel(self):
        with open(os.path.join(ROOT, "plugins", "talkback", "engine", "uv.lock"), encoding="utf-8") as f:
            lock = f.read()
        blocks = [b for b in lock.split("[[package]]") if b.lstrip().startswith('name = "onnxruntime"')]
        self.assertTrue(any(re.search(r"onnxruntime-[\d.]+-cp\d+-cp\d+-macosx_\d+_\d+_(x86_64|universal2)\.whl", b)
                            for b in blocks), "no onnxruntime wheel for Intel Macs in uv.lock")

    def test_gitattributes_keeps_lf_in_shell_files(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src")
            shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "core.autocrlf=true"]
            subprocess.run(git + ["init", "-q", src], check=True, capture_output=True)
            subprocess.run(git + ["-C", src, "add", "-A"], check=True, capture_output=True)
            subprocess.run(git + ["-C", src, "commit", "-qm", "x"], check=True, capture_output=True)
            dst = os.path.join(tmp, "dst")
            subprocess.run(git + ["clone", "-q", src, dst], check=True, capture_output=True)
            for rel in ("plugins/talkback/bin/run.sh", "plugins/talkback/commands/talkback.md"):
                with open(os.path.join(dst, rel), "rb") as f:
                    self.assertNotIn(b"\r", f.read(), rel)


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
