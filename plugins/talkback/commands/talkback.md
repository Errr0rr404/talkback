---
description: Spoken replies for this session — on | off | status | full | length <n|full> | voice <name> | speed <x> | autoenter on|off | default on|off | say <text>
argument-hint: "[on|off|status|full|length full|voice af_heart|speed 1.1|autoenter off|default on]"
allowed-tools: Bash(uv run:*)
---

!`uv run --no-project --quiet python "${CLAUDE_PLUGIN_ROOT}/bin/talkback.py" $ARGUMENTS`

Reply with only the line above, verbatim. Do nothing else.
