# talkback

Claude Code reads its replies aloud in a natural voice ([Kokoro](https://huggingface.co/hexgrad/Kokoro-82M)) that runs on your own machine. Works on macOS, Linux and Windows.

## Install

```sh
claude plugin marketplace add Errr0rr404/talkback
claude plugin install talkback@talkback
```

Then, in Claude Code:

```
/talkback on
```

The first session sets up the voice in the background (a few hundred MB, no `sudo`); `/talkback status` shows progress. If `/talkback` is unknown, use the full name `/talkback:talkback on`, or run `/reload-plugins`.

## Talk back

- **Claude Code's `/voice`**: built in, needs a claude.ai login.
- **[Hex](https://github.com/kitlangton/Hex)** (macOS): works with any login, keeps your voice on your machine, and talkback presses Enter when you let go of the hotkey. Run `/talkback setup` to install it.

Speech stops as soon as you start talking or send a message.

## Commands

| Command | What it does |
|---|---|
| `/talkback on` / `off` | Spoken replies for this session |
| `/talkback status` | Settings, and whether the voice is ready |
| `/talkback full` | Read the last reply again, in full |
| `/talkback voice bf_emma` | Change the [voice](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md) |
| `/talkback default on` | Turn it on in every new session |

More (speed, length, auto-Enter, say, setup) in [How it works](docs/how-it-works.md#commands).

## Troubleshooting

- **No sound:** run `/talkback status`. `installing Kokoro` means wait; `stopped` means look at `~/.talkback/voiced.log`.
- **`needs uv`:** the automatic download failed (see `~/.talkback/uv-install.log`). [Install uv](https://docs.astral.sh/uv/getting-started/installation/) and restart Claude Code.
- **Linux, no sound:** install an audio player (`pipewire`, `pulseaudio-utils` or `alsa-utils`).
- **macOS, Enter isn't pressed after Hex:** give your terminal Accessibility permission (System Settings, Privacy & Security).

## Uninstall

```sh
claude plugin uninstall talkback@talkback
pkill -x talkback-helper   # macOS only
rm -rf ~/.talkback
```

---

[How it works](docs/how-it-works.md) · MIT license
