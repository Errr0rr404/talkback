# talkback

Talk to Claude Code and hear it answer, all on your Mac.

- **Hear replies** in a natural voice from [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M), running locally on the Apple GPU (MLX). Code blocks, tables and long paths are skipped, and long replies are trimmed (or read in full if you prefer).
- **Talk back** with [Hex](https://github.com/anomalyco/hex): hold the hotkey, speak, let go. talkback presses Return for you, so the message sends itself.
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

The first time, talkback installs Kokoro in the background (about 600 MB into `~/.talkback`), which takes a minute or two. You'll hear replies once it's ready.

### Requirements

- macOS on Apple silicon
- [uv](https://docs.astral.sh/uv/) (`brew install uv`): installs Kokoro's Python environment
- Optional: [Hex](https://github.com/anomalyco/hex) (`brew install --cask anomalyco/tap/hex`) for voice input. Without it you still hear replies.
- For auto-Enter: Terminal or iTerm2, with Accessibility allowed for that terminal (System Settings, Privacy & Security, Accessibility). macOS asks the first time.

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

From a shell: `npm test 2>&1 | tail -3 | python3 <plugin>/bin/talkback.py speak`.

## How it works

- A `Stop` hook takes Claude's last reply, turns the Markdown into plain sentences and sends it to a small Kokoro service that stays loaded (about 0.6 s to make 10 s of speech; it quits after an hour unused).
- A `UserPromptSubmit` hook stops speech when you send a message.
- A tiny native helper (`helper/main.swift`, built on first use) does two things:
  - It watches for any microphone turning on, which is what happens when you press Hex's hotkey, and stops speech.
  - It watches Hex's history. After a dictation lands in Terminal or iTerm2, it presses Return, but only if the front tab is running Claude Code, so dictated words never run as a shell command.

State, logs and settings live in `~/.talkback/` (`voiced.log`, `helper.log`, `config.json`).

## Uninstall

```sh
claude plugin uninstall talkback@talkback
kill $(cat ~/.talkback/helper.pid) 2>/dev/null; rm -rf ~/.talkback
```

## Licenses

talkback is MIT. Kokoro-82M weights are Apache 2.0. Hex is MIT. Python dependencies are installed from PyPI at setup time under their own licenses (espeak-ng, used for rare-word pronunciation, is GPL-3.0).
