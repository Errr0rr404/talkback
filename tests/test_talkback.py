"""Tests for talkback.py's text cleanup, commands and hooks (no TTS engine needed).

Run: uv run --no-project python -m unittest discover -s tests
"""
import importlib
import io
import json
import os
import sys
import tempfile
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
        md = "\n\n".join(["Para number %d is here." % i for i in range(100)])
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


class InstallStatusTest(TalkbackTest):
    def write_starting(self, first):
        with open(self.tb.STARTING, "w") as f:
            json.dump({"since": 0, "first": first}, f)

    def test_status_while_installing(self):
        self.write_starting(first=True)
        with mock.patch.object(self.tb, "ping", return_value=None):
            with self.tb.FileLock(self.tb.DAEMON_LOCK):
                self.assertIn("installing Kokoro", self.tb.engine_status())
            self.assertEqual(self.tb.engine_status(), "stopped")  # starter gone: the file is stale

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


if __name__ == "__main__":
    unittest.main()
