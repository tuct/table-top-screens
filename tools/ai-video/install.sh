#!/bin/sh
# macOS / Linux entry point.
#
# Its only job is to make sure `uv` exists and then hand over to bootstrap.py,
# which holds the actual install logic and is shared with Windows. Keeping this
# thin is deliberate: logic duplicated across a shell script and a PowerShell
# script drifts, and you find out on the machine you use least.
#
# Any arguments are passed straight through, e.g. --no-models, --update.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  echo "==> installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # The installer appends to the shell profile, which this shell has not read.
  PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  export PATH
fi

command -v uv >/dev/null 2>&1 || {
  echo "uv installed but not on PATH. Open a new terminal and re-run." >&2
  exit 1
}

# uv supplies the interpreter too, so no system Python is required.
exec uv run --python 3.12 "$HERE/bootstrap.py" "$@"
