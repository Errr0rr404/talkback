---
description: Spoken replies for this session — on | off | status | full | length <n|full> | voice <name> | speed <x> | autoenter on|off | default on|off | say <text> | setup [yes|skip]
argument-hint: "[on|off|status|full|length full|voice af_heart|speed 1.1|autoenter off|default on|setup]"
allowed-tools: "Bash(exec sh:*)"
---

```!
exec sh "${CLAUDE_PLUGIN_ROOT}/bin/run.sh" --from-stdin <<'TALKBACK_EOF'
$ARGUMENTS
TALKBACK_EOF
```

Reply with only the line above, verbatim. Do nothing else.
