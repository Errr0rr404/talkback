# How talkback works

## Pieces

- **Hooks** (`plugins/talkback/hooks/hooks.json`):
  - `Stop` takes Claude's last reply, turns the Markdown into plain sentences and sends it to the speech service.
  - `UserPromptSubmit` stops speech when you send a message.
  - `SessionStart` warms the engine for sessions where talkback is on.
- **Speech service** (`engine/voiced.py`): a small Kokoro process on `127.0.0.1` (port and token in `~/.talkback/voiced.json`).
  - It stays loaded between replies and quits after an hour unused.
  - On Apple silicon it makes about 10 s of speech in 0.6 s.
  - After a plugin update, the next reply replaces a service left over from the older version.
- **macOS helper** (`helper/main.swift`, built on first use; `talkback-helper-arm64` is a prebuilt copy for Macs without Xcode tools):
  - It stops speech when any microphone turns on, for example when you press Hex's hotkey.
  - It presses Return after a Hex dictation lands in Terminal or iTerm2, but only when Claude Code is the foreground program of the front tab, so dictated words never run as a shell command.
  - Only one copy runs at a time. It applies to every Claude Code tab once running.

The voice always runs on your machine. Claude Code's `/voice` sends your audio to Anthropic for transcription; Hex keeps it local.

## Commands

| Command | What it does |
|---|---|
| `/talkback on` / `off` | Turn spoken replies on or off for this session (`/talkback` alone toggles) |
| `/talkback status` | Show settings and whether the voice is ready; says what's missing if a download failed |
| `/talkback full` | Read the last reply again, in full |
| `/talkback length 1500` | Read up to 1500 characters per reply. Also `60s` or `2m` (about that long at the current speed), `full` (`all`, `0`) for whole replies; `length` alone shows the setting |
| `/talkback voice bf_emma` | Change the [voice](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md) (default `af_heart`) |
| `/talkback speed 1.3` | Speaking speed, 0.5 to 2 (default 1.1) |
| `/talkback default on` | Turn it on in every new session (not in headless `claude -p` runs; `/talkback on` there still works) |
| `/talkback autoenter off` | Stop pressing Enter after Hex |
| `/talkback say hello` | Speak any text |
| `/talkback setup` | macOS: offer to install Hex and open its permission panes (`setup yes` to go ahead, `setup skip` to decline) |

What gets read: code blocks, tables and long inline code are skipped, links become their label and long paths their file name. Replies past the length limit (700 characters by default) are cut at a sentence end and speech ends with "The rest was trimmed."; the first time in a session, Claude Code also shows a note on how to change it. If only code or a table was left out, speech ends with "Code skipped."

The words after `/talkback` are passed to talkback through a quoted heredoc, never through a shell parse, so quotes, `$HOME` or `$(...)` in `/talkback say` are spoken as written. The command's `allowed-tools` permits only `sh "${CLAUDE_PLUGIN_ROOT}/bin/run.sh" --from-stdin`.

## Talking to Claude

talkback only reads replies aloud. To talk to Claude, use either (Hex is optional; replies are read aloud without it):

- **Claude Code's `/voice`** (built in, for claude.ai logins, nothing to install). Run `/voice tap`, then tap Space, speak, tap Space: your message sends itself. It doesn't work with Amazon Bedrock, Google Cloud, Microsoft Foundry, an API key or a company gateway.
- **[Hex](https://github.com/kitlangton/Hex)** if `/voice` isn't available to you, or to keep your voice on your machine. On macOS, talkback presses Enter for you when you release Hex's hotkey (Terminal and iTerm2).

Hex is a macOS app that needs your Microphone and Accessibility permissions, so talkback never installs it behind your back. `/talkback setup` explains what it will do; `/talkback setup yes` downloads Hex's latest release into `/Applications` and opens the two permission panes; `/talkback setup skip` declines. Nothing happens on Linux and Windows.

## What works where

| | macOS (Apple silicon) | macOS (Intel) | Linux | Windows |
|---|---|---|---|---|
| Engine | MLX on the GPU | ONNX on the CPU | ONNX on the CPU | ONNX on the CPU |
| Stop when you send a message | yes | yes | yes | yes |
| Stop when the mic turns on | yes | yes (needs Xcode tools) | yes, via `pactl` (PulseAudio/PipeWire) | yes, via Windows' microphone privacy records |
| Enter after Hex dictation | Terminal, iTerm2 | Terminal, iTerm2 | no | no |

On Linux and Windows there's no reliable way yet to tell which terminal tab has focus, so auto-Enter is off there.

## First run

The first `SessionStart` after the plugin install (or `/talkback on`) starts a background job, `talkback.py _warm`, that chains:
1. **uv.** Hooks run through `bin/run.sh`, which uses uv if it finds one (PATH, `~/.talkback/uv`, `~/.local/bin`, Homebrew, `~/.cargo/bin`), otherwise any Python 3.8+, otherwise (a session-start hook only) starts Astral's installer itself, holding `uv.lock` and leaving the same `uv-starting.json` / `uv-failed` markers. `talkback.py` then runs Astral's standalone installer (`curl ... install.sh | sh`, or the PowerShell script on Windows) with `UV_UNMANAGED_INSTALL=~/.talkback/uv` and `UV_NO_MODIFY_PATH=1`, so there's no `sudo`, no PATH or profile edit, and uv is used by absolute path. A failure leaves `~/.talkback/uv-failed` (retried by hooks after an hour, or at once by `/talkback on`) and `/talkback status` says what's missing.
2. **Kokoro.** uv installs the Python libraries into `~/.talkback/venv`, and the service downloads the voice model: from Hugging Face on Apple silicon, from the kokoro-onnx GitHub release elsewhere.

What the first run puts on disk:

| What | Size | Where it goes |
|---|---|---|
| [uv](https://docs.astral.sh/uv/), only if you don't have it (Astral's official installer; PATH and shell profile untouched) | about 20 MB | `~/.talkback/uv` |
| Python libraries and the Kokoro voice | a few hundred MB | `~/.talkback/venv` and the model cache (`~/.talkback/models` or the Hugging Face cache) |
| Hex (macOS only, optional, asked first) | about 15 MB | `/Applications` |

Setup takes a minute or two, longer on a slow connection. While that runs, `/talkback status` shows `installing uv` or `installing Kokoro`, and a reply that finishes early shows a note that it will be spoken once ready.

**Hex** is never installed from a hook. On macOS the first-run message mentions `/talkback setup`; `setup yes` downloads the `.dmg` from the latest release of kitlangton/Hex (it has no Homebrew cask), copies `Hex.app` to `/Applications`, and opens the Microphone and Accessibility panes.

## Files

`~/.talkback/` holds:
- `config.json`: settings.
- `uv/`: uv, when talkback had to install it. `uv-install.log`: the installer's output.
- `venv/`: virtual environment with Kokoro runtime dependencies.
- `models/`: ONNX voice models (`kokoro-v1.0.onnx`, `voices-v1.0.bin`) on Linux, Windows, and Intel Macs.
- `sessions/`: one on/off file per session, pruned after two weeks unused.
- `voiced.json`: socket address and auth token for the running daemon.
- `bin/`: macOS helper binary (`talkback-helper`).
- `voiced.log` and `helper.log`: the logs.

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

## Shell use

```sh
npm test 2>&1 | tail -3 | uv run --no-config --no-project python <plugin>/bin/talkback.py speak
```

## Development

```sh
git clone https://github.com/Errr0rr404/talkback && cd talkback
python3 -m unittest discover -s tests  # or: uv run --no-config --no-project python -m unittest discover -s tests
claude plugin marketplace add "$PWD" && claude plugin install talkback@talkback
```

The plugin is in `plugins/talkback/`:
- `bin/run.sh`: the launcher the hooks and `/talkback` go through.
- `bin/talkback.py`: the hooks and the command. Standard library only, Python 3.8+.
- `engine/`: the speech service and its locked dependencies.
- `helper/`: the macOS helper.

After a change, bump `version` in `plugins/talkback/.claude-plugin/plugin.json` and run `claude plugin update talkback@talkback`.

## Licenses

talkback is MIT. Kokoro-82M weights are Apache 2.0. Hex is MIT. Python dependencies come from PyPI under their own licenses (espeak-ng, used for rare words, is GPL-3.0).
