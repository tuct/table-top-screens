#!/usr/bin/env python3
"""Install the local AI video stack: ComfyUI, its node packs, and the models.

The repository carries none of that. It carries this script and a manifest of
what to fetch, pinned to exact commits and file hashes, so a machine set up
today and one set up in six months end up with the same stack -- which matters
because ComfyUI and the AnimateDiff nodes both move fast, and "it worked on the
Mac" is not a useful thing to tell someone on Windows.

Everything here is idempotent. Re-running repairs a half-finished install
rather than starting over, and an interrupted download resumes where it
stopped instead of beginning again from zero.

    python bootstrap.py                 install or repair
    python bootstrap.py --no-models     code only, skip the weights
    python bootstrap.py --extras        also fetch LTX-Video and SVD (~20 GB)
    python bootstrap.py --update        bump the pins to current upstream
    python bootstrap.py --dir D         install somewhere other than ~/ComfyUI

macOS is tested. Windows is written but UNTESTED -- see README.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.toml"

IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"


# ---------------------------------------------------------------- reporting

class Reporter:
    """Progress output. Quiet by default; every step says what it decided."""

    def say(self, msg: str) -> None:
        print(f"\n\033[1m==> {msg}\033[0m" if not IS_WINDOWS else f"\n==> {msg}")

    def info(self, msg: str) -> None:
        print(f"    {msg}")

    def warn(self, msg: str) -> None:
        print(f"    ! {msg}")


R = Reporter()


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"\nerror: {msg}", file=sys.stderr)
    raise SystemExit(1)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a command, failing loudly with the command line that broke."""
    try:
        return subprocess.run(cmd, check=True, **kw)
    except FileNotFoundError:
        die(f"{cmd[0]} not found on PATH")
    except subprocess.CalledProcessError as e:
        die(f"command failed ({e.returncode}): {' '.join(cmd)}")


# ------------------------------------------------------------------- layout

def venv_python(comfy: Path) -> Path:
    """Where the venv puts its interpreter, which differs by platform."""
    return comfy / ".venv" / ("Scripts" if IS_WINDOWS else "bin") / \
        ("python.exe" if IS_WINDOWS else "python")


def find_uv() -> str:
    """uv owns the Python version, so the system Python never has to match."""
    found = shutil.which("uv")
    if found:
        return found
    # The installers put it here but may not have updated PATH in this shell.
    for cand in (Path.home() / ".local" / "bin" / "uv",
                 Path.home() / ".cargo" / "bin" / "uv",
                 Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin" / "uv.exe"):
        if cand.exists():
            return str(cand)
    die("uv not found. Run install.sh (macOS/Linux) or install.ps1 (Windows), "
        "or install it from https://docs.astral.sh/uv/")


# ------------------------------------------------------------------ fetching

def git_sync(repo: str, commit: str | None, dest: Path, name: str) -> None:
    """Clone at a pin, or move an existing checkout onto it.

    A full clone rather than --depth 1: a shallow clone cannot check out an
    arbitrary older commit, which is exactly what a pin asks for.
    """
    if (dest / ".git").exists():
        head = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        if commit and head == commit:
            R.info(f"{name} at pin")
            return
        R.info(f"{name}: fetching")
        run(["git", "-C", str(dest), "fetch", "--tags", "origin"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        R.info(f"{name}: cloning")
        dest.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", repo, str(dest)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if commit:
        R.info(f"{name}: checking out {commit[:10]}")
        try:
            run(["git", "-C", str(dest), "checkout", "--quiet", commit],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except SystemExit:
            die(f"{name}: commit {commit} not found. Upstream may have "
                f"force-pushed; re-run with --update to re-pin.")


def download(url: str, target: Path, sha256: str | None, label: str) -> None:
    """Fetch to <target>.part and move on success, resuming if interrupted.

    The part-file dance matters: a truncated file at the final name looks
    complete to the next run, and the failure then shows up much later as a
    confusing model-loading error.
    """
    if target.exists() and target.stat().st_size > 0:
        if sha256 and not verify(target, sha256, label):
            R.warn(f"{label}: hash mismatch, re-downloading")
            target.unlink()
        else:
            R.info(f"have {label}")
            return

    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0

    req = urllib.request.Request(url, headers={"User-Agent": "tabletop-bootstrap"})
    if have:
        req.add_header("Range", f"bytes={have}-")

    try:
        resp = urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        if have and e.code in (416, 200):      # stale or unsupported resume
            part.unlink(missing_ok=True)
            have = 0
            resp = urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": "tabletop-bootstrap"}))
        else:
            die(f"{label}: download failed ({e.code} {e.reason})")

    if have and resp.status == 200:
        # Server ignored the Range header and restarted the file.
        part.unlink(missing_ok=True)
        have = 0

    total = int(resp.headers.get("Content-Length", 0)) + have
    done = have
    R.info(f"downloading {label}" + (f" (resuming at {have >> 20} MB)" if have else ""))

    with open(part, "ab" if have else "wb") as fh:
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                print(f"\r      {pct:3d}%  {done >> 20:,} / {total >> 20:,} MB",
                      end="", flush=True)
    print()

    part.replace(target)
    if sha256 and not verify(target, sha256, label):
        target.unlink()
        die(f"{label}: hash mismatch after download. The URL may have changed "
            f"upstream; re-run with --update to re-record hashes.")


def verify(path: Path, expected: str, label: str) -> bool:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 22):
            h.update(chunk)
    return h.hexdigest() == expected


# -------------------------------------------------------------------- torch

def torch_packages(cfg: dict) -> list[str]:
    """The right torch build for this machine.

    Apple Silicon gets the plain PyPI wheels, which carry MPS. Anything with an
    NVIDIA card gets the CUDA index. Everything else gets CPU wheels and a
    warning, because CPU diffusion is slow enough to feel broken.
    """
    if IS_MACOS:
        if platform.machine() != "arm64":
            R.warn("Intel Mac: no MPS, generation will run on CPU and be very slow")
        return ["torch", "torchvision", "torchaudio"]

    if shutil.which("nvidia-smi"):
        index = cfg.get("cuda_index", "https://download.pytorch.org/whl/cu126")
        R.info(f"NVIDIA GPU detected; using {index}")
        return ["torch", "torchvision", "torchaudio", "--index-url", index]

    R.warn("no NVIDIA GPU detected; installing CPU-only torch. Generation will "
           "be very slow. If you do have one, make sure nvidia-smi is on PATH.")
    return ["torch", "torchvision", "torchaudio",
            "--index-url", cfg.get("cpu_index", "https://download.pytorch.org/whl/cpu")]


# ----------------------------------------------------------------- launcher

def write_launcher(comfy: Path) -> None:
    """A start script per platform, so nobody has to remember the flags."""
    if IS_WINDOWS:
        path = comfy / "start.cmd"
        # newline="" turns OFF text-mode translation. Without it every \r\n
        # here is written as \r\r\n, because text mode expands the \n a second
        # time on Windows -- verified with od -c on a generated start.cmd.
        path.write_text(
            "@echo off\r\n"
            "rem ComfyUI launcher.\r\n"
            "cd /d \"%~dp0\"\r\n"
            ".venv\\Scripts\\python.exe main.py --listen 127.0.0.1 --port 8188 %*\r\n",
            newline="")
    else:
        path = comfy / "start.sh"
        path.write_text(
            "#!/bin/sh\n"
            "# ComfyUI launcher. PYTORCH_ENABLE_MPS_FALLBACK lets the handful of\n"
            "# ops MPS lacks run on CPU instead of killing the sampler mid-run.\n"
            'cd "$(dirname "$0")"\n'
            "export PYTORCH_ENABLE_MPS_FALLBACK=1\n"
            'exec ./.venv/bin/python main.py --listen 127.0.0.1 --port 8188 "$@"\n')
        path.chmod(0o755)
    R.info(f"wrote {path.name}")


# ------------------------------------------------------------------- update

def update_pins() -> None:
    """Re-point the manifest at current upstream HEADs, for review and commit."""
    text = MANIFEST.read_text()
    data = tomllib.loads(text)

    targets = [("comfyui", data["comfyui"]["repo"], data["comfyui"]["commit"])]
    for pack in data.get("node_packs", []):
        targets.append((pack["name"], pack["repo"], pack["commit"]))

    changed = 0
    for name, repo, old in targets:
        out = subprocess.run(["git", "ls-remote", repo, "HEAD"],
                             capture_output=True, text=True)
        if out.returncode != 0 or not out.stdout.strip():
            R.warn(f"{name}: could not reach {repo}")
            continue
        new = out.stdout.split()[0]
        if new == old:
            R.info(f"{name}: unchanged")
            continue
        R.info(f"{name}: {old[:10]} -> {new[:10]}")
        text = text.replace(old, new)
        changed += 1

    if changed:
        MANIFEST.write_text(text)
        R.say(f"Updated {changed} pin(s) in manifest.toml")
        R.info("Review the diff, re-run bootstrap.py, test, then commit.")
    else:
        R.say("All pins current")


# --------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=Path.home() / "ComfyUI",
                    help="install location (default ~/ComfyUI)")
    ap.add_argument("--no-models", action="store_true",
                    help="skip the weights; install code only")
    ap.add_argument("--extras", action="store_true",
                    help="also fetch the extra models (LTX-Video, SVD): ~20 GB more")
    ap.add_argument("--update", action="store_true",
                    help="bump manifest pins to upstream HEAD and exit")
    args = ap.parse_args()

    if not MANIFEST.exists():
        die(f"manifest.toml not found next to {Path(__file__).name}")

    if args.update:
        update_pins()
        return

    data = tomllib.loads(MANIFEST.read_text())
    comfy = args.dir.expanduser().resolve()
    uv = find_uv()
    pyver = data.get("python", {}).get("version", "3.12")

    R.say("Prerequisites")
    R.info(f"platform {platform.system()} {platform.machine()}")
    if IS_WINDOWS:
        R.warn("Windows support is written but UNTESTED; please report what breaks")
    if not shutil.which("git"):
        die("git not found on PATH")
    R.info(f"uv {subprocess.run([uv, '--version'], capture_output=True, text=True).stdout.strip()}")
    R.info(f"fetching python {pyver}")
    run([uv, "python", "install", pyver], stdout=subprocess.DEVNULL)

    R.say("ComfyUI")
    git_sync(data["comfyui"]["repo"], data["comfyui"].get("commit"), comfy, "ComfyUI")

    if not venv_python(comfy).exists():
        R.info(f"creating venv on python {pyver}")
        run([uv, "venv", "--python", pyver, str(comfy / ".venv")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    env = {**os.environ, "VIRTUAL_ENV": str(comfy / ".venv")}
    pip = [uv, "pip", "install", "--quiet"]

    R.info("installing torch")
    run(pip + torch_packages(data.get("torch", {})), env=env)
    R.info("installing ComfyUI requirements")
    run(pip + ["-r", str(comfy / "requirements.txt")], env=env)
    # A bundled ffmpeg so video output works even with none on PATH.
    run(pip + ["imageio-ffmpeg"], env=env)

    R.say("Node packs")
    for pack in data.get("node_packs", []):
        dest = comfy / "custom_nodes" / pack["name"]
        git_sync(pack["repo"], pack.get("commit"), dest, pack["name"])
        reqs = dest / "requirements.txt"
        if reqs.exists():
            try:
                run(pip + ["-r", str(reqs)], env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except SystemExit:
                R.warn(f"{pack['name']}: some deps failed to install "
                       f"(often CUDA-only extras; usually harmless here)")

    if args.no_models:
        R.say("Models skipped (--no-models)")
    else:
        models = list(data.get("models", []))
        if args.extras:
            models += data.get("extra_models", [])
        total_mb = sum(m.get("size_mb", 0) for m in models)
        R.say(f"Models (~{total_mb/1024:.1f} GB total)")
        for m in models:
            download(m["url"], comfy / "models" / m["dir"] / m["name"],
                     m.get("sha256"), m["name"])
        if not args.extras and data.get("extra_models"):
            extra_mb = sum(m.get("size_mb", 0) for m in data["extra_models"])
            R.info(f"(--extras would add {len(data['extra_models'])} more, "
                   f"~{extra_mb/1024:.0f} GB: LTX-Video and SVD)")

    R.say("Scripts")
    tabletop = comfy / "tabletop"
    tabletop.mkdir(parents=True, exist_ok=True)
    for name in ("gen.py", "to_ui.py"):
        src = HERE / name
        if src.exists():
            shutil.copy2(src, tabletop / name)
            R.info(f"installed {name}")
        else:
            R.warn(f"{name} missing from {HERE}")
    write_launcher(comfy)

    R.say("Verifying")
    check = subprocess.run(
        [str(venv_python(comfy)), "-c",
         "import sys, torch;"
         "print('python', sys.version.split()[0]);"
         "print('torch', torch.__version__);"
         "import torch.backends.mps as m;"
         "print('mps', m.is_available());"
         "print('cuda', torch.cuda.is_available())"],
        capture_output=True, text=True)
    for line in check.stdout.strip().splitlines():
        R.info(line)
    if check.returncode != 0:
        R.warn(check.stderr.strip()[:400])

    launcher = comfy / ("start.cmd" if IS_WINDOWS else "start.sh")
    py = venv_python(comfy)
    R.say("Done")
    print(f"""    Start ComfyUI:   {launcher}
    Then open:       http://127.0.0.1:8188

    Generate a clip (ComfyUI must be running):
      {py} {tabletop / 'gen.py'} \\
        "a torchlit tavern at night, firelight flickering" --screen p4 --lcm

    Build the GUI workflows (ComfyUI must be running):
      {py} {tabletop / 'to_ui.py'}
""")


if __name__ == "__main__":
    main()
