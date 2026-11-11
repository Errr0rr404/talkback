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

# True when uv-failed is younger than an hour (talkback.py's ensure_uv uses the same wait).
uv_failed_recently() {
  [ -f "$state/uv-failed" ] || return 1
  now=$(date +%s)
  if mtime=$(stat -c %Y "$state/uv-failed" 2>/dev/null); then
    :
  else
    mtime=$(stat -f %m "$state/uv-failed" 2>/dev/null) || return 1
  fi
  [ $((now - mtime)) -lt 3600 ]
}

# Internal, only from the locked background starter below. Same markers as ensure_uv:
# uv-starting.json while it runs, uv-failed if no binary appears. The caller holds uv.lock.
if [ "$1" = "_uv_install" ]; then
  trap 'rm -f "$state/uv-starting.json"' EXIT
  if [ -x "$state/uv/uv" ] || [ -x "$state/uv/uv.exe" ]; then
    rm -f "$state/uv-failed"
    exit 0
  fi
  if uv_failed_recently; then
    exit 0
  fi
  mkdir -p "$state"
  printf '{"since":%s}\n' "$(date +%s)" >"$state/uv-starting.json"
  UV_UNMANAGED_INSTALL="$state/uv" UV_NO_MODIFY_PATH=1 \
    curl -LsSf https://astral.sh/uv/install.sh | sh >"$state/uv-install.log" 2>&1
  if [ -x "$state/uv/uv" ] || [ -x "$state/uv/uv.exe" ]; then
    rm -f "$state/uv-failed"
  else
    : >"$state/uv-failed"
  fi
  exit 0
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

# Neither: one session-start fetches uv in the background (Astral's installer, no PATH or profile changes).
# uv.lock is the same file talkback.py's FileLock uses, so a shell starter and ensure_uv can't overlap.
if [ "$1" = "hook" ] && [ "$2" = "session-start" ]; then
  mkdir -p "$state"
  if uv_failed_recently; then
    exit 0
  fi
  if ! command -v curl >/dev/null 2>&1; then
    printf '%s\n' "talkback: curl not found, so uv can't be downloaded." >"$state/uv-install.log"
    : >"$state/uv-failed"
    exit 0
  fi
  (
    if command -v flock >/dev/null 2>&1; then
      exec flock "$state/uv.lock" /bin/sh "$0" _uv_install
    elif command -v perl >/dev/null 2>&1; then
      # Clear FD_CLOEXEC or perl drops the lock on exec (Perl sets it by default).
      exec perl -e 'use Fcntl qw(:flock F_SETFD); open my $fh, ">>", shift or die $!; flock($fh, LOCK_EX) or die $!; fcntl($fh, F_SETFD, 0) or die $!; exec @ARGV or die $!' \
        "$state/uv.lock" /bin/sh "$0" _uv_install
    else
      exec /bin/sh "$0" _uv_install
    fi
  ) </dev/null >/dev/null 2>&1 &
  exit 0
fi
[ "$1" = "hook" ] && exit 0
# Nothing will read the saved words. Remove them before telling the user to retry.
if [ "$1" = "--args-file" ] && [ -n "$2" ]; then
  rm -f "$2"
fi
if [ -f "$state/uv-failed" ]; then
  echo "talkback needs uv: the automatic install failed (see $state/uv-install.log). Install it yourself: https://docs.astral.sh/uv/getting-started/installation/"
else
  echo "talkback is installing uv in the background; try again in a minute. If that doesn't help, install uv: https://docs.astral.sh/uv/getting-started/installation/"
fi
