#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys; sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None
"""
build.py — Build Pack Sync for the current platform.
Produces a self-contained executable in dist/<platform>/

Usage:
    python build.py
"""
import os, platform, shutil, subprocess, sys
from pathlib import Path

HERE   = Path(__file__).parent
SCRIPT = HERE / "pack_sync.py"

MACHINE = platform.machine().lower()   # amd64 / arm64 / aarch64 / x86_64
PLAT    = sys.platform                 # win32 / darwin / linux

if PLAT == "win32":
    folder = "windows-arm64" if MACHINE in ("arm64", "aarch64") else "windows-x64"
elif PLAT == "darwin":
    folder = "macos-arm64" if MACHINE in ("arm64", "aarch64") else "macos-x64"
else:
    folder = "linux-x64"

DIST      = HERE / "dist" / folder
BUILD_TMP = DIST / "_build"
DIST.mkdir(parents=True, exist_ok=True)
BUILD_TMP.mkdir(parents=True, exist_ok=True)

EXE = "PackSync.exe" if PLAT == "win32" else "PackSync"
print(f"Building Pack Sync  →  dist/{folder}/{EXE}")
print(f"Platform: {sys.platform} / {MACHINE} / Python {sys.version.split()[0]}\n")

def pip(*args):
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "--quiet", *args])

# ── Install runtime deps (needed by PyInstaller to bundle them) ──────────────
runtime = ["pystray", "Pillow", "pyinstaller"]
if PLAT != "win32":
    runtime.append("watchdog")
print(f"Checking deps: {', '.join(runtime)} …")
pip(*runtime)

# ── Generate .ico for Windows taskbar / Explorer icon ────────────────────────
def _build_ico(path: Path) -> bool:
    """Generate a multi-size .ico with the Pack Sync sync-ring logo."""
    import struct, zlib, io, math
    from PIL import Image

    def _png_chunk(tag, data):
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    def _make_img(size):
        CR    = size * 0.18
        BG_C  = (0x1e, 0x1e, 0x2e, 0xff)
        BLU   = (0x89, 0xb4, 0xfa, 0xff)
        GRN   = (0xa6, 0xe3, 0xa1, 0xff)
        CLR   = (0, 0, 0, 0)
        cx = cy = size / 2
        r_mid = size * 0.34
        r_w   = size * 0.10
        rows = bytearray()
        for y in range(size):
            rows += b'\x00'
            for x in range(size):
                lx = min(x, size - 1 - x)
                ly = min(y, size - 1 - y)
                in_sq = (lx >= CR or ly >= CR or (lx - CR) ** 2 + (ly - CR) ** 2 <= CR * CR)
                if not in_sq:
                    rows += bytes(CLR); continue
                dx, dy = x - cx, y - cy
                dist  = math.hypot(dx, dy)
                angle = math.degrees(math.atan2(-dy, dx)) % 360
                if abs(dist - r_mid) <= r_w:
                    rows += bytes(BLU if 30 <= angle <= 210 else GRN)
                else:
                    rows += bytes(BG_C)
        png = (b'\x89PNG\r\n\x1a\n'
               + _png_chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
               + _png_chunk(b'IDAT', zlib.compress(bytes(rows), 9))
               + _png_chunk(b'IEND', b''))
        return Image.open(io.BytesIO(png))

    try:
        imgs = [_make_img(s) for s in (16, 32, 48, 256)]
        imgs[0].save(str(path), format="ICO",
                     append_images=imgs[1:],
                     sizes=[(s, s) for s in (16, 32, 48, 256)])
        return True
    except Exception as e:
        print(f"  Warning: icon generation failed: {e}")
        return False

ico_path      = None
tray_helper   = None

if PLAT == "win32":
    # ── Generate .ico ────────────────────────────────────────────────────────
    ico_path = BUILD_TMP / "PackSync.ico"
    if _build_ico(ico_path):
        print(f"  Icon: {ico_path.name}")
    else:
        ico_path = None

    # ── Compile TrayHelper.cs with csc.exe (ships with .NET Framework) ───────
    import glob as _glob
    def _find_csc():
        for pat in [
            r"C:\Windows\Microsoft.NET\Framework64\v4*\csc.exe",
            r"C:\Windows\Microsoft.NET\Framework\v4*\csc.exe",
        ]:
            hits = sorted(_glob.glob(pat), reverse=True)
            if hits:
                return hits[0]
        return None

    csc = _find_csc()
    tray_src = HERE / "TrayHelper.cs"
    tray_exe = BUILD_TMP / "TrayHelper.exe"

    if csc and tray_src.exists():
        print(f"Compiling TrayHelper.cs with {Path(csc).name} …")
        r = subprocess.run(
            [csc, "/target:exe", "/optimize+", "/nologo",
             "/reference:System.Windows.Forms.dll",
             "/reference:System.Drawing.dll",
             f"/out:{tray_exe}", str(tray_src)],
            capture_output=True, text=True)
        if r.returncode == 0:
            tray_helper = tray_exe
            print(f"  TrayHelper.exe compiled  ({tray_exe.stat().st_size // 1024} KB)")
        else:
            print(f"  ✗  TrayHelper compilation failed:\n{r.stdout}{r.stderr}")
    else:
        if not csc:
            print("  Warning: csc.exe not found — TrayHelper.exe will not be bundled.")
        if not tray_src.exists():
            print(f"  Warning: TrayHelper.cs not found at {tray_src}")

# ── PyInstaller command ───────────────────────────────────────────────────────
cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--name", "PackSync",
    "--distpath", str(DIST),
    "--workpath", str(BUILD_TMP),
    "--specpath", str(BUILD_TMP),
    # Exclude heavy PIL modules we don't use — saves ~15 MB
    "--exclude-module", "PIL.ImageDraw",
    "--exclude-module", "PIL.ImageFilter",
    "--exclude-module", "PIL.ImageFont",
    "--exclude-module", "PIL.ImageEnhance",
    "--exclude-module", "PIL.ImageTk",
    "--exclude-module", "PIL.ImageQt",
    "--exclude-module", "PIL.ImageSequence",
    "--exclude-module", "PIL.GifImagePlugin",
    "--exclude-module", "PIL.JpegImagePlugin",
    # Needed for the self-updater's HTTPS calls — PyInstaller misses ssl/_ssl
    # because the app imports urllib lazily, not ssl directly.
    "--hidden-import", "ssl",
    "--hidden-import", "_ssl",
]

if PLAT == "win32":
    cmd += ["--windowed", "--noconsole"]
    if ico_path:
        cmd += ["--icon", str(ico_path)]

elif PLAT == "darwin":
    # macOS: build then wrap in DMG
    cmd += ["--windowed"]

cmd.append(str(SCRIPT))

print("Running PyInstaller …\n")
result = subprocess.run(cmd, cwd=HERE)
if result.returncode != 0:
    print("\n✗  Build failed.")
    sys.exit(1)

built_exe = DIST / EXE
built_app = DIST / "PackSync.app"

# ── macOS: wrap .app in .dmg ──────────────────────────────────────────────────
if PLAT == "darwin" and built_app.exists():
    dmg = DIST / "PackSync.dmg"
    print("\nCreating DMG …")
    subprocess.run([
        "hdiutil", "create",
        "-volname", "Pack Sync",
        "-srcfolder", str(built_app),
        "-ov", "-format", "UDZO",
        str(dmg)
    ], check=True)
    size_mb = dmg.stat().st_size / 1_048_576
    print(f"✓  dist/{folder}/PackSync.dmg  ({size_mb:.1f} MB)")

# ── Linux: wrap in .deb (Debian/Ubuntu only) ─────────────────────────────────
elif IS_LINUX := (PLAT == "linux") and built_exe.exists():
    size_mb = built_exe.stat().st_size / 1_048_576
    print(f"\n✓  dist/{folder}/PackSync  ({size_mb:.1f} MB)")
    if shutil.which("dpkg-deb"):
        print("\nBuilding .deb package …")
        pkg = BUILD_TMP / "deb"
        for d in ["DEBIAN",
                  "usr/local/bin",
                  "usr/share/applications",
                  "usr/share/icons/hicolor/256x256/apps"]:
            (pkg / d).mkdir(parents=True, exist_ok=True)

        (pkg / "DEBIAN" / "control").write_text(
            "Package: pack-sync\n"
            "Version: 1.0\n"
            "Architecture: amd64\n"
            "Maintainer: Queuereel\n"
            "Description: Pack Sync - Live-sync Minecraft Bedrock repos to com.mojang\n"
        )
        shutil.copy2(built_exe, pkg / "usr" / "local" / "bin" / "PackSync")
        os.chmod(pkg / "usr" / "local" / "bin" / "PackSync", 0o755)
        (pkg / "usr" / "share" / "applications" / "pack-sync.desktop").write_text(
            "[Desktop Entry]\nName=Pack Sync\n"
            "Exec=/usr/local/bin/PackSync\nType=Application\n"
            "Categories=Utility;\n"
        )
        deb = DIST / "PackSync.deb"
        subprocess.run(["dpkg-deb", "--build", str(pkg), str(deb)], check=True)
        size_mb = deb.stat().st_size / 1_048_576
        print(f"✓  dist/{folder}/PackSync.deb  ({size_mb:.1f} MB)")
    else:
        print("  (dpkg-deb not found — skipping .deb packaging)")

# ── Windows / plain exe ───────────────────────────────────────────────────────
elif built_exe.exists():
    # Copy TrayHelper.exe and PackSync.ico next to PackSync.exe.
    # They must stay in the same folder — PackSync.exe looks for them at startup.
    if tray_helper and tray_helper.exists():
        shutil.copy2(tray_helper, DIST / "TrayHelper.exe")
        print(f"  Copied TrayHelper.exe → dist/{folder}/")
    if ico_path and ico_path.exists():
        shutil.copy2(ico_path, DIST / "PackSync.ico")
        print(f"  Copied PackSync.ico   → dist/{folder}/")
    size_mb = built_exe.stat().st_size / 1_048_576
    print(f"\n✓  dist/{folder}/PackSync.exe  ({size_mb:.1f} MB)")
else:
    print("\n⚠  Build finished but output not found — check dist/ folder.")

# Clean up PyInstaller temp files
shutil.rmtree(BUILD_TMP, ignore_errors=True)
