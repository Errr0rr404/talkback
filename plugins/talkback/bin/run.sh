#!/bin/sh
# Launcher for talkback's hooks and /talkback command: runs talkback.py with uv, or with a system Python if uv
# isn't there yet, and starts the uv install in the background when neither exists. Never blocks, never uses sudo.
here=${0%/*}
py="$here/talkback.py"
state="${TALKBACK_HOME:-$HOME/.talkback}"

# /talkback's words arrive on stdin. Save them before anything else reads stdin, and pass a path
# so the shell never word-splits, expands, or redirects them.
if [ "$1" = "--from-stdin" ]; then
  shift
  args_file=$(mktemp "${TMPDIR:-/tmp}/talkback-args.XXXXXX") || exit 1
  cat > "$args_file" || { rm -f "$args_file"; exit 1; }
  set -- --args-file "$args_file" "$@"
fi

uv=$(command -v uv 2>/dev/null)
if [ -z "$uv" ]; then
  for c in "$state/uv/uv" "$state/uv/uv.exe" "$HOME/.local/bin/uv" "$HOME/.local/bin/uv.exe" \
           /opt/homebrew/bin/uv /usr/local/bin/uv "$HOME/.cargo/bin/uv"; do
    if [ -x "$c" ]; then uv=$c; break; fi
  done
fi
if [ -n "$uv" ]; then
  exec "$uv" run --no-project --quiet python "$py" "$@"
fi

# No uv yet: talkback.py is standard-library only, so any Python 3.8+ can run it (it installs uv itself).
for p in python3 python; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; then
    exec "$p" "$py" "$@"
  fi
done

# Neither: fetch uv with Astral's installer, in the background, into ~/.talkback/uv (no PATH or profile changes).
if [ "$1" = "hook" ] && [ "$2" = "session-start" ] && command -v curl >/dev/null 2>&1; then
  mkdir -p "$state"
  ( UV_UNMANAGED_INSTALL="$state/uv" UV_NO_MODIFY_PATH=1 \
      curl -LsSf https://astral.sh/uv/install.sh | sh ) >"$state/uv-install.log" 2>&1 </dev/null &
  exit 0
fi
[ "$1" = "hook" ] && exit 0
echo "talkback is installing uv in the background; try again in a minute. If that doesn't help, install uv: https://docs.astral.sh/uv/getting-started/installation/"
