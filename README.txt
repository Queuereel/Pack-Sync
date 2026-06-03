Pack Sync
=========
Live-sync Minecraft Bedrock pack repos to com.mojang.
Replaces Regolith — no terminal needed after first launch.


RUNNING (from source)
---------------------
Requires Python 3.11 or newer.

On first launch, Pack Sync automatically installs missing packages:
  pystray   — system tray integration
  Pillow    — tray icon rendering
  watchdog  — OS-native file watching (zero idle CPU)

If the auto-install fails (e.g. no internet or pip blocked), run manually:

  Windows:
    python -m pip install pystray Pillow watchdog

  macOS / Linux:
    python3 -m pip install pystray Pillow watchdog

Then start the app:
  python pack_sync.py          (Windows)
  python3 pack_sync.py         (macOS / Linux)


COMPILED BUILDS
---------------
Pre-built single-file executables are in the dist/ folder:

  dist/windows-x64/PackSync.exe       — Windows 10/11 (x64)
  dist/windows-arm64/PackSync.exe     — Windows 11 ARM (e.g. Surface Pro X)
  dist/macos-arm64/PackSync            — macOS Apple Silicon (M1/M2/M3)
  dist/macos-x64/PackSync             — macOS Intel
  dist/linux-x64/PackSync             — Ubuntu / Debian x64

Double-click the executable for your platform — no Python or pip needed.

To rebuild the executables yourself, see BUILD.txt.


HOW IT WORKS
------------
1. Pack Sync scans your GitHub folder for repos that contain a manifest.json
   with modules of type "resources" (RP) or "data"/"script" (BP).

2. It renames packs automatically:
     Project-Corestone/packs/RP  →  development_resource_packs/ProjectCorestoneRP
     Project-Corestone/packs/BP  →  development_behavior_packs/ProjectCorestoneBP


6. The Remove button deletes the destination pack folders from com.mojang.
   If permissions block removal, Pack Sync force-removes using takeown/icacls
   (Windows) or administrator privileges (macOS/Linux).


SETTINGS
--------
GitHub folder:       Where your repos live  (default: ~/Documents/GitHub)
Destination folder:  com.mojang root        (default: auto-detected per OS)
Launch on startup:   Adds Pack Sync to login items so it starts automatically


CONFIG FILE
-----------
Settings are saved to:  Pack Sync/pack_sync_config.json
Delete this file to reset to first-launch state.
