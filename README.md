# talkback

Talk to Claude Code and hear it answer, all on your own machine. Works on macOS, Linux and Windows.

- **Hear replies** in a natural voice from [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M), running locally (on the GPU through MLX on Apple silicon, on the CPU with ONNX everywhere else). Code blocks, tables and long paths are skipped, and long replies are trimmed (or read in full if you prefer).
- **Talk back** with [Hex](https://github.com/anomalyco/hex) (or any dictation tool): hold the hotkey, speak, let go. On macOS, talkback presses Return for you, so the message sends itself.
- **Interrupt** by pressing the Hex hotkey (or sending any message): speech stops the moment the mic turns on.

Nothing leaves your machine: Hex and Kokoro both run locally.

## Install

```sh
claude plugin marketplace add Errr0rr404/talkback
claude plugin install talkback@talkback
```

Then, in a Claude Code session:

```
/talkback on
```

(In a headless `claude -p` session the command's full name is `/talkback:talkback`.)

The first time, talkback installs Kokoro in the background into `~/.talkback` (about 600 MB on Apple silicon, about 450 MB elsewhere), which takes a minute or two. You'll hear replies once it's ready.

### Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) on your `PATH`. talkback's hooks run through `uv`, which also supplies Python, so you don't need Python installed.
  - macOS: `brew install uv`
  - Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
  - Windows: `winget install astral-sh.uv` (or `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`)
- Linux: an audio player, any one of `pw-play` (PipeWire), `paplay` (PulseAudio), `aplay` (ALSA) or `ffplay`. Most desktops have one already.
- Optional: [Hex](https://github.com/anomalyco/hex) for voice input (`brew install --cask anomalyco/tap/hex` on macOS; installers for Windows and Linux on its releases page). Without it you still hear replies.

### What works where

| | macOS (Apple silicon) | macOS (Intel) | Linux | Windows |
|---|---|---|---|---|
| Spoken replies | MLX on the GPU | ONNX on the CPU | ONNX on the CPU | ONNX on the CPU |
| Stop speaking when you send a message | yes | yes | yes | yes |
| Stop speaking when the mic turns on (Hex hotkey) | yes | yes (needs Xcode tools to build the helper) | yes, with PulseAudio or PipeWire (`pactl`) | yes |
| Auto-Enter after Hex dictation | Terminal, iTerm2 | Terminal, iTerm2 | not yet | not yet |

Auto-Enter needs Accessibility permission for your terminal (System Settings, Privacy & Security, Accessibility); macOS asks the first time. It only presses Return when the front tab is running Claude Code, so dictated words never run as a shell command. On Linux and Windows there is no reliable way yet to tell which terminal tab has focus, so auto-Enter is off there: press Enter yourself.

## Commands

| Command | What it does |
|---|---|
| `/talkback on` / `off` | Spoken replies for this session (off by default, so other sessions stay quiet) |
| `/talkback status` | Current settings and whether the engine and helper are running |
| `/talkback full` | Read the last reply again, in full |
| `/talkback length full` / `length 700` | Read every reply in full, or up to N characters |
| `/talkback voice bf_emma` | Pick a [Kokoro voice](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md) (default `af_heart`) |
| `/talkback speed 1.3` | Speaking speed (default 1.1) |
| `/talkback autoenter off` | Stop pressing Return after Hex dictation |
| `/talkback default on` | Turn talkback on in every new session |
| `/talkback say <text>` | Speak any text |

From a shell: `npm test 2>&1 | tail -3 | uv run --no-project python <plugin>/bin/talkback.py speak`.

## How it works

- A `Stop` hook takes Claude's last reply, turns the Markdown into plain sentences and sends it to a small Kokoro service on `127.0.0.1` that stays loaded (on Apple silicon about 0.6 s to make 10 s of speech; it quits after an hour unused).
- A `UserPromptSubmit` hook stops speech when you send a message.
- On Linux and Windows the service itself stops speech when a microphone turns on (via `pactl` on Linux, Windows' microphone privacy records on Windows).
- On macOS, a tiny native helper (`helper/main.swift`, built on first use) does two things:
  - It watches for any microphone turning on, which is what happens when you press Hex's hotkey, and stops speech.
  - It watches Hex's history. After a dictation lands in Terminal or iTerm2, it presses Return, but only if the front tab is running Claude Code, so dictated words never run as a shell command.

State, logs and settings live in `~/.talkback/` (`voiced.log`, `helper.log`, `config.json`).

## Uninstall

```sh
claude plugin uninstall talkback@talkback
```

Then stop the background processes and delete `~/.talkback` (macOS and Linux: `kill $(cat ~/.talkback/helper.pid) 2>/dev/null; rm -rf ~/.talkback`; on Windows, delete `%USERPROFILE%\.talkback` after the speech service quits on its own, within an hour).

## Licenses

talkback is MIT. Kokoro-82M weights are Apache 2.0. Hex is MIT. Python dependencies are installed from PyPI at setup time under their own licenses (espeak-ng, used for rare-word pronunciation, is GPL-3.0).
