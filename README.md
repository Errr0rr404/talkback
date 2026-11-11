# talkback

Claude Code reads its replies aloud in a natural voice ([Kokoro](https://huggingface.co/hexgrad/Kokoro-82M)) that runs on your own machine. Works on macOS, Linux and Windows (Windows needs [Git for Windows](https://git-scm.com/download/win)).

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

talkback only reads replies aloud. To speak to Claude:

- **Signed in with claude.ai:** use Claude Code's built-in `/voice`. Nothing to install.
- **API key, Bedrock, Vertex, Foundry or a gateway, or you want your voice to stay on your Mac:** `/talkback setup` installs [Hex](https://github.com/kitlangton/Hex) (free, macOS). talkback then presses Enter when you let go of the hotkey and stops speech when you press it.

## Commands

| Command | What it does |
|---|---|
| `/talkback on` / `off` | Spoken replies for this session |
| `/talkback status` | Settings, and whether the voice is ready |
| `/talkback full` | Read the last reply again, in full |
| `/talkback length 1500` | How much of a reply is read (characters, `60s`, or `full`; default 700) |
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
