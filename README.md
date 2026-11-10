# talkback

Hear Claude Code read its replies aloud, in a natural voice ([Kokoro](https://huggingface.co/hexgrad/Kokoro-82M)) that runs on your own machine. macOS, Linux and Windows.

- Code, tables and long paths are skipped; long replies are trimmed.
- Speech stops as soon as you start talking or send a message.

## Install

```sh
claude plugin marketplace add Errr0rr404/talkback
claude plugin install talkback@talkback
```

That's the whole install. The first time Claude Code starts a session, talkback sets up everything else in the background (no `sudo`, no Homebrew, nothing blocks Claude Code):

| What | Size | Where it goes |
|---|---|---|
| [uv](https://docs.astral.sh/uv/), only if you don't have it (Astral's official installer; your PATH and shell profile are left alone) | about 20 MB | `~/.talkback/uv` |
| Python libraries and the [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) voice | a few hundred MB | `~/.talkback/venv` and the model cache (`~/.talkback/models` or Hugging Face cache), installed by uv |
| [Hex](https://github.com/kitlangton/Hex) (macOS only, optional, asked first; see below) | about 15 MB | `/Applications` |

Then in Claude Code:

```
/talkback on
```

If that says *Unknown command* or *Unknown skill*, use the full name: `/talkback:talkback on`. (Still unknown? Run `/reload-plugins`.)

Setup takes a minute or two (longer on a slow connection). `/talkback status` shows progress, and tells you what's missing and how to fix it if a download fails.

## Talking to Claude

talkback does the listening part. To talk, use either:

- **Claude Code's `/voice`** (built in, needs a claude.ai login). Run `/voice tap`, then tap Space, speak, tap Space: your message sends itself.
- **[Hex](https://github.com/kitlangton/Hex)** if you use Claude Code through Amazon Bedrock, Google Cloud, Microsoft Foundry, an API key or a company gateway (`/voice` doesn't work there), or if you want your voice to stay on your machine. On macOS, talkback presses Enter for you when you release Hex's hotkey (Terminal and iTerm2).

Hex is a macOS app that needs your Microphone and Accessibility permissions, so talkback never installs it behind your back. Run `/talkback setup`: it explains what it will do, and `/talkback setup yes` downloads Hex's latest release into `/Applications` and opens the two permission panes. `/talkback setup skip` declines. Nothing happens on Linux and Windows.

## Commands

| Command | What it does |
|---|---|
| `/talkback on` / `off` | Turn spoken replies on or off for this session (running `/talkback` alone toggles) |
| `/talkback status` | Show settings and whether the voice is ready |
| `/talkback full` | Read the last reply again, in full |
| `/talkback length full` | Always read whole replies (`full`, `all`, or `0`; `length 700` to trim again) |
| `/talkback voice bf_emma` | Change the [voice](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md) (default `af_heart`) |
| `/talkback speed 1.3` | Speaking speed, 0.5 to 2 |
| `/talkback default on` | Turn it on in every new session |
| `/talkback autoenter off` | Stop pressing Enter after Hex |
| `/talkback say hello` | Speak any text |
| `/talkback setup` | macOS: offer to install Hex and open its permission panes (`yes` to go ahead, `skip` to decline) |

## Troubleshooting

- **No sound:** run `/talkback status`. If it says `installing Kokoro`, wait. If it says `stopped`, see `~/.talkback/voiced.log`. If it says `running`, check your volume and try `/talkback say hello`.
- **`status` says `needs uv`:** the automatic uv install failed (no internet, or no `curl`/`wget`); see `~/.talkback/uv-install.log`. Install uv yourself ([instructions](https://docs.astral.sh/uv/getting-started/installation/)), then restart Claude Code. `/talkback on` also tries the download again.
- **Linux, no sound:** install an audio player (`pipewire`, `pulseaudio-utils` or `alsa-utils`).
- **macOS, Enter isn't pressed after Hex:** give your terminal Accessibility permission (System Settings, Privacy & Security, Accessibility).

## Uninstall

```sh
claude plugin uninstall talkback@talkback
pkill -x talkback-helper   # macOS only
rm -rf ~/.talkback
```

`~/.talkback` holds the settings, the Kokoro environment uv built and the uv talkback installed for itself, so removing it removes all of those. A uv you installed yourself (Homebrew, `~/.local/bin`, winget) is left in place, and so is Hex: drag Hex to the Trash if you don't want it. The background voice service quits by itself within an hour.

---

[How it works](docs/how-it-works.md) · MIT license · Kokoro-82M is Apache 2.0, Hex is MIT
