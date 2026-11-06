# talkback

Hear Claude Code read its replies aloud, in a natural voice ([Kokoro](https://huggingface.co/hexgrad/Kokoro-82M)) that runs on your own machine. macOS, Linux and Windows.

- Code, tables and long paths are skipped; long replies are trimmed.
- Speech stops as soon as you start talking or send a message.

## Install

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) (`brew install uv`, or `winget install astral-sh.uv` on Windows). talkback uses it to install everything else, Kokoro included.

```sh
claude plugin marketplace add Errr0rr404/talkback
claude plugin install talkback@talkback
```

Then in Claude Code:

```
/talkback on
```

If that says *Unknown command* or *Unknown skill*, use the full name: `/talkback:talkback on`. (Still unknown? Run `/reload-plugins`.)

The first time, Kokoro downloads in the background (a few hundred MB, a minute or two). `/talkback status` shows `installing Kokoro` until it's ready.

## Talking to Claude

talkback does the listening part. To talk, use either:

- **Claude Code's `/voice`** (built in, needs a claude.ai login). Run `/voice tap`, then tap Space, speak, tap Space: your message sends itself.
- **[Hex](https://github.com/anomalyco/hex)** if you want your voice to stay on your machine. On macOS, talkback presses Enter for you when you release Hex's hotkey (Terminal and iTerm2).

## Commands

| Command | What it does |
|---|---|
| `/talkback on` / `off` | Turn spoken replies on or off for this session |
| `/talkback status` | Show settings and whether the voice is ready |
| `/talkback full` | Read the last reply again, in full |
| `/talkback length full` | Always read whole replies (`length 700` to trim again) |
| `/talkback voice bf_emma` | Change the [voice](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md) (default `af_heart`) |
| `/talkback speed 1.3` | Speaking speed, 0.5 to 2 |
| `/talkback default on` | Turn it on in every new session |
| `/talkback autoenter off` | Stop pressing Enter after Hex |
| `/talkback say hello` | Speak any text |

## Troubleshooting

- **No sound:** run `/talkback status`. If it says `installing Kokoro`, wait. If it says `stopped`, see `~/.talkback/voiced.log`. If it says `running`, check your volume and try `/talkback say hello`.
- **Errors about `uv`:** install uv, then restart Claude Code.
- **Linux, no sound:** install an audio player (`pipewire`, `pulseaudio-utils` or `alsa-utils`).
- **macOS, Enter isn't pressed after Hex:** give your terminal Accessibility permission (System Settings, Privacy & Security, Accessibility).

## Uninstall

```sh
claude plugin uninstall talkback@talkback
pkill -x talkback-helper   # macOS only
rm -rf ~/.talkback
```

The background voice service quits by itself within an hour.

---

[How it works](docs/how-it-works.md) · MIT license · Kokoro-82M is Apache 2.0, Hex is MIT
