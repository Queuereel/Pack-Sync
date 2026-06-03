#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8")
    _sys.stderr.reconfigure(encoding="utf-8")
"""
release_pack_sync.py — Build Pack Sync for ALL platforms in one click.

Pushes a version tag to GitHub, which triggers the CI workflow to build:
  • Windows x64     → PackSync.exe
  • Windows ARM64   → PackSync.exe
  • macOS ARM64     → PackSync.dmg
  • macOS Intel     → PackSync.dmg
  • Linux x64       → PackSync.deb

All five builds are collected into a GitHub Release automatically.

Usage:
    python release_pack_sync.py              # auto-increments patch version
    python release_pack_sync.py 1.2.0        # specific version
    python release_pack_sync.py --dry-run    # preview only, no tag pushed
"""

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT  = Path(__file__).parent
REMOTE_URL = "https://github.com/Queuereel/PixelArtTexture-Generator"
TAG_PREFIX = "pack-sync-v"

# ── Helpers ───────────────────────────────────────────────────────────────────
def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT, **kw)

def die(msg: str):
    print(f"\n  ✗  {msg}\n", file=sys.stderr); sys.exit(1)

def header(msg: str):
    print(f"\n  {'─'*60}\n  {msg}\n  {'─'*60}")

# ── Parse args ────────────────────────────────────────────────────────────────
args     = sys.argv[1:]
dry_run  = "--dry-run" in args
version_arg = next((a for a in args if re.match(r"^\d+\.\d+\.\d+$", a)), None)

# ── Git sanity checks ─────────────────────────────────────────────────────────
header("Pack Sync — Release Builder")

status = run(["git", "status", "--porcelain"])
if status.stdout.strip():
    print("  Uncommitted changes detected:")
    for line in status.stdout.strip().splitlines():
        print(f"    {line}")
    ans = input("\n  Continue anyway? (y/N): ").strip().lower()
    if ans != "y":
        die("Aborted. Commit or stash your changes first.")

branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
print(f"  Branch:  {branch}")

# ── Determine version ─────────────────────────────────────────────────────────
existing = run(["git", "tag", "--list", f"{TAG_PREFIX}*"]).stdout.strip().splitlines()
existing_versions = sorted(
    [t.removeprefix(TAG_PREFIX) for t in existing if t.startswith(TAG_PREFIX)],
    key=lambda v: [int(x) for x in v.split(".")]
)

if version_arg:
    new_version = version_arg
elif existing_versions:
    last = existing_versions[-1]
    major, minor, patch = (int(x) for x in last.split("."))
    new_version = f"{major}.{minor}.{patch + 1}"
    print(f"  Last tag: {TAG_PREFIX}{last}")
else:
    new_version = "1.0.0"

tag = f"{TAG_PREFIX}{new_version}"

if existing_versions:
    print(f"  All tags: {', '.join(TAG_PREFIX + v for v in existing_versions[-5:])}")

print(f"\n  New tag:  {tag}")

if not version_arg and not dry_run:
    ans = input(f"  Use version {new_version}? (Y/n): ").strip().lower()
    if ans == "n":
        new_version = input("  Enter version (e.g. 1.2.0): ").strip()
        if not re.match(r"^\d+\.\d+\.\d+$", new_version):
            die("Invalid version format. Use MAJOR.MINOR.PATCH")
        tag = f"{TAG_PREFIX}{new_version}"

# ── Confirm ───────────────────────────────────────────────────────────────────
print(f"""
  This will:
    1. Create git tag     {tag}
    2. Push tag to GitHub
    3. GitHub Actions builds on 5 platforms simultaneously (~8 min)
    4. Creates GitHub Release with all 5 executables attached

  Release URL (after build):
    {REMOTE_URL}/releases/tag/{tag}

  Monitor progress:
    {REMOTE_URL}/actions
""")

if dry_run:
    print("  DRY RUN — nothing was pushed.\n")
    sys.exit(0)

ans = input("  Push tag and start build? (y/N): ").strip().lower()
if ans != "y":
    die("Aborted.")

# ── Tag and push ──────────────────────────────────────────────────────────────
print(f"\n  Creating tag {tag} ...")
r = run(["git", "tag", tag])
if r.returncode != 0:
    die(f"Could not create tag:\n{r.stderr.strip()}")

print(f"  Pushing tag to GitHub ...")
r = run(["git", "push", "origin", tag])
if r.returncode != 0:
    # Try to clean up the local tag
    run(["git", "tag", "-d", tag])
    die(f"Push failed:\n{r.stderr.strip()}")

# ── Done ──────────────────────────────────────────────────────────────────────
print(f"""
  ✓  Tag pushed!  GitHub Actions is now building Pack Sync for all platforms.

  Monitor:   {REMOTE_URL}/actions
  Release:   {REMOTE_URL}/releases/tag/{tag}

  Builds take ~8 minutes.  Once done, the Release page will have:
    PackSync.exe         (Windows x64)
    PackSync.exe         (Windows ARM64)
    PackSync.dmg         (macOS Apple Silicon)
    PackSync.dmg         (macOS Intel)
    PackSync.deb         (Linux x64)
""")

# Try to open the Actions page in the browser
try:
    import webbrowser
    webbrowser.open(f"{REMOTE_URL}/actions")
except Exception:
    pass
