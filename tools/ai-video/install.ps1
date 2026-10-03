# Windows entry point.  UNTESTED -- see README.
#
# Mirrors install.sh: ensure uv is present, then hand over to bootstrap.py,
# which holds all the real logic so the two platforms cannot drift apart.
#
# If PowerShell refuses to run this, it is the execution policy, not the script:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# Any arguments are passed through, e.g. --no-models, --update.

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "==> installing uv"
    irm https://astral.sh/uv/install.ps1 | iex
    # The installer updates the user PATH, which this process has already read.
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv installed but not on PATH. Open a new terminal and re-run."
    exit 1
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "git not found. Install it from https://git-scm.com and re-run."
    exit 1
}

# uv supplies the interpreter too, so no system Python is required.
& uv run --python 3.12 "$Here\bootstrap.py" @args
exit $LASTEXITCODE
