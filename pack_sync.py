#!/usr/bin/env python3
"""
Pack Sync — Live-sync Minecraft Bedrock pack repos to com.mojang
• Instant file-watching (OS-native, zero idle CPU)
• Branch-change guard  •  System tray  •  Windows / macOS / Linux
"""
import io, json, os, re, shlex, shutil, stat, struct, subprocess, sys
import threading, time, zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

IS_WIN   = sys.platform == "win32"
IS_MAC   = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
_NO_WIN  = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW — suppress console flashes

# ── App version + self-update (GitHub Releases) ──────────────────────────────
# APP_VERSION must match the release tag (release tags are "pack-sync-v<APP_VERSION>").
# Bump this in lock-step with release_pack_sync.py when cutting a release.
APP_VERSION   = "1.1.1"
UPDATE_REPO   = "Queuereel/Pack-Sync"  # where releases are published
UPDATE_TAG_PREFIX = "pack-sync-v"

if IS_WIN:
    import ctypes, ctypes.wintypes, winreg

# ─── C# tray helper (Windows) ────────────────────────────────────────────────
# TrayHelper.exe is a tiny WinForms app compiled from TrayHelper.cs.
# It owns the NotifyIcon and communicates with Pack Sync over stdin/stdout.

def _find_tray_helper() -> "Path | None":
    """Return path to TrayHelper.exe. It may be bundled INSIDE the onefile exe
    (PyInstaller extracts it to sys._MEIPASS), or sit next to PackSync.exe."""
    bases = [Path(sys.executable).parent, Path(__file__).parent]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bases.insert(0, Path(meipass))
    for b in bases:
        p = b / "TrayHelper.exe"
        if p.exists():
            return p
    return None

def _find_bundled_ico() -> str:
    """Return path to PackSync.ico (sits next to PackSync.exe), or empty string."""
    bases = [Path(sys.executable).parent, Path(__file__).parent]
    for b in bases:
        p = b / "PackSync.ico"
        if p.exists():
            return str(p)
    return ""

class _CSharpTray:
    """
    Manages TrayHelper.exe as a child process.
    Protocol over stdin/stdout:
      stdin  ← "QUIT"                        ask helper to exit
      stdin  ← "BALLOON:<title>|<text>|<ms>" show a balloon tip
      stdout → "READY"                        icon is live in the tray
      stdout → "OPEN"                         user wants to open the window
      stdout → "SYNC"                         user triggered Sync All
      stdout → "QUIT"                         user chose Quit from menu
      stdout → "BALLOON_CLICK"                user clicked the balloon tip
    """
    def __init__(self, ico_path: str, on_open, on_sync, on_quit,
                 on_balloon_click=None, on_ready=None, tk_schedule=None):
        self._ico_path         = ico_path
        self._on_open          = on_open
        self._on_sync          = on_sync
        self._on_quit          = on_quit
        self._on_balloon_click = on_balloon_click
        self._on_ready         = on_ready
        self._tk_schedule      = tk_schedule
        self._proc             = None
        self._active           = False

    def run_detached(self):
        helper = _find_tray_helper()
        if helper is None:
            raise FileNotFoundError("TrayHelper.exe not found — rebuild with build.py")

        args = [str(helper)]
        if self._ico_path:
            args.append(self._ico_path)

        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        # Send the Pack Sync logo as RGBA bytes so TrayHelper can show it.
        # TrayHelper's reader thread picks this up and calls NotifyIcon.Icon =.
        try:
            import base64 as _b64
            _img = _make_icon(32)
            _raw = _img.tobytes('raw', 'RGBA')
            _b64str = _b64.b64encode(_raw).decode()
            self._proc.stdin.write(f"RGBA:32:{_b64str}\n".encode())
            self._proc.stdin.flush()
        except Exception:
            pass  # icon update is best-effort; fallback icon stays

        def _reader():
            try:
                for raw in self._proc.stdout:
                    cmd = raw.decode(errors="replace").strip()
                    if   cmd == "READY":
                        self._active = True
                        if self._on_ready: self._dispatch(self._on_ready)
                    elif cmd == "OPEN"         : self._dispatch(self._on_open)
                    elif cmd == "SYNC"         : self._dispatch(self._on_sync)
                    elif cmd == "QUIT"         : self._dispatch(self._on_quit)
                    elif cmd == "BALLOON_CLICK":
                        if self._on_balloon_click:
                            self._dispatch(self._on_balloon_click)
            except Exception:
                pass
            self._active = False

        threading.Thread(target=_reader, daemon=True).start()

    def _dispatch(self, fn):
        """Call fn on the tkinter main thread (thread-safe)."""
        if self._tk_schedule:
            self._tk_schedule(fn)
        else:
            fn()

    def notify(self, title: str, text: str, timeout_ms: int = 5000):
        if self._proc and self._proc.poll() is None:
            title = title.replace("|", " ")
            text  = text.replace("|", " ")
            try:
                self._proc.stdin.write(f"BALLOON:{title}|{text}|{timeout_ms}\n".encode())
                self._proc.stdin.flush()
            except Exception:
                pass

    def stop(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.write(b"QUIT\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=3)
            except Exception:
                try: self._proc.terminate()
                except Exception: pass
        self._active = False

    @property
    def active(self) -> bool:
        return self._active and (self._proc is not None and self._proc.poll() is None)

# ─── Dependency bootstrap ─────────────────────────────────────────────────────
# On Windows: only pystray + Pillow (watchdog replaced by Win32 ctypes watcher)
# On macOS/Linux: also watchdog
_REQUIRED = ["pystray", "Pillow"] + ([] if IS_WIN else ["watchdog"])

def _bootstrap():
    # Skip in compiled builds (Nuitka sets __compiled__, PyInstaller sets sys.frozen)
    if getattr(sys, "frozen", False) or globals().get("__compiled__"):
        return
    import importlib.util
    missing = [p for p in _REQUIRED
               if importlib.util.find_spec("PIL" if p == "Pillow" else p) is None]
    if not missing:
        return
    # Show a visible tkinter window — the console may not be visible on Windows
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.title("Pack Sync — First Run Setup")
    root.geometry("380x140")
    root.configure(bg="#1e1e2e")
    root.resizable(False, False)
    tk.Label(root, text="Installing missing packages…",
             font=("Segoe UI", 11), bg="#1e1e2e", fg="#cdd6f4").pack(pady=(20, 4))
    tk.Label(root, text="  ".join(missing),
             font=("Consolas", 9), bg="#1e1e2e", fg="#89b4fa").pack()
    bar = ttk.Progressbar(root, mode="indeterminate", length=320)
    bar.pack(pady=10); bar.start(10)
    root.update()
    failed = False
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet"] + missing)
    except subprocess.CalledProcessError:
        failed = True
    root.destroy()
    if failed:
        # Show error in a fresh window
        err = tk.Tk(); err.title("Install Failed"); err.geometry("420x120")
        err.configure(bg="#1e1e2e")
        tk.Label(err, text="Could not install packages automatically.",
                 font=("Segoe UI", 10), bg="#1e1e2e", fg="#f38ba8").pack(pady=(20, 4))
        tk.Label(err, text="Run:  pip install " + " ".join(missing),
                 font=("Consolas", 9), bg="#1e1e2e", fg="#cdd6f4").pack()
        tk.Button(err, text="OK", command=err.destroy, bg="#45475a", fg="#cdd6f4",
                  relief="flat", padx=20, pady=4).pack(pady=10)
        err.mainloop(); sys.exit(1)
    # Restart so newly installed packages are importable
    os.execv(sys.executable, [sys.executable] + sys.argv)

_bootstrap()

import pystray
from PIL import Image  # only Image.open — no ImageDraw needed

if not IS_WIN:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

# ─── Default paths ────────────────────────────────────────────────────────────
HOME = Path.home()
if IS_WIN:
    DEFAULT_GITHUB = HOME / "Documents" / "GitHub"
    DEFAULT_MOJANG = (HOME / "AppData" / "Roaming" / "Minecraft Bedrock"
                      / "Users" / "Shared" / "games" / "com.mojang")
elif IS_MAC:
    DEFAULT_GITHUB = HOME / "Documents" / "GitHub"
    DEFAULT_MOJANG = (HOME / "Library" / "Application Support"
                      / "com.mojang.minecraftpe" / "games" / "com.mojang")
else:
    DEFAULT_GITHUB = HOME / "GitHub"
    DEFAULT_MOJANG = (HOME / ".local" / "share" / "mcpelauncher"
                      / "games" / "com.mojang")

# In a frozen (PyInstaller --onefile) build, __file__ lives in the temp _MEIPASS
# folder which is deleted on exit.  Use the exe's own directory instead so that
# the config survives between runs.
CONFIG_FILE = (Path(sys.executable).parent if getattr(sys, "frozen", False)
               else Path(__file__).parent) / "pack_sync_config.json"
APP_NAME    = "PackSync"
RUN_KEY     = r"Software\Microsoft\Windows\CurrentVersion\Run"

# ─── Single-instance guard ────────────────────────────────────────────────────
# Pack Sync watches the filesystem and writes into com.mojang; two instances
# would race each other (duplicate watchers, conflicting wipe-and-reupload).
# Enforce exactly one running instance per user. Windows: a named mutex (lives
# for the life of the holding process, released automatically on exit/crash).
# macOS/Linux: an exclusive flock on a lockfile (also auto-released on exit).
_INSTANCE_HANDLE = None  # keep the OS handle/fd alive for the whole process

def acquire_single_instance() -> bool:
    """Return True if we are the only instance; False if one is already running.
    The acquired handle is parked in a module global so it is never GC'd (which
    would release the lock) for the lifetime of the process."""
    global _INSTANCE_HANDLE
    if IS_WIN:
        import ctypes
        ERROR_ALREADY_EXISTS = 183
        # Per-user, session-spanning name. Global\ would block across users; we
        # only want to block this user's second launch.
        name = f"Local\\{APP_NAME}_single_instance_mutex"
        h = ctypes.windll.kernel32.CreateMutexW(None, False, name)
        last = ctypes.windll.kernel32.GetLastError()
        if not h:
            return True  # couldn't create the mutex — fail open, don't block app
        if last == ERROR_ALREADY_EXISTS:
            ctypes.windll.kernel32.CloseHandle(h)
            return False
        _INSTANCE_HANDLE = h
        return True
    else:
        import fcntl, tempfile
        lock_path = Path(tempfile.gettempdir()) / f"{APP_NAME}.lock"
        try:
            fd = open(lock_path, "w")
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        except Exception:
            return True  # lock mechanism unavailable — fail open
        _INSTANCE_HANDLE = fd  # held open = lock held; closes/releases on exit
        return True

# ─── Palette ──────────────────────────────────────────────────────────────────
BG=      "#1e1e2e"; BG2=    "#181825"; SURFACE="#313244"; SURF2=  "#45475a"
TEXT=    "#cdd6f4"; SUB=    "#9399b2"; MUTED=  "#6c7086"
BLUE=    "#89b4fa"; GREEN=  "#a6e3a1"; RED=    "#f38ba8"
PEACH=   "#fab387"; YELLOW= "#f9e2af"

if IS_WIN:   UI_FONT = "Segoe UI"
elif IS_MAC: UI_FONT = "SF Pro Display"
else:        UI_FONT = "Ubuntu"

# ─── Internationalisation ─────────────────────────────────────────────────────
# 12 supported languages in display order
LANG_NAMES: dict[str, str] = {
    "en": "English",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
    "pt": "Português",
    "ru": "Русский",
    "zh": "中文",
    "ja": "日本語",
    "ko": "한국어",
    "it": "Italiano",
    "pl": "Polski",
    "tr": "Türkçe",
    "uk": "Українська",
    "no": "Norsk",
    "tl": "Filipino",
    "th": "ภาษาไทย",
}

# Each key maps to a dict of lang→translated string.
# English is the fallback for any missing translation.
_TR: dict[str, dict[str, str]] = {
    # ── App chrome ────────────────────────────────────────────────────────────
    "app_title": {
        "en": "Pack Sync", "es": "Pack Sync", "fr": "Pack Sync",
        "de": "Pack Sync", "pt": "Pack Sync", "ru": "Pack Sync",
        "zh": "Pack Sync", "ja": "Pack Sync", "ko": "Pack Sync",
        "it": "Pack Sync", "pl": "Pack Sync", "tr": "Pack Sync",
    },
    "btn_refresh": {
        "en": "↺  Refresh",     "es": "↺  Actualizar",  "fr": "↺  Actualiser",
        "de": "↺  Aktualisieren","pt": "↺  Atualizar",  "ru": "↺  Обновить",
        "zh": "↺  刷新",         "ja": "↺  更新",         "ko": "↺  새로고침",
        "it": "↺  Aggiorna",    "pl": "↺  Odśwież",     "tr": "↺  Yenile",
    },
    "btn_settings": {
        "en": "⚙  Settings",    "es": "⚙  Ajustes",     "fr": "⚙  Paramètres",
        "de": "⚙  Einstellungen","pt": "⚙  Configurações","ru": "⚙  Настройки",
        "zh": "⚙  设置",         "ja": "⚙  設定",          "ko": "⚙  설정",
        "it": "⚙  Impostazioni","pl": "⚙  Ustawienia",  "tr": "⚙  Ayarlar",
    },
    "btn_sync_all": {
        "en": "↑↓  Sync All Projects",      "es": "↑↓  Sincronizar todo",
        "fr": "↑↓  Tout synchroniser",      "de": "↑↓  Alles synchronisieren",
        "pt": "↑↓  Sincronizar tudo",       "ru": "↑↓  Синхронизировать всё",
        "zh": "↑↓  同步全部项目",             "ja": "↑↓  すべて同期",
        "ko": "↑↓  모두 동기화",             "it": "↑↓  Sincronizza tutto",
        "pl": "↑↓  Synchronizuj wszystko",  "tr": "↑↓  Tümünü Eşitle",
    },
    "btn_sync": {
        "en": "↑↓  Sync",   "es": "↑↓  Sincronizar", "fr": "↑↓  Synchroniser",
        "de": "↑↓  Sync",   "pt": "↑↓  Sincronizar",  "ru": "↑↓  Синхронизировать",
        "zh": "↑↓  同步",    "ja": "↑↓  同期",          "ko": "↑↓  동기화",
        "it": "↑↓  Sincr.", "pl": "↑↓  Synchronizuj", "tr": "↑↓  Eşitle",
    },
    "btn_remove": {
        "en": "✕  Remove",  "es": "✕  Quitar",   "fr": "✕  Supprimer",
        "de": "✕  Entfernen","pt": "✕  Remover",  "ru": "✕  Удалить",
        "zh": "✕  移除",     "ja": "✕  削除",      "ko": "✕  제거",
        "it": "✕  Rimuovi", "pl": "✕  Usuń",      "tr": "✕  Kaldır",
    },
    "lbl_live": {
        "en": "↺ live",     "es": "↺ activo",    "fr": "↺ actif",
        "de": "↺ aktiv",    "pt": "↺ ativo",     "ru": "↺ активен",
        "zh": "↺ 实时",      "ja": "↺ ライブ",     "ko": "↺ 실시간",
        "it": "↺ attivo",   "pl": "↺ aktywny",   "tr": "↺ canlı",
    },
    "lbl_branch_changed": {
        "en": "⚠ branch changed",    "es": "⚠ rama cambiada",
        "fr": "⚠ branche changée",   "de": "⚠ Branch gewechselt",
        "pt": "⚠ branch alterado",   "ru": "⚠ ветка изменена",
        "zh": "⚠ 分支已切换",         "ja": "⚠ ブランチ変更",
        "ko": "⚠ 브랜치 변경됨",      "it": "⚠ branch cambiato",
        "pl": "⚠ gałąź zmieniona",   "tr": "⚠ dal değişti",
    },
    "lbl_synced": {
        "en": "✓ synced",  "es": "✓ sincronizado", "fr": "✓ synchronisé",
        "de": "✓ synchron","pt": "✓ sincronizado",  "ru": "✓ синхронизировано",
        "zh": "✓ 已同步",   "ja": "✓ 同期済み",      "ko": "✓ 동기화됨",
        "it": "✓ sincr.",  "pl": "✓ zsynchronizowano","tr": "✓ eşitlendi",
    },
    "lbl_not_synced": {
        "en": "○ not synced",  "es": "○ sin sincronizar","fr": "○ non synchronisé",
        "de": "○ nicht sync.", "pt": "○ não sincronizado","ru": "○ не синхронизировано",
        "zh": "○ 未同步",       "ja": "○ 未同期",            "ko": "○ 동기화 안됨",
        "it": "○ non sincr.",  "pl": "○ nie zsynchronizowano","tr": "○ eşitlenmedi",
    },
    "status_ready": {
        "en": "Ready",       "es": "Listo",        "fr": "Prêt",
        "de": "Bereit",      "pt": "Pronto",        "ru": "Готово",
        "zh": "就绪",         "ja": "準備完了",       "ko": "준비됨",
        "it": "Pronto",      "pl": "Gotowy",        "tr": "Hazır",
    },
    "status_syncing": {
        "en": "Syncing…",    "es": "Sincronizando…","fr": "Synchronisation…",
        "de": "Synchronisiere…","pt": "Sincronizando…","ru": "Синхронизация…",
        "zh": "同步中…",       "ja": "同期中…",         "ko": "동기화 중…",
        "it": "Sincronizzando…","pl": "Synchronizowanie…","tr": "Eşitleniyor…",
    },
    "status_synced": {
        "en": "Synced: {0}",      "es": "Sincronizado: {0}","fr": "Synchronisé : {0}",
        "de": "Synchronisiert: {0}","pt": "Sincronizado: {0}","ru": "Синхронизировано: {0}",
        "zh": "已同步：{0}",       "ja": "同期完了：{0}",     "ko": "동기화됨: {0}",
        "it": "Sincronizzato: {0}","pl": "Zsynchronizowano: {0}","tr": "Eşitlendi: {0}",
    },
    "status_synced_all": {
        "en": "Synced {0} project(s).",      "es": "Sincronizados {0} proyecto(s).",
        "fr": "Synchronisé {0} projet(s).",  "de": "{0} Projekt(e) synchronisiert.",
        "pt": "Sincronizados {0} projeto(s).","ru": "Синхронизировано {0} проект(ов).",
        "zh": "已同步 {0} 个项目。",           "ja": "{0} 個のプロジェクトを同期しました。",
        "ko": "{0}개 프로젝트 동기화됨.",      "it": "{0} progetto/i sincronizzato/i.",
        "pl": "Zsynchronizowano {0} projekt(ów).","tr": "{0} proje eşitlendi.",
    },
    "status_removed": {
        "en": "Removed: {0}",    "es": "Eliminado: {0}","fr": "Supprimé : {0}",
        "de": "Entfernt: {0}",   "pt": "Removido: {0}", "ru": "Удалено: {0}",
        "zh": "已移除：{0}",      "ja": "削除：{0}",      "ko": "제거됨: {0}",
        "it": "Rimosso: {0}",    "pl": "Usunięto: {0}", "tr": "Kaldırıldı: {0}",
    },
    "search_hint": {
        "en": "Filter projects…",       "es": "Filtrar proyectos…",
        "fr": "Filtrer les projets…",   "de": "Projekte filtern…",
        "pt": "Filtrar projetos…",      "ru": "Фильтр проектов…",
        "zh": "搜索项目…",               "ja": "プロジェクトを絞り込む…",
        "ko": "프로젝트 검색…",          "it": "Filtra progetti…",
        "pl": "Filtruj projekty…",      "tr": "Projeleri filtrele…",
    },
    "empty_no_projects": {
        "en": "No repositories with RP or BP packs found.\n\nCheck your GitHub folder in Settings.",
        "es": "No se encontraron repositorios con packs RP o BP.\n\nRevisa tu carpeta de GitHub en Ajustes.",
        "fr": "Aucun dépôt avec des packs RP ou BP trouvé.\n\nVérifiez votre dossier GitHub dans Paramètres.",
        "de": "Keine Repositorys mit RP- oder BP-Packs gefunden.\n\nPrüfe deinen GitHub-Ordner in den Einstellungen.",
        "pt": "Nenhum repositório com packs RP ou BP encontrado.\n\nVerifique sua pasta do GitHub nas Configurações.",
        "ru": "Репозитории с паками RP или BP не найдены.\n\nПроверьте папку GitHub в Настройках.",
        "zh": "未找到包含 RP 或 BP 包的仓库。\n\n请在设置中检查 GitHub 文件夹。",
        "ja": "RP または BP パックを含むリポジトリが見つかりません。\n\n設定で GitHub フォルダを確認してください。",
        "ko": "RP 또는 BP 팩이 있는 리포지토리를 찾을 수 없습니다.\n\n설정에서 GitHub 폴더를 확인하세요.",
        "it": "Nessun repository con pack RP o BP trovato.\n\nControlla la cartella GitHub nelle Impostazioni.",
        "pl": "Nie znaleziono repozytoriów z pakami RP lub BP.\n\nSprawdź folder GitHub w Ustawieniach.",
        "tr": "RP veya BP paketi olan depo bulunamadı.\n\nAyarlar'dan GitHub klasörünü kontrol edin.",
    },
    "empty_no_match": {
        "en": 'No projects match "{0}".',  "es": 'Sin resultados para «{0}».',
        "fr": 'Aucun projet pour « {0} ».',"de": 'Keine Projekte für „{0}".',
        "pt": 'Nenhum projeto para "{0}".',"ru": 'Нет проектов для «{0}».',
        "zh": '没有匹配"{0}"的项目。',       "ja": '「{0}」に一致するプロジェクトがありません。',
        "ko": '"{0}"에 일치하는 프로젝트 없음.',"it": 'Nessun progetto per «{0}».',
        "pl": 'Brak projektów dla „{0}".',  "tr": '"{0}" için proje bulunamadı.',
    },
    # ── First-sync warning ────────────────────────────────────────────────────
    "warn_first_title": {
        "en": "Before your first sync",          "es": "Antes de tu primera sincronización",
        "fr": "Avant votre première sync",        "de": "Vor der ersten Synchronisation",
        "pt": "Antes da primeira sincronização",  "ru": "Перед первой синхронизацией",
        "zh": "首次同步前须知",                    "ja": "初回同期の前に",
        "ko": "첫 동기화 전",                     "it": "Prima della prima sincronizzazione",
        "pl": "Przed pierwszą synchronizacją",    "tr": "İlk eşitlemeden önce",
    },
    "warn_first_body": {
        "en": (
            "Pack Sync will copy your pack files into com.mojang.\n\n"
            "If the destination folder already contains files:\n"
            "  •  Files are merged — nothing is deleted\n"
            "  •  The newer version of each file wins\n"
            "  •  Files you added in-game are kept safely\n\n"
            "Tip: commit your repo first so you can roll back if needed."
        ),
        "es": (
            "Pack Sync copiará tus archivos de pack a com.mojang.\n\n"
            "Si la carpeta de destino ya tiene archivos:\n"
            "  •  Los archivos se fusionan — nada se elimina\n"
            "  •  Gana la versión más reciente de cada archivo\n"
            "  •  Los archivos añadidos en el juego se conservan\n\n"
            "Consejo: haz commit antes de sincronizar."
        ),
        "fr": (
            "Pack Sync va copier vos fichiers de pack dans com.mojang.\n\n"
            "Si le dossier de destination contient déjà des fichiers :\n"
            "  •  Les fichiers sont fusionnés — rien n'est supprimé\n"
            "  •  La version la plus récente de chaque fichier gagne\n"
            "  •  Les fichiers ajoutés en jeu sont conservés\n\n"
            "Conseil : committez votre dépôt avant de synchroniser."
        ),
        "de": (
            "Pack Sync kopiert deine Pack-Dateien in com.mojang.\n\n"
            "Falls der Zielordner bereits Dateien enthält:\n"
            "  •  Dateien werden zusammengeführt — nichts wird gelöscht\n"
            "  •  Die neuere Version jeder Datei gewinnt\n"
            "  •  Im Spiel hinzugefügte Dateien bleiben erhalten\n\n"
            "Tipp: Erst committen, dann synchronisieren."
        ),
        "pt": (
            "O Pack Sync copiará os arquivos do seu pack para com.mojang.\n\n"
            "Se a pasta de destino já contiver arquivos:\n"
            "  •  Os arquivos são mesclados — nada é excluído\n"
            "  •  A versão mais recente de cada arquivo prevalece\n"
            "  •  Arquivos adicionados no jogo são mantidos\n\n"
            "Dica: faça commit antes de sincronizar."
        ),
        "ru": (
            "Pack Sync скопирует файлы пака в com.mojang.\n\n"
            "Если в папке назначения уже есть файлы:\n"
            "  •  Файлы объединяются — ничего не удаляется\n"
            "  •  Побеждает более новая версия каждого файла\n"
            "  •  Файлы, добавленные в игре, сохраняются\n\n"
            "Совет: сначала сделайте commit, чтобы можно было откатиться."
        ),
        "zh": (
            "Pack Sync 将把您的包文件复制到 com.mojang。\n\n"
            "如果目标文件夹已有文件：\n"
            "  •  文件会合并——不会删除任何内容\n"
            "  •  每个文件以较新的版本为准\n"
            "  •  游戏中添加的文件会被保留\n\n"
            "建议：同步前先提交您的仓库，以便回滚。"
        ),
        "ja": (
            "Pack Sync はパックファイルを com.mojang にコピーします。\n\n"
            "コピー先フォルダにすでにファイルがある場合：\n"
            "  •  ファイルはマージされます — 削除はありません\n"
            "  •  各ファイルの新しいバージョンが優先されます\n"
            "  •  ゲーム内で追加したファイルは保持されます\n\n"
            "ヒント：同期前にリポジトリをコミットしておくと安心です。"
        ),
        "ko": (
            "Pack Sync는 팩 파일을 com.mojang에 복사합니다.\n\n"
            "대상 폴더에 이미 파일이 있는 경우:\n"
            "  •  파일이 병합됩니다 — 삭제되지 않습니다\n"
            "  •  각 파일의 더 새로운 버전이 우선됩니다\n"
            "  •  게임 내에서 추가한 파일은 유지됩니다\n\n"
            "팁: 동기화 전에 먼저 커밋하세요."
        ),
        "it": (
            "Pack Sync copierà i file del tuo pack in com.mojang.\n\n"
            "Se la cartella di destinazione contiene già file:\n"
            "  •  I file vengono uniti — nulla viene eliminato\n"
            "  •  La versione più recente di ogni file ha la priorità\n"
            "  •  I file aggiunti in gioco vengono mantenuti\n\n"
            "Consiglio: esegui il commit prima di sincronizzare."
        ),
        "pl": (
            "Pack Sync skopiuje pliki paczki do com.mojang.\n\n"
            "Jeśli folder docelowy już zawiera pliki:\n"
            "  •  Pliki są scalane — nic nie jest usuwane\n"
            "  •  Nowsza wersja każdego pliku ma pierwszeństwo\n"
            "  •  Pliki dodane w grze są zachowywane\n\n"
            "Wskazówka: zrób commit przed synchronizacją."
        ),
        "tr": (
            "Pack Sync, paket dosyalarınızı com.mojang'a kopyalayacak.\n\n"
            "Hedef klasör zaten dosya içeriyorsa:\n"
            "  •  Dosyalar birleştirilir — hiçbir şey silinmez\n"
            "  •  Her dosyanın daha yeni sürümü geçerlidir\n"
            "  •  Oyun içinde eklenen dosyalar korunur\n\n"
            "İpucu: Eşitlemeden önce deponuzu commit edin."
        ),
    },
    "warn_first_skip": {
        "en": "Don't show this again",          "es": "No mostrar de nuevo",
        "fr": "Ne plus afficher",               "de": "Nicht mehr anzeigen",
        "pt": "Não mostrar novamente",          "ru": "Больше не показывать",
        "zh": "不再显示",                        "ja": "次から表示しない",
        "ko": "다시 보지 않기",                  "it": "Non mostrare più",
        "pl": "Nie pokazuj ponownie",           "tr": "Tekrar gösterme",
    },
    "btn_got_it": {
        "en": "Got it — Sync",              "es": "Entendido — Sincronizar",
        "fr": "Compris — Synchroniser",     "de": "Verstanden — Sync",
        "pt": "Entendido — Sincronizar",    "ru": "Понятно — Синхронизировать",
        "zh": "明白了，开始同步",              "ja": "了解 — 同期する",
        "ko": "확인 — 동기화",               "it": "Capito — Sincronizza",
        "pl": "Rozumiem — Synchronizuj",    "tr": "Anladım — Eşitle",
    },
    "btn_cancel": {
        "en": "Cancel",    "es": "Cancelar",  "fr": "Annuler",
        "de": "Abbrechen", "pt": "Cancelar",  "ru": "Отмена",
        "zh": "取消",       "ja": "キャンセル","ko": "취소",
        "it": "Annulla",   "pl": "Anuluj",    "tr": "İptal",
    },
    # ── Branch warning ────────────────────────────────────────────────────────
    "warn_branch_title": {
        "en": "Git Branch Changed",             "es": "Rama de Git cambiada",
        "fr": "Branche Git modifiée",           "de": "Git-Branch gewechselt",
        "pt": "Branch Git alterado",            "ru": "Ветка Git изменена",
        "zh": "Git 分支已切换",                  "ja": "Git ブランチが変更されました",
        "ko": "Git 브랜치 변경됨",               "it": "Branch Git cambiato",
        "pl": "Zmieniono gałąź Git",            "tr": "Git Dal Değişti",
    },
    "warn_branch_body": {
        "en": "Syncing now will replace the destination folder\nwith the contents of the new branch. Continue?",
        "es": "Sincronizar ahora reemplazará la carpeta de destino\ncon el contenido de la nueva rama. ¿Continuar?",
        "fr": "Synchroniser maintenant remplacera le dossier de destination\npar le contenu de la nouvelle branche. Continuer ?",
        "de": "Das Synchronisieren ersetzt jetzt den Zielordner\nmit dem Inhalt des neuen Branches. Fortfahren?",
        "pt": "Sincronizar agora substituirá a pasta de destino\npelo conteúdo do novo branch. Continuar?",
        "ru": "Синхронизация заменит папку назначения\nсодержимым новой ветки. Продолжить?",
        "zh": "立即同步将用新分支的内容替换目标文件夹。继续？",
        "ja": "今同期すると、コピー先フォルダが新しいブランチの内容で\n置き換えられます。続けますか？",
        "ko": "지금 동기화하면 대상 폴더가 새 브랜치의 내용으로\n교체됩니다. 계속하시겠습니까?",
        "it": "Sincronizzare ora sostituirà la cartella di destinazione\ncon il contenuto del nuovo branch. Continuare?",
        "pl": "Synchronizacja zastąpi folder docelowy\nzawartością nowej gałęzi. Kontynuować?",
        "tr": "Şimdi eşitlemek, hedef klasörü yeni dal içeriğiyle\ndekiştirecek. Devam edilsin mi?",
    },
    "btn_yes_sync": {
        "en": "Yes, Sync",      "es": "Sí, sincronizar", "fr": "Oui, synchroniser",
        "de": "Ja, sync",       "pt": "Sim, sincronizar", "ru": "Да, синхронизировать",
        "zh": "是，同步",         "ja": "はい、同期する",   "ko": "예, 동기화",
        "it": "Sì, sincronizza","pl": "Tak, synchronizuj","tr": "Evet, Eşitle",
    },
    # ── Remove dialog ─────────────────────────────────────────────────────────
    "remove_title": {
        "en": "Remove from destination",         "es": "Quitar del destino",
        "fr": "Supprimer de la destination",     "de": "Aus Ziel entfernen",
        "pt": "Remover do destino",              "ru": "Удалить из папки назначения",
        "zh": "从目标位置移除",                   "ja": "コピー先から削除",
        "ko": "대상에서 제거",                    "it": "Rimuovi dalla destinazione",
        "pl": "Usuń z miejsca docelowego",       "tr": "Hedeften Kaldır",
    },
    "remove_body": {
        "en": "Delete {0} from the destination?\nYour GitHub repo is not affected.",
        "es": "¿Eliminar {0} del destino?\nTu repositorio de GitHub no se verá afectado.",
        "fr": "Supprimer {0} de la destination ?\nVotre dépôt GitHub n'est pas affecté.",
        "de": "{0} aus dem Ziel löschen?\nDein GitHub-Repository bleibt unberührt.",
        "pt": "Excluir {0} do destino?\nSeu repositório GitHub não será afetado.",
        "ru": "Удалить {0} из папки назначения?\nВаш репозиторий GitHub не будет затронут.",
        "zh": "从目标位置删除 {0}？\n您的 GitHub 仓库不受影响。",
        "ja": "{0} をコピー先から削除しますか？\nGitHub リポジトリには影響しません。",
        "ko": "대상에서 {0}을(를) 삭제하시겠습니까?\nGitHub 리포지토리는 영향을 받지 않습니다.",
        "it": "Eliminare {0} dalla destinazione?\nIl tuo repository GitHub non sarà influenzato.",
        "pl": "Usunąć {0} z miejsca docelowego?\nTwoje repozytorium GitHub nie zostanie zmienione.",
        "tr": "{0} hedeften silinsin mi?\nGitHub deponuz etkilenmez.",
    },
    # ── Setup dialog ──────────────────────────────────────────────────────────
    "setup_title": {
        "en": "Welcome to Pack Sync",         "es": "Bienvenido a Pack Sync",
        "fr": "Bienvenue dans Pack Sync",     "de": "Willkommen bei Pack Sync",
        "pt": "Bem-vindo ao Pack Sync",       "ru": "Добро пожаловать в Pack Sync",
        "zh": "欢迎使用 Pack Sync",            "ja": "Pack Sync へようこそ",
        "ko": "Pack Sync에 오신 것을 환영합니다","it": "Benvenuto in Pack Sync",
        "pl": "Witaj w Pack Sync",            "tr": "Pack Sync'e Hoş Geldiniz",
    },
    "setup_subtitle": {
        "en": "One-time setup — change later in Settings.",
        "es": "Configuración única — cámbiala después en Ajustes.",
        "fr": "Configuration unique — modifiable dans Paramètres.",
        "de": "Einmalige Einrichtung — später in Einstellungen änderbar.",
        "pt": "Configuração única — altere depois nas Configurações.",
        "ru": "Разовая настройка — можно изменить в Настройках.",
        "zh": "一次性设置 — 之后可在设置中更改。",
        "ja": "初回設定 — 設定から後で変更できます。",
        "ko": "최초 설정 — 나중에 설정에서 변경 가능합니다.",
        "it": "Configurazione unica — modificabile nelle Impostazioni.",
        "pl": "Konfiguracja jednorazowa — zmień w Ustawieniach.",
        "tr": "Tek seferlik kurulum — daha sonra Ayarlar'dan değiştirilebilir.",
    },
    "setup_project_type": {
        "en": "Project type:",    "es": "Tipo de proyecto:", "fr": "Type de projet :",
        "de": "Projekttyp:",      "pt": "Tipo de projeto:",  "ru": "Тип проекта:",
        "zh": "项目类型：",         "ja": "プロジェクトの種類：","ko": "프로젝트 유형:",
        "it": "Tipo progetto:",   "pl": "Typ projektu:",     "tr": "Proje türü:",
    },
    "setup_github_folder": {
        "en": "GitHub folder:",     "es": "Carpeta de GitHub:", "fr": "Dossier GitHub :",
        "de": "GitHub-Ordner:",     "pt": "Pasta do GitHub:",   "ru": "Папка GitHub:",
        "zh": "GitHub 文件夹：",     "ja": "GitHub フォルダ：",   "ko": "GitHub 폴더:",
        "it": "Cartella GitHub:",   "pl": "Folder GitHub:",     "tr": "GitHub klasörü:",
    },
    "setup_dest_folder": {
        "en": "Destination folder:","es": "Carpeta de destino:","fr": "Dossier de destination :",
        "de": "Zielordner:",        "pt": "Pasta de destino:",  "ru": "Папка назначения:",
        "zh": "目标文件夹：",         "ja": "コピー先フォルダ：",  "ko": "대상 폴더:",
        "it": "Cartella dest.:",    "pl": "Folder docelowy:",   "tr": "Hedef klasör:",
    },
    "btn_get_started": {
        "en": "Get Started",     "es": "Comenzar",       "fr": "Démarrer",
        "de": "Loslegen",        "pt": "Começar",         "ru": "Начать",
        "zh": "开始使用",          "ja": "始める",           "ko": "시작하기",
        "it": "Inizia",          "pl": "Zacznij",         "tr": "Başla",
    },
    # ── Settings dialog ───────────────────────────────────────────────────────
    "settings_title": {
        "en": "Settings",       "es": "Ajustes",        "fr": "Paramètres",
        "de": "Einstellungen",  "pt": "Configurações",  "ru": "Настройки",
        "zh": "设置",            "ja": "設定",            "ko": "설정",
        "it": "Impostazioni",   "pl": "Ustawienia",     "tr": "Ayarlar",
    },
    "settings_startup": {
        "en": "Launch Pack Sync on system startup",
        "es": "Iniciar Pack Sync al arrancar el sistema",
        "fr": "Lancer Pack Sync au démarrage du système",
        "de": "Pack Sync beim Systemstart starten",
        "pt": "Iniciar Pack Sync na inicialização do sistema",
        "ru": "Запускать Pack Sync при старте системы",
        "zh": "系统启动时自动运行 Pack Sync",
        "ja": "システム起動時に Pack Sync を起動",
        "ko": "시스템 시작 시 Pack Sync 실행",
        "it": "Avvia Pack Sync all'avvio del sistema",
        "pl": "Uruchamiaj Pack Sync przy starcie systemu",
        "tr": "Sistem başlangıcında Pack Sync'i başlat",
    },
    "settings_language": {
        "en": "Language:",      "es": "Idioma:",        "fr": "Langue :",
        "de": "Sprache:",       "pt": "Idioma:",        "ru": "Язык:",
        "zh": "语言：",          "ja": "言語：",          "ko": "언어:",
        "it": "Lingua:",        "pl": "Język:",         "tr": "Dil:",
    },
    "btn_save": {
        "en": "Save",    "es": "Guardar", "fr": "Enregistrer",
        "de": "Speichern","pt": "Salvar", "ru": "Сохранить",
        "zh": "保存",     "ja": "保存",    "ko": "저장",
        "it": "Salva",   "pl": "Zapisz", "tr": "Kaydet",
    },
    # ── Progress ──────────────────────────────────────────────────────────────
    "progress_syncing_files": {
        "en": "Syncing…  {0} of {1} files  ({2}%)",
        "es": "Sincronizando…  {0} de {1} archivos  ({2}%)",
        "fr": "Synchronisation…  {0} sur {1} fichiers  ({2}%)",
        "de": "Synchronisiere…  {0} von {1} Dateien  ({2}%)",
        "pt": "Sincronizando…  {0} de {1} arquivos  ({2}%)",
        "ru": "Синхронизация…  {0} из {1} файлов  ({2}%)",
        "zh": "同步中…  {0}/{1} 个文件  ({2}%)",
        "ja": "同期中…  {1} ファイル中 {0} 個  ({2}%)",
        "ko": "동기화 중…  {1}개 중 {0}개  ({2}%)",
        "it": "Sincronizzando…  {0} di {1} file  ({2}%)",
        "pl": "Synchronizowanie…  {0} z {1} plików  ({2}%)",
        "tr": "Eşitleniyor…  {1} dosyadan {0}'ı  ({2}%)",
    },
    # ── Onboarding — tray page (page 0) ──────────────────────────────────────
    "pg_tray_title": {
        "en": "💡  Good to know",        "es": "💡  Bueno saberlo",
        "fr": "💡  Bon à savoir",        "de": "💡  Gut zu wissen",
        "pt": "💡  Bom saber",           "ru": "💡  Полезно знать",
        "zh": "💡  温馨提示",              "ja": "💡  豆知識",
        "ko": "💡  알아두세요",           "it": "💡  Da sapere",
        "pl": "💡  Warto wiedzieć",      "tr": "💡  Bilmekte fayda var",
    },
    "pg_tray_win": {
        "en": "Pack Sync hides in the Windows system tray when you close the window.\nClick the icon there to reopen it anytime.",
        "es": "Pack Sync se oculta en la bandeja del sistema al cerrar la ventana.\nHaz clic en el icono para volver a abrirla.",
        "fr": "Pack Sync se cache dans la zone de notification Windows à la fermeture.\nCliquez sur l'icône pour la rouvrir à tout moment.",
        "de": "Pack Sync versteckt sich im Windows-Systemtray beim Schließen.\nKlicke auf das Symbol, um es jederzeit wieder zu öffnen.",
        "pt": "Pack Sync se esconde na bandeja do sistema ao fechar a janela.\nClique no ícone para reabri-la a qualquer momento.",
        "ru": "Pack Sync сворачивается в системный трей Windows при закрытии окна.\nНажмите на значок, чтобы открыть его снова.",
        "zh": "关闭窗口后，Pack Sync 最小化到 Windows 系统托盘。\n点击图标随时重新打开。",
        "ja": "ウィンドウを閉じると Pack Sync は Windows のシステムトレイに隠れます。\nアイコンをクリックしていつでも再表示できます。",
        "ko": "창을 닫으면 Pack Sync가 Windows 시스템 트레이에 숨습니다.\n아이콘을 클릭해 언제든지 다시 열 수 있습니다.",
        "it": "Pack Sync si nasconde nell'area di notifica di Windows alla chiusura.\nFai clic sull'icona per riaprirla in qualsiasi momento.",
        "pl": "Pack Sync chowa się w zasobniku systemowym Windows po zamknięciu.\nKliknij ikonę, aby otworzyć ją ponownie.",
        "tr": "Pencereyi kapattığınızda Pack Sync sistem tepsisine gizlenir.\nYeniden açmak için simgeye tıklayın.",
    },
    "pg_tray_mac": {
        "en": "Pack Sync hides in the macOS menu bar when you close the window.\nClick the icon in the top-right to reopen it anytime.",
        "es": "Pack Sync se oculta en la barra de menú de macOS al cerrar.\nHaz clic en el icono en la esquina superior derecha.",
        "fr": "Pack Sync se cache dans la barre de menus macOS à la fermeture.\nCliquez sur l'icône en haut à droite pour la rouvrir.",
        "de": "Pack Sync verbirgt sich in der macOS-Menüleiste beim Schließen.\nKlicke oben rechts auf das Symbol, um es wieder zu öffnen.",
        "pt": "Pack Sync se esconde na barra de menus do macOS ao fechar.\nClique no ícone no canto superior direito para reabri-la.",
        "ru": "Pack Sync сворачивается в строку меню macOS при закрытии окна.\nНажмите значок в правом верхнем углу, чтобы открыть.",
        "zh": "关闭窗口后，Pack Sync 隐藏在 macOS 菜单栏中。\n点击右上角图标随时重新打开。",
        "ja": "ウィンドウを閉じると Pack Sync は macOS のメニューバーに隠れます。\n右上のアイコンをクリックして再表示できます。",
        "ko": "창을 닫으면 Pack Sync가 macOS 메뉴 막대에 숨습니다.\n오른쪽 상단 아이콘을 클릭해 다시 열 수 있습니다.",
        "it": "Pack Sync si nasconde nella barra dei menu di macOS alla chiusura.\nFai clic sull'icona in alto a destra per riaprirla.",
        "pl": "Pack Sync chowa się na pasku menu macOS po zamknięciu.\nKliknij ikonę w prawym górnym rogu, aby ją ponownie otworzyć.",
        "tr": "Pencereyi kapattığınızda Pack Sync macOS menü çubuğuna gizlenir.\nYeniden açmak için sağ üstteki simgeye tıklayın.",
    },
    "pg_tray_lin": {
        "en": "Pack Sync hides in the system notification area when you close the window.\nClick its icon in the panel to reopen it anytime.",
        "es": "Pack Sync se oculta en el área de notificación al cerrar la ventana.\nHaz clic en su icono en el panel para volver a abrirla.",
        "fr": "Pack Sync se cache dans la zone de notification système à la fermeture.\nCliquez sur son icône dans le panneau pour la rouvrir.",
        "de": "Pack Sync versteckt sich beim Schließen im Systembenachrichtigungsbereich.\nKlicke auf das Symbol im Panel, um es wieder zu öffnen.",
        "pt": "Pack Sync se esconde na área de notificação ao fechar a janela.\nClique em seu ícone no painel para reabri-la.",
        "ru": "Pack Sync сворачивается в системную область уведомлений при закрытии.\nНажмите значок на панели, чтобы открыть снова.",
        "zh": "关闭窗口后，Pack Sync 隐藏在系统通知区域中。\n点击面板中的图标随时重新打开。",
        "ja": "ウィンドウを閉じると Pack Sync はシステム通知エリアに隠れます。\nパネルのアイコンをクリックして再表示できます。",
        "ko": "창을 닫으면 Pack Sync가 시스템 알림 영역에 숨습니다.\n패널의 아이콘을 클릭해 다시 열 수 있습니다.",
        "it": "Pack Sync si nasconde nell'area di notifica di sistema alla chiusura.\nFai clic sull'icona nel pannello per riaprirla.",
        "pl": "Pack Sync chowa się w obszarze powiadomień systemu po zamknięciu.\nKliknij ikonę w panelu, aby ją ponownie otworzyć.",
        "tr": "Pencereyi kapattığınızda Pack Sync sistem bildirim alanına gizlenir.\nYeniden açmak için paneldeki simgeye tıklayın.",
    },
    "pg_tray_tip1": {
        "en": "Files sync the instant you save — no manual step needed.",
        "es": "Los archivos se sincronizan al guardar — sin pasos manuales.",
        "fr": "Les fichiers se synchronisent à la sauvegarde — sans action manuelle.",
        "de": "Dateien werden beim Speichern sofort synchronisiert — kein manueller Schritt.",
        "pt": "Arquivos sincronizam ao salvar — sem etapa manual.",
        "ru": "Файлы синхронизируются мгновенно при сохранении — без лишних действий.",
        "zh": "保存时立即同步文件 — 无需手动操作。",
        "ja": "保存した瞬間にファイルが同期されます — 手動操作は不要です。",
        "ko": "저장하는 즉시 파일이 동기화됩니다 — 수동 단계가 필요 없습니다.",
        "it": "I file si sincronizzano al salvataggio — nessun passaggio manuale.",
        "pl": "Pliki synchronizują się natychmiast po zapisaniu — bez ręcznych kroków.",
        "tr": "Dosyalar kaydeder kaydetmez senkronize edilir — manuel adım gerekmez.",
    },
    "pg_tray_tip2": {
        "en": "Auto-sync: enable ⚡ Auto in the header — branch switches copy files without any dialogs.",
        "es": "Sincronización automática: activa ⚡ Auto en el encabezado — los cambios de rama copian archivos sin diálogos.",
        "fr": "Synchronisation automatique : activez ⚡ Auto dans l'en-tête — les changements de branche copient les fichiers sans dialogue.",
        "de": "Auto-Sync: ⚡ Auto in der Kopfzeile aktivieren — Branch-Wechsel kopieren Dateien ohne Dialoge.",
        "pt": "Sincronização automática: ative ⚡ Auto no cabeçalho — trocas de branch copiam arquivos sem diálogos.",
        "ru": "Авто-синхронизация: включите ⚡ Авто в заголовке — смена ветки копирует файлы без диалогов.",
        "zh": "自动同步：在标题栏启用 ⚡ Auto — 切换分支时自动复制文件，无需确认。",
        "ja": "自動同期：ヘッダーの ⚡ Auto を有効にすると — ブランチ切り替え時にダイアログなしで自動コピー。",
        "ko": "자동 동기화: 헤더에서 ⚡ Auto 활성화 — 브랜치 전환 시 다이얼로그 없이 자동으로 파일 복사.",
        "it": "Sincronizzazione automatica: abilita ⚡ Auto nell'intestazione — i cambi di branch copiano i file senza dialogo.",
        "pl": "Auto-sync: włącz ⚡ Auto w nagłówku — przełączanie gałęzi kopiuje pliki bez dialogów.",
        "tr": "Otomatik eşitleme: başlıkta ⚡ Auto'yu etkinleştirin — dal geçişleri dosyaları diyalog olmadan kopyalar.",
    },
    "pg_tray_tip3": {
        "en": "Double-click any project card to configure folder pairs or enable Regolith controls.",
        "es": "Haz doble clic en la tarjeta del proyecto para configurar carpetas o activar controles Regolith.",
        "fr": "Double-cliquez sur une carte de projet pour configurer les dossiers ou activer les contrôles Regolith.",
        "de": "Doppelklick auf eine Projektkarte zum Konfigurieren von Ordnern oder Aktivieren der Regolith-Steuerung.",
        "pt": "Dê duplo clique em qualquer cartão de projeto para configurar pastas ou ativar controles Regolith.",
        "ru": "Двойной клик по карточке проекта — настройка папок или включение управления Regolith.",
        "zh": "双击项目卡片可配置文件夹对或启用 Regolith 控件。",
        "ja": "プロジェクトカードをダブルクリックしてフォルダーを設定するか Regolith コントロールを有効化。",
        "ko": "프로젝트 카드를 더블클릭하여 폴더를 구성하거나 Regolith 컨트롤을 활성화하세요.",
        "it": "Doppio clic su una scheda progetto per configurare le cartelle o abilitare i controlli Regolith.",
        "pl": "Kliknij dwukrotnie kartę projektu, aby skonfigurować foldery lub włączyć kontrolki Regolith.",
        "tr": "Klasörleri yapılandırmak veya Regolith kontrollerini etkinleştirmek için proje kartına çift tıklayın.",
    },
    "pg_tray_tip4": {
        "en": "Regolith project? Double-click the card → Project Settings → enable Regolith controls.",
        "es": "¿Proyecto Regolith? Doble clic → Configuración del proyecto → activa controles Regolith.",
        "fr": "Projet Regolith ? Double-clic → Paramètres du projet → activez les contrôles Regolith.",
        "de": "Regolith-Projekt? Doppelklick → Projekteinstellungen → Regolith-Steuerung aktivieren.",
        "pt": "Projeto Regolith? Duplo clique → Configurações do projeto → ative os controles Regolith.",
        "ru": "Проект Regolith? Двойной клик → Настройки проекта → включите управление Regolith.",
        "zh": "Regolith 项目？双击卡片 → 项目设置 → 启用 Regolith 控件。",
        "ja": "Regolith プロジェクト？カードをダブルクリック → プロジェクト設定 → Regolith コントロールを有効化。",
        "ko": "Regolith 프로젝트? 카드 더블클릭 → 프로젝트 설정 → Regolith 컨트롤 활성화.",
        "it": "Progetto Regolith? Doppio clic sulla scheda → Impostazioni → abilita i controlli Regolith.",
        "pl": "Projekt Regolith? Podwójne kliknięcie karty → Ustawienia → włącz kontrolki Regolith.",
        "tr": "Regolith projesi? Karta çift tıkla → Proje Ayarları → Regolith kontrollerini etkinleştir.",
    },
    "pg_tray_tip5": {
        "en": "Reopen this guide anytime with the  ?  button in the header.",
        "es": "Reabre esta guía en cualquier momento con el botón  ?  del encabezado.",
        "fr": "Rouvrez ce guide à tout moment avec le bouton  ?  dans l'en-tête.",
        "de": "Öffne diese Anleitung jederzeit mit dem  ?  Button in der Kopfzeile.",
        "pt": "Reabra este guia a qualquer momento com o botão  ?  no cabeçalho.",
        "ru": "Откройте это руководство снова кнопкой  ?  в заголовке.",
        "zh": "随时点击标题中的  ?  按钮重新打开此指南。",
        "ja": "ヘッダーの  ?  ボタンからいつでもこのガイドを再表示できます。",
        "ko": "헤더의  ?  버튼을 눌러 언제든지 이 가이드를 다시 열 수 있습니다.",
        "it": "Riapri questa guida con il pulsante  ?  nell'intestazione in qualsiasi momento.",
        "pl": "Otwórz ten przewodnik ponownie przyciskiem  ?  w nagłówku.",
        "tr": "Bu kılavuzu istediğiniz zaman başlıktaki  ?  düğmesiyle yeniden açın.",
    },
    # ── Onboarding page 1 — How it works ─────────────────────────────────────
    "pg_how_title": {
        "en": "Two ways to work — your choice",
        "es": "Dos formas de trabajar — tu elección",
        "fr": "Deux façons de travailler — votre choix",
        "de": "Zwei Arbeitsweisen — deine Wahl",
        "pt": "Duas formas de trabalhar — sua escolha",
        "ru": "Два способа работы — ваш выбор",
        "zh": "两种工作方式 — 你的选择",
        "ja": "2つの作業方法 — あなたの選択",
        "ko": "두 가지 작업 방식 — 당신의 선택",
        "it": "Due modi di lavorare — la tua scelta",
        "pl": "Dwa sposoby pracy — twój wybór",
        "tr": "İki çalışma yöntemi — senin seçimin",
    },
    "pg_how_a_hdr": {
        "en": "Plain Sync  — any project",
        "es": "Sync simple  — cualquier proyecto",
        "fr": "Sync simple  — tout projet",
        "de": "Einfache Sync  — jedes Projekt",
        "pt": "Sync simples  — qualquer projeto",
        "ru": "Простая синхронизация  — любой проект",
        "zh": "普通同步  — 任意项目",
        "ja": "シンプル同期  — どのプロジェクトでも",
        "ko": "일반 동기화  — 모든 프로젝트",
        "it": "Sync semplice  — qualsiasi progetto",
        "pl": "Prosta Sync  — dowolny projekt",
        "tr": "Basit Sync  — herhangi bir proje",
    },
    "pg_how_a_body": {
        "en": "Pack Sync watches your repo and instantly copies changed files\ninto com.mojang — no filters, no build step, zero CPU at idle.\nSync is one-way: repo → output only.",
        "es": "Pack Sync vigila tu repositorio y copia al instante los archivos cambiados\nen com.mojang — sin filtros, sin compilación, CPU cero en reposo.\nLa sincronización es unidireccional: repositorio → salida.",
        "fr": "Pack Sync surveille votre dépôt et copie instantanément les fichiers modifiés\ndans com.mojang — sans filtres, sans build, CPU zéro au repos.\nLa synchronisation est unidirectionnelle : dépôt → sortie.",
        "de": "Pack Sync überwacht dein Repo und kopiert geänderte Dateien sofort\nin com.mojang — keine Filter, kein Build, CPU im Leerlauf null.\nSync ist einseitig: Repo → Ausgabe.",
        "pt": "Pack Sync monitora seu repositório e copia instantaneamente os arquivos alterados\npara com.mojang — sem filtros, sem build, CPU zero em repouso.\nA sincronização é unidirecional: repositório → saída.",
        "ru": "Pack Sync следит за репозиторием и мгновенно копирует изменённые файлы\nв com.mojang — без фильтров, без сборки, нулевой CPU в ожидании.\nСинхронизация односторонняя: репозиторий → выходная папка.",
        "zh": "Pack Sync 监视你的仓库并立即将更改的文件复制到 com.mojang\n— 无过滤器，无构建步骤，空闲时零 CPU。\n同步是单向的：仓库 → 输出。",
        "ja": "Pack Sync はリポジトリを監視し、変更ファイルを即座に com.mojang にコピーします\n— フィルターなし、ビルドなし、アイドル時 CPU ゼロ。\n同期は一方向です：リポジトリ → 出力。",
        "ko": "Pack Sync는 저장소를 모니터링하며 변경된 파일을 즉시 com.mojang에 복사합니다\n— 필터 없음, 빌드 없음, 유휴 CPU 제로.\n동기화는 단방향입니다: 저장소 → 출력.",
        "it": "Pack Sync monitora il repository e copia immediatamente i file modificati\nin com.mojang — nessun filtro, nessun build, CPU zero a riposo.\nLa sincronizzazione è unidirezionale: repository → output.",
        "pl": "Pack Sync monitoruje repozytorium i natychmiast kopiuje zmienione pliki\ndo com.mojang — bez filtrów, bez budowania, zero CPU w trybie bezczynności.\nSync jest jednokierunkowy: repozytorium → wyjście.",
        "tr": "Pack Sync deponuzu izler ve değişen dosyaları anında com.mojang'a kopyalar\n— filtre yok, derleme yok, boşta CPU sıfır.\nEşitleme tek yönlüdür: depo → çıktı.",
    },
    "pg_how_b_hdr": {
        "en": "⚙ Regolith Build  — detected automatically",
        "es": "⚙ Regolith Build  — detectado automáticamente",
        "fr": "⚙ Regolith Build  — détecté automatiquement",
        "de": "⚙ Regolith Build  — wird automatisch erkannt",
        "pt": "⚙ Regolith Build  — detectado automaticamente",
        "ru": "⚙ Regolith Build  — обнаруживается автоматически",
        "zh": "⚙ Regolith Build  — 自动检测",
        "ja": "⚙ Regolith Build  — 自動検出",
        "ko": "⚙ Regolith Build  — 자동으로 감지됨",
        "it": "⚙ Regolith Build  — rilevato automaticamente",
        "pl": "⚙ Regolith Build  — wykrywany automatycznie",
        "tr": "⚙ Regolith Build  — otomatik olarak algılandı",
    },
    "pg_how_b_body": {
        "en": "Pack Sync auto-detects Regolith projects (config.json in the repo root).\nTo unlock ▶ Build: double-click the project card and enable\n«Show Regolith controls» in Project Settings.\nBuild runs the full filter pipeline then exports to com.mojang.",
        "es": "Pack Sync detecta automáticamente proyectos Regolith (config.json en la raíz del repositorio).\nPara desbloquear ▶ Build: haz doble clic en la tarjeta del proyecto y activa\n«Mostrar controles Regolith» en Configuración del proyecto.\nBuild ejecuta el pipeline de filtros completo y exporta a com.mojang.",
        "fr": "Pack Sync détecte automatiquement les projets Regolith (config.json à la racine du dépôt).\nPour débloquer ▶ Build : double-cliquez sur la carte du projet et activez\n«Afficher les contrôles Regolith» dans Paramètres du projet.\nBuild exécute tout le pipeline de filtres puis exporte vers com.mojang.",
        "de": "Pack Sync erkennt automatisch Regolith-Projekte (config.json im Repo-Stammverzeichnis).\nUm ▶ Build zu entsperren: Doppelklick auf die Projektkarte und\n«Regolith-Steuerung anzeigen» in Projekteinstellungen aktivieren.\nBuild führt die gesamte Filter-Pipeline aus und exportiert nach com.mojang.",
        "pt": "Pack Sync detecta automaticamente projetos Regolith (config.json na raiz do repositório).\nPara desbloquear ▶ Build: dê duplo clique no cartão do projeto e ative\n«Mostrar controles Regolith» nas Configurações do projeto.\nBuild executa o pipeline de filtros completo e exporta para com.mojang.",
        "ru": "Pack Sync автоматически обнаруживает проекты Regolith (config.json в корне репо).\nЧтобы разблокировать ▶ Сборка: двойной клик по карточке и включите\n«Показать управление Regolith» в настройках проекта.\nСборка запускает полный конвейер фильтров и экспортирует в com.mojang.",
        "zh": "Pack Sync 自动检测 Regolith 项目（repo 根目录中的 config.json）。\n要解锁 ▶ Build：双击项目卡片并启用\n项目设置中的「显示 Regolith 控件」。\nBuild 运行完整的过滤器管道并导出到 com.mojang。",
        "ja": "Pack Sync は Regolith プロジェクト（リポジトリルートの config.json）を自動検出します。\n▶ Build を解放するには：プロジェクトカードをダブルクリックして\nプロジェクト設定の「Regolith コントロールを表示」を有効化。\nBuild はフィルターパイプライン全体を実行し com.mojang にエクスポートします。",
        "ko": "Pack Sync는 Regolith 프로젝트(리포지토리 루트의 config.json)를 자동 감지합니다.\n▶ Build를 해제하려면: 프로젝트 카드를 더블클릭하고\n프로젝트 설정에서 「Regolith 컨트롤 표시」를 활성화하세요.\nBuild는 전체 필터 파이프라인을 실행하고 com.mojang으로 내보냅니다.",
        "it": "Pack Sync rileva automaticamente i progetti Regolith (config.json nella radice del repository).\nPer sbloccare ▶ Build: fai doppio clic sulla scheda del progetto e abilita\n«Mostra controlli Regolith» nelle Impostazioni del progetto.\nBuild esegue la pipeline di filtri completa e la esporta in com.mojang.",
        "pl": "Pack Sync automatycznie wykrywa projekty Regolith (config.json w katalogu głównym repozytorium).\nAby odblokować ▶ Build: kliknij dwukrotnie kartę projektu i włącz\n«Pokaż kontrolki Regolith» w Ustawieniach projektu.\nBuild uruchamia cały potok filtrów i eksportuje do com.mojang.",
        "tr": "Pack Sync, Regolith projelerini (depo kökündeki config.json) otomatik olarak algılar.\n▶ Build'i açmak için: proje kartına çift tıklayın ve\nProje Ayarlarında «Regolith kontrollerini göster»'i etkinleştirin.\nBuild tüm filtre ardışık düzenini çalıştırır ve com.mojang'a aktarır.",
    },
    # ── Onboarding page 2 — Adding projects ───────────────────────────────────
    "pg_proj_title": {
        "en": "📂  Adding Your Projects",
        "es": "📂  Añadir tus proyectos",
        "fr": "📂  Ajouter vos projets",
        "de": "📂  Projekte hinzufügen",
        "pt": "📂  Adicionando seus projetos",
        "ru": "📂  Добавление проектов",
        "zh": "📂  添加你的项目",
        "ja": "📂  プロジェクトの追加",
        "ko": "📂  프로젝트 추가",
        "it": "📂  Aggiungere i tuoi progetti",
        "pl": "📂  Dodawanie projektów",
        "tr": "📂  Projelerini ekleme",
    },
    "pg_proj_sub": {
        "en": "Point Pack Sync at your GitHub folder once — it finds projects automatically.",
        "es": "Apunta Pack Sync a tu carpeta de GitHub una vez — encuentra proyectos automáticamente.",
        "fr": "Pointez Pack Sync vers votre dossier GitHub une fois — il trouve les projets automatiquement.",
        "de": "Weise Pack Sync einmalig auf deinen GitHub-Ordner — es findet Projekte automatisch.",
        "pt": "Aponte Pack Sync para sua pasta GitHub uma vez — ele encontra projetos automaticamente.",
        "ru": "Укажите Pack Sync папку GitHub один раз — проекты находятся автоматически.",
        "zh": "将 Pack Sync 指向你的 GitHub 文件夹一次 — 它会自动查找项目。",
        "ja": "Pack Sync を GitHub フォルダに一度向けるだけ — プロジェクトを自動検出します。",
        "ko": "Pack Sync를 GitHub 폴더로 한 번 지정하면 — 프로젝트를 자동으로 찾습니다.",
        "it": "Punta Pack Sync sulla tua cartella GitHub una volta — trova i progetti automaticamente.",
        "pl": "Wskaż Pack Sync folder GitHub raz — znajdzie projekty automatycznie.",
        "tr": "Pack Sync'i GitHub klasörünüze bir kez yönlendirin — projeleri otomatik olarak bulur.",
    },
    "pg_proj_auto": {
        "en": "Projects appear automatically — no manual import needed",
        "es": "Los proyectos aparecen automáticamente — sin importación manual",
        "fr": "Les projets apparaissent automatiquement — pas d'import manuel",
        "de": "Projekte erscheinen automatisch — kein manueller Import nötig",
        "pt": "Projetos aparecem automaticamente — sem importação manual",
        "ru": "Проекты появляются автоматически — ручной импорт не нужен",
        "zh": "项目自动出现 — 无需手动导入",
        "ja": "プロジェクトが自動的に表示されます — 手動インポート不要",
        "ko": "프로젝트가 자동으로 나타납니다 — 수동 가져오기 불필요",
        "it": "I progetti appaiono automaticamente — nessuna importazione manuale",
        "pl": "Projekty pojawiają się automatycznie — bez ręcznego importu",
        "tr": "Projeler otomatik görünür — manuel içe aktarma gerekmez",
    },
    "pg_proj_step1": {
        "en": "Open Settings and set your GitHub folder path.",
        "es": "Abre Ajustes y establece la ruta de tu carpeta de GitHub.",
        "fr": "Ouvrez Paramètres et définissez le chemin de votre dossier GitHub.",
        "de": "Öffne Einstellungen und setze den Pfad zu deinem GitHub-Ordner.",
        "pt": "Abra Configurações e defina o caminho da sua pasta GitHub.",
        "ru": "Откройте Настройки и укажите путь к папке GitHub.",
        "zh": "打开设置并设置你的 GitHub 文件夹路径。",
        "ja": "設定を開き、GitHub フォルダのパスを設定します。",
        "ko": "설정을 열고 GitHub 폴더 경로를 설정합니다.",
        "it": "Apri Impostazioni e imposta il percorso della tua cartella GitHub.",
        "pl": "Otwórz Ustawienia i ustaw ścieżkę do folderu GitHub.",
        "tr": "Ayarları açın ve GitHub klasör yolunuzu ayarlayın.",
    },
    "pg_proj_step2": {
        "en": "Every subfolder with a Bedrock pack is detected instantly.",
        "es": "Cada subcarpeta con un pack de Bedrock se detecta al instante.",
        "fr": "Chaque sous-dossier avec un pack Bedrock est détecté instantanément.",
        "de": "Jeder Unterordner mit einem Bedrock-Pack wird sofort erkannt.",
        "pt": "Cada subpasta com um pack Bedrock é detectada instantaneamente.",
        "ru": "Каждая подпапка с паком Bedrock обнаруживается мгновенно.",
        "zh": "每个包含 Bedrock 包的子文件夹都会被立即检测到。",
        "ja": "Bedrock パックを含むすべてのサブフォルダが即座に検出されます。",
        "ko": "Bedrock 팩이 있는 모든 하위 폴더가 즉시 감지됩니다.",
        "it": "Ogni sottocartella con un pack Bedrock viene rilevata immediatamente.",
        "pl": "Każdy podfolder z paczką Bedrock jest wykrywany natychmiast.",
        "tr": "Bedrock paketi olan her alt klasör anında algılanır.",
    },
    "pg_proj_step3": {
        "en": "Regolith projects auto-detected — double-click the card to enable the Build button.",
        "es": "Proyectos Regolith detectados automáticamente — doble clic en la tarjeta para activar Build.",
        "fr": "Projets Regolith détectés automatiquement — double-clic sur la carte pour activer Build.",
        "de": "Regolith-Projekte automatisch erkannt — Doppelklick auf die Karte zum Aktivieren von Build.",
        "pt": "Projetos Regolith detectados automaticamente — duplo clique no cartão para ativar Build.",
        "ru": "Проекты Regolith обнаруживаются автоматически — двойной клик по карточке для включения Сборки.",
        "zh": "Regolith 项目自动检测 — 双击卡片以启用 Build 按钮。",
        "ja": "Regolith プロジェクトを自動検出 — カードをダブルクリックして Build ボタンを有効化。",
        "ko": "Regolith 프로젝트 자동 감지 — 카드를 더블클릭하여 Build 버튼을 활성화하세요.",
        "it": "Progetti Regolith rilevati automaticamente — doppio clic sulla scheda per abilitare Build.",
        "pl": "Projekty Regolith wykrywane automatycznie — kliknij dwukrotnie kartę, aby włączyć Build.",
        "tr": "Regolith projeleri otomatik algılandı — Build düğmesini etkinleştirmek için karta çift tıklayın.",
    },
    # ── Onboarding page 3 — Sync in action ────────────────────────────────────
    "pg_sync_title": {
        "en": "⚡  Sync in Action",
        "es": "⚡  Sincronización en acción",
        "fr": "⚡  Sync en action",
        "de": "⚡  Sync in Aktion",
        "pt": "⚡  Sync em ação",
        "ru": "⚡  Синхронизация в действии",
        "zh": "⚡  同步运行中",
        "ja": "⚡  同期の動作",
        "ko": "⚡  동기화 실행 중",
        "it": "⚡  Sync in azione",
        "pl": "⚡  Sync w akcji",
        "tr": "⚡  Sync çalışıyor",
    },
    "pg_sync_sub": {
        "en": "Files are copied the instant you save — you never press Sync manually.",
        "es": "Los archivos se copian en el momento de guardar — nunca pulsas Sync manualmente.",
        "fr": "Les fichiers sont copiés dès que vous sauvegardez — vous n'appuyez jamais sur Sync.",
        "de": "Dateien werden beim Speichern sofort kopiert — du drückst nie manuell Sync.",
        "pt": "Os arquivos são copiados no instante em que você salva — nunca pressiona Sync manualmente.",
        "ru": "Файлы копируются в момент сохранения — вам никогда не нужно нажимать Sync вручную.",
        "zh": "保存时立即复制文件 — 你无需手动按 Sync。",
        "ja": "保存した瞬間にファイルがコピーされます — 手動で Sync を押す必要はありません。",
        "ko": "저장하는 즉시 파일이 복사됩니다 — 수동으로 Sync를 누를 필요가 없습니다.",
        "it": "I file vengono copiati nel momento in cui salvi — non devi mai premere Sync manualmente.",
        "pl": "Pliki są kopiowane w momencie zapisu — nigdy nie naciskasz Sync ręcznie.",
        "tr": "Kaydettiğiniz anda dosyalar kopyalanır — Sync'e hiç manuel basmanıza gerek kalmaz.",
    },
    "pg_sync_newer": {
        "en": "Newer file wins — no overwrites of fresh edits",
        "es": "El archivo más reciente gana — sin sobrescribir ediciones recientes",
        "fr": "Le fichier le plus récent gagne — pas d'écrasement des modifications fraîches",
        "de": "Die neuere Datei gewinnt — keine Überschreibung frischer Bearbeitungen",
        "pt": "O arquivo mais recente vence — sem sobrescrever edições recentes",
        "ru": "Побеждает более новый файл — свежие правки не перезаписываются",
        "zh": "较新的文件优先 — 不会覆盖新鲜的编辑",
        "ja": "新しいファイルが勝ちます — 新しい編集は上書きされません",
        "ko": "더 새로운 파일이 우선 — 최신 편집이 덮어쓰이지 않습니다",
        "it": "Il file più recente vince — nessuna sovrascrittura delle modifiche recenti",
        "pl": "Nowszy plik wygrywa — brak nadpisywania świeżych edycji",
        "tr": "Daha yeni dosya kazanır — taze düzenlemeler üzerine yazılmaz",
    },
    "pg_sync_fact1": {
        "en": "Green dot = pack is currently synced to com.mojang.",
        "es": "Punto verde = el pack está sincronizado actualmente con com.mojang.",
        "fr": "Point vert = le pack est actuellement synchronisé avec com.mojang.",
        "de": "Grüner Punkt = Pack ist aktuell mit com.mojang synchronisiert.",
        "pt": "Ponto verde = pack está atualmente sincronizado com com.mojang.",
        "ru": "Зелёная точка = пак сейчас синхронизирован с com.mojang.",
        "zh": "绿点 = 包当前已同步到 com.mojang。",
        "ja": "緑の点 = パックが現在 com.mojang と同期済みです。",
        "ko": "초록 점 = 팩이 현재 com.mojang과 동기화됨.",
        "it": "Punto verde = il pack è attualmente sincronizzato con com.mojang.",
        "pl": "Zielona kropka = paczka jest aktualnie zsynchronizowana z com.mojang.",
        "tr": "Yeşil nokta = paket şu anda com.mojang ile senkronize.",
    },
    "pg_sync_fact2": {
        "en": "Empty dot = pack not yet copied — hit Sync to start.",
        "es": "Punto vacío = pack aún no copiado — pulsa Sync para comenzar.",
        "fr": "Point vide = pack pas encore copié — cliquez sur Sync pour démarrer.",
        "de": "Leerer Punkt = Pack noch nicht kopiert — drücke Sync zum Starten.",
        "pt": "Ponto vazio = pack ainda não copiado — pressione Sync para iniciar.",
        "ru": "Пустая точка = пак ещё не скопирован — нажмите Sync для начала.",
        "zh": "空点 = 包尚未复制 — 点击 Sync 开始。",
        "ja": "空の点 = パックがまだコピーされていません — Sync を押して開始します。",
        "ko": "빈 점 = 팩이 아직 복사되지 않음 — Sync를 눌러 시작합니다.",
        "it": "Punto vuoto = pack non ancora copiato — premi Sync per iniziare.",
        "pl": "Pusta kropka = paczka jeszcze nie skopiowana — naciśnij Sync, aby rozpocząć.",
        "tr": "Boş nokta = paket henüz kopyalanmadı — başlamak için Sync'e basın.",
    },
    "pg_sync_fact3": {
        "en": "→ One-way: files copy from your repo (input) to the output folder only.",
        "es": "→ Unidireccional: los archivos se copian solo desde el repositorio (entrada) a la carpeta de salida.",
        "fr": "→ Unidirectionnel : les fichiers sont copiés depuis le dépôt (entrée) vers le dossier de sortie uniquement.",
        "de": "→ Einseitig: Dateien werden nur vom Repository (Eingang) in den Ausgabeordner kopiert.",
        "pt": "→ Unidirecional: os arquivos são copiados apenas do repositório (entrada) para a pasta de saída.",
        "ru": "→ Односторонняя: файлы копируются только из репозитория (входная папка) в выходную папку.",
        "zh": "→ 单向：文件仅从仓库（输入）复制到输出文件夹。",
        "ja": "→ 一方向：ファイルはリポジトリ（入力）から出力フォルダーにのみコピーされます。",
        "ko": "→ 단방향: 파일은 저장소(입력)에서 출력 폴더로만 복사됩니다.",
        "it": "→ Unidirezionale: i file vengono copiati solo dal repository (input) alla cartella di output.",
        "pl": "→ Jednokierunkowy: pliki kopiowane są tylko z repozytorium (wejście) do folderu wyjściowego.",
        "tr": "→ Tek yönlü: dosyalar yalnızca depodan (giriş) çıktı klasörüne kopyalanır.",
    },
    "pg_sync_fact4": {
        "en": "Zero CPU at idle — watcher sleeps until a file changes.",
        "es": "CPU cero en reposo — el vigilante duerme hasta que cambia un archivo.",
        "fr": "CPU zéro au repos — le surveillant dort jusqu'à ce qu'un fichier change.",
        "de": "CPU null im Leerlauf — der Beobachter schläft bis eine Datei sich ändert.",
        "pt": "CPU zero em repouso — o observador dorme até que um arquivo mude.",
        "ru": "Нулевой CPU в ожидании — наблюдатель спит до изменения файла.",
        "zh": "空闲时零 CPU — 监视器休眠直到文件发生变化。",
        "ja": "アイドル時 CPU ゼロ — ファイルが変更されるまでウォッチャーが眠ります。",
        "ko": "유휴 시 CPU 제로 — 파일이 변경될 때까지 감시자가 잠듭니다.",
        "it": "CPU zero a riposo — il watcher dorme finché un file non cambia.",
        "pl": "Zero CPU w trybie bezczynności — obserwator śpi do zmiany pliku.",
        "tr": "Boşta CPU sıfır — izleyici bir dosya değişene kadar uyur.",
    },
    # ── Onboarding page 4 — Regolith ──────────────────────────────────────────
    "pg_reg_title": {
        "en": "⚙  Building with Regolith",
        "es": "⚙  Compilando con Regolith",
        "fr": "⚙  Compiler avec Regolith",
        "de": "⚙  Mit Regolith bauen",
        "pt": "⚙  Compilando com Regolith",
        "ru": "⚙  Сборка с Regolith",
        "zh": "⚙  使用 Regolith 构建",
        "ja": "⚙  Regolith でビルド",
        "ko": "⚙  Regolith로 빌드하기",
        "it": "⚙  Compilare con Regolith",
        "pl": "⚙  Budowanie z Regolith",
        "tr": "⚙  Regolith ile derleme",
    },
    "pg_reg_sub": {
        "en": "If your project uses Regolith, Pack Sync adds a Build button with a live terminal.",
        "es": "Si tu proyecto usa Regolith, Pack Sync añade un botón Build con terminal en vivo.",
        "fr": "Si votre projet utilise Regolith, Pack Sync ajoute un bouton Build avec terminal live.",
        "de": "Wenn dein Projekt Regolith verwendet, fügt Pack Sync einen Build-Button mit Live-Terminal hinzu.",
        "pt": "Se seu projeto usa Regolith, Pack Sync adiciona um botão Build com terminal ao vivo.",
        "ru": "Если проект использует Regolith, Pack Sync добавляет кнопку Build с живым терминалом.",
        "zh": "如果你的项目使用 Regolith，Pack Sync 会添加一个带实时终端的 Build 按钮。",
        "ja": "プロジェクトが Regolith を使用している場合、Pack Sync はライブターミナル付きの Build ボタンを追加します。",
        "ko": "프로젝트가 Regolith를 사용하면 Pack Sync는 라이브 터미널이 있는 Build 버튼을 추가합니다.",
        "it": "Se il tuo progetto usa Regolith, Pack Sync aggiunge un pulsante Build con terminale live.",
        "pl": "Jeśli projekt używa Regolith, Pack Sync dodaje przycisk Build z terminalem na żywo.",
        "tr": "Projeniz Regolith kullanıyorsa Pack Sync, canlı terminalli bir Build düğmesi ekler.",
    },
    "pg_reg_fact1": {
        "en": "Hit Build to run the full filter pipeline — a live terminal shows progress.",
        "es": "Pulsa Build para ejecutar el pipeline completo — un terminal en vivo muestra el progreso.",
        "fr": "Cliquez sur Build pour lancer le pipeline complet — un terminal live affiche la progression.",
        "de": "Klicke auf Build, um die volle Pipeline auszuführen — ein Live-Terminal zeigt den Fortschritt.",
        "pt": "Clique em Build para executar o pipeline completo — um terminal ao vivo mostra o progresso.",
        "ru": "Нажмите Build для запуска полного конвейера — живой терминал показывает прогресс.",
        "zh": "点击 Build 运行完整管道 — 实时终端显示进度。",
        "ja": "Build を押してパイプライン全体を実行 — ライブターミナルが進捗を表示します。",
        "ko": "Build를 눌러 전체 파이프라인 실행 — 라이브 터미널이 진행 상황을 표시합니다.",
        "it": "Premi Build per eseguire la pipeline completa — un terminale live mostra il progresso.",
        "pl": "Kliknij Build, aby uruchomić pełny potok — terminal na żywo pokazuje postęp.",
        "tr": "Tam ardışık düzeni çalıştırmak için Build'e basın — canlı terminal ilerlemeyi gösterir.",
    },
    "pg_reg_fact2": {
        "en": "Sync (without Build) copies raw source files instantly, skipping filters.",
        "es": "Sync (sin Build) copia los archivos fuente al instante, saltando los filtros.",
        "fr": "Sync (sans Build) copie les fichiers source instantanément, en sautant les filtres.",
        "de": "Sync (ohne Build) kopiert Quelldateien sofort, ohne Filter.",
        "pt": "Sync (sem Build) copia os arquivos fonte instantaneamente, pulando os filtros.",
        "ru": "Sync (без Build) мгновенно копирует исходные файлы, пропуская фильтры.",
        "zh": "Sync（不含 Build）立即复制原始源文件，跳过过滤器。",
        "ja": "Sync（Build なし）はフィルターをスキップして元のソースファイルを即座にコピーします。",
        "ko": "Sync (Build 없이)는 필터를 건너뛰고 원본 소스 파일을 즉시 복사합니다.",
        "it": "Sync (senza Build) copia immediatamente i file sorgente grezzi, saltando i filtri.",
        "pl": "Sync (bez Build) kopiuje surowe pliki źródłowe natychmiast, pomijając filtry.",
        "tr": "Sync (Build olmadan) filtreleri atlayarak ham kaynak dosyaları anında kopyalar.",
    },
    "pg_reg_fact3": {
        "en": "Profile dropdown selects which Regolith profile to run (dev / release).",
        "es": "El menú de perfil selecciona qué perfil de Regolith ejecutar (dev / release).",
        "fr": "Le menu déroulant de profil choisit quel profil Regolith exécuter (dev / release).",
        "de": "Das Profil-Dropdown wählt, welches Regolith-Profil ausgeführt wird (dev / release).",
        "pt": "O menu suspenso de perfil seleciona qual perfil Regolith executar (dev / release).",
        "ru": "Выпадающий список профилей выбирает профиль Regolith (dev / release).",
        "zh": "配置文件下拉菜单选择运行哪个 Regolith 配置文件（dev / release）。",
        "ja": "プロファイルドロップダウンで実行する Regolith プロファイルを選択します（dev / release）。",
        "ko": "프로필 드롭다운으로 실행할 Regolith 프로필을 선택합니다 (dev / release).",
        "it": "Il menu a tendina del profilo seleziona quale profilo Regolith eseguire (dev / release).",
        "pl": "Lista rozwijana profilu wybiera, który profil Regolith uruchomić (dev / release).",
        "tr": "Profil açılır menüsü hangi Regolith profilinin çalıştırılacağını seçer (dev / release).",
    },
    "pg_reg_fact4": {
        "en": "Double-click the project card → Project Settings → enable Regolith controls to show Build.",
        "es": "Doble clic en la tarjeta → Configuración del proyecto → activa controles Regolith para mostrar Build.",
        "fr": "Double-clic sur la carte → Paramètres → activez les contrôles Regolith pour afficher Build.",
        "de": "Doppelklick auf die Karte → Projekteinstellungen → Regolith-Steuerung aktivieren, um Build anzuzeigen.",
        "pt": "Duplo clique no cartão → Configurações → ative controles Regolith para mostrar Build.",
        "ru": "Двойной клик по карточке → Настройки проекта → включите управление Regolith для показа Сборки.",
        "zh": "双击项目卡片 → 项目设置 → 启用 Regolith 控件以显示 Build。",
        "ja": "プロジェクトカードをダブルクリック → プロジェクト設定 → Regolith コントロールを有効化して Build を表示。",
        "ko": "프로젝트 카드 더블클릭 → 프로젝트 설정 → Regolith 컨트롤 활성화 → Build 표시.",
        "it": "Doppio clic sulla scheda → Impostazioni → abilita i controlli Regolith per mostrare Build.",
        "pl": "Podwójne kliknięcie karty → Ustawienia → włącz kontrolki Regolith, aby pokazać Build.",
        "tr": "Karta çift tıkla → Proje Ayarları → Build'i göstermek için Regolith kontrollerini etkinleştir.",
    },
    # ── Onboarding page 5 — Tips & Tricks ─────────────────────────────────────
    "pg_tips_title": {
        "en": "✨  Tips & Tricks",
        "es": "✨  Consejos y trucos",
        "fr": "✨  Astuces et conseils",
        "de": "✨  Tipps & Tricks",
        "pt": "✨  Dicas e truques",
        "ru": "✨  Советы и подсказки",
        "zh": "✨  技巧与提示",
        "ja": "✨  ヒントとコツ",
        "ko": "✨  팁 & 트릭",
        "it": "✨  Consigli e trucchi",
        "pl": "✨  Wskazówki i triki",
        "tr": "✨  İpuçları ve Püf Noktaları",
    },
    "pg_tip1_hdr": {
        "en": "Search bar",       "es": "Barra de búsqueda", "fr": "Barre de recherche",
        "de": "Suchleiste",       "pt": "Barra de pesquisa", "ru": "Строка поиска",
        "zh": "搜索栏",            "ja": "検索バー",           "ko": "검색 창",
        "it": "Barra di ricerca", "pl": "Pasek wyszukiwania", "tr": "Arama çubuğu",
    },
    "pg_tip1_txt": {
        "en": "Type part of a project name to filter the list instantly.",
        "es": "Escribe parte del nombre de un proyecto para filtrar la lista al instante.",
        "fr": "Tapez une partie du nom d'un projet pour filtrer la liste instantanément.",
        "de": "Gib einen Teil des Projektnamens ein, um die Liste sofort zu filtern.",
        "pt": "Digite parte do nome de um projeto para filtrar a lista instantaneamente.",
        "ru": "Введите часть названия проекта, чтобы мгновенно отфильтровать список.",
        "zh": "输入项目名称的一部分即可立即过滤列表。",
        "ja": "プロジェクト名の一部を入力してリストを即座にフィルタリングします。",
        "ko": "프로젝트 이름의 일부를 입력하면 목록이 즉시 필터링됩니다.",
        "it": "Digita parte del nome di un progetto per filtrare la lista istantaneamente.",
        "pl": "Wpisz część nazwy projektu, aby natychmiast przefiltrować listę.",
        "tr": "Listeyi anında filtrelemek için proje adının bir kısmını yazın.",
    },
    "pg_tip2_hdr": {
        "en": "Auto-sync",              "es": "Sincronización automática", "fr": "Sync automatique",
        "de": "Auto-Sync",              "pt": "Sincronização automática",  "ru": "Авто-синхронизация",
        "zh": "自动同步",               "ja": "自動同期",                   "ko": "자동 동기화",
        "it": "Sync automatico",        "pl": "Auto-synchronizacja",       "tr": "Otomatik eşitleme",
    },
    "pg_tip2_txt": {
        "en": "Enable ⚡ Auto in the header to sync automatically\nwhen your git branch changes — no dialogs.",
        "es": "Activa ⚡ Auto en el encabezado para sincronizar automáticamente\ncuando cambia tu rama git — sin diálogos.",
        "fr": "Activez ⚡ Auto dans l'en-tête pour synchroniser automatiquement\nlorsque votre branche git change — sans dialogues.",
        "de": "Aktiviere ⚡ Auto in der Kopfzeile, um automatisch zu synchronisieren,\nwenn sich dein Git-Branch ändert — keine Dialoge.",
        "pt": "Ative ⚡ Auto no cabeçalho para sincronizar automaticamente\nquando sua branch git mudar — sem diálogos.",
        "ru": "Включи ⚡ Авто в заголовке для автоматической синхронизации\nпри смене ветки git — без диалогов.",
        "zh": "在标题栏启用 ⚡ Auto，git 分支切换时自动同步，无需对话框。",
        "ja": "ヘッダーの ⚡ Auto を有効にすると、git ブランチが変わると自動的に同期されます — ダイアログなし。",
        "ko": "헤더에서 ⚡ Auto를 활성화하면 git 브랜치 변경 시 자동 동기화됩니다 — 대화 상자 없음.",
        "it": "Abilita ⚡ Auto nell'intestazione per sincronizzare automaticamente\nquando cambia il branch git — senza dialoghi.",
        "pl": "Włącz ⚡ Auto w nagłówku, aby synchronizować automatycznie\ngdy zmienia się gałąź git — bez dialogów.",
        "tr": "Git dalı değiştiğinde otomatik eşitlemek için\nbașlıkta ⚡ Auto'yu etkinleştirin — iletişim kutusu yok.",
    },
    "pg_tip3_hdr": {
        "en": "16 languages",    "es": "16 idiomas",       "fr": "16 langues",
        "de": "16 Sprachen",     "pt": "16 idiomas",       "ru": "16 языков",
        "zh": "16 种语言",        "ja": "16 言語",           "ko": "16개 언어",
        "it": "16 lingue",       "pl": "16 języków",       "tr": "16 dil",
    },
    "pg_tip3_txt": {
        "en": "Settings → Language to switch the UI language.",
        "es": "Ajustes → Idioma para cambiar el idioma de la interfaz.",
        "fr": "Paramètres → Langue pour changer la langue de l'interface.",
        "de": "Einstellungen → Sprache zum Wechseln der UI-Sprache.",
        "pt": "Configurações → Idioma para trocar o idioma da interface.",
        "ru": "Настройки → Язык для смены языка интерфейса.",
        "zh": "设置 → 语言以切换界面语言。",
        "ja": "設定 → 言語で UI 言語を切り替えます。",
        "ko": "설정 → 언어에서 UI 언어를 전환합니다.",
        "it": "Impostazioni → Lingua per cambiare la lingua dell'interfaccia.",
        "pl": "Ustawienia → Język, aby zmienić język interfejsu.",
        "tr": "Ayarlar → Dil ile arayüz dilini değiştirebilirsiniz.",
    },
    "pg_tip4_hdr": {
        "en": "Tray menu",      "es": "Menú de bandeja",  "fr": "Menu de la zone de notification",
        "de": "Tray-Menü",      "pt": "Menu da bandeja",  "ru": "Меню трея",
        "zh": "托盘菜单",        "ja": "トレイメニュー",    "ko": "트레이 메뉴",
        "it": "Menu dell'area di notifica", "pl": "Menu zasobnika", "tr": "Tepsi menüsü",
    },
    "pg_tip4_txt": {
        "en": "Right-click the tray icon for quick Sync All or Quit.",
        "es": "Clic derecho en el icono de la bandeja para Sincronizar todo o Salir.",
        "fr": "Clic droit sur l'icône de notification pour Tout synchroniser ou Quitter.",
        "de": "Rechtsklick auf das Tray-Symbol für Alles synchronisieren oder Beenden.",
        "pt": "Clique com o botão direito no ícone da bandeja para Sincronizar tudo ou Sair.",
        "ru": "Нажмите правой кнопкой на значок трея для быстрого Sync All или Выхода.",
        "zh": "右键单击托盘图标可快速同步全部或退出。",
        "ja": "トレイアイコンを右クリックしてすべて同期または終了を素早く選択できます。",
        "ko": "트레이 아이콘을 우클릭하면 모두 동기화 또는 종료를 빠르게 선택할 수 있습니다.",
        "it": "Clic destro sull'icona della notifica per Sincronizza tutto o Esci.",
        "pl": "Kliknij prawym przyciskiem ikonę zasobnika, aby szybko zsynchronizować wszystko lub wyjść.",
        "tr": "Hızlı Tümünü Eşitle veya Çık için tepsi simgesine sağ tıklayın.",
    },
    "pg_tip5_hdr": {
        "en": "This guide",     "es": "Esta guía",        "fr": "Ce guide",
        "de": "Diese Anleitung","pt": "Este guia",        "ru": "Это руководство",
        "zh": "本指南",          "ja": "このガイド",        "ko": "이 가이드",
        "it": "Questa guida",   "pl": "Ten przewodnik",   "tr": "Bu kılavuz",
    },
    "pg_tip5_txt": {
        "en": "Click the ? button in the header to reopen this guide.",
        "es": "Haz clic en el botón ? del encabezado para volver a abrir esta guía.",
        "fr": "Cliquez sur le bouton ? dans l'en-tête pour rouvrir ce guide.",
        "de": "Klicke auf den ?-Button im Header, um diese Anleitung wieder zu öffnen.",
        "pt": "Clique no botão ? no cabeçalho para reabrir este guia.",
        "ru": "Нажмите кнопку ? в заголовке, чтобы открыть это руководство снова.",
        "zh": "点击标题中的 ? 按钮重新打开本指南。",
        "ja": "ヘッダーの ? ボタンをクリックしてこのガイドを再表示します。",
        "ko": "헤더의 ? 버튼을 클릭해 이 가이드를 다시 엽니다.",
        "it": "Fai clic sul pulsante ? nell'intestazione per riaprire questa guida.",
        "pl": "Kliknij przycisk ? w nagłówku, aby ponownie otworzyć ten przewodnik.",
        "tr": "Bu kılavuzu yeniden açmak için başlıktaki ? düğmesine tıklayın.",
    },
    "pg_tip6_hdr": {
        "en": "Startup launch",  "es": "Inicio automático", "fr": "Lancement au démarrage",
        "de": "Autostart",       "pt": "Inicialização",      "ru": "Автозапуск",
        "zh": "开机启动",         "ja": "起動時に実行",        "ko": "시작 시 실행",
        "it": "Avvio automatico","pl": "Autostart",          "tr": "Otomatik başlatma",
    },
    "pg_tip6_txt": {
        "en": "Enable 'Start with Windows' in Settings — Pack Sync\nhides in the tray automatically on login.",
        "es": "Activa 'Iniciar con Windows' en Ajustes — Pack Sync\nse oculta en la bandeja automáticamente al iniciar sesión.",
        "fr": "Activez 'Démarrer avec Windows' dans Paramètres — Pack Sync\nse cache dans la zone de notification au démarrage.",
        "de": "Aktiviere 'Mit Windows starten' in den Einstellungen — Pack Sync\nversteckt sich beim Login automatisch im Tray.",
        "pt": "Ative 'Iniciar com Windows' nas Configurações — Pack Sync\nse esconde na bandeja automaticamente no login.",
        "ru": "Включите «Запуск с Windows» в Настройках — Pack Sync\nавтоматически скрывается в трей при входе.",
        "zh": "在设置中启用「随 Windows 启动」— Pack Sync\n登录时自动隐藏到托盘。",
        "ja": "設定で「Windows と一緒に起動」を有効にすると\nログイン時に Pack Sync が自動的にトレイに隠れます。",
        "ko": "설정에서 'Windows와 함께 시작'을 활성화하면\n로그인 시 Pack Sync가 자동으로 트레이에 숨습니다.",
        "it": "Abilita 'Avvia con Windows' in Impostazioni — Pack Sync\nsi nasconde automaticamente nella notifica al login.",
        "pl": "Włącz 'Start z Windows' w Ustawieniach — Pack Sync\nautomatycznie chowa się w zasobniku przy logowaniu.",
        "tr": "Ayarlar'da 'Windows ile başlat'ı etkinleştirin — Pack Sync\ngirişte otomatik olarak tepside gizlenir.",
    },
}

# Extra translations for languages added after the main _TR dict
_EXTRA_TR: dict[str, dict[str, str]] = {
    "btn_refresh":    {"uk":"↺  Оновити",      "no":"↺  Oppdater",        "tl":"↺  I-refresh",    "th":"↺  รีเฟรช"},
    "btn_settings":   {"uk":"⚙  Налаштування", "no":"⚙  Innstillinger",   "tl":"⚙  Mga Setting",  "th":"⚙  การตั้งค่า"},
    "btn_sync_all":   {"uk":"↑↓  Синхронізувати все","no":"↑↓  Synkroniser alt","tl":"↑↓  I-sync Lahat","th":"↑↓  ซิงค์ทั้งหมด"},
    "btn_sync":       {"uk":"↑↓  Синхронізувати","no":"↑↓  Synkroniser",  "tl":"↑↓  I-sync",      "th":"↑↓  ซิงค์"},
    "btn_remove":     {"uk":"✕  Видалити",      "no":"✕  Fjern",           "tl":"✕  Alisin",        "th":"✕  ลบออก"},
    "lbl_live":       {"uk":"↺ активний",       "no":"↺ aktiv",            "tl":"↺ live",           "th":"↺ สด"},
    "lbl_branch_changed":{"uk":"⚠ гілка змінена","no":"⚠ gren endret",   "tl":"⚠ nabago ang branch","th":"⚠ branch เปลี่ยน"},
    "lbl_synced":     {"uk":"✓ синхронізовано", "no":"✓ synkronisert",    "tl":"✓ naka-sync",      "th":"✓ ซิงค์แล้ว"},
    "lbl_not_synced": {"uk":"○ не синхронізовано","no":"○ ikke synkronisert","tl":"○ hindi naka-sync","th":"○ ยังไม่ซิงค์"},
    "status_ready":   {"uk":"Готово",            "no":"Klar",               "tl":"Handa",            "th":"พร้อม"},
    "status_syncing": {"uk":"Синхронізація…",   "no":"Synkroniserer…",    "tl":"Nag-si-sync…",     "th":"กำลังซิงค์…"},
    "status_synced":  {"uk":"Синхронізовано: {0}","no":"Synkronisert: {0}","tl":"Naka-sync: {0}",  "th":"ซิงค์แล้ว: {0}"},
    "status_synced_all":{"uk":"Синхронізовано {0} проект(ів).","no":"Synkroniserte {0} prosjekt(er).","tl":"Na-sync ang {0} proyekto.","th":"ซิงค์ {0} โปรเจกต์แล้ว"},
    "status_removed": {"uk":"Видалено: {0}",    "no":"Fjernet: {0}",      "tl":"Naalis: {0}",      "th":"ลบออกแล้ว: {0}"},
    "search_hint":    {"uk":"Фільтр проектів…", "no":"Filtrer prosjekter…","tl":"I-filter ang mga proyekto…","th":"กรองโปรเจกต์…"},
    "empty_no_projects":{"uk":"Репозиторії з паками RP або BP не знайдено.\n\nПеревірте папку GitHub у Налаштуваннях.","no":"Ingen repoer med RP- eller BP-pakker funnet.\n\nSjekk GitHub-mappen i Innstillinger.","tl":"Walang mga repositoryong may RP o BP pack na nahanap.\n\nSuriin ang iyong GitHub folder sa Mga Setting.","th":"ไม่พบรีโพที่มีแพ็ก RP หรือ BP\n\nตรวจสอบโฟลเดอร์ GitHub ในการตั้งค่า"},
    "empty_no_match": {"uk":"Немає проектів для «{0}».","no":"Ingen prosjekter for «{0}».","tl":"Walang proyektong tumutugma sa \"{0}\".","th":"ไม่มีโปรเจกต์ที่ตรงกับ \"{0}\""},
    "warn_first_title":{"uk":"Перед першою синхронізацією","no":"Før første synkronisering","tl":"Bago ang iyong unang sync","th":"ก่อนซิงค์ครั้งแรก"},
    "warn_first_body":{"uk":"Pack Sync скопіює файли пака до com.mojang.\n\nЯкщо папка призначення вже містить файли:\n  •  Файли об'єднуються — нічого не видаляється\n  •  Перемагає новіша версія кожного файлу\n  •  Файли, додані в грі, зберігаються\n\nПорада: спочатку зробіть commit.","no":"Pack Sync vil kopiere pakkefiler til com.mojang.\n\nHvis målmappen allerede inneholder filer:\n  •  Filer flettes — ingenting slettes\n  •  Nyere versjon av hver fil vinner\n  •  Filer lagt til i spillet beholdes\n\nTips: commit repoet ditt først.","tl":"Kokopyahin ng Pack Sync ang iyong mga pack file sa com.mojang.\n\nKung mayroon nang mga file sa destination:\n  •  Pinagsasama ang mga file — walang tinatanggal\n  •  Ang mas bagong bersyon ng bawat file ang mananalo\n  •  Mga file na idinagdag sa laro ay pinapanatili\n\nTip: i-commit muna ang iyong repo.","th":"Pack Sync จะคัดลอกไฟล์แพ็กของคุณไปยัง com.mojang\n\nหากโฟลเดอร์ปลายทางมีไฟล์อยู่แล้ว:\n  •  ไฟล์จะถูกรวม — ไม่มีการลบ\n  •  เวอร์ชันล่าสุดของแต่ละไฟล์จะชนะ\n  •  ไฟล์ที่เพิ่มในเกมจะถูกเก็บไว้\n\nเคล็ดลับ: คอมมิตรีโพของคุณก่อน"},
    "warn_first_skip":{"uk":"Більше не показувати","no":"Ikke vis igjen","tl":"Huwag ipakita muli","th":"อย่าแสดงอีก"},
    "btn_got_it":     {"uk":"Зрозуміло — Синхронізувати","no":"Forstått — Synkroniser","tl":"Naintindihan — I-sync","th":"เข้าใจแล้ว — ซิงค์"},
    "btn_cancel":     {"uk":"Скасувати",        "no":"Avbryt",             "tl":"Kanselahin",       "th":"ยกเลิก"},
    "warn_branch_title":{"uk":"Гілку Git змінено","no":"Git-gren endret","tl":"Nagbago ang Git Branch","th":"เปลี่ยน Git Branch แล้ว"},
    "warn_branch_body":{"uk":"Синхронізація замінить папку призначення\nвмістом нової гілки. Продовжити?","no":"Synkronisering vil nå erstatte målmappen\nmed innholdet i den nye grenen. Fortsett?","tl":"Ang pag-sync ngayon ay papalitan ang destination folder\nng nilalaman ng bagong branch. Magpatuloy?","th":"การซิงค์จะแทนที่โฟลเดอร์ปลายทาง\nด้วยเนื้อหาของ branch ใหม่ ดำเนินการต่อ?"},
    "btn_yes_sync":   {"uk":"Так, синхронізувати","no":"Ja, synkroniser","tl":"Oo, I-sync",         "th":"ใช่ ซิงค์"},
    "remove_title":   {"uk":"Видалити з місця призначення","no":"Fjern fra destinasjon","tl":"Alisin sa destination","th":"ลบออกจากปลายทาง"},
    "remove_body":    {"uk":"Видалити {0} з папки призначення?\nВаш репозиторій GitHub не буде змінено.","no":"Slette {0} fra destinasjonen?\nGitHub-repoet ditt påvirkes ikke.","tl":"Burahin ang {0} mula sa destination?\nHindi maaapektuhan ang iyong GitHub repo.","th":"ลบ {0} จากปลายทาง?\nรีโพ GitHub ของคุณจะไม่ได้รับผลกระทบ"},
    "setup_title":    {"uk":"Ласкаво просимо до Pack Sync","no":"Velkommen til Pack Sync","tl":"Maligayang pagdating sa Pack Sync","th":"ยินดีต้อนรับสู่ Pack Sync"},
    "setup_subtitle": {"uk":"Одноразове налаштування — змінити пізніше у Налаштуваннях.","no":"Engangsoppsett — endre senere i Innstillinger.","tl":"Isang beses na setup — baguhin mamaya sa Mga Setting.","th":"ตั้งค่าครั้งเดียว — เปลี่ยนแปลงได้ในการตั้งค่า"},
    "setup_project_type":{"uk":"Тип проекту:","no":"Prosjekttype:","tl":"Uri ng proyekto:","th":"ประเภทโปรเจกต์:"},
    "setup_github_folder":{"uk":"Папка GitHub:","no":"GitHub-mappe:","tl":"GitHub folder:","th":"โฟลเดอร์ GitHub:"},
    "setup_dest_folder":{"uk":"Папка призначення:","no":"Destinasjonsmappe:","tl":"Destination folder:","th":"โฟลเดอร์ปลายทาง:"},
    "btn_get_started":{"uk":"Розпочати",        "no":"Kom i gang",         "tl":"Magsimula",        "th":"เริ่มต้น"},
    "settings_title": {"uk":"Налаштування",     "no":"Innstillinger",      "tl":"Mga Setting",      "th":"การตั้งค่า"},
    "settings_startup":{"uk":"Запускати Pack Sync при старті системи","no":"Start Pack Sync ved systemoppstart","tl":"Ilunsad ang Pack Sync sa startup ng sistema","th":"เปิด Pack Sync เมื่อเริ่มระบบ"},
    "settings_language":{"uk":"Мова:",          "no":"Språk:",             "tl":"Wika:",             "th":"ภาษา:"},
    "btn_save":       {"uk":"Зберегти",         "no":"Lagre",              "tl":"I-save",           "th":"บันทึก"},
    "progress_syncing_files":{"uk":"Синхронізація…  {0} з {1} файлів  ({2}%)","no":"Synkroniserer…  {0} av {1} filer  ({2}%)","tl":"Nag-si-sync…  {0} ng {1} file  ({2}%)","th":"กำลังซิงค์…  {0} จาก {1} ไฟล์  ({2}%)"},
    # ── Onboarding tray page — extra languages ────────────────────────────────
    "pg_tray_title":{"uk":"💡  Корисно знати","no":"💡  Greit å vite","tl":"💡  Magandang malaman","th":"💡  ควรรู้"},
    "pg_tray_win":{
        "uk":"Pack Sync ховається в системному треї Windows при закритті вікна.\nНатисніть значок, щоб відкрити знову.",
        "no":"Pack Sync gjemmer seg i Windows-systemstatusfeltet når du lukker vinduet.\nKlikk på ikonet for å åpne det igjen.",
        "tl":"Nagtatago ang Pack Sync sa Windows system tray kapag isinarado ang window.\nI-click ang icon para buksan muli.",
        "th":"Pack Sync จะซ่อนอยู่ใน system tray เมื่อปิดหน้าต่าง\nคลิกที่ไอคอนเพื่อเปิดอีกครั้ง",
    },
    "pg_tray_mac":{
        "uk":"Pack Sync ховається в рядку меню macOS при закритті вікна.\nНатисніть значок у верхньому правому куті.",
        "no":"Pack Sync gjemmer seg i macOS-menylinjen når du lukker vinduet.\nKlikk på ikonet øverst til høyre.",
        "tl":"Nagtatago ang Pack Sync sa macOS menu bar kapag isinarado ang window.\nI-click ang icon sa kanang sulok sa itaas.",
        "th":"Pack Sync จะซ่อนอยู่ใน menu bar ของ macOS เมื่อปิดหน้าต่าง\nคลิกที่ไอคอนด้านบนขวา",
    },
    "pg_tray_lin":{
        "uk":"Pack Sync ховається в системній області сповіщень при закритті.\nНатисніть значок на панелі, щоб відкрити.",
        "no":"Pack Sync gjemmer seg i systemvarselsfeltet når du lukker vinduet.\nKlikk på ikonet i panelet for å åpne det.",
        "tl":"Nagtatago ang Pack Sync sa notification area kapag isinarado ang window.\nI-click ang icon sa panel para buksan.",
        "th":"Pack Sync จะซ่อนอยู่ในพื้นที่แจ้งเตือนระบบเมื่อปิดหน้าต่าง\nคลิกที่ไอคอนในแถบเพื่อเปิดอีกครั้ง",
    },
    "pg_tray_tip1":{"uk":"Файли синхронізуються миттєво при збереженні — без додаткових дій.","no":"Filer synkroniseres i det øyeblikket du lagrer — ingen manuelle trinn.","tl":"Nag-si-sync ang mga file sa sandaling i-save — hindi na kailangan ng manu-manong hakbang.","th":"ไฟล์ซิงค์ทันทีเมื่อบันทึก — ไม่ต้องทำด้วยตนเอง"},
    "pg_tray_tip2":{"uk":"Авто-синхронізація: увімкніть ⚡ Авто в заголовку — зміна гілки копіює файли без діалогів.","no":"Auto-sync: aktiver ⚡ Auto i overskriften — grenskifter kopierer filer uten dialoger.","tl":"Auto-sync: i-enable ang ⚡ Auto sa header — ang branch switch ay kumokopya ng file nang walang dialog.","th":"Auto-sync: เปิดใช้ ⚡ Auto ในส่วนหัว — การเปลี่ยน branch จะคัดลอกไฟล์โดยไม่มีป๊อปอัป"},
    "pg_tray_tip3":{"uk":"Двічі клацніть картку проекту, щоб налаштувати папки або увімкнути управління Regolith.","no":"Dobbeltklikk på et prosjektkort for å konfigurere mapper eller aktivere Regolith-kontroller.","tl":"I-double-click ang project card para i-configure ang mga folder o i-enable ang Regolith controls.","th":"ดับเบิลคลิกที่การ์ดโปรเจกต์เพื่อตั้งค่าโฟลเดอร์หรือเปิดใช้ Regolith controls"},
    "pg_tray_tip4":{"uk":"Проект Regolith? Двічі клацніть картку → Налаштування → увімкніть управління Regolith.","no":"Regolith-prosjekt? Dobbeltklikk → Prosjektinnstillinger → aktiver Regolith-kontroller.","tl":"Regolith project? Double-click → Project Settings → i-enable ang Regolith controls.","th":"โปรเจกต์ Regolith? ดับเบิลคลิก → การตั้งค่าโปรเจกต์ → เปิดใช้ Regolith controls"},
    "pg_tray_tip5":{"uk":"Відкрийте цей посібник знову кнопкою  ?  у заголовку.","no":"Åpne denne veiledningen igjen med  ?  knappen i overskriften.","tl":"Buksan muli ang gabay na ito anumang oras gamit ang  ?  na button sa header.","th":"เปิดคู่มือนี้อีกครั้งได้ทุกเมื่อโดยใช้ปุ่ม  ?  ในส่วนหัว"},
    # ── Onboarding pages 1-5 — extra languages ───────────────────────────────
    "pg_how_title":{"uk":"Два способи роботи — ваш вибір","no":"To måter å jobbe på — ditt valg","tl":"Dalawang paraan ng trabaho — ang iyong pagpipilian","th":"สองวิธีการทำงาน — ทางเลือกของคุณ"},
    "pg_how_a_hdr":{"uk":"Проста синхронізація  — будь-який проект","no":"Enkel Sync  — ethvert prosjekt","tl":"Simpleng Sync  — anumang proyekto","th":"Sync ธรรมดา  — ทุกโปรเจกต์"},
    "pg_how_a_body":{
        "uk":"Pack Sync стежить за репозиторієм та миттєво копіює змінені файли\nв com.mojang — без фільтрів, без збірки, нульовий CPU в очікуванні.\nСинхронізація одностороння: репозиторій → вихідна папка.",
        "no":"Pack Sync overvåker repoet og kopierer endrede filer umiddelbart\ntil com.mojang — ingen filtre, ingen build, null CPU i dvale.\nSync er enveis: repo → utgang.",
        "tl":"Binabantayan ng Pack Sync ang iyong repo at agad na kinokopya ang mga nabagong file\nsa com.mojang — walang filter, walang build, zero CPU sa idle.\nAng sync ay isang direksyon: repo → output.",
        "th":"Pack Sync ตรวจสอบ repo ของคุณและคัดลอกไฟล์ที่เปลี่ยนแปลงไปยัง com.mojang ทันที\n— ไม่มีตัวกรอง ไม่มีการ build ใช้ CPU เป็นศูนย์ขณะ idle\nการซิงค์เป็นแบบทางเดียว: repo → output",
    },
    "pg_how_b_hdr":{"uk":"⚙ Regolith Build  — виявляється автоматично","no":"⚙ Regolith Build  — oppdages automatisk","tl":"⚙ Regolith Build  — awtomatikong natutukoy","th":"⚙ Regolith Build  — ตรวจพบอัตโนมัติ"},
    "pg_how_b_body":{
        "uk":"Pack Sync автоматично виявляє проекти Regolith (config.json у корені репо).\nЩоб розблокувати ▶ Збірка: двічі клацніть картку і увімкніть\n«Показати управління Regolith» у налаштуваннях проекту.\nЗбірка запускає повний конвеєр фільтрів та експортує до com.mojang.",
        "no":"Pack Sync oppdager automatisk Regolith-prosjekter (config.json i repo-roten).\nFor å låse opp ▶ Build: dobbeltklikk på kortet og aktiver\n«Vis Regolith-kontroller» i Prosjektinnstillinger.\nBuild kjører hele filterpipelinen og eksporterer til com.mojang.",
        "tl":"Awtomatikong natutukoy ng Pack Sync ang mga Regolith project (config.json sa root ng repo).\nPara ma-unlock ang ▶ Build: i-double-click ang project card at i-enable ang\n«Ipakita ang Regolith controls» sa Project Settings.\nPinapatakbo ng Build ang buong filter pipeline at nag-e-export sa com.mojang.",
        "th":"Pack Sync ตรวจจับโปรเจกต์ Regolith โดยอัตโนมัติ (config.json ที่รากของ repo)\nเพื่อปลดล็อค ▶ Build: ดับเบิลคลิกที่การ์ดโปรเจกต์และเปิดใช้\n«แสดง Regolith controls» ในการตั้งค่าโปรเจกต์\nBuild จะรัน filter pipeline ทั้งหมดและ export ไปยัง com.mojang",
    },
    "pg_proj_title":{"uk":"📂  Додавання проектів","no":"📂  Legge til prosjekter","tl":"📂  Pagdaragdag ng iyong mga proyekto","th":"📂  การเพิ่มโปรเจกต์"},
    "pg_proj_sub":{"uk":"Вкажіть Pack Sync папку GitHub один раз — проекти знаходяться автоматично.","no":"Pek Pack Sync til GitHub-mappen din én gang — den finner prosjekter automatisk.","tl":"I-point ang Pack Sync sa iyong GitHub folder nang isang beses — awtomatiko itong naghahanap ng mga proyekto.","th":"ชี้ Pack Sync ไปที่โฟลเดอร์ GitHub ของคุณครั้งเดียว — มันจะหาโปรเจกต์โดยอัตโนมัติ"},
    "pg_proj_auto":{"uk":"Проекти з'являються автоматично — ручний імпорт не потрібен","no":"Prosjekter vises automatisk — ingen manuell import nødvendig","tl":"Awtomatikong lumalabas ang mga proyekto — hindi na kailangan ng manu-manong import","th":"โปรเจกต์ปรากฏขึ้นอัตโนมัติ — ไม่ต้องนำเข้าด้วยตนเอง"},
    "pg_proj_step1":{"uk":"Відкрийте Налаштування та вкажіть шлях до папки GitHub.","no":"Åpne Innstillinger og sett GitHub-mappebanen.","tl":"Buksan ang Mga Setting at itakda ang iyong GitHub folder path.","th":"เปิดการตั้งค่าและกำหนดเส้นทางโฟลเดอร์ GitHub"},
    "pg_proj_step2":{"uk":"Кожна підпапка з паком Bedrock виявляється миттєво.","no":"Hver undermappe med en Bedrock-pakke oppdages umiddelbart.","tl":"Bawat subfolder na may Bedrock pack ay agad na natutukoy.","th":"ทุกโฟลเดอร์ย่อยที่มี Bedrock pack จะถูกตรวจพบทันที"},
    "pg_proj_step3":{"uk":"Проекти Regolith виявляються автоматично — двічі клацніть картку для увімкнення Build.","no":"Regolith-prosjekter oppdages automatisk — dobbeltklikk kortet for å aktivere Build-knappen.","tl":"Ang mga Regolith project ay awtomatikong natutukoy — i-double-click ang card para i-enable ang Build button.","th":"โปรเจกต์ Regolith ถูกตรวจพบอัตโนมัติ — ดับเบิลคลิกที่การ์ดเพื่อเปิดใช้ปุ่ม Build"},
    "pg_sync_title":{"uk":"⚡  Синхронізація в дії","no":"⚡  Sync i aksjon","tl":"⚡  Sync sa Aksyon","th":"⚡  Sync ในการทำงาน"},
    "pg_sync_sub":{"uk":"Файли копіюються в момент збереження — вам ніколи не потрібно натискати Sync вручну.","no":"Filer kopieres i det øyeblikket du lagrer — du trykker aldri Sync manuelt.","tl":"Ang mga file ay kinokopya sa sandaling i-save mo — hindi ka na kailangang pindutin ang Sync nang manu-mano.","th":"ไฟล์จะถูกคัดลอกทันทีที่คุณบันทึก — คุณไม่จำเป็นต้องกด Sync ด้วยตนเองเลย"},
    "pg_sync_newer":{"uk":"Перемагає новіший файл — свіжі правки не перезаписуються","no":"Nyere fil vinner — ingen overskrivning av ferske redigeringer","tl":"Ang mas bagong file ang nananalo — walang pag-overwrite ng sariwang mga edit","th":"ไฟล์ที่ใหม่กว่าชนะ — ไม่มีการทับไฟล์ที่เพิ่งแก้ไข"},
    "pg_sync_fact1":{"uk":"Зелена точка = пак зараз синхронізований з com.mojang.","no":"Grønn prikk = pakken er nå synkronisert med com.mojang.","tl":"Berdeng tuldok = ang pack ay kasalukuyang naka-sync sa com.mojang.","th":"จุดสีเขียว = pack ซิงค์กับ com.mojang แล้วในขณะนี้"},
    "pg_sync_fact2":{"uk":"Порожня точка = пак ще не скопійовано — натисніть Sync для початку.","no":"Tom prikk = pakken ikke kopiert ennå — trykk Sync for å starte.","tl":"Walang laman na tuldok = hindi pa nakopya ang pack — pindutin ang Sync para magsimula.","th":"จุดว่าง = pack ยังไม่ได้คัดลอก — กด Sync เพื่อเริ่มต้น"},
    "pg_sync_fact3":{"uk":"→ В один бік: файли копіюються лише з репозиторію (вхід) до вихідної папки.","no":"→ Enveis: filer kopieres fra depotet (inndata) til utdatamappen bare.","tl":"→ One-way: ang mga file ay kinokopya mula sa inyong repo (input) papunta sa output folder lamang.","th":"→ ทางเดียว: ไฟล์คัดลอกจาก repo (input) ไปยังโฟลเดอร์ output เท่านั้น"},
    "pg_sync_fact4":{"uk":"Нульовий CPU в очікуванні — спостерігач спить до зміни файлу.","no":"Null CPU i dvale — overvåkeren sover til en fil endres.","tl":"Zero CPU sa idle — natutulog ang watcher hanggang magbago ang isang file.","th":"CPU เป็นศูนย์ขณะ idle — watcher หลับจนกว่าไฟล์จะเปลี่ยนแปลง"},
    "pg_reg_title":{"uk":"⚙  Збірка з Regolith","no":"⚙  Bygge med Regolith","tl":"⚙  Pagbuo gamit ang Regolith","th":"⚙  การ Build ด้วย Regolith"},
    "pg_reg_sub":{"uk":"Якщо проект використовує Regolith, Pack Sync додає кнопку Build з живим терміналом.","no":"Hvis prosjektet bruker Regolith, legger Pack Sync til en Build-knapp med live terminal.","tl":"Kung gumagamit ng Regolith ang iyong proyekto, nagdaragdag ang Pack Sync ng Build button na may live terminal.","th":"หากโปรเจกต์ใช้ Regolith Pack Sync จะเพิ่มปุ่ม Build พร้อม terminal แบบสด"},
    "pg_reg_fact1":{"uk":"Натисніть Build для запуску повного конвеєра — живий термінал показує прогрес.","no":"Trykk Build for å kjøre hele pipelinen — en live terminal viser fremdriften.","tl":"Pindutin ang Build para patakbuhin ang buong pipeline — isang live terminal ang nagpapakita ng progreso.","th":"กด Build เพื่อรัน pipeline ทั้งหมด — terminal แบบสดแสดงความคืบหน้า"},
    "pg_reg_fact2":{"uk":"Sync (без Build) миттєво копіює вихідні файли, пропускаючи фільтри.","no":"Sync (uten Build) kopierer råkildefiler umiddelbart, uten filtre.","tl":"Ang Sync (nang walang Build) ay agad na nagkokopya ng raw source files, nilalaktawan ang mga filter.","th":"Sync (ไม่มี Build) คัดลอกไฟล์ต้นทางดิบทันทีโดยข้ามตัวกรอง"},
    "pg_reg_fact3":{"uk":"Випадаючий список профілів вибирає профіль Regolith (dev / release).","no":"Profil-nedtrekkslisten velger hvilken Regolith-profil som skal kjøres (dev / release).","tl":"Ang profile dropdown ay pumipili kung aling Regolith profile ang tatakbuhin (dev / release).","th":"เมนูดรอปดาวน์ของโปรไฟล์เลือกว่าจะรัน Regolith profile ใด (dev / release)"},
    "pg_reg_fact4":{"uk":"Двічі клацніть картку проекту → Налаштування проекту → увімкніть Regolith-елементи для показу Build.","no":"Dobbeltklikk prosjektkortet → Prosjektinnstillinger → aktiver Regolith-kontroller for å vise Build.","tl":"I-double-click ang project card → Project Settings → i-enable ang Regolith controls para ipakita ang Build.","th":"ดับเบิลคลิกที่การ์ดโปรเจกต์ → การตั้งค่าโปรเจกต์ → เปิดใช้ Regolith controls เพื่อแสดง Build"},
    "pg_tips_title":{"uk":"✨  Поради та підказки","no":"✨  Tips og triks","tl":"✨  Mga Tip at Trick","th":"✨  เคล็ดลับและกลเม็ด"},
    "pg_tip1_hdr":{"uk":"Рядок пошуку","no":"Søkefelt","tl":"Search bar","th":"แถบค้นหา"},
    "pg_tip1_txt":{"uk":"Введіть частину назви проекту, щоб миттєво відфільтрувати список.","no":"Skriv en del av prosjektnavnet for å filtrere listen umiddelbart.","tl":"Mag-type ng bahagi ng pangalan ng proyekto para agad na ma-filter ang listahan.","th":"พิมพ์ชื่อโปรเจกต์บางส่วนเพื่อกรองรายการทันที"},
    "pg_tip2_hdr":{"uk":"Авто-синхронізація","no":"Auto-synkronisering","tl":"Auto-sync","th":"ออโต้ซิงค์"},
    "pg_tip2_txt":{"uk":"Увімкніть ⚡ Авто у заголовку для автоматичної синхронізації\nпри зміні гілки git — без діалогів.","no":"Aktiver ⚡ Auto i overskriften for å synkronisere automatisk\nnår git-grenen endres — ingen dialoger.","tl":"I-enable ang ⚡ Auto sa header para mag-sync nang awtomatiko\nkapag nagbago ang iyong git branch — walang dialogo.","th":"เปิดใช้ ⚡ Auto ในส่วนหัวเพื่อซิงค์อัตโนมัติ\nเมื่อ git branch เปลี่ยน — ไม่มีกล่องโต้ตอบ"},
    "pg_tip3_hdr":{"uk":"16 мов","no":"16 språk","tl":"16 wika","th":"16 ภาษา"},
    "pg_tip3_txt":{"uk":"Налаштування → Мова для зміни мови інтерфейсу.","no":"Innstillinger → Språk for å bytte UI-språk.","tl":"Mga Setting → Wika para palitan ang UI language.","th":"การตั้งค่า → ภาษา เพื่อเปลี่ยนภาษา UI"},
    "pg_tip4_hdr":{"uk":"Меню трею","no":"Tray-meny","tl":"Tray menu","th":"เมนูถาดระบบ"},
    "pg_tip4_txt":{"uk":"Клацніть правою кнопкою значок трею для швидкого Sync All або Виходу.","no":"Høyreklikk på tray-ikonet for rask Synk alle eller Avslutt.","tl":"I-right-click ang tray icon para sa mabilis na Sync All o Quit.","th":"คลิกขวาที่ไอคอน tray เพื่อ Sync All หรือ Quit อย่างรวดเร็ว"},
    "pg_tip5_hdr":{"uk":"Цей посібник","no":"Denne veiledningen","tl":"Ang gabay na ito","th":"คู่มือนี้"},
    "pg_tip5_txt":{"uk":"Натисніть кнопку ? у заголовку, щоб знову відкрити цей посібник.","no":"Klikk på ?-knappen i overskriften for å åpne denne veiledningen igjen.","tl":"I-click ang ? button sa header para buksan muli ang gabay na ito.","th":"คลิกปุ่ม ? ในส่วนหัวเพื่อเปิดคู่มือนี้อีกครั้ง"},
    "pg_tip6_hdr":{"uk":"Автозапуск","no":"Oppstartsstart","tl":"Startup launch","th":"เปิดตอน Startup"},
    "pg_tip6_txt":{"uk":"Увімкніть «Запуск з Windows» у Налаштуваннях — Pack Sync\nавтоматично ховається в трей при вході.","no":"Aktiver 'Start med Windows' i Innstillinger — Pack Sync\ngjemmer seg automatisk i tray ved innlogging.","tl":"I-enable ang 'Start with Windows' sa Mga Setting — Pack Sync\nawtomatikong nagtatago sa tray sa pag-login.","th":"เปิดใช้ 'เริ่มกับ Windows' ในการตั้งค่า — Pack Sync\nจะซ่อนตัวใน tray โดยอัตโนมัติเมื่อเข้าสู่ระบบ"},
}

# Active language — resolved once config is loaded
_lang: str = "en"

def t(key: str, *args) -> str:
    """Return translated string for the current language, merging main + extra dicts."""
    row = {**_TR.get(key, {}), **_EXTRA_TR.get(key, {})}
    s   = row.get(_lang) or row.get("en", key)
    return s.format(*args) if args else s

def detect_system_lang() -> str:
    """Return a 2-char ISO 639-1 code matching the system UI language."""
    try:
        import locale
        code = locale.getlocale()[0] or ""
        # Windows sometimes returns '' for 'C' locale; also try winreg
        if IS_WIN and not code:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Control Panel\International") as k:
                code = winreg.QueryValueEx(k, "LocaleName")[0]
        lang = code[:2].lower()
        return lang if lang in LANG_NAMES else "en"
    except Exception:
        return "en"

def apply_lang(code: str):
    global _lang
    _lang = code if code in LANG_NAMES else "en"

# ─── Pure-Python tray icon (no ImageDraw, no PIL format plugins) ──────────────
def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

def _make_icon(size: int = 64) -> Image.Image:
    """Tray icon built with Image.frombytes — no PNG plugin needed in frozen builds."""
    import math
    CR   = size * 0.18
    BG_C = (0x1e, 0x1e, 0x2e, 0xff)
    BLU  = (0x89, 0xb4, 0xfa, 0xff)
    GRN  = (0xa6, 0xe3, 0xa1, 0xff)
    CLR  = (0, 0, 0, 0)
    cx = cy = size / 2
    r_mid = size * 0.34
    r_w   = size * 0.10

    def _in_rounded_square(x, y):
        lx = min(x, size - 1 - x)
        ly = min(y, size - 1 - y)
        if lx >= CR or ly >= CR:
            return True
        return (lx - CR) ** 2 + (ly - CR) ** 2 <= CR * CR

    def _arc_color(angle):
        if 30 <= angle <= 210:
            return BLU
        return GRN

    def _arrowhead_pixels(tip_angle, color, fwd_angle):
        pts = set()
        tip_rad = math.radians(tip_angle)
        tx = cx + r_mid * math.cos(tip_rad)
        ty = cy - r_mid * math.sin(tip_rad)
        perp = math.radians(fwd_angle + 90)
        fwd  = math.radians(fwd_angle)
        sz = size * 0.11
        corners = [
            (tx + sz * math.cos(fwd),  ty - sz * math.sin(fwd)),
            (tx + sz/2 * math.cos(perp), ty - sz/2 * math.sin(perp)),
            (tx - sz/2 * math.cos(perp), ty + sz/2 * math.sin(perp)),
        ]
        x0 = int(min(c[0] for c in corners)) - 2
        x1 = int(max(c[0] for c in corners)) + 2
        y0 = int(min(c[1] for c in corners)) - 2
        y1 = int(max(c[1] for c in corners)) + 2
        def _sign(p1, p2, p3):
            return (p1[0]-p3[0])*(p2[1]-p3[1]) - (p2[0]-p3[0])*(p1[1]-p3[1])
        for px in range(x0, x1+1):
            for py in range(y0, y1+1):
                d1 = _sign((px,py), corners[0], corners[1])
                d2 = _sign((px,py), corners[1], corners[2])
                d3 = _sign((px,py), corners[2], corners[0])
                has_neg = (d1<0) or (d2<0) or (d3<0)
                has_pos = (d1>0) or (d2>0) or (d3>0)
                if not (has_neg and has_pos):
                    if 0 <= px < size and 0 <= py < size:
                        pts.add((px, py, color))
        return pts

    arrow_pixels: dict[tuple,tuple] = {}
    for px, py, col in _arrowhead_pixels(212, BLU, 300):
        arrow_pixels[(px, py)] = col
    for px, py, col in _arrowhead_pixels(28, GRN, 120):
        arrow_pixels[(px, py)] = col

    # Build raw RGBA bytes directly — no PNG encode/decode, no format plugin needed
    pixels = bytearray(size * size * 4)
    idx = 0
    for y in range(size):
        for x in range(size):
            if not _in_rounded_square(x, y):
                color = CLR
            elif (x, y) in arrow_pixels:
                color = arrow_pixels[(x, y)]
            else:
                dx, dy = x - cx, y - cy
                dist  = math.hypot(dx, dy)
                angle = math.degrees(math.atan2(-dy, dx)) % 360
                color = _arc_color(angle) if abs(dist - r_mid) <= r_w else BG_C
            pixels[idx:idx+4] = color
            idx += 4

    return Image.frombytes('RGBA', (size, size), bytes(pixels))

# ─── Admin / elevation ────────────────────────────────────────────────────────
def is_admin() -> bool:
    if IS_WIN:
        try: return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except: return False
    return os.geteuid() == 0

def elevate_and_exit():
    if IS_WIN:
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable,
            " ".join(f'"{a}"' for a in sys.argv), None, 1)
    elif IS_MAC:
        subprocess.Popen(["osascript", "-e",
            f'do shell script "{sys.executable} '
            f'{shlex.quote(str(Path(__file__).resolve()))}" '
            f'with administrator privileges'])
    else:
        subprocess.Popen(["pkexec", sys.executable, str(Path(__file__).resolve())])
    sys.exit()

# ─── Force-removal ────────────────────────────────────────────────────────────
def _rmtree_force_win(fn, path, _):
    try: os.chmod(path, 0o777); fn(path)
    except: pass

def force_remove(path: Path):
    if IS_WIN:
        subprocess.run(["takeown", "/f", str(path), "/r", "/d", "y"], capture_output=True)
        subprocess.run(["icacls", str(path), "/grant",
                        "administrators:F", "/t", "/c", "/q"], capture_output=True)
        shutil.rmtree(path, onerror=_rmtree_force_win)
    elif IS_MAC:
        subprocess.run(["osascript", "-e",
            f'do shell script "rm -rf {shlex.quote(str(path))}" '
            f'with administrator privileges'], check=True)
    else:
        subprocess.run(["pkexec", "rm", "-rf", str(path)], check=True)

# ─── Startup on login ─────────────────────────────────────────────────────────
def _run_value(): return f'"{sys.executable}" "{Path(__file__).resolve()}" --minimized'

def set_startup(enable: bool):
    if IS_WIN:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if enable: winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _run_value())
            else:
                try: winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError: pass
    elif IS_MAC:
        pf = HOME / "Library" / "LaunchAgents" / f"com.{APP_NAME}.plist"
        if enable:
            pf.parent.mkdir(parents=True, exist_ok=True)
            pf.write_text(
                f'<?xml version="1.0"?>'
                f'<plist version="1.0"><dict>'
                f'<key>Label</key><string>com.{APP_NAME}</string>'
                f'<key>ProgramArguments</key>'
                f'<array><string>{sys.executable}</string>'
                f'<string>{Path(__file__).resolve()}</string></array>'
                f'<key>RunAtLoad</key><true/></dict></plist>', encoding="utf-8")
        else: pf.unlink(missing_ok=True)
    else:
        df = HOME / ".config" / "autostart" / f"{APP_NAME}.desktop"
        if enable:
            df.parent.mkdir(parents=True, exist_ok=True)
            df.write_text(
                f"[Desktop Entry]\nType=Application\nName={APP_NAME}\n"
                f"Exec={sys.executable} {Path(__file__).resolve()}\n"
                f"Hidden=false\nX-GNOME-Autostart-enabled=true\n", encoding="utf-8")
        else: df.unlink(missing_ok=True)

def is_startup_enabled() -> bool:
    if IS_WIN:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, APP_NAME); return True
        except FileNotFoundError: return False
    elif IS_MAC:
        return (HOME / "Library" / "LaunchAgents" / f"com.{APP_NAME}.plist").exists()
    else:
        return (HOME / ".config" / "autostart" / f"{APP_NAME}.desktop").exists()

# ─── Config ───────────────────────────────────────────────────────────────────
def load_cfg() -> dict:
    if CONFIG_FILE.exists():
        try: return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except: pass
    return {}

def _center_on(dialog: "tk.Toplevel", parent: "tk.Misc", w: int, h: int):
    parent.update_idletasks()
    px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
    py = parent.winfo_y() + (parent.winfo_height() - h) // 2
    dialog.geometry(f"{w}x{h}+{max(0,px)}+{max(0,py)}")

def save_cfg(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

# ─── Git ──────────────────────────────────────────────────────────────────────
def git_branch(repo: Path) -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo, capture_output=True, text=True, timeout=5,
            creationflags=_NO_WIN)
        return r.stdout.strip() if r.returncode == 0 else None
    except: return None

def git_head_sha(repo: Path) -> str | None:
    """Return current HEAD commit SHA by reading .git plumbing (fast, no subprocess).
    Falls back to `git rev-parse HEAD` if plumbing parse fails."""
    try:
        git_dir = repo / ".git"
        head = (git_dir / "HEAD").read_text(errors="replace").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            ref_file = git_dir / ref
            if ref_file.exists():
                return ref_file.read_text(errors="replace").strip() or None
            packed = git_dir / "packed-refs"
            if packed.exists():
                for line in packed.read_text(errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("^"):
                        continue
                    sha, _, name = line.partition(" ")
                    if name == ref:
                        return sha or None
        elif len(head) >= 7:
            return head
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo, capture_output=True, text=True, timeout=5,
            creationflags=_NO_WIN)
        return r.stdout.strip() if r.returncode == 0 else None
    except: return None

# ─── Self-update (download prebuilt release binary) ──────────────────────────
# Pack Sync updates by downloading the prebuilt binary from the latest GitHub
# Release (no git, no recompile — the distributed .exe can't rebuild itself).
# Flow: query the Releases API → compare versions → download the asset for this
# platform → swap it in next to the running exe → ask the user to restart.

def _version_tuple(v: str) -> tuple:
    """'1.2.10' -> (1, 2, 10). Non-numeric parts compare as 0. Never raises."""
    out = []
    for part in re.split(r"[.\-+]", v.strip()):
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out) or (0,)

def _running_exe_path() -> Path | None:
    """Path to the running PackSync executable, or None if running from source
    (not frozen) — in which case download-updates don't apply."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return None

def _platform_slug() -> str:
    """The release-asset slug for the running OS + CPU arch, matching the names
    produced by the CI workflow (e.g. 'windows-x64', 'windows-arm64',
    'macos-arm64', 'macos-x64', 'linux-x64')."""
    import platform
    machine = platform.machine().lower()
    is_arm  = machine in ("arm64", "aarch64")
    if IS_WIN:   return "windows-arm64" if is_arm else "windows-x64"
    if IS_MAC:   return "macos-arm64"   if is_arm else "macos-x64"
    if IS_LINUX: return "linux-x64"
    return ""

def _platform_asset_match(name: str) -> bool:
    """True if a release asset filename is the right artifact for this OS *and*
    CPU arch. Assets are named per-arch (e.g. PackSync-windows-arm64.exe), so we
    match the slug to avoid handing an x64 user the ARM64 build or vice-versa."""
    n = name.lower()
    slug = _platform_slug()
    if not slug:
        return False
    # Windows ships as a .zip (PackSync.exe + TrayHelper.exe in a folder);
    # mac/Linux ship .dmg/.deb installers.
    ext = {".zip"} if IS_WIN else {".dmg", ".deb"}
    has_ext = any(n.endswith(e) for e in ext)
    return slug in n and has_ext

def _http_get_json(url: str, timeout: int = 20):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "PackSync-Updater",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def check_for_app_update() -> tuple[bool, str, dict | None]:
    """Query the latest GitHub Release. Returns (update_available, message, info).
    `info` carries {'version', 'tag', 'asset_url', 'asset_name'} when an update is
    available. Never raises."""
    import urllib.error
    try:
        try:
            data = _http_get_json(
                f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest")
        except urllib.error.HTTPError as he:
            # 404 = the repo has no published 'latest' release yet. Not an error
            # from the user's perspective — just nothing to update to.
            if he.code == 404:
                return (False, "no releases published yet", None)
            raise
        tag = data.get("tag_name", "")
        latest = tag[len(UPDATE_TAG_PREFIX):] if tag.startswith(UPDATE_TAG_PREFIX) else tag
        if not latest:
            return (False, "no released version found", None)
        if _version_tuple(latest) <= _version_tuple(APP_VERSION):
            return (False, f"up to date (v{APP_VERSION})", None)
        asset = next((a for a in data.get("assets", [])
                      if _platform_asset_match(a.get("name", ""))), None)
        if not asset:
            return (False, f"v{latest} available but no build for this platform", None)
        return (True, f"v{latest} available (you have v{APP_VERSION})", {
            "version": latest, "tag": tag,
            "asset_url": asset["browser_download_url"],
            "asset_name": asset["name"],
        })
    except Exception as e:
        return (False, f"check error: {e}", None)

def _download(url: str, dest: Path, timeout: int = 600):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "PackSync-Updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)

def apply_app_update(info: dict, log=lambda *_: None) -> tuple[bool, str]:
    """Download the release asset and swap it in next to the running exe.
    Returns (ok, message). Windows can't overwrite a running exe, so we rename
    the current one to *.old (cleaned up on next launch) and move the new one in."""
    exe = _running_exe_path()
    if exe is None:
        return (False, "self-update only works for the packaged build, not source")
    try:
        # On macOS the asset is a .dmg and on Linux a .deb — we can't hot-swap a
        # running app bundle / system package safely, so just download it and
        # point the user at it rather than attempting an in-place swap.
        new_dir = exe.parent
        if not IS_WIN:
            out = new_dir / info["asset_name"]
            log(f"Downloading {info['asset_name']}…")
            _download(info["asset_url"], out)
            return (True, f"downloaded to {out} — install it to finish updating")

        # Windows asset is a .zip containing a folder with PackSync.exe +
        # TrayHelper.exe. Download, extract, and swap each file in next to the
        # running exe (the running PackSync.exe is moved aside to *.old since it
        # can't be overwritten while running).
        import zipfile, tempfile
        zpath = new_dir / (info["asset_name"])
        log(f"Downloading v{info['version']}…")
        _download(info["asset_url"], zpath)
        if zpath.stat().st_size < 1024:
            zpath.unlink(missing_ok=True)
            return (False, "downloaded file looks corrupt (too small)")
        log("Extracting…")
        with tempfile.TemporaryDirectory(dir=str(new_dir)) as td:
            with zipfile.ZipFile(zpath) as z:
                z.extractall(td)
            # Find the extracted files anywhere under the temp dir (the zip wraps
            # them in a folder), and place them next to the current exe.
            extracted = {p.name: p for p in Path(td).rglob("*") if p.is_file()}
            new_exe = next((p for n, p in extracted.items()
                            if n.lower() == "packsync.exe"), None)
            if new_exe is None:
                return (False, "update zip did not contain PackSync.exe")
            log("Installing update…")
            old = new_dir / (exe.name + ".old")
            # Clear any stale *.old from a previous update. Deleting it can fail
            # with WinError 5 if it's still locked (old process holding it) or has
            # a restrictive ACL. Windows allows *renaming* a file you can't
            # delete, so fall back to shoving it aside under a unique name — the
            # next _cleanup_old_update sweep will retry removing those.
            _retire_stale(old, new_dir, exe.name, log)
            os.replace(exe, old)              # move running exe aside
            shutil.copy2(new_exe, exe)        # install new exe under existing name
            helper = extracted.get("TrayHelper.exe") or next(
                (p for n, p in extracted.items() if n.lower() == "trayhelper.exe"), None)
            if helper is not None:
                try: shutil.copy2(helper, new_dir / "TrayHelper.exe")
                except Exception: pass
        zpath.unlink(missing_ok=True)
        return (True, f"updated to v{info['version']} — restart Pack Sync to use it")
    except Exception as e:
        return (False, f"update error: {e}")

def _retire_stale(old: Path, new_dir: Path, exe_name: str, log=None):
    """Make `old` available as a rename target by clearing whatever is there.

    Plain unlink() can raise WinError 5 (access denied) when the stale file is
    locked or read-only. We try, in order: clear the read-only bit + unlink,
    then — if that still fails — rename the stale file to a unique
    `<exe>.old.<n>.stale` name (Windows permits renaming files it won't let you
    delete). Either way `old` ends up free for os.replace() to move into."""
    if not old.exists():
        return
    try:
        try: os.chmod(old, stat.S_IWRITE)
        except Exception: pass
        old.unlink()
        return
    except Exception:
        pass
    # Couldn't delete it — move it out of the way under a unique name instead.
    for n in range(1, 1000):
        alt = new_dir / f"{exe_name}.old.{n}.stale"
        if alt.exists():
            continue
        try:
            os.replace(old, alt)
            if log: log(f"stale update file locked; set aside as {alt.name}")
            return
        except Exception:
            continue
    # Last resort: let the caller's os.replace try anyway (may still succeed).
    if log: log("warning: could not clear stale .old file")

def _cleanup_old_update():
    """Remove leftover *.old / *.old.N.stale binaries from previous updates.
    Best-effort: anything still locked is left for a future sweep."""
    exe = _running_exe_path()
    if exe is None:
        return
    d = exe.parent
    candidates = [d / (exe.name + ".old")]
    try:
        candidates += list(d.glob(exe.name + ".old.*.stale"))
    except Exception:
        pass
    for old in candidates:
        try:
            if old.exists():
                try: os.chmod(old, stat.S_IWRITE)
                except Exception: pass
                old.unlink()
        except Exception:
            pass

# ─── Pack detection ───────────────────────────────────────────────────────────
def clean_name(raw: str) -> str:
    return "".join(p.capitalize() for p in re.split(r"[-_\s]+", raw) if p)

def _pack_type(manifest: Path) -> str | None:
    try:
        types = {m.get("type","").lower()
                 for m in json.loads(manifest.read_text(encoding="utf-8"))
                               .get("modules", [])}
        if "resources" in types: return "RP"
        if types & {"data","script","javascript"} and "resources" not in types: return "BP"
    except: pass
    return None

def find_packs(repo: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for mf in repo.rglob("manifest.json"):
        if "node_modules" in mf.parts: continue
        pt = _pack_type(mf)
        if pt and pt not in result: result[pt] = mf.parent
    return result

# ─── Regolith detection ────────────────────────────────────────────────────────
def _find_regolith_exe() -> str:
    """Return path to the regolith executable, or empty string if not found."""
    import shutil as _shutil
    found = _shutil.which("regolith")
    if found: return found
    candidates = [
        Path(r"C:\Program Files (x86)\regolith\regolith.exe"),
        Path(r"C:\Program Files\Regolith\regolith.exe"),
        Path.home() / "go" / "bin" / "regolith.exe",
        Path.home() / ".local" / "bin" / "regolith",
        Path("/usr/local/bin/regolith"),
    ]
    for c in candidates:
        if c.exists(): return str(c)
    return ""

def _runtime_ok(runtime: str) -> bool:
    import shutil as _s
    return bool(_s.which("node") if runtime == "nodejs"
                else _s.which("python") or _s.which("python3"))

def _parse_regolith_config(repo: Path) -> dict | None:
    """Return parsed Regolith info dict if repo has a valid config.json, else None."""
    cfg_path = repo / "config.json"
    if not cfg_path.exists(): return None
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        reg  = data.get("regolith", {})
        if not isinstance(reg.get("profiles"), dict) or not reg["profiles"]:
            return None
        packs  = data.get("packs", {})
        bp_rel = packs.get("behaviorPack", "packs/BP").lstrip("./\\")
        rp_rel = packs.get("resourcePack", "packs/RP").lstrip("./\\")
        filter_defs = reg.get("filterDefinitions", {})
        profiles    = {}
        for pname, pcfg in reg["profiles"].items():
            filters = pcfg.get("filters", [])
            export  = pcfg.get("export", {})
            # Strip surrounding quotes that sometimes appear in config values
            bp_name = export.get("bpName", f"{clean_name(repo.name)}BP").strip("'\"")
            rp_name = export.get("rpName", f"{clean_name(repo.name)}RP").strip("'\"")
            target  = export.get("target", "development")
            profiles[pname] = {
                "filters": filters, "export": export,
                "bp_name": bp_name, "rp_name": rp_name, "target": target,
            }
        return {
            "bp_path":      repo / bp_rel,
            "rp_path":      repo / rp_rel,
            "data_path":    reg.get("dataPath", "packs/data"),
            "filter_defs":  filter_defs,
            "profiles":     profiles,
            "profile_names":list(profiles.keys()),
        }
    except Exception:
        return None

def discover_projects(github_dir: Path) -> list[dict]:
    if not github_dir.exists(): return []
    out = []
    for repo in sorted(github_dir.iterdir()):
        if not repo.is_dir() or repo.name.startswith("."): continue
        rg = _parse_regolith_config(repo)
        if rg:
            out.append({
                "name": repo.name, "clean": clean_name(repo.name),
                "path": repo,
                # packs used for watcher & simple-sync fallback
                "packs": {"BP": rg["bp_path"], "RP": rg["rp_path"]},
                "regolith": rg, "is_regolith": True,
            })
        else:
            packs = find_packs(repo)
            if packs:
                out.append({"name": repo.name, "clean": clean_name(repo.name),
                            "path": repo, "packs": packs, "is_regolith": False})
    return out

# ─── Sync helpers ─────────────────────────────────────────────────────────────
_TOL = 2  # mtime tolerance in seconds

def _needs_copy(src: Path, dst: Path) -> bool:
    """True if dst is missing or older than src. One stat() per side, no extra
    exists() call (we treat a stat failure on dst as 'missing')."""
    try:
        s_m = src.stat().st_mtime
    except OSError:
        return False
    try:
        d_m = dst.stat().st_mtime
    except OSError:
        return True  # dst missing
    return s_m > d_m + _TOL

def _copy_if_newer(src: Path, dst: Path, log=None) -> bool:
    """Copy src→dst if newer. Returns True if a copy happened."""
    if _needs_copy(src, dst):
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        if log: log(f"→ {src.name}")
        return True
    return False

# Worker count for parallel copies. File I/O to com.mojang is I/O-bound and
# shutil.copy2 / os.stat release the GIL, so threads give a real speedup.
_SYNC_WORKERS = min(32, (os.cpu_count() or 4) * 4)

def sync_bidir(src: Path, dst: Path, log=None, progress_cb=None):
    dst.mkdir(parents=True, exist_ok=True)
    pairs = [(f, dst / f.relative_to(src)) for f in src.rglob("*")
             if f.is_file() and "node_modules" not in f.parts]
    total = len(pairs)
    if not total:
        if progress_cb: progress_cb(0, 0)
        return

    done = 0
    done_lock = threading.Lock()
    last_emit = [0.0]
    _PROG_INTERVAL = 0.033  # ~30 UI updates/sec, regardless of file count

    def _emit(force=False):
        # Throttle progress reporting: per-file UI marshaling (25k+ files) is
        # what made syncs crawl. Report at most ~30x/sec, plus a final 100%.
        if not progress_cb:
            return
        now = time.monotonic()
        if force or now - last_emit[0] >= _PROG_INTERVAL:
            last_emit[0] = now
            progress_cb(done, total)

    # NOTE: workers must not touch the log callback — _status touches Tkinter
    # directly and is not safe to call from multiple threads. Per-file logging
    # at 25k files was pure noise/overhead anyway. Progress goes through
    # progress_cb, which marshals to the UI thread via .after() and is safe.
    errors: list[str] = []
    err_lock = threading.Lock()

    def _work(pair):
        nonlocal done
        s, d = pair
        try:
            _copy_if_newer(s, d, log=None)
        except Exception as e:
            with err_lock:
                errors.append(f"{s.name}: {e}")
        with done_lock:
            done += 1
        _emit()

    with ThreadPoolExecutor(max_workers=_SYNC_WORKERS) as ex:
        list(ex.map(_work, pairs))
    _emit(force=True)
    if errors and log:
        log(f"{len(errors)} copy error(s); first: {errors[0]}")

def mirror_clean(src: Path, dst: Path, log=None, progress_cb=None):
    """Wipe dst entirely, then copy every file from src fresh.

    Unlike sync_bidir (incremental, mtime-gated, merge — newer-wins), this is a
    destructive full re-upload: the destination pack folder is deleted and
    rebuilt to exactly match the repo. Used so that any change landing in the
    git folder (a pulled/pushed commit, or a local file add/edit) produces a
    com.mojang pack that is byte-for-byte the repo, with no stale or orphaned
    files left behind."""
    # Remove the existing destination so nothing from a previous sync survives.
    if dst.exists():
        try:
            shutil.rmtree(dst)
        except Exception:
            try:
                force_remove(dst)
            except Exception as e:
                if log: log(f"wipe failed for {dst.name}: {e}")
    dst.mkdir(parents=True, exist_ok=True)

    files = [f for f in src.rglob("*")
             if f.is_file() and "node_modules" not in f.parts]
    total = len(files)
    if not total:
        if progress_cb: progress_cb(0, 0)
        return

    done = 0
    done_lock = threading.Lock()
    last_emit = [0.0]
    _PROG_INTERVAL = 0.033

    def _emit(force=False):
        if not progress_cb:
            return
        now = time.monotonic()
        if force or now - last_emit[0] >= _PROG_INTERVAL:
            last_emit[0] = now
            progress_cb(done, total)

    errors: list[str] = []
    err_lock = threading.Lock()

    def _work(s):
        nonlocal done
        d = dst / s.relative_to(src)
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
        except Exception as e:
            with err_lock:
                errors.append(f"{s.name}: {e}")
        with done_lock:
            done += 1
        _emit()

    with ThreadPoolExecutor(max_workers=_SYNC_WORKERS) as ex:
        list(ex.map(_work, files))
    _emit(force=True)
    if errors and log:
        log(f"{len(errors)} copy error(s); first: {errors[0]}")

def dest_path(mojang: Path, proj: dict, pack_type: str) -> Path:
    sub = "development_resource_packs" if pack_type=="RP" else "development_behavior_packs"
    return mojang / sub / f"{proj['clean']}{pack_type}"

# ─── OS-native file watcher (Windows) ────────────────────────────────────────
# ReadDirectoryChangesW — zero-dependency, zero idle CPU.
if IS_WIN:
    _FILE_LIST_DIRECTORY        = 0x0001
    _FILE_SHARE_ALL             = 0x0007
    _OPEN_EXISTING              = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _NOTIFY_FILTER              = 0x11  # LAST_WRITE | FILE_NAME

    # 256 KB notify buffer. ReadDirectoryChangesW reports a buffer overflow
    # (bytes_returned == 0) when more changes occur than fit here — common
    # during a `git pull` or a bulk save on a large pack. A bigger buffer makes
    # that rarer, but the real safety net is on_overflow below: overflow can
    # never be fully prevented, so we recover from it instead (Regolith-style:
    # when individual events are lost, re-mirror the whole pack).
    _WATCH_BUF_BYTES = 262144

    class _Win32DirWatcher(threading.Thread):
        def __init__(self, path: Path, on_file, recursive: bool = True,
                     on_overflow=None):
            super().__init__(daemon=True)
            self.path        = path
            self.on_file     = on_file      # called with (abs_path: Path, action: int)
            self.recursive   = recursive
            self.on_overflow = on_overflow  # called with no args when events are lost
            self._buf        = ctypes.create_string_buffer(_WATCH_BUF_BYTES)
            self._br         = ctypes.wintypes.DWORD(0)
            self._handle     = None
            self._stop       = threading.Event()

        def _open(self):
            k32 = ctypes.windll.kernel32
            self._handle = k32.CreateFileW(
                str(self.path), _FILE_LIST_DIRECTORY, _FILE_SHARE_ALL,
                None, _OPEN_EXISTING, _FILE_FLAG_BACKUP_SEMANTICS, None)
            INVALID = ctypes.wintypes.HANDLE(-1).value
            if self._handle == INVALID:
                self._handle = None
            return self._handle is not None

        def _close(self):
            if self._handle:
                try:
                    ctypes.windll.kernel32.CloseHandle(self._handle)
                except Exception:
                    pass
                self._handle = None

        def run(self):
            k32 = ctypes.windll.kernel32
            if not self._open():
                return
            while not self._stop.is_set():
                ok = k32.ReadDirectoryChangesW(
                    self._handle, self._buf, len(self._buf),
                    self.recursive, _NOTIFY_FILTER,
                    ctypes.byref(self._br), None, None)
                if self._stop.is_set():
                    break
                if not ok or self._br.value == 0:
                    # Overflow or read error. We've lost the precise list of
                    # changed files, so don't trust incremental copies — signal
                    # a full re-mirror and keep watching (never let the thread
                    # die, which was the old `break` bug). Re-open the handle in
                    # case the error invalidated it; back off briefly so a
                    # persistently failing handle can't busy-spin.
                    self._close()
                    if self.on_overflow:
                        try: self.on_overflow()
                        except Exception: pass
                    if self._stop.is_set() or not self._open():
                        if self._stop.wait(1.0):
                            break
                        if not self._open():
                            break  # directory gone for good; give up cleanly
                    continue
                offset = 0
                while True:
                    next_off, action, name_len = struct.unpack_from(
                        '<III', self._buf.raw, offset)
                    name = self._buf.raw[
                        offset+12: offset+12+name_len
                    ].decode('utf-16-le', errors='replace')
                    self.on_file(self.path / name, action)
                    if next_off == 0:
                        break
                    offset += next_off

        def stop(self):
            self._stop.set()
            if self._handle:
                ctypes.windll.kernel32.CancelIoEx(self._handle, None)
                self._close()

# ─── Cross-platform Watcher Manager ──────────────────────────────────────────
_DEBOUNCE = 0.4  # seconds

class _PackWatcher:
    """Debounced file-change handler for one src→dst pack pair."""
    def __init__(self, src: Path, dst: Path, log_fn, flush_cb=None):
        self.src       = src
        self.dst       = dst
        self.log_fn    = log_fn
        self._flush_cb = flush_cb
        self._lock     = threading.Lock()
        self._q:     set[Path] = set()
        self._del_q: set[Path] = set()
        self._timer: threading.Timer | None = None
        self._paused  = False  # set during a git pull/checkout so the full
                               # post-pull sync replaces incremental copies

    def pause(self):
        """Stop reacting to file changes and drop anything pending.
        Used during git pulls so partial state never lands in Mojang."""
        with self._lock:
            self._paused = True
            self._q.clear()
            self._del_q.clear()
            if self._timer:
                self._timer.cancel()
                self._timer = None

    def resume(self):
        with self._lock:
            self._paused = False

    def on_overflow(self):
        """The OS watcher lost events (buffer overflow). We no longer know which
        files changed, so re-mirror the whole pack the same way the manual Sync
        button does. mtime-gated, so unchanged files aren't recopied."""
        if self._paused:
            return
        try:
            mirror_clean(self.src, self.dst, self.log_fn)
            if self._flush_cb:
                # Report as a resync so the UI/toast reflects that something landed.
                try: self._flush_cb({"overwritten": set(), "new": set(),
                                     "deleted": set(), "resync": True})
                except Exception: pass
            self.log_fn("⟳ resynced (watcher overflow)")
        except Exception as e:
            self.log_fn(f"overflow resync err: {e}")

    def on_change(self, path: Path, action=None):
        if "node_modules" in path.parts: return
        if self._paused: return
        # action 2 = FILE_ACTION_REMOVED on Windows
        if action == 2:
            with self._lock:
                if self._paused: return
                self._del_q.add(path)
                if self._timer: self._timer.cancel()
                t = threading.Timer(_DEBOUNCE, self._flush)
                t.daemon = True
                self._timer = t
            t.start()
            return
        if not path.is_file(): return
        with self._lock:
            if self._paused: return
            self._q.add(path)
            if self._timer: self._timer.cancel()
            t = threading.Timer(_DEBOUNCE, self._flush)
            t.daemon = True
            self._timer = t
        t.start()

    def _flush(self):
        with self._lock:
            paths,     self._q     = set(self._q),     set()
            del_paths, self._del_q = set(self._del_q), set()
        # Any local change (add / edit / delete) in the git folder triggers a
        # full wipe-and-reupload: the destination is deleted and rebuilt from
        # the repo so it always matches the source exactly. We no longer do
        # per-file incremental copies — that left stale/orphaned files behind.
        if not paths and not del_paths:
            return
        if self._paused:
            return
        try:
            mirror_clean(self.src, self.dst, self.log_fn)
            self.log_fn("⟳ resynced (local change → full re-upload)")
        except Exception as e:
            self.log_fn(f"watch resync err: {e}")
            return
        if self._flush_cb:
            # Report as a resync so the UI/toast reflects that something landed.
            try: self._flush_cb({"overwritten": set(), "new": set(),
                                 "deleted": set(), "resync": True})
            except Exception: pass


class WatcherManager:
    # Files in .git/ whose write means "a pull/checkout/merge/commit may have
    # changed HEAD". We re-read the HEAD SHA on any of these and dispatch
    # branch_cb (branch switched) or pull_cb (commit moved on same branch).
    _GIT_TRIGGERS = {"HEAD", "index", "ORIG_HEAD", "FETCH_HEAD", "MERGE_HEAD"}
    _PULL_DEBOUNCE = 1.2  # seconds; coalesce fetch+merge+index writes

    def __init__(self, log_fn, branch_cb, flush_cb=None, pull_cb=None):
        self._log       = log_fn
        self._branch_cb = branch_cb
        self._flush_cb  = flush_cb  # (proj_name, pack_type, paths) -> None
        self._pull_cb   = pull_cb   # (proj) -> None; called after a pull settles
        self._handles: dict[str, list] = {}
        self._packs:   dict[str, list[_PackWatcher]] = {}

    def start(self, proj: dict, pairs: list):
        name = proj["name"]
        if name in self._handles: return
        handles = []
        pack_watchers: list[_PackWatcher] = []

        for label, src, dst in pairs:
            if not dst.exists(): continue
            def _mk_cb(pn=name, pt=label):
                def cb(stats):
                    if self._flush_cb: self._flush_cb(pn, pt, stats)
                return cb
            pw = _PackWatcher(src, dst, self._log, flush_cb=_mk_cb())
            pack_watchers.append(pw)
            if IS_WIN:
                w = _Win32DirWatcher(src, pw.on_change, recursive=True,
                                     on_overflow=pw.on_overflow)
                w.start(); handles.append(w)
            else:
                obs = Observer()
                class _H(FileSystemEventHandler):
                    def on_modified(s, e):
                        if not e.is_directory: pw.on_change(Path(e.src_path))
                    on_created = on_modified
                obs.schedule(_H(), str(src), recursive=True)
                obs.daemon = True; obs.start(); handles.append(obs)

        # Watch .git/ for branch switches AND for pulls landing new commits on
        # the same branch. The latter is the case the per-file pack watcher
        # mishandles: it copies files mid-pull and Minecraft sees partial state.
        git_dir = proj["path"] / ".git"
        if (git_dir / "HEAD").exists():
            last_head_text = [(git_dir / "HEAD").read_text(errors='replace')]
            last_sha       = [git_head_sha(proj["path"])]
            pull_timer: list[threading.Timer | None] = [None]
            pull_lock = threading.Lock()

            def _settle_and_pull():
                # Pull has settled. Re-check SHA. If it moved, run the full
                # silent sync via pull_cb.
                #
                # If we CAN'T confirm the SHA moved, we used to just resume the
                # paused pack watchers — but that loses the pull's files: while
                # paused, every per-file change event was dropped, and modern
                # git often updates refs via packed-refs / logs that our loose
                # ref watcher doesn't always catch, so the SHA read can miss a
                # real move. So in the unconfirmed case we now ALSO do a full
                # re-mirror (Regolith-style: when unsure, just re-sync). It's
                # mtime-gated and cheap when nothing actually changed.
                moved = False
                try:
                    sha_now = git_head_sha(proj["path"])
                    moved = bool(sha_now) and sha_now != last_sha[0]
                    if sha_now:
                        last_sha[0] = sha_now
                except Exception:
                    pass
                if self._pull_cb:
                    # pull_cb runs the full silent sync and owns resume(). Run it
                    # whenever a pull may have landed, not only on a confirmed SHA
                    # move, so unconfirmed pulls still reach Mojang.
                    try: self._pull_cb(proj)
                    except Exception as e:
                        self._log(f"pull sync err: {e}")
                    return
                # No pull_cb wired: re-mirror each pack ourselves, then resume.
                if moved:
                    for pw in pack_watchers:
                        try: mirror_clean(pw.src, pw.dst, self._log)
                        except Exception as e:
                            self._log(f"pull resync err: {e}")
                for pw in pack_watchers:
                    pw.resume()

            def _check_git(path: Path, _action=None):
                if path.name not in self._GIT_TRIGGERS \
                   and not str(path).replace("\\", "/").startswith(
                       str(git_dir).replace("\\", "/") + "/refs/heads/"):
                    return
                try:
                    # Branch switch? (HEAD file's text changes when ref: line moves)
                    head_text = (git_dir / "HEAD").read_text(errors='replace')
                    if head_text != last_head_text[0]:
                        last_head_text[0] = head_text
                        last_sha[0] = git_head_sha(proj["path"])
                        # Branch change keeps existing flow (auto-sync toggle,
                        # confirm dialog). It already triggers a full _sync_one.
                        self._branch_cb(proj, git_branch(proj["path"]))
                        return
                    # Same branch, but a commit may have landed (pull/merge/commit).
                    # Pause pack watchers immediately so the in-flight partial
                    # copy can't slip through, then debounce until git is done.
                    for pw in pack_watchers:
                        pw.pause()
                    with pull_lock:
                        if pull_timer[0]: pull_timer[0].cancel()
                        t = threading.Timer(self._PULL_DEBOUNCE, _settle_and_pull)
                        t.daemon = True
                        pull_timer[0] = t
                    t.start()
                except Exception:
                    pass

            if IS_WIN:
                hw = _Win32DirWatcher(git_dir, _check_git, recursive=False)
                hw.start(); handles.append(hw)
                refs_heads = git_dir / "refs" / "heads"
                if refs_heads.exists():
                    rw = _Win32DirWatcher(refs_heads, _check_git, recursive=True)
                    rw.start(); handles.append(rw)
            else:
                obs2 = Observer()
                class _HH(FileSystemEventHandler):
                    def on_modified(s, e): _check_git(Path(e.src_path))
                    on_created = on_modified
                obs2.schedule(_HH(), str(git_dir), recursive=False)
                refs_heads = git_dir / "refs" / "heads"
                if refs_heads.exists():
                    obs2.schedule(_HH(), str(refs_heads), recursive=True)
                obs2.daemon = True; obs2.start(); handles.append(obs2)

        self._handles[name] = handles
        self._packs[name]   = pack_watchers

    def pause_packs(self, proj_name: str):
        for pw in self._packs.get(proj_name, []):
            pw.pause()

    def resume_packs(self, proj_name: str):
        for pw in self._packs.get(proj_name, []):
            pw.resume()

    def stop(self, proj_name: str):
        for h in self._handles.pop(proj_name, []):
            if IS_WIN: h.stop()
            else: h.stop(); h.join(timeout=2)
        self._packs.pop(proj_name, None)

    def restart(self, proj: dict, pairs: list):
        self.stop(proj["name"]); self.start(proj, pairs)

    def stop_all(self):
        for name in list(self._handles): self.stop(name)

# ─── Toast notifications ──────────────────────────────────────────────────────
class _Toast(tk.Toplevel):
    WIDTH      = 340
    HEIGHT     = 64
    STAY_MS    = 3000
    FADE_STEPS = 15
    FADE_MS    = 40

    def __init__(self, parent, title: str, manager: "ToastManager"):
        super().__init__(parent)
        self._manager = manager
        self._done    = False

        self.wm_overrideredirect(True)
        self.wm_attributes("-topmost", True)
        if IS_WIN:
            self.wm_attributes("-alpha", 0.93)

        self.configure(bg=BG2, highlightbackground=SURF2, highlightthickness=1)

        pad = tk.Frame(self, bg=BG2, padx=12, pady=8)
        pad.pack(fill=tk.BOTH, expand=True)

        row = tk.Frame(pad, bg=BG2)
        row.pack(fill=tk.X)
        self._title_var = tk.StringVar(value=title)
        tk.Label(row, textvariable=self._title_var,
                 font=(UI_FONT, 9, "bold"), bg=BG2, fg=TEXT,
                 anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._pct_lbl = tk.Label(row, text="0%", font=(UI_FONT, 8),
                                  bg=BG2, fg=SUB)
        self._pct_lbl.pack(side=tk.RIGHT)

        self._bar_cv = tk.Canvas(pad, bg=SURF2, height=4, highlightthickness=0)
        self._bar_cv.pack(fill=tk.X, pady=(6, 0))

        self.geometry(f"{self.WIDTH}x{self.HEIGHT}+9999+9999")
        self.update_idletasks()

    def set_progress(self, done: int, total: int):
        if self._done: return
        pct = int(done / total * 100) if total else 0
        self._draw_bar(pct, BLUE)
        self._pct_lbl.configure(text=f"{pct}%")

    def set_done(self):
        if self._done: return
        self._done = True
        self._draw_bar(100, GREEN)
        self._title_var.set(f"✓  {self._title_var.get()}")
        self._pct_lbl.configure(text="")
        self.after(self.STAY_MS, self._start_fade)

    def _draw_bar(self, pct: int, color: str):
        self._bar_cv.update_idletasks()
        w = self._bar_cv.winfo_width()
        if w < 4: w = self.WIDTH - 24
        fill_w = max(2, int(w * pct / 100))
        self._bar_cv.delete("all")
        self._bar_cv.create_rectangle(0, 0, fill_w, 4, fill=color, outline="")

    def move(self, x: int, y: int):
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

    def _start_fade(self):
        self._do_fade(self.FADE_STEPS)

    def _do_fade(self, step: int):
        if step <= 0:
            self._manager._remove(self)
            try: self.destroy()
            except Exception: pass
            return
        try:
            self.wm_attributes("-alpha", (step / self.FADE_STEPS) * 0.93)
            self.after(self.FADE_MS, lambda: self._do_fade(step - 1))
        except Exception:
            try: self.destroy()
            except Exception: pass


class ToastManager:
    _MARGIN_R = 14
    _MARGIN_B = 50
    _GAP      = 6

    def __init__(self, root: tk.Tk):
        self._root   = root
        self._toasts: list[_Toast] = []

    def show(self, title: str) -> _Toast:
        t = _Toast(self._root, title, self)
        self._toasts.append(t)
        self._reposition()
        return t

    def _remove(self, toast: _Toast):
        try: self._toasts.remove(toast)
        except ValueError: pass
        self._reposition()

    def _reposition(self):
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        y  = sh - self._MARGIN_B
        for t in reversed(self._toasts):
            y -= _Toast.HEIGHT
            t.move(sw - _Toast.WIDTH - self._MARGIN_R, y)
            y -= self._GAP


# ─── One-time dialogs ─────────────────────────────────────────────────────────
class BranchWarningDialog(tk.Toplevel):
    def __init__(self, parent, proj_name, old, new):
        super().__init__(parent); self.title("Branch Changed")
        self.configure(bg=BG)
        self.grab_set(); self.resizable(False, False)
        self.result = False
        tk.Label(self, text="⚠  Branch Changed", font=("Segoe UI",13,"bold"),
                 bg=BG, fg=YELLOW).pack(pady=(18,6))
        tk.Label(self, text=f"{proj_name}\n{old}  →  {new}",
                 font=("Segoe UI",10), bg=BG, fg=TEXT, justify="center").pack(pady=4)
        tk.Label(self, text=("Syncing will overwrite the destination folder\n"
                             "with the new branch contents. Continue?"),
                 font=("Segoe UI",9), bg=BG, fg=SUB, justify="center").pack(pady=8)
        row = tk.Frame(self, bg=BG); row.pack(pady=10)
        tk.Button(row, text="Yes, Sync", command=self._yes, bg=GREEN, fg=BG,
                  font=("Segoe UI",9,"bold"), relief=tk.FLAT, padx=14, pady=5,
                  cursor="hand2").pack(side=tk.LEFT, padx=6)
        tk.Button(row, text="Cancel", command=self.destroy, bg=SURF2, fg=TEXT,
                  font=("Segoe UI",9), relief=tk.FLAT, padx=14, pady=5,
                  cursor="hand2").pack(side=tk.LEFT, padx=6)
        _center_on(self, parent, 460, 240)
    def _yes(self): self.result = True; self.destroy()


class FirstSyncWarningDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent); self.title("Before You Sync")
        self.configure(bg=BG)
        self.grab_set(); self.resizable(False, False)
        self.proceed = False; self.skip_next = False
        tk.Label(self, text="Heads up!", font=("Segoe UI",13,"bold"),
                 bg=BG, fg=YELLOW).pack(pady=(18,6))
        tk.Label(self,
                 text=("Pack Sync will copy files into your com.mojang folder.\n\n"
                       "If a destination pack folder already exists, its contents\n"
                       "will be merged — newer files win. Conflicts are not tracked\n"
                       "by git, so commit your repo before syncing."),
                 font=("Segoe UI",9), bg=BG, fg=TEXT, justify="center").pack(pady=4, padx=20)
        self._skip = tk.BooleanVar(value=False)
        tk.Checkbutton(self, text="Don't show this again", variable=self._skip,
                       bg=BG, fg=SUB, selectcolor=SURFACE, activebackground=BG,
                       activeforeground=TEXT, font=("Segoe UI",9)).pack(pady=6)
        row = tk.Frame(self, bg=BG); row.pack(pady=6)
        tk.Button(row, text="Got it — Sync", command=self._ok, bg=GREEN, fg=BG,
                  font=("Segoe UI",9,"bold"), relief=tk.FLAT, padx=14, pady=5,
                  cursor="hand2").pack(side=tk.LEFT, padx=6)
        tk.Button(row, text="Cancel", command=self.destroy, bg=SURF2, fg=TEXT,
                  font=("Segoe UI",9), relief=tk.FLAT, padx=14, pady=5,
                  cursor="hand2").pack(side=tk.LEFT, padx=6)
        _center_on(self, parent, 480, 265)
    def _ok(self):
        self.proceed = True; self.skip_next = self._skip.get(); self.destroy()

# ─── Per-project config dialog ────────────────────────────────────────────────
class ProjectConfigDialog(tk.Toplevel):
    """Opens when the user double-clicks a project card.
    Lets them configure custom folder pairs and toggle Regolith controls."""

    def __init__(self, parent, proj: dict, proj_cfg: dict, default_pairs: list):
        super().__init__(parent)
        self.title(f"Project Settings  ·  {proj['name']}")
        self.configure(bg=BG)
        self.grab_set()
        self.resizable(True, True)
        self.result = None  # set to updated dict on Save

        self._proj = proj
        # Working copy of pairs: [{"label": str, "src": str, "dst": str}]
        if proj_cfg.get("pairs"):
            self._pairs = [dict(p) for p in proj_cfg["pairs"]]
        else:
            self._pairs = [{"label": lbl, "src": str(s), "dst": str(d)}
                           for lbl, s, d in default_pairs]

        self._show_rg = tk.BooleanVar(value=proj_cfg.get("show_regolith", False))
        self._build()
        _center_on(self, parent, 620, 400)

    def _build(self):
        # Header
        hdr = tk.Frame(self, bg=BG2, pady=10); hdr.pack(fill=tk.X)
        tk.Label(hdr, text=f"⚙  {self._proj['name']}",
                 font=(UI_FONT, 13, "bold"), bg=BG2, fg=TEXT).pack(side=tk.LEFT, padx=14)

        # Note
        tk.Label(self, text="Files copy from  Input → Output  only (one-way).",
                 font=(UI_FONT, 8), bg=BG, fg=SUB).pack(anchor="w", padx=14, pady=(10, 2))
        tk.Label(self, text="Folder Pairs", font=(UI_FONT, 10, "bold"),
                 bg=BG, fg=TEXT).pack(anchor="w", padx=14, pady=(0, 4))

        # Pairs list
        self._pairs_frame = tk.Frame(self, bg=BG)
        self._pairs_frame.pack(fill=tk.X, padx=14)
        self._redraw_pairs()

        # Add pair
        add_row = tk.Frame(self, bg=BG); add_row.pack(fill=tk.X, padx=14, pady=6)
        tk.Button(add_row, text="+ Add Pair", command=self._add_pair,
                  bg=SURF2, fg=TEXT, relief=tk.FLAT, font=(UI_FONT, 9),
                  padx=10, pady=4, cursor="hand2").pack(side=tk.LEFT)

        # Regolith toggle (only for regolith projects)
        if self._proj.get("is_regolith"):
            tk.Frame(self, bg=SURF2, height=1).pack(fill=tk.X, padx=14, pady=(6, 0))
            rg_row = tk.Frame(self, bg=BG); rg_row.pack(fill=tk.X, padx=14, pady=8)
            tk.Checkbutton(rg_row,
                           text="Show Regolith controls  (profile selector + Build button)",
                           variable=self._show_rg,
                           bg=BG, fg=TEXT, selectcolor=SURFACE,
                           activebackground=BG, activeforeground=TEXT,
                           font=(UI_FONT, 9)).pack(side=tk.LEFT)

        # Footer
        tk.Frame(self, bg=SURF2, height=1).pack(fill=tk.X, pady=(6, 0))
        foot = tk.Frame(self, bg=BG, pady=8); foot.pack(fill=tk.X)
        tk.Button(foot, text="Save", command=self._save,
                  bg=BLUE, fg=BG, font=(UI_FONT, 9, "bold"), relief=tk.FLAT,
                  padx=16, pady=5, cursor="hand2").pack(side=tk.RIGHT, padx=14)
        tk.Button(foot, text="Cancel", command=self.destroy,
                  bg=SURF2, fg=TEXT, font=(UI_FONT, 9), relief=tk.FLAT,
                  padx=16, pady=5, cursor="hand2").pack(side=tk.RIGHT, padx=4)

    def _redraw_pairs(self):
        for w in self._pairs_frame.winfo_children(): w.destroy()
        for i, pair in enumerate(self._pairs):
            self._draw_pair_row(i, pair)

    def _draw_pair_row(self, idx: int, pair: dict):
        row = tk.Frame(self._pairs_frame, bg=SURFACE, padx=8, pady=6)
        row.pack(fill=tk.X, pady=2)

        # Label
        lbl_var = tk.StringVar(value=pair.get("label", ""))
        tk.Label(row, text="Label:", bg=SURFACE, fg=MUTED,
                 font=(UI_FONT, 8)).pack(side=tk.LEFT)
        lbl_e = tk.Entry(row, textvariable=lbl_var, width=5,
                         bg=SURF2, fg=TEXT, insertbackground=TEXT,
                         relief=tk.FLAT, font=(UI_FONT, 8))
        lbl_e.pack(side=tk.LEFT, padx=(2, 8))
        lbl_var.trace_add("write",
                          lambda *_, v=lbl_var, i=idx: self._pairs[i].update(label=v.get()))

        # Input
        src_var = tk.StringVar(value=pair.get("src", ""))
        tk.Label(row, text="In:", bg=SURFACE, fg=MUTED,
                 font=(UI_FONT, 8)).pack(side=tk.LEFT)
        tk.Entry(row, textvariable=src_var, width=20,
                 bg=SURF2, fg=TEXT, insertbackground=TEXT,
                 relief=tk.FLAT, font=(UI_FONT, 8)).pack(side=tk.LEFT, padx=2)
        tk.Button(row, text="📂",
                  command=lambda i=idx, v=src_var: self._browse(i, "src", v),
                  bg=SURF2, fg=TEXT, relief=tk.FLAT, font=(UI_FONT, 8),
                  cursor="hand2", padx=2).pack(side=tk.LEFT, padx=(0, 6))
        src_var.trace_add("write",
                          lambda *_, v=src_var, i=idx: self._pairs[i].update(src=v.get()))

        # Arrow
        tk.Label(row, text="→", bg=SURFACE, fg=MUTED,
                 font=(UI_FONT, 9)).pack(side=tk.LEFT)

        # Output
        dst_var = tk.StringVar(value=pair.get("dst", ""))
        tk.Label(row, text="Out:", bg=SURFACE, fg=MUTED,
                 font=(UI_FONT, 8)).pack(side=tk.LEFT)
        tk.Entry(row, textvariable=dst_var, width=20,
                 bg=SURF2, fg=TEXT, insertbackground=TEXT,
                 relief=tk.FLAT, font=(UI_FONT, 8)).pack(side=tk.LEFT, padx=2)
        tk.Button(row, text="📂",
                  command=lambda i=idx, v=dst_var: self._browse(i, "dst", v),
                  bg=SURF2, fg=TEXT, relief=tk.FLAT, font=(UI_FONT, 8),
                  cursor="hand2", padx=2).pack(side=tk.LEFT)
        dst_var.trace_add("write",
                          lambda *_, v=dst_var, i=idx: self._pairs[i].update(dst=v.get()))

        # Remove
        tk.Button(row, text="✕",
                  command=lambda i=idx: self._remove_pair(i),
                  bg=SURFACE, fg=RED, relief=tk.FLAT,
                  font=(UI_FONT, 9), cursor="hand2", padx=6).pack(side=tk.RIGHT)

    def _browse(self, idx: int, key: str, var: tk.StringVar):
        initial = var.get().strip()
        if not initial or not Path(initial).exists():
            initial = str(Path.home())
        path = filedialog.askdirectory(parent=self, initialdir=initial,
                                       title="Select folder")
        if path:
            var.set(path)
            self._pairs[idx][key] = path

    def _add_pair(self):
        self._pairs.append({"label": f"P{len(self._pairs)+1}", "src": "", "dst": ""})
        self._redraw_pairs()

    def _remove_pair(self, idx: int):
        if 0 <= idx < len(self._pairs):
            self._pairs.pop(idx)
            self._redraw_pairs()

    def _save(self):
        valid = [p for p in self._pairs if p.get("src") and p.get("dst")]
        self.result = {"pairs": valid, "show_regolith": self._show_rg.get()}
        self.destroy()


# ─── Main window ──────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Pack Sync")
        self.minsize(560, 400); self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self._hide)
        _cleanup_old_update()  # remove leftover *.old binary from a prior update
        self.cfg        = load_cfg()
        self._tray      = None
        self._toast_mgr = ToastManager(self)
        self._watchers  = WatcherManager(self._status, self._on_branch_change,
                                         self._on_watcher_flush,
                                         self._on_pull_detected)
        self._pack_bars: dict[str, ttk.Progressbar] = {}  # "projname:PT" → bar widget
        self._selection_mode    = False
        self._card_checks:      dict[str, tk.BooleanVar] = {}
        self._card_sel_widgets: dict[str, tuple]         = {}
        self._all_card_frames:  dict[str, tk.Frame]      = {}
        self._start_minimized   = "--minimized" in sys.argv
        apply_lang(self.cfg.get("language", detect_system_lang()))

        style = ttk.Style(self); style.theme_use("clam")
        style.configure("Vertical.TScrollbar", background=SURFACE,
                        troughcolor=BG2, borderwidth=0, arrowcolor=SUB)

        # Set titlebar icon from the same PNG used for tray
        self._set_window_icon()
        # Restore or centre window
        self._restore_geometry()
        # Save position whenever it moves or resizes
        self.bind("<Configure>", self._on_configure)

        if not self.cfg.get("setup_done"):
            self.after(80, self._first_launch)
        else:
            self._build_ui(); self._start_tray()
            if self._start_minimized:
                self.withdraw()
                if IS_WIN:
                    self._win_set_taskbar(visible=False)
            elif not self.cfg.get("intro_seen"):
                self.after(200, self._show_intro)
            # First run after this feature ships: ask once whether to enable
            # automatic update checking. Then run the check on every startup if on.
            self.after(400, self._prompt_auto_update_once)
            self.after(1500, self._maybe_check_update)

    def _prompt_auto_update_once(self):
        """Ask the user (once) whether Pack Sync may check for updates. Default
        is ON. Stored in cfg['auto_update']; toggle later in Settings."""
        if self.cfg.get("auto_update_prompted"):
            return
        self.cfg["auto_update_prompted"] = True
        enable = messagebox.askyesno(
            "Automatic updates",
            "Allow Pack Sync to check for updates on startup and install the "
            "latest version automatically (downloaded from GitHub Releases)?\n\n"
            "You can change this anytime in Settings.",
            default=messagebox.YES)
        self.cfg["auto_update"] = bool(enable)
        save_cfg(self.cfg)
        if enable:
            self._maybe_check_update()

    def _set_window_icon(self):
        # On Windows, iconbitmap(.ico) is the most reliable method
        if IS_WIN:
            ico = _find_bundled_ico()
            if ico:
                try:
                    self.iconbitmap(ico)
                    return
                except Exception:
                    pass
        # Fallback: build PNG in memory and hand it to tkinter's PhotoImage
        try:
            import base64
            img = _make_icon(32)
            buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
            photo = tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode())
            self.iconphoto(True, photo)
            self._icon_photo = photo  # prevent GC
        except Exception:
            pass

    def _restore_geometry(self):
        saved_w = self.cfg.get("window_w", 700)
        saved_h = self.cfg.get("window_h", 540)
        saved_x = self.cfg.get("window_x")
        saved_y = self.cfg.get("window_y")
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        if saved_x is not None and saved_y is not None:
            # Clamp to screen so window can't end up off-screen
            x = max(0, min(saved_x, sw - saved_w))
            y = max(0, min(saved_y, sh - saved_h))
        else:
            x = (sw - saved_w) // 2
            y = (sh - saved_h) // 2
        self.geometry(f"{saved_w}x{saved_h}+{x}+{y}")

    def _on_configure(self, _event=None):
        geo = self.geometry()   # "WxH+X+Y"
        try:
            size, rest = geo.split("+", 1)
            x, y = rest.split("+")
            w, h = size.split("x")
            self.cfg.update(window_w=int(w), window_h=int(h),
                            window_x=int(x), window_y=int(y))
            save_cfg(self.cfg)
        except (ValueError, AttributeError):
            pass

    # ── Tray ──────────────────────────────────────────────────────────────────
    def _start_tray(self):
        self._tray_active = False
        try:
            if IS_WIN:
                # Launch TrayHelper.exe (C# WinForms NotifyIcon) as a child process.
                # Callbacks are dispatched back to this tkinter thread via self.after().
                self._tray = _CSharpTray(
                    ico_path         = _find_bundled_ico(),
                    on_open          = self._show,
                    on_sync          = lambda: threading.Thread(
                                           target=self._do_sync_all, daemon=True).start(),
                    on_quit          = self._quit_app,
                    on_balloon_click = self._on_tray_balloon_click,
                    on_ready         = self._on_tray_ready,
                    tk_schedule      = lambda fn: self.after(0, fn),
                )
                self._tray.run_detached()
                # "READY" from TrayHelper sets _tray.active; verify after 3 s
                self.after(3000, self._verify_tray)
                return      # skip pystray path below

            # ── macOS / Linux: use pystray ────────────────────────────────────
            icon_img = _make_icon()
            icon_img.load()
            icon_img = icon_img.copy()
            menu = pystray.Menu(
                pystray.MenuItem("Open Pack Sync", self._show, default=True),
                pystray.MenuItem("Sync All", lambda: threading.Thread(
                    target=self._do_sync_all, daemon=True).start()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit_app))
            self._tray = pystray.Icon(APP_NAME, icon_img, "Pack Sync", menu)

            def _on_setup(icon):
                self._tray_active = True

            def _run_tray():
                try:
                    self._tray.run(setup=_on_setup)
                except Exception as exc:
                    self._tray = None
                    self._tray_active = False
                    self.after(0, lambda e=exc: self._on_tray_error(e))

            threading.Thread(target=_run_tray, daemon=True).start()
            self.after(3000, self._verify_tray)

        except Exception as e:
            self._tray = None
            self._on_tray_error(e)

    def _verify_tray(self):
        if self._tray is None:
            return
        # _CSharpTray.active becomes True when TrayHelper sends "READY"
        ok = self._tray.active if hasattr(self._tray, 'active') else self._tray_active
        if ok:
            self._tray_active = True
        else:
            self._tray = None
            self._on_tray_error(Exception(
                "TrayHelper.exe did not send READY — the tray icon may be missing.\n"
                "Check pack_sync_tray_error.log next to the executable."))

    def _on_tray_error(self, exc: Exception):
        """Log tray failures to a file next to the exe for post-mortem debugging."""
        msg = str(exc)
        try:
            log_path = CONFIG_FILE.parent / "pack_sync_tray_error.log"
            log_path.write_text(f"Tray error: {msg}\n", encoding="utf-8")
        except Exception:
            pass

    def _on_tray_ready(self):
        """Called on the tkinter thread when TrayHelper sends READY."""
        self._tray_active = True
        if self._start_minimized and self.cfg.get("startup_notify", True):
            self.after(500, self._show_startup_balloon)

    def _show_startup_balloon(self):
        if self._tray and hasattr(self._tray, "notify"):
            self._tray.notify(
                "Pack Sync started",
                "Running in background. Click this notification to stop showing it.",
                5000)

    def _on_tray_balloon_click(self):
        """User clicked the startup balloon — disable future startup notifications."""
        if self.cfg.get("startup_notify", True):
            self.cfg["startup_notify"] = False
            save_cfg(self.cfg)

    # ── Platform helpers for Discord-style hide/show ───────────────────────────
    def _win_set_taskbar(self, visible: bool):
        """Add or remove the window from the Windows taskbar via Win32 styles."""
        try:
            hwnd = self.winfo_id()
            GWL_EXSTYLE    = -20
            WS_EX_APPWINDOW  = 0x00040000
            WS_EX_TOOLWINDOW = 0x00000080
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if visible:
                style = (style | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
            else:
                style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass

    def _mac_set_dock(self, visible: bool):
        """Show or hide the macOS Dock icon (requires pyobjc, pre-installed on macOS)."""
        try:
            from AppKit import (NSApp,
                                NSApplicationActivationPolicyRegular,
                                NSApplicationActivationPolicyProhibited)
            if visible:
                NSApp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
                NSApp.activateIgnoringOtherApps_(True)
            else:
                NSApp.setActivationPolicy_(NSApplicationActivationPolicyProhibited)
        except Exception:
            pass

    def _hide(self):
        # Only do a full Discord-style hide when the tray icon is confirmed visible.
        # If the icon never appeared, minimise instead so the user can still reopen.
        if self._tray is not None and getattr(self, '_tray_active', False):
            self.withdraw()
            if IS_WIN:
                self._win_set_taskbar(visible=False)
                if not self.cfg.get("tray_hint_shown") and hasattr(self._tray, "notify"):
                    self._tray.notify(
                        "Pack Sync is still running",
                        "Find the icon near the clock to reopen.",
                        5000)
                    self.cfg["tray_hint_shown"] = True
                    save_cfg(self.cfg)
            elif IS_MAC:
                self._mac_set_dock(visible=False)
        else:
            self.iconify()

    def _show(self, *_):
        # Restore taskbar/Dock entry before making window visible
        if IS_WIN:
            self._win_set_taskbar(visible=True)
        elif IS_MAC:
            self._mac_set_dock(visible=True)
        self.deiconify(); self.lift(); self.focus_force()

    def _quit_app(self, *_):
        self._watchers.stop_all()
        if self._tray: self._tray.stop()
        self.destroy()

    # ── First launch ──────────────────────────────────────────────────────────
    def _first_launch(self):
        dlg = OnboardingWizard(self, start_page=0); self.wait_window(dlg)
        if dlg.result:
            self.cfg.update(dlg.result)
            self.cfg["setup_done"] = True
            self.cfg["intro_seen"] = True
            save_cfg(self.cfg)
        self._build_ui(); self._start_tray()

    def _show_intro(self):
        dlg = OnboardingWizard(self, start_page=0, intro_only=True)
        self.wait_window(dlg)
        self.cfg["intro_seen"] = True
        save_cfg(self.cfg)

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # ── Header ──
        hdr = tk.Frame(self, bg=BG2, pady=10); hdr.pack(fill=tk.X)
        # Inline sync-arrows canvas icon
        ic = tk.Canvas(hdr, width=26, height=26, bg=BG2, highlightthickness=0)
        ic.pack(side=tk.LEFT, padx=(14, 0), pady=2)
        ic.create_arc(3, 3, 23, 23, start=30, extent=160, style="arc",
                      outline=BLUE, width=3)
        ic.create_arc(3, 3, 23, 23, start=210, extent=160, style="arc",
                      outline=GREEN, width=3)
        # Arrow tips
        ic.create_polygon(20, 5, 23, 10, 16, 9, fill=BLUE, outline="")
        ic.create_polygon(6, 21, 3, 16, 10, 17, fill=GREEN, outline="")
        tk.Label(hdr, text="Pack Sync", font=(UI_FONT, 14, "bold"),
                 bg=BG2, fg=TEXT).pack(side=tk.LEFT, padx=6)
        right = tk.Frame(hdr, bg=BG2); right.pack(side=tk.RIGHT, padx=10)
        self._mk_btn(right, "?", self._show_intro, SURF2, YELLOW,
                     padx=8, pady=5).pack(side=tk.LEFT, padx=3)
        auto_on = self.cfg.get("auto_sync", False)
        self._auto_btn = self._mk_btn(right, "⚡ Auto", self._toggle_auto,
                                      GREEN if auto_on else SURF2,
                                      BG   if auto_on else TEXT,
                                      padx=8, pady=5)
        self._auto_btn.pack(side=tk.LEFT, padx=3)
        self._sel_mode_btn = self._mk_btn(right, "Hide Repos", self._toggle_select_mode,
                                          SURF2, TEXT, padx=8, pady=5)
        self._sel_mode_btn.pack(side=tk.LEFT, padx=3)
        self._hide_sel_btn = self._mk_btn(right, "Hide Selected", self._hide_selected,
                                          YELLOW, BG, padx=8, pady=5)
        # shown only in selection mode
        self._mk_btn(right, t("btn_refresh"), self._refresh, SURF2, TEXT).pack(
            side=tk.LEFT, padx=3)
        self._mk_btn(right, t("btn_settings"), self._open_settings, SURF2, TEXT).pack(
            side=tk.LEFT, padx=3)

        # ── Status bar ──
        status_bar = tk.Frame(self, bg=BG2); status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self._status_icon = tk.Label(status_bar, text="●", bg=BG2, fg=GREEN,
                                     font=(UI_FONT, 9))
        self._status_icon.pack(side=tk.LEFT, padx=(10, 4), pady=3)
        self._status_var = tk.StringVar(value=t("status_ready"))
        tk.Label(status_bar, textvariable=self._status_var, bg=BG2, fg=SUB,
                 font=(UI_FONT, 9), anchor="w", pady=3).pack(
                     side=tk.LEFT, fill=tk.X)
        style = ttk.Style()
        style.configure("Sync.Horizontal.TProgressbar",
                        troughcolor=BG2, background=BLUE, thickness=4)
        self._prog_bar = ttk.Progressbar(status_bar, orient="horizontal",
                                         length=140, mode="determinate",
                                         style="Sync.Horizontal.TProgressbar")
        self._prog_bar.pack(side=tk.RIGHT, padx=(4, 10), pady=6)

        # ── Sync-all footer ──
        foot = tk.Frame(self, bg=BG, pady=10); foot.pack(side=tk.BOTTOM, fill=tk.X)
        self._mk_btn(
            foot,
            t("btn_sync_all"),
            lambda: threading.Thread(target=self._do_sync_all, daemon=True).start(),
            BLUE, BG, font=(UI_FONT, 10, "bold"), padx=28, pady=8
        ).pack()

        # ── Search / filter ──
        search_bar = tk.Frame(self, bg=BG, pady=6); search_bar.pack(fill=tk.X, padx=14)
        tk.Label(search_bar, text="🔍", bg=BG, fg=MUTED,
                 font=("Segoe UI", 10)).pack(side=tk.LEFT, padx=(0, 6))
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._filter_cards())
        entry = tk.Entry(search_bar, textvariable=self._search_var,
                         bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                         relief=tk.FLAT, font=("Segoe UI", 10))
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        # Placeholder hint
        hint = tk.Label(search_bar, text=t("search_hint"), bg=SURFACE, fg=MUTED,
                        font=(UI_FONT, 10))
        hint.place_forget()
        entry.bind("<FocusIn>",  lambda _: hint.place_forget())
        entry.bind("<FocusOut>",
                   lambda _: hint.place(in_=entry, x=6, y=2)
                   if not self._search_var.get() else None)
        hint.place(in_=entry, x=6, y=2)
        hint.bind("<Button-1>", lambda _: entry.focus_set())

        # ── Scrollable card list ──
        outer = tk.Frame(self, bg=BG); outer.pack(fill=tk.BOTH, expand=True)
        self._canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=self._canvas.yview)
        self._cards = tk.Frame(self._canvas, bg=BG)
        self._cards.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        _win_id = self._canvas.create_window((0, 0), window=self._cards, anchor="nw")
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfig(_win_id, width=e.width))
        self._canvas.configure(yscrollcommand=sb.set)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.bind_all("<MouseWheel>",
            lambda e: self._canvas.yview_scroll(-1 * (e.delta // 120), "units"))
        self._refresh()

    @staticmethod
    def _mk_btn(parent, text, cmd, bg, fg, font=("Segoe UI", 9),
                padx=12, pady=5) -> tk.Button:
        return tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                         relief=tk.FLAT, activebackground=SURF2,
                         activeforeground=TEXT, font=font,
                         padx=padx, pady=pady, cursor="hand2")

    def _status(self, msg: str, ok: bool = True):
        self._status_var.set(msg)
        self._status_icon.config(fg=GREEN if ok else YELLOW)
        self.update_idletasks()

    def _set_progress(self, done: int, total: int):
        pct = int(done / total * 100) if total else 0
        self._prog_bar["value"] = pct
        self._status_var.set(t("progress_syncing_files", done, total, pct))
        self.update_idletasks()

    def _reset_progress(self):
        self._prog_bar["value"] = 0

    def _get_paths(self) -> tuple[Path, Path]:
        return (Path(self.cfg.get("github_dir", str(DEFAULT_GITHUB))),
                Path(self.cfg.get("mojang_dir",  str(DEFAULT_MOJANG))))

    def _refresh(self):
        """Full discovery + card rebuild. Called on startup, manual refresh, settings change."""
        for w in self._cards.winfo_children(): w.destroy()
        self._pack_bars = {}
        self._card_checks.clear()
        self._card_sel_widgets.clear()
        self._card_frames:     list[tuple[tk.Frame, str]] = []
        self._all_card_frames: dict[str, tk.Frame]        = {}
        github, mojang = self._get_paths()
        projects = discover_projects(github)
        self._all_projects = projects
        hidden = set(self.cfg.get("hidden_repos", []))

        if not projects:
            tk.Label(self._cards, text=t("empty_no_projects"), bg=BG, fg=MUTED,
                     font=(UI_FONT, 11), justify="center").pack(pady=60)
            return
        for p in projects:
            row_wrap = self._make_card(p)
            self._all_card_frames[p["name"]] = row_wrap
            if p["name"] not in hidden:
                self._card_frames.append((row_wrap, p["name"].lower()))
                self._watchers.restart(p, self._get_sync_pairs(p))
        self._filter_cards()

    def _filter_cards(self):
        """Show/hide already-built card frames by search text — no filesystem access."""
        q = getattr(self, "_search_var", None)
        q = q.get().lower().strip() if q else ""
        frames = getattr(self, "_card_frames", [])

        # Remove any stale "no match" label
        for w in self._cards.winfo_children():
            if getattr(w, "_no_match_label", False):
                w.destroy()

        visible = 0
        for outer, name in frames:
            if not q or q in name:
                outer.pack(fill=tk.X, padx=14, pady=6)
                visible += 1
            else:
                outer.pack_forget()

        if visible == 0 and q:
            lbl = tk.Label(self._cards, text=t("empty_no_match", q), bg=BG, fg=MUTED,
                           font=(UI_FONT, 11), justify="center")
            lbl._no_match_label = True
            lbl.pack(pady=60)

    def _make_card(self, proj: dict) -> tk.Frame:
        show_rg = (proj.get("is_regolith") and
                   self.cfg.get("proj_cfg", {}).get(proj["name"], {}).get("show_regolith", False))
        if show_rg:
            return self._make_regolith_card(proj)
        return self._make_sync_card(proj)

    def _make_regolith_card(self, proj: dict):
        rg      = proj["regolith"]
        _, mojang = self._get_paths()
        branch  = git_branch(proj["path"])
        regolith_exe = _find_regolith_exe()

        # ── Row wrapper (checkbox lives outside the card, to its left) ──
        row_wrap = tk.Frame(self._cards, bg=BG)

        chk_var = tk.BooleanVar(value=False)
        self._card_checks[proj["name"]] = chk_var
        chk_holder = tk.Frame(row_wrap, bg=BG, width=36, cursor="hand2")
        chk_holder.pack_propagate(False)
        tk.Checkbutton(chk_holder, variable=chk_var, bg=BG, fg=TEXT,
                       selectcolor=SURFACE, activebackground=BG,
                       relief=tk.FLAT, cursor="hand2").pack(anchor="center", expand=True)
        chk_holder.bind("<Button-1>", lambda e, v=chk_var: v.set(not v.get()))
        if self._selection_mode:
            chk_holder.pack(side=tk.LEFT, fill=tk.Y)

        # ── Card shell ──
        outer = tk.Frame(row_wrap, bg=BG2,
                         highlightbackground=PEACH, highlightthickness=1)
        card = tk.Frame(outer, bg=SURFACE, padx=16, pady=12)
        card.pack(fill=tk.X)
        outer.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._card_sel_widgets[proj["name"]] = (chk_holder, outer)

        # ── Title row ──
        top = tk.Frame(card, bg=SURFACE); top.pack(fill=tk.X)

        # ── Profile selector + all action buttons — packed RIGHT first ──
        profile_names = rg["profile_names"]
        cfg_key = f"rg_profile_{proj['name']}"
        saved_profile = self.cfg.get(cfg_key, profile_names[0] if profile_names else "default")
        if saved_profile not in profile_names and profile_names:
            saved_profile = profile_names[0]
        prof_var = tk.StringVar(value=saved_profile)

        btns = tk.Frame(top, bg=SURFACE); btns.pack(side=tk.RIGHT)
        def _save_profile(*_):
            self.cfg[cfg_key] = prof_var.get(); save_cfg(self.cfg)

        # Left-side labels
        tk.Label(top, text=proj["name"], font=(UI_FONT, 11, "bold"),
                 bg=SURFACE, fg=TEXT).pack(side=tk.LEFT)

        if branch or not regolith_exe:
            sub = tk.Frame(card, bg=SURFACE); sub.pack(fill=tk.X, pady=(2, 0))
            if branch:
                tk.Label(sub, text=f"⎇  {branch}", font=(UI_FONT, 8),
                         bg=SURFACE, fg=SUB).pack(side=tk.LEFT)
            if not regolith_exe:
                tk.Label(sub, text="⚠ regolith not found", font=(UI_FONT, 8),
                         bg=RED, fg=BG, padx=4).pack(side=tk.LEFT,
                         padx=(6 if branch else 0, 0))
        # Sync button — plain file sync without running filters
        self._mk_btn(btns, t("btn_sync"),
                     lambda p=proj: threading.Thread(
                         target=self._sync_one, args=(p,), daemon=True).start(),
                     GREEN, BG, padx=10, pady=3).pack(side=tk.LEFT, padx=3)
        # Profile dropdown
        om = tk.OptionMenu(btns, prof_var, *profile_names, command=_save_profile)
        om.config(bg=SURF2, fg=TEXT, activebackground=SURF2, activeforeground=TEXT,
                  relief=tk.FLAT, font=(UI_FONT, 9), highlightthickness=0, width=10)
        om["menu"].config(bg=SURFACE, fg=TEXT, activebackground=PEACH, activeforeground=BG)
        om.pack(side=tk.LEFT, padx=3)
        # Build button — full Regolith pipeline
        self._mk_btn(btns, "▶  Build",
                     lambda p=proj, pv=prof_var: self._regolith_build(p, pv.get()),
                     PEACH, BG, padx=10, pady=3).pack(side=tk.LEFT, padx=3)
        self._mk_btn(btns, t("btn_remove"),
                     lambda p=proj: self._remove_project(p),
                     RED, BG, padx=10, pady=3).pack(side=tk.LEFT, padx=3)

        # ── Divider ──
        tk.Frame(card, bg=SURF2, height=1).pack(fill=tk.X, pady=(8, 6))

        # ── Pack rows (same as non-Regolith) ──
        for pack_type, src in proj["packs"].items():
            row = tk.Frame(card, bg=SURFACE, pady=3); row.pack(fill=tk.X)
            badge_bg = BLUE if pack_type == "RP" else PEACH
            tk.Label(row, text=f" {pack_type} ", bg=badge_bg, fg=BG,
                     font=(UI_FONT, 8, "bold")).pack(side=tk.LEFT, padx=(0, 10))
            rel = src.relative_to(proj["path"]) if src.is_relative_to(proj["path"]) else src
            tk.Label(row, text=f"📂  {rel}", bg=SURFACE, fg=SUB,
                     font=(UI_FONT, 9), anchor="w", justify="left").pack(side=tk.LEFT, fill=tk.X)
            dst = dest_path(mojang, proj, pack_type)
            state = (t("lbl_synced") if dst.exists() else t("lbl_not_synced"))
            st_fg = (GREEN if dst.exists() else MUTED)
            tk.Label(row, text=state, bg=SURFACE, fg=st_fg,
                     font=(UI_FONT, 8)).pack(side=tk.RIGHT)
            # Per-pack progress bar
            bar_key = f"{proj['name']}:{pack_type}"
            bar_frame = tk.Frame(card, bg=SURFACE); bar_frame.pack(fill=tk.X, pady=(0, 2))
            style_key = f"Pack{bar_key.replace(':', '_').replace('-', '_')}.Horizontal.TProgressbar"
            bar_color = BLUE if pack_type == "RP" else PEACH
            ttk.Style().configure(style_key, troughcolor=SURFACE, background=bar_color, thickness=3)
            pb = ttk.Progressbar(bar_frame, orient="horizontal", mode="determinate",
                                 style=style_key, length=0)
            pb.pack(fill=tk.X); pb.pack_forget()
            self._pack_bars[bar_key] = pb

        tk.Frame(card, bg=SURF2, height=1).pack(fill=tk.X, pady=(8, 6))

        # ── Filter pipeline for selected profile ──
        def _draw_pipeline(profile_name: str, frame: tk.Frame):
            for w in frame.winfo_children(): w.destroy()
            pcfg = rg["profiles"].get(profile_name, {})
            filters = pcfg.get("filters", [])
            frow = tk.Frame(frame, bg=SURFACE); frow.pack(fill=tk.X)
            tk.Label(frow, text="Pipeline:", font=(UI_FONT, 8, "bold"),
                     bg=SURFACE, fg=MUTED).pack(side=tk.LEFT, padx=(0, 6))
            for i, flt in enumerate(filters):
                fname = flt.get("filter", "?")
                fdef  = rg["filter_defs"].get(fname, {})
                runtime = fdef.get("runWith", "ext" if fdef.get("url") else "?")
                runtime_short = {"nodejs": "JS", "python": "Py", "ext": "↓"}.get(runtime, runtime)
                ok = runtime == "ext" or _runtime_ok(runtime)
                rt_col = GREEN if ok else RED
                # Badge: name + runtime
                fbg = SURF2
                fframe = tk.Frame(frow, bg=fbg, padx=4, pady=1)
                fframe.pack(side=tk.LEFT, padx=2)
                tk.Label(fframe, text=fname, font=(UI_FONT, 7), bg=fbg, fg=TEXT).pack(side=tk.LEFT)
                tk.Label(fframe, text=f" {runtime_short}", font=(UI_FONT, 7, "bold"),
                         bg=fbg, fg=rt_col).pack(side=tk.LEFT)
                if i < len(filters) - 1:
                    tk.Label(frow, text="→", font=(UI_FONT, 8), bg=SURFACE, fg=MUTED).pack(side=tk.LEFT)
            # Export target info
            exp = pcfg.get("export", {})
            bp_n = exp.get("bpName", "?").strip("'\"")
            rp_n = exp.get("rpName", "?").strip("'\"")
            tgt  = exp.get("target", "?")
            tk.Label(frame,
                     text=f"Export → {tgt}:  BP={bp_n}  |  RP={rp_n}",
                     font=(UI_FONT, 8), bg=SURFACE, fg=SUB).pack(anchor="w", pady=(4, 0))

        pipeline_frame = tk.Frame(card, bg=SURFACE)
        pipeline_frame.pack(fill=tk.X)
        _draw_pipeline(saved_profile, pipeline_frame)
        prof_var.trace_add("write", lambda *_: _draw_pipeline(prof_var.get(), pipeline_frame))

        # ── Double-click → project config ──
        self._bind_dblclick(outer, lambda e, p=proj: self._open_proj_cfg(p))
        return row_wrap

    def _regolith_build(self, proj: dict, profile: str):
        exe = _find_regolith_exe()
        if not exe:
            messagebox.showerror("Regolith not found",
                "Could not find regolith executable.\n\n"
                "Install from: https://bedrock-oss.github.io/regolith/\n"
                "Or add it to your PATH.")
            return
        dlg = RegolithBuildDialog(self, proj, profile, exe)
        self.after(0, lambda: dlg.start_build())

    def _make_sync_card(self, proj: dict) -> tk.Frame:
        pairs  = self._get_sync_pairs(proj)
        branch = git_branch(proj["path"])
        stored = self.cfg.get("branches", {}).get(proj["name"])
        changed = bool(stored and branch and stored != branch)
        synced  = any(dst.exists() for _, _, dst in pairs)
        is_rg   = proj.get("is_regolith", False)

        # ── Row wrapper (checkbox lives outside the card, to its left) ──
        row_wrap = tk.Frame(self._cards, bg=BG)

        chk_var = tk.BooleanVar(value=False)
        self._card_checks[proj["name"]] = chk_var
        chk_holder = tk.Frame(row_wrap, bg=BG, width=36, cursor="hand2")
        chk_holder.pack_propagate(False)
        tk.Checkbutton(chk_holder, variable=chk_var, bg=BG, fg=TEXT,
                       selectcolor=SURFACE, activebackground=BG,
                       relief=tk.FLAT, cursor="hand2").pack(anchor="center", expand=True)
        chk_holder.bind("<Button-1>", lambda e, v=chk_var: v.set(not v.get()))
        if self._selection_mode:
            chk_holder.pack(side=tk.LEFT, fill=tk.Y)

        # ── Card shell ──
        border  = PEACH if is_rg else SURF2
        outer   = tk.Frame(row_wrap, bg=BG2,
                           highlightbackground=border, highlightthickness=1)
        card    = tk.Frame(outer, bg=SURFACE, padx=16, pady=12)
        card.pack(fill=tk.X)
        outer.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._card_sel_widgets[proj["name"]] = (chk_holder, outer)

        # ── Title row ──
        top = tk.Frame(card, bg=SURFACE); top.pack(fill=tk.X)

        btns = tk.Frame(top, bg=SURFACE); btns.pack(side=tk.RIGHT)
        self._mk_btn(btns, t("btn_sync"),
                     lambda p=proj: threading.Thread(
                         target=self._sync_one, args=(p,), daemon=True).start(),
                     GREEN, BG, padx=10, pady=3).pack(side=tk.LEFT, padx=3)
        self._mk_btn(btns, t("btn_remove"),
                     lambda p=proj: self._remove_project(p),
                     RED, BG, padx=10, pady=3).pack(side=tk.LEFT, padx=3)

        dot_color = GREEN if synced else MUTED
        tk.Label(top, text="●" if synced else "○", font=(UI_FONT, 10),
                 bg=SURFACE, fg=dot_color).pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(top, text=proj["name"], font=("Segoe UI", 11, "bold"),
                 bg=SURFACE, fg=TEXT).pack(side=tk.LEFT)

        if branch or changed:
            sub = tk.Frame(card, bg=SURFACE); sub.pack(fill=tk.X, pady=(2, 0))
            if branch:
                tk.Label(sub, text=f"⎇  {branch}", font=(UI_FONT, 8),
                         bg=SURFACE, fg=YELLOW if changed else SUB).pack(side=tk.LEFT)
            if changed:
                tk.Label(sub, text=t("lbl_branch_changed"), font=(UI_FONT, 7),
                         bg=YELLOW, fg=BG, padx=3).pack(side=tk.LEFT, padx=(6, 0))

        # ── Divider ──
        tk.Frame(card, bg=SURF2, height=1).pack(fill=tk.X, pady=(8, 6))

        # ── Pair rows ──
        for label, src, dst in pairs:
            row = tk.Frame(card, bg=SURFACE, pady=3); row.pack(fill=tk.X)
            badge_bg = BLUE if label == "RP" else PEACH
            tk.Label(row, text=f" {label} ", bg=badge_bg, fg=BG,
                     font=("Segoe UI", 8, "bold")).pack(side=tk.LEFT, padx=(0, 10))
            try:   rel_src = src.relative_to(proj["path"])
            except ValueError: rel_src = src
            tk.Label(row, text=f"📂  {rel_src}   →   {dst.name}",
                     bg=SURFACE, fg=SUB, font=("Segoe UI", 9),
                     anchor="w", justify="left").pack(side=tk.LEFT, fill=tk.X)
            state = t("lbl_synced") if dst.exists() else t("lbl_not_synced")
            tk.Label(row, text=state, bg=SURFACE,
                     fg=GREEN if dst.exists() else MUTED,
                     font=(UI_FONT, 8)).pack(side=tk.RIGHT)
            bar_key   = f"{proj['name']}:{label}"
            bar_frame = tk.Frame(card, bg=SURFACE); bar_frame.pack(fill=tk.X, pady=(0, 2))
            bar_color = BLUE if label == "RP" else PEACH
            style_key = f"Pack{bar_key.replace(':', '_').replace('-', '_')}.Horizontal.TProgressbar"
            ttk.Style().configure(style_key, troughcolor=SURFACE,
                                  background=bar_color, thickness=3)
            pb = ttk.Progressbar(bar_frame, orient="horizontal", mode="determinate",
                                 style=style_key, length=0)
            pb.pack(fill=tk.X); pb.pack_forget()
            self._pack_bars[bar_key] = pb

        # ── Double-click → project config ──
        self._bind_dblclick(outer, lambda e, p=proj: self._open_proj_cfg(p))
        return row_wrap

    # ── Branch change ─────────────────────────────────────────────────────────
    def _on_branch_change(self, proj: dict, new: str | None):
        if not new: return
        stored = self.cfg.get("branches", {}).get(proj["name"])
        if not stored or stored == new: return
        self.after(0, lambda: self._handle_branch_dlg(proj, stored, new))

    def _on_pull_detected(self, proj: dict):
        """A new commit landed on the current branch (git pull/merge/commit).
        Pack watchers were paused the moment .git/ activity began. Now that
        git has settled, run a full silent sync so Minecraft never sees the
        partial mid-pull state, then resume the watchers."""
        def _run():
            try:
                # Post-pull full sync = a "build/export", so it bumps the version
                # (same as the manual Sync button — see _sync_one).
                self._sync_one(proj, silent=True)
            finally:
                self._watchers.resume_packs(proj["name"])
        threading.Thread(target=_run, daemon=True).start()

    # ── Self-update (download prebuilt release) ───────────────────────────────
    def _maybe_check_update(self, manual=False):
        """Kick off a background update check if enabled (or if manual). Safe to
        call from the UI thread; all network/IO happens off-thread."""
        if not manual and not self.cfg.get("auto_update", False):
            return
        if _running_exe_path() is None:
            if manual:
                self.after(0, lambda: messagebox.showinfo(
                    "Update",
                    "Self-update applies to the packaged Pack Sync build. "
                    "You're running from source — pull with git instead."))
            return
        if getattr(self, "_update_running", False):
            return
        self._update_running = True
        threading.Thread(target=self._do_update, args=(manual,),
                         daemon=True).start()

    def _do_update(self, manual: bool):
        try:
            self.after(0, lambda: self._status("Checking for updates…"))
            available, msg, info = check_for_app_update()
            if not available:
                if manual:
                    self.after(0, lambda m=msg: messagebox.showinfo(
                        "Update", f"Pack Sync: {m}."))
                self.after(0, lambda m=msg: self._status(f"Update: {m}"))
                return
            # If automatic, only prompt-then-install when the user opted in;
            # auto_update means "install automatically", so go ahead silently.
            ok, result = apply_app_update(
                info, log=lambda m: self.after(0, lambda mm=m: self._status(mm)))
            self.after(0, lambda r=result, k=ok: self._status(
                ("✓ " if k else "✕ ") + r))
            if ok:
                # On Windows the new exe is now in place — relaunch it and quit
                # so the user runs the updated build without doing anything.
                if IS_WIN and _running_exe_path() is not None:
                    self.after(0, lambda v=info["version"]: self._restart_into_update(v))
                else:
                    self.after(0, lambda r=result: messagebox.showinfo(
                        "Pack Sync updated", r))
            elif manual:
                self.after(0, lambda r=result: messagebox.showwarning(
                    "Update", r))
        finally:
            self._update_running = False

    def _restart_into_update(self, version: str):
        """Relaunch the freshly-swapped exe and exit this (old) process."""
        try:
            messagebox.showinfo(
                "Pack Sync updated",
                f"Updated to v{version}. Pack Sync will restart now.")
            exe = _running_exe_path()
            # Start the new exe minimized so it lands back in the tray, detached
            # from this process so our exit doesn't kill it.
            subprocess.Popen([str(exe), "--minimized"],
                             creationflags=(0x00000008 if IS_WIN else 0),  # DETACHED_PROCESS
                             close_fds=True)
        except Exception as e:
            messagebox.showwarning(
                "Update", f"Updated, but couldn't auto-restart: {e}\n"
                          "Please reopen Pack Sync manually.")
        finally:
            # Tear down tray + window and exit so the .old file can be cleaned
            # up by the new process on its next launch.
            try: self._quit_app()
            except Exception:
                os._exit(0)

    def _handle_branch_dlg(self, proj: dict, old: str, new: str):
        if self.cfg.get("auto_sync"):
            threading.Thread(target=self._sync_one, args=(proj, True),
                             daemon=True).start()
            return
        dlg = BranchWarningDialog(self, proj["name"], old, new)
        self.wait_window(dlg)
        if dlg.result:
            threading.Thread(target=self._sync_one, args=(proj, True),
                             daemon=True).start()

    # ── Guards ────────────────────────────────────────────────────────────────
    def _check_first_warning(self) -> bool:
        if self.cfg.get("skip_first_sync_warning"): return True
        dlg = FirstSyncWarningDialog(self); self.wait_window(dlg)
        if dlg.proceed and dlg.skip_next:
            self.cfg["skip_first_sync_warning"] = True; save_cfg(self.cfg)
        return dlg.proceed

    def _check_branch(self, proj: dict) -> bool:
        b = git_branch(proj["path"])
        if not b: return True
        stored = self.cfg.get("branches", {}).get(proj["name"])
        if stored and stored != b:
            dlg = BranchWarningDialog(self, proj["name"], stored, b)
            self.wait_window(dlg); return dlg.result
        return True

    def _record_branch(self, proj: dict):
        b = git_branch(proj["path"])
        if b:
            self.cfg.setdefault("branches", {})[proj["name"]] = b
            save_cfg(self.cfg)

    # ── Sync ──────────────────────────────────────────────────────────────────
    def _sync_one(self, proj: dict, silent=False):
        if not silent:
            if not self._run_on_main(self._check_first_warning): return
            if not self._run_on_main(self._check_branch, proj): return
        pairs      = self._get_sync_pairs(proj)
        names, errs = [], []

        for label, src, dst in pairs:
            bar_key = f"{proj['name']}:{label}"
            toast   = self._run_on_main(self._toast_mgr.show,
                                        f"{proj['name']}  ·  {label}")
            try:
                def _prog(done, total, _sp=self, _bk=bar_key, _t=toast):
                    pct = int(done / total * 100) if total else 0
                    def _ui(d=done, tt=total, bk=_bk, p=pct, t=_t):
                        _sp._set_progress(d, tt)
                        pb = _sp._pack_bars.get(bk)
                        if pb and _sp.state() == ("normal",):
                            pb["value"] = p
                            if not pb.winfo_ismapped(): pb.pack(fill=tk.X)
                        t.set_progress(d, tt)
                    _sp.after(0, _ui)
                # Wipe-and-reupload: the destination is fully replaced with the
                # repo's current contents (no merge, no stale files).
                mirror_clean(src, dst, self._status, progress_cb=_prog)

                for flt in self.cfg.get("regolith_filters", []):
                    if flt.get("enabled", True) and flt.get("cmd", "").strip():
                        self._status(f"Filter: {flt.get('name', flt['cmd'])}…")
                        try:
                            subprocess.run(flt["cmd"], shell=True, cwd=str(dst),
                                           check=True, timeout=120,
                                           creationflags=_NO_WIN)
                        except Exception as fe:
                            errs.append(f"Filter '{flt.get('name','?')}': {fe}")
                names.append(label)
            except Exception as e:
                errs.append(f"{label}: {e}")
            self.after(0, toast.set_done)
            def _hide_bar(bk=bar_key):
                pb = self._pack_bars.get(bk)
                if pb:
                    pb["value"] = 0; pb.pack_forget()
            self.after(300, _hide_bar)
        self._record_branch(proj)
        self._watchers.restart(proj, pairs)
        self.after(0, self._reset_progress)
        if not silent:
            if errs:
                self.after(0, lambda: messagebox.showerror("Sync error", "\n".join(errs)))
            else:
                self._status(t("status_synced", ", ".join(names)))
            self.after(0, self._refresh)

    def _do_sync_all(self):
        if not self._run_on_main(self._check_first_warning): return
        github, _ = self._get_paths()
        projects  = discover_projects(github)
        self._status(t("status_syncing"))
        for p in projects:
            if not self._run_on_main(self._check_branch, p): continue
            self._sync_one(p, silent=True)
        self._status(t("status_synced_all", len(projects)))
        self.after(0, self._refresh)

    def _remove_project(self, proj: dict):
        pairs = self._get_sync_pairs(proj)
        names = [lbl for lbl, _, _ in pairs]
        if not messagebox.askyesno(t("remove_title"),
                t("remove_body", " / ".join(names))):
            return
        self._watchers.stop(proj["name"])
        removed = []
        for _, _, dst in pairs:
            if not dst.exists(): continue
            try:
                shutil.rmtree(dst); removed.append(dst.name)
            except Exception:
                try: force_remove(dst); removed.append(dst.name)
                except Exception as e:
                    messagebox.showerror("Removal failed",
                        f"Could not remove {dst.name}:\n{e}")
        self._status(t("status_removed", ", ".join(removed) or "—"))
        self._refresh()

    def _open_settings(self):
        dlg = SettingsDialog(self, self.cfg); self.wait_window(dlg)
        if dlg.result:
            self.cfg.update(dlg.result); save_cfg(self.cfg); self._refresh()

    def _toggle_auto(self):
        on = not self.cfg.get("auto_sync", False)
        self.cfg["auto_sync"] = on
        save_cfg(self.cfg)
        self._auto_btn.configure(bg=GREEN if on else SURF2,
                                 fg=BG   if on else TEXT)

    # ── Per-project config ────────────────────────────────────────────────────
    def _get_sync_pairs(self, proj: dict) -> list:
        """Returns [(label, src_path, dst_path)] using custom config or auto-discovery."""
        proj_cfg = self.cfg.get("proj_cfg", {}).get(proj["name"], {})
        custom   = proj_cfg.get("pairs", [])
        if custom:
            return [(p["label"], Path(p["src"]), Path(p["dst"]))
                    for p in custom if p.get("src") and p.get("dst")]
        _, mojang = self._get_paths()
        return [(pt, src, dest_path(mojang, proj, pt))
                for pt, src in proj["packs"].items()]

    def _open_proj_cfg(self, proj: dict):
        proj_cfg     = self.cfg.get("proj_cfg", {}).get(proj["name"], {})
        default_pairs = self._get_sync_pairs(proj)
        dlg = ProjectConfigDialog(self, proj, proj_cfg, default_pairs)
        self.wait_window(dlg)
        if dlg.result is not None:
            self.cfg.setdefault("proj_cfg", {})[proj["name"]] = dlg.result
            save_cfg(self.cfg)
            self._refresh()

    _SKIP_BIND = (tk.Button, tk.Menubutton, tk.Entry, tk.Text)

    def _bind_dblclick(self, widget, handler):
        if not isinstance(widget, self._SKIP_BIND):
            widget.bind("<Double-Button-1>", handler, add="+")
        for child in widget.winfo_children():
            self._bind_dblclick(child, handler)

    # ── Watcher flush toast (called from background thread) ───────────────────
    def _on_watcher_flush(self, proj_name: str, pack_type: str, stats: dict):
        # NOTE: live edits do NOT bump the version on purpose — saving a texture
        # locally shouldn't churn the version. The version is only bumped when a
        # new commit is pulled from GitHub (see _on_pull_detected).
        overwritten = stats.get("overwritten", set())
        new_files   = stats.get("new", set())
        deleted     = stats.get("deleted", set())
        parts = []
        if overwritten:
            parts.append(f"{len(overwritten)} overwritten")
        if new_files:
            n = len(new_files)
            parts.append(next(iter(new_files)).name if n == 1 else f"{n} new")
        if deleted:
            parts.append(f"{len(deleted)} deleted")
        if not parts:
            return
        title = f"{proj_name}  ·  {pack_type}  ·  {'  ·  '.join(parts)}"
        self.after(0, lambda: self._flash_watcher_toast(title))

    def _flash_watcher_toast(self, title: str):
        toast = self._toast_mgr.show(title)
        self.after(60, toast.set_done)

    # ── Selection mode (hide repos) ───────────────────────────────────────────
    def _toggle_select_mode(self):
        self._selection_mode = not self._selection_mode
        if self._selection_mode:
            self._sel_mode_btn.config(text="✕ Cancel", bg=YELLOW, fg=BG)
            self._hide_sel_btn.pack(side=tk.LEFT, padx=3,
                                    before=self._sel_mode_btn)
        else:
            self._sel_mode_btn.config(text="Hide Repos", bg=SURF2, fg=TEXT)
            self._hide_sel_btn.pack_forget()
            for v in self._card_checks.values():
                v.set(False)
        for chk_holder, outer in self._card_sel_widgets.values():
            if self._selection_mode:
                chk_holder.pack(side=tk.LEFT, fill=tk.Y, before=outer)
            else:
                chk_holder.pack_forget()

    def _hide_selected(self):
        to_hide = [name for name, v in self._card_checks.items() if v.get()]
        if not to_hide:
            return
        hidden = set(self.cfg.get("hidden_repos", []))
        hidden.update(to_hide)
        self.cfg["hidden_repos"] = sorted(hidden)
        save_cfg(self.cfg)
        # Instant hide — just unpack the pre-built frames, no rediscovery
        lo = {n.lower() for n in to_hide}
        for name in to_hide:
            frame = self._all_card_frames.get(name)
            if frame:
                frame.pack_forget()
            self._watchers.stop(name)
        self._card_frames = [(f, n) for f, n in self._card_frames if n not in lo]
        self._selection_mode = False
        self._sel_mode_btn.config(text="Hide Repos", bg=SURF2, fg=TEXT)
        self._hide_sel_btn.pack_forget()
        for v in self._card_checks.values():
            v.set(False)
        for chk_holder, outer in self._card_sel_widgets.values():
            chk_holder.pack_forget()

    def _show_hidden_repos(self):
        HiddenReposDialog(self)

    # ── Thread helper ─────────────────────────────────────────────────────────
    def _run_on_main(self, fn, *args):
        result = [None]; done = threading.Event()
        def run(): result[0] = fn(*args); done.set()
        self.after(0, run); done.wait(); return result[0]


# ─── Regolith build dialog ────────────────────────────────────────────────────
class RegolithBuildDialog(tk.Toplevel):
    """Streaming terminal dialog for running `regolith run <profile>`."""

    ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')

    def __init__(self, parent, proj: dict, profile: str, exe: str):
        super().__init__(parent)
        self.title(f"Building: {proj['name']}  [{profile}]")
        self.configure(bg=BG)
        self.resizable(True, True)
        _center_on(self, parent, 720, 540)
        self._proj    = proj
        self._profile = profile
        self._exe     = exe
        self._proc: subprocess.Popen | None = None
        self._build()

    def _build(self):
        # ── Header ──
        hdr = tk.Frame(self, bg=BG2, pady=8); hdr.pack(fill=tk.X)
        ic = tk.Canvas(hdr, width=20, height=20, bg=BG2, highlightthickness=0)
        ic.pack(side=tk.LEFT, padx=(12, 4))
        ic.create_arc(2, 2, 18, 18, start=30, extent=150, style="arc", outline=PEACH, width=2)
        ic.create_arc(2, 2, 18, 18, start=210, extent=150, style="arc", outline=PEACH, width=2)
        tk.Label(hdr, text=f"⚙ Regolith  ·  {self._proj['name']}  ·  profile: {self._profile}",
                 font=(UI_FONT, 11, "bold"), bg=BG2, fg=TEXT).pack(side=tk.LEFT)

        # ── Progress bar ──
        style = ttk.Style()
        style.configure("Rg.Horizontal.TProgressbar",
                        troughcolor=BG2, background=PEACH, thickness=4)
        self._prog = ttk.Progressbar(self, orient="horizontal", mode="indeterminate",
                                     style="Rg.Horizontal.TProgressbar")
        self._prog.pack(fill=tk.X, padx=0, pady=0)

        # ── Terminal text area ──
        term_frame = tk.Frame(self, bg=BG); term_frame.pack(fill=tk.BOTH, expand=True)
        self._txt = tk.Text(term_frame, bg="#0D0D0D", fg="#D4D4D4",
                            font=("Consolas", 9), wrap=tk.WORD,
                            relief=tk.FLAT, padx=8, pady=6,
                            state=tk.DISABLED, cursor="arrow")
        vsb = ttk.Scrollbar(term_frame, orient="vertical", command=self._txt.yview)
        self._txt.configure(yscrollcommand=vsb.set)
        self._txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        # Colour tags
        self._txt.tag_configure("err",  foreground="#F44747")
        self._txt.tag_configure("warn", foreground="#FFCC02")
        self._txt.tag_configure("ok",   foreground="#4EC994")
        self._txt.tag_configure("muted",foreground="#808080")
        self._txt.tag_configure("step", foreground=PEACH)

        # ── Footer ──
        foot = tk.Frame(self, bg=BG2, pady=6); foot.pack(fill=tk.X)
        self._status_lbl = tk.Label(foot, text="Starting…", bg=BG2, fg=SUB,
                                    font=(UI_FONT, 9))
        self._status_lbl.pack(side=tk.LEFT, padx=12)
        self._close_btn = tk.Button(foot, text="Close", command=self._close,
                                    bg=SURF2, fg=TEXT, relief=tk.FLAT, padx=16, pady=4,
                                    font=(UI_FONT, 9), cursor="hand2", state=tk.DISABLED)
        self._close_btn.pack(side=tk.RIGHT, padx=12)
        tk.Button(foot, text="✕  Kill", command=self._kill,
                  bg=RED, fg=BG, relief=tk.FLAT, padx=12, pady=4,
                  font=(UI_FONT, 9), cursor="hand2").pack(side=tk.RIGHT, padx=4)

    def start_build(self):
        self._prog.start(12)
        self._append(f"▶  regolith run {self._profile}\n", "muted")
        self._append(f"   Working dir: {self._proj['path']}\n\n", "muted")
        cmd = [self._exe, "run", self._profile]
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=str(self._proj["path"]),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1)
        except Exception as e:
            self._append(f"ERROR: Could not start regolith:\n{e}\n", "err")
            self._done(success=False); return
        threading.Thread(target=self._read_output, daemon=True).start()

    def _read_output(self):
        assert self._proc
        for line in self._proc.stdout:
            clean = self.ANSI_RE.sub("", line)
            tag   = ("err"  if any(w in clean.lower() for w in ("error", "failed", "fatal"))
                     else "warn" if any(w in clean.lower() for w in ("warning", "warn"))
                     else "ok"   if any(w in clean.lower() for w in ("success", "done", "complete", "exported"))
                     else "step" if clean.strip().startswith(("[", "Running", "Exporting", "Filter"))
                     else "")
            self.after(0, self._append, clean, tag)
        self._proc.wait()
        success = self._proc.returncode == 0
        self.after(0, self._done, success)

    def _append(self, text: str, tag: str = ""):
        self._txt.configure(state=tk.NORMAL)
        self._txt.insert(tk.END, text, tag)
        self._txt.see(tk.END)
        self._txt.configure(state=tk.DISABLED)

    def _done(self, success: bool):
        self._prog.stop(); self._prog["value"] = 100 if success else 0
        if success:
            self._append("\n✓  Build finished successfully.\n", "ok")
            self._status_lbl.config(text="✓  Done", fg=GREEN)
        else:
            self._append("\n✗  Build failed — see output above.\n", "err")
            self._status_lbl.config(text="✗  Failed", fg=RED)
        self._close_btn.config(state=tk.NORMAL)

    def _kill(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            self._append("\n[Killed by user]\n", "warn")

    def _close(self):
        self._kill(); self.destroy()


# ─── Onboarding wizard ────────────────────────────────────────────────────────
class OnboardingWizard(tk.Toplevel):
    """3-page first-launch wizard: OS tray hint → how-it-works → setup form."""

    def __init__(self, parent, start_page: int = 0, intro_only: bool = False):
        super().__init__(parent)
        self.title(t("setup_title"))
        self.configure(bg=BG)
        self.grab_set(); self.resizable(False, False)
        self.result: dict | None = None
        self._intro_only = intro_only
        self._ptype = tk.StringVar(value="minecraft")
        self._gv    = tk.StringVar(value=str(DEFAULT_GITHUB))
        self._dv    = tk.StringVar(value=str(DEFAULT_MOJANG))
        self._frame = tk.Frame(self, bg=BG)
        self._frame.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)
        self._show(start_page)
        # Centre on parent after layout is resolved
        self.update_idletasks()
        w, h = 560, 520
        px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
        py = parent.winfo_y() + (parent.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{max(0,px)}+{max(0,py)}")

    # ── Navigation ────────────────────────────────────────────────────────────
    def _show(self, idx: int):
        for w in self._frame.winfo_children(): w.destroy()
        pages = [self._pg_tray, self._pg_how, self._pg_projects,
                 self._pg_sync_action, self._pg_regolith_slide, self._pg_tips, self._pg_setup]
        pages[idx]()

    def _nav(self, parent, back_idx, next_idx, next_label="Next  →", cur_page=0):
        tk.Frame(parent, bg=BG).pack(fill=tk.BOTH, expand=True)  # spacer — pushes buttons to bottom
        row = tk.Frame(parent, bg=BG); row.pack(pady=8)
        if back_idx is not None:
            tk.Button(row, text="←  Back", command=lambda: self._show(back_idx),
                      bg=SURF2, fg=TEXT, relief=tk.FLAT, padx=14, pady=5,
                      font=(UI_FONT, 9), cursor="hand2").pack(side=tk.LEFT, padx=6)
        intro_only = self._intro_only
        if intro_only and next_idx == 6:
            tk.Button(row, text="Got it  ✓", bg=GREEN, fg=BG, relief=tk.FLAT,
                      padx=20, pady=6, font=(UI_FONT, 10, "bold"), cursor="hand2",
                      command=self.destroy).pack(side=tk.LEFT, padx=6)
        else:
            tk.Button(row, text=next_label, bg=BLUE, fg=BG, relief=tk.FLAT,
                      padx=20, pady=6, font=(UI_FONT, 10, "bold"), cursor="hand2",
                      command=(self._confirm if next_idx is None
                               else lambda i=next_idx: self._show(i))).pack(side=tk.LEFT, padx=6)
        # Page indicator  e.g. "2 / 6"
        total = 6 if intro_only else 7
        prog_lbl = tk.Label(row, text=f"{cur_page+1} / {total}",
                            font=(UI_FONT, 8), bg=BG, fg=MUTED)
        prog_lbl.pack(side=tk.LEFT, padx=10)
        # Dot strip
        dots = tk.Frame(parent, bg=BG); dots.pack(pady=(0, 4))
        shown = 6 if intro_only else 7
        for i in range(shown):
            col = BLUE if i == cur_page else SURF2
            tk.Label(dots, text="●", font=(UI_FONT, 6), bg=BG, fg=col).pack(side=tk.LEFT, padx=1)

    # ── Page 0 — OS tray hint ─────────────────────────────────────────────────
    def _pg_tray(self):
        f = self._frame
        tk.Label(f, text=t("pg_tray_title"), font=(UI_FONT, 14, "bold"),
                 bg=BG, fg=YELLOW).pack(pady=(16, 2))
        if IS_WIN:
            tk.Label(f, text=t("pg_tray_win"),
                     font=(UI_FONT, 10), bg=BG, fg=TEXT, justify="center").pack(pady=(0, 6))
            self._draw_win_tray(f)
        elif IS_MAC:
            tk.Label(f, text=t("pg_tray_mac"),
                     font=(UI_FONT, 10), bg=BG, fg=TEXT, justify="center").pack(pady=(0, 6))
            self._draw_mac_bar(f)
        else:
            tk.Label(f, text=t("pg_tray_lin"),
                     font=(UI_FONT, 10), bg=BG, fg=TEXT, justify="center").pack(pady=(0, 6))
            self._draw_linux_panel(f)

        # Tips grid
        tips_frame = tk.Frame(f, bg=BG2, padx=14, pady=10)
        tips_frame.pack(fill=tk.X, padx=24, pady=(8, 4))
        tips = [
            ("⚡", t("pg_tray_tip1")),
            ("↕",  t("pg_tray_tip2")),
            ("🔀", t("pg_tray_tip3")),
            ("⚙",  t("pg_tray_tip4")),
            ("?",  t("pg_tray_tip5")),
        ]
        for icon, tip in tips:
            row = tk.Frame(tips_frame, bg=BG2); row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=icon, font=(UI_FONT, 11), bg=BG2, fg=YELLOW,
                     width=2).pack(side=tk.LEFT)
            tk.Label(row, text=tip, font=(UI_FONT, 9), bg=BG2, fg=TEXT,
                     justify="left", anchor="w").pack(side=tk.LEFT, padx=6)
        self._nav(f, None, 1, "How it works  →", cur_page=0)

    def _canvas(self, parent, w=500, h=90):
        c = tk.Canvas(parent, width=w, height=h, bg=BG, highlightthickness=0)
        c.pack(pady=4); return c

    def _draw_win_tray(self, parent):
        c = self._canvas(parent, w=500, h=120)
        # Desktop background hint
        c.create_rectangle(0, 0, 500, 72, fill="#0d1b2e", outline="")
        c.create_text(250, 36, text="[ your desktop ]", fill="#1a3050",
                      font=("Segoe UI", 10, "bold"))

        # ── Taskbar (Win11: 48 px, dark, sits at bottom) ──
        tb_y = 72
        c.create_rectangle(0, tb_y, 500, tb_y+48, fill="#202020", outline="")

        # Win11 Start button: 4-pane logo, centered on left
        sx, cy = 30, tb_y+24
        g = 1  # gap between panes
        c.create_rectangle(sx-10, cy-10, sx-g,  cy-g,  fill="#F35325", outline="")
        c.create_rectangle(sx+g,  cy-10, sx+10, cy-g,  fill="#81BC06", outline="")
        c.create_rectangle(sx-10, cy+g,  sx-g,  cy+10, fill="#05A6F0", outline="")
        c.create_rectangle(sx+g,  cy+g,  sx+10, cy+10, fill="#FFBA08", outline="")

        # Win11 Search pill (rounded rectangle)
        sp_x1, sp_x2 = 52, 168
        sp_y1, sp_y2 = tb_y+10, tb_y+38
        r = 6
        c.create_rectangle(sp_x1+r, sp_y1, sp_x2-r, sp_y2, fill="#2d2d2d", outline="")
        c.create_rectangle(sp_x1, sp_y1+r, sp_x2, sp_y2-r, fill="#2d2d2d", outline="")
        for (ox, oy) in [(sp_x1, sp_y1), (sp_x2-2*r, sp_y1),
                          (sp_x1, sp_y2-2*r), (sp_x2-2*r, sp_y2-2*r)]:
            c.create_oval(ox, oy, ox+2*r, oy+2*r, fill="#2d2d2d", outline="")
        c.create_oval(sp_x1, sp_y1, sp_x1+2*r, sp_y1+2*r, fill="#3d3d3d", outline="")
        c.create_text((sp_x1+sp_x2)//2, (sp_y1+sp_y2)//2,
                      text="🔍 Search", fill="#888", font=("Segoe UI", 8))

        # Pinned app icons (Win11 centered layout — just left of center here)
        for i, (bg_col, label) in enumerate([
                ("#E6A800", "📁"), ("#24292E", "GH"), ("#0066B8", "</>")]):
            ax = 190 + i*30
            c.create_rectangle(ax, tb_y+10, ax+22, tb_y+38, fill=bg_col, outline="")
            c.create_text(ax+11, tb_y+24, text=label, fill="white",
                          font=("Segoe UI", 7, "bold"))
            # Active indicator dot
            c.create_rectangle(ax+8, tb_y+42, ax+14, tb_y+45, fill="#888", outline="")

        # ── System tray (right side) ──
        # Overflow chevron "∧"
        c.create_text(308, tb_y+24, text="∧", fill="#aaa", font=("Segoe UI", 9))
        # Status icons
        for i, sym in enumerate(["Ψ", "◁", "▨"]):
            c.create_text(325 + i*16, tb_y+24, text=sym, fill="#bbb", font=("Segoe UI", 7))

        # ── Pack Sync tray icon (highlighted) ──
        px = 392
        c.create_oval(px-11, tb_y+13, px+11, tb_y+35,
                      fill="#1e1e2e", outline="#a6e3a1", width=2)
        c.create_arc(px-9, tb_y+15, px+9, tb_y+33,
                     start=30, extent=150, style="arc", outline="#89b4fa", width=2)
        c.create_arc(px-9, tb_y+15, px+9, tb_y+33,
                     start=210, extent=150, style="arc", outline="#a6e3a1", width=2)

        # Arrow + label pointing to the tray icon
        c.create_line(px, tb_y-2, px, tb_y+11,
                      fill="#a6e3a1", width=2, arrow=tk.LAST)
        c.create_text(px, tb_y-10, text="Pack Sync",
                      fill="#a6e3a1", font=("Segoe UI", 7, "bold"))

        # Clock block
        c.create_text(427, tb_y+18, text="12:34", fill="#ddd", font=("Consolas", 9))
        c.create_text(427, tb_y+32, text="17/5/26", fill="#999", font=("Consolas", 8))

        # Notification panel button
        c.create_rectangle(453, tb_y+10, 476, tb_y+38, fill="#2a2a2a", outline="")
        c.create_text(465, tb_y+24, text="🔔", fill="#888", font=("Segoe UI", 7))

        # Bottom caption
        c.create_text(250, 116,
                      text="system tray  ·  right-click the icon for Sync All / Quit",
                      fill="#555", font=("Segoe UI", 7))

    def _draw_mac_bar(self, parent):
        c = self._canvas(parent, h=100)
        # Menu bar background
        c.create_rectangle(0, 0, 500, 28, fill="#1E1E1E", outline="")
        # Apple logo (simplified)
        c.create_oval(8, 4, 20, 22, fill="#CCC", outline="")
        c.create_rectangle(12, 2, 16, 8, fill="#1E1E1E", outline="")   # stem cutout
        c.create_rectangle(14, 10, 20, 16, fill="#1E1E1E", outline="")  # bite
        # App menu items
        for i, item in enumerate(["Finder", "File", "Edit", "View", "Go", "Help"]):
            c.create_text(38 + i*52, 14, text=item, fill="#DDD", font=("Helvetica", 8))
        # Right side: Control Center, clock, Pack Sync
        c.create_text(360, 14, text="Fri 16 May", fill="#CCC", font=("Helvetica", 8))
        c.create_text(410, 14, text="12:34", fill="#DDD", font=("Helvetica Neue", 9, "bold"))
        # Control Center button
        c.create_rectangle(425, 4, 445, 22, fill="#333", outline="")
        c.create_text(435, 13, text="⊞", fill="#CCC", font=("Helvetica", 9))
        # Spotlight
        c.create_oval(448, 5, 463, 21, fill="#333", outline="")
        c.create_text(456, 13, text="⌘", fill="#AAA", font=("Helvetica", 9))
        # Pack Sync tray icon
        c.create_oval(468, 4, 492, 22, fill="#1e1e2e", outline="#a6e3a1", width=2)
        c.create_arc(470, 6, 490, 20, start=30, extent=150, style="arc", outline="#89b4fa", width=2)
        c.create_arc(470, 6, 490, 20, start=210, extent=150, style="arc", outline="#a6e3a1", width=2)
        # Arrow label
        c.create_line(480, 32, 480, 26, fill="#a6e3a1", width=2, arrow=tk.LAST)
        c.create_text(380, 42, text="Pack Sync icon in menu bar  ——→", fill="#a6e3a1",
                      font=("Helvetica Neue", 8, "bold"))
        # Dock mockup at bottom
        c.create_rectangle(60, 60, 440, 88, fill="#2A2A2A", outline="#444",
                           width=1)
        dock_apps = [
            ("#0066B8", "VS\nCode"), ("#24292E", "GH"), ("#FFB900", "📁"),
            ("#1DB954", "♫"), ("#FF3B30", "●"), ("#34C759", "●"),
        ]
        for i, (col, label) in enumerate(dock_apps):
            dx = 80 + i * 60
            c.create_rectangle(dx-18, 63, dx+18, 85, fill=col, outline="")
            c.create_text(dx, 74, text=label, fill="white", font=("Helvetica", 7, "bold"))
        c.create_text(250, 96, text="macOS menu bar  ·  click Pack Sync icon to show/hide",
                      fill="#666", font=("Helvetica", 7))

    def _draw_linux_panel(self, parent):
        c = self._canvas(parent, h=100)
        # Top panel background (GNOME style)
        c.create_rectangle(0, 0, 500, 28, fill="#2C2C2C", outline="")
        # Activities button
        c.create_rectangle(2, 2, 70, 26, fill="#3D3D3D", outline="")
        c.create_text(36, 14, text="Activities", fill="#EEE", font=("Ubuntu", 9))
        # App name in center (GNOME puts focused app name here)
        c.create_text(250, 14, text="Files", fill="#DDD", font=("Ubuntu", 9, "bold"))
        # Clock center
        c.create_text(250, 14, text="", fill="#DDD", font=("Ubuntu", 9))  # overwritten below
        # Right side: icons + clock
        c.create_text(340, 14, text="Fri 16 May  12:34", fill="#DDD", font=("Ubuntu", 9, "bold"))
        # System indicators
        for i, sym in enumerate(["▲", "◉", "♪", "🔋"], 1):
            c.create_text(390+i*16, 14, text=sym, fill="#CCC", font=("Ubuntu", 7))
        # Pack Sync tray icon
        c.create_oval(466, 4, 490, 24, fill="#1e1e2e", outline="#a6e3a1", width=2)
        c.create_arc(468, 6, 488, 22, start=30, extent=150, style="arc", outline="#89b4fa", width=2)
        c.create_arc(468, 6, 488, 22, start=210, extent=150, style="arc", outline="#a6e3a1", width=2)
        # Arrow label
        c.create_line(478, 33, 478, 28, fill="#a6e3a1", width=2, arrow=tk.LAST)
        c.create_text(370, 42, text="Pack Sync in system tray  ——→", fill="#a6e3a1",
                      font=("Ubuntu", 8, "bold"))
        # App grid / taskbar at bottom (simulated)
        c.create_rectangle(0, 56, 500, 84, fill="#333", outline="#444")
        linux_apps = [
            ("#E95420", "Ubuntu"), ("#4A90D9", "Files"), ("#24292E", "GitHub"),
            ("#0066B8", "VSCode"), ("#5C6BC0", "Term"),
        ]
        for i, (col, label) in enumerate(linux_apps):
            dx = 20 + i * 80
            c.create_rectangle(dx, 58, dx+60, 82, fill=col, outline="")
            c.create_text(dx+30, 70, text=label, fill="white", font=("Ubuntu", 7, "bold"))
        c.create_text(250, 96, text="GNOME / Linux system tray  ·  right-click Pack Sync icon for menu",
                      fill="#666", font=("Ubuntu", 7))

    # ── Page 1 — How it works (two modes) ────────────────────────────────────
    def _pg_how(self):
        f = self._frame
        tk.Label(f, text=t("pg_how_title"), font=(UI_FONT, 13, "bold"),
                 bg=BG, fg=TEXT).pack(pady=(16, 4))

        # ── Mode A: plain sync ──
        box_a = tk.Frame(f, bg=SURFACE, padx=14, pady=10,
                         highlightbackground=BLUE, highlightthickness=1)
        box_a.pack(fill=tk.X, padx=22, pady=(4, 3))
        hdr_a = tk.Frame(box_a, bg=SURFACE); hdr_a.pack(fill=tk.X)
        ic_a = tk.Canvas(hdr_a, width=16, height=16, bg=SURFACE, highlightthickness=0)
        ic_a.pack(side=tk.LEFT, padx=(0, 6))
        ic_a.create_arc(1,1,15,15, start=30, extent=150, style="arc", outline=BLUE, width=2)
        ic_a.create_arc(1,1,15,15, start=210, extent=150, style="arc", outline=BLUE, width=2)
        tk.Label(hdr_a, text=t("pg_how_a_hdr"), font=(UI_FONT, 10, "bold"),
                 bg=SURFACE, fg=BLUE).pack(side=tk.LEFT)
        tk.Label(box_a, text=t("pg_how_a_body"),
                 font=(UI_FONT, 9), bg=SURFACE, fg=SUB, justify="left").pack(anchor="w", pady=(4, 0))

        # ── Tiny flow diagram for mode A ──
        ca = tk.Canvas(box_a, width=420, height=48, bg=SURFACE, highlightthickness=0)
        ca.pack(pady=(6, 0))
        self._ico_github(ca, 36, 24, 34)
        ca.create_text(36, 44, text="GitHub", fill=MUTED, font=(UI_FONT, 7))
        ca.create_line(58, 24, 138, 24, fill=BLUE, width=2, arrow=tk.LAST)
        ca.create_text(98, 14, text="→ live", fill=BLUE, font=(UI_FONT, 7, "bold"))
        self._ico_minecraft(ca, 168, 24, 34)
        ca.create_text(168, 44, text="com.mojang", fill=MUTED, font=(UI_FONT, 7))
        # Green sync button label
        ca.create_rectangle(220, 10, 290, 38, fill=GREEN, outline="")
        ca.create_text(255, 24, text="Sync", fill=BG, font=(UI_FONT, 8, "bold"))

        # ── Mode B: Regolith ──
        box_b = tk.Frame(f, bg=SURFACE, padx=14, pady=10,
                         highlightbackground=PEACH, highlightthickness=1)
        box_b.pack(fill=tk.X, padx=22, pady=(3, 4))
        hdr_b = tk.Frame(box_b, bg=SURFACE); hdr_b.pack(fill=tk.X)
        ic_b = tk.Canvas(hdr_b, width=16, height=16, bg=SURFACE, highlightthickness=0)
        ic_b.pack(side=tk.LEFT, padx=(0, 6))
        ic_b.create_arc(1,1,15,15, start=30, extent=150, style="arc", outline=PEACH, width=2)
        ic_b.create_arc(1,1,15,15, start=210, extent=150, style="arc", outline=PEACH, width=2)
        tk.Label(hdr_b, text=t("pg_how_b_hdr"),
                 font=(UI_FONT, 10, "bold"), bg=SURFACE, fg=PEACH).pack(side=tk.LEFT)
        tk.Label(box_b, text=t("pg_how_b_body"),
                 font=(UI_FONT, 9), bg=SURFACE, fg=SUB, justify="left").pack(anchor="w", pady=(4, 0))

        # ── Tiny flow diagram for mode B ──
        cb = tk.Canvas(box_b, width=420, height=48, bg=SURFACE, highlightthickness=0)
        cb.pack(pady=(6, 0))
        self._ico_github(cb, 36, 24, 34)
        cb.create_text(36, 44, text="GitHub", fill=MUTED, font=(UI_FONT, 7))
        cb.create_line(58, 24, 118, 24, fill=PEACH, width=2, arrow=tk.LAST)
        cb.create_rectangle(122, 10, 218, 38, fill=SURFACE, outline=PEACH, width=1)
        cb.create_text(170, 20, text="Filters", fill=PEACH, font=(UI_FONT, 7, "bold"))
        cb.create_text(170, 32, text="TS · Py · JS", fill=MUTED, font=(UI_FONT, 7))
        cb.create_line(220, 24, 280, 24, fill=PEACH, width=2, arrow=tk.LAST)
        self._ico_minecraft(cb, 308, 24, 34)
        cb.create_text(308, 44, text="com.mojang", fill=MUTED, font=(UI_FONT, 7))
        # Peach build button label
        cb.create_rectangle(340, 10, 410, 38, fill=PEACH, outline="")
        cb.create_text(375, 24, text="▶  Build", fill=BG, font=(UI_FONT, 8, "bold"))

        self._nav(f, 0, 2, "Next  →", cur_page=1)

    def _ico_github(self, c, cx, cy, size):
        r = size // 2
        c.create_oval(cx-r, cy-r, cx+r, cy+r, fill="#24292E", outline="#6e7681", width=1)
        c.create_text(cx, cy-4, text="GH", fill="white", font=(UI_FONT, 11, "bold"))
        # Tentacle lines
        for dx in (-10, -4, 4, 10):
            c.create_line(cx+dx, cy+r, cx+dx, cy+r+6, fill="#6e7681", width=1)

    def _ico_minecraft(self, c, cx, cy, size):
        r = size // 2
        # Dirt
        c.create_rectangle(cx-r, cy, cx+r, cy+r, fill="#7B5E3A", outline="#6B4E2A")
        # Grass top
        c.create_rectangle(cx-r, cy-r, cx+r, cy, fill="#5B8C3A", outline="#4A7A2A")
        # Grass edge strip
        c.create_rectangle(cx-r, cy-5, cx+r, cy+1, fill="#6BA040", outline="")
        # Pixel highlights (pixelated feel)
        ps = max(3, size // 8)
        for px, py in [(-r//2, -r+2), (0, -r+3), (r//2-ps, -r+2)]:
            c.create_rectangle(cx+px, cy+py, cx+px+ps, cy+py+ps, fill="#8BC34A", outline="")

    # ── Page 2 — Adding projects ──────────────────────────────────────────────
    def _pg_projects(self):
        f = self._frame
        tk.Label(f, text=t("pg_proj_title"), font=(UI_FONT, 13, "bold"),
                 bg=BG, fg=TEXT).pack(pady=(14, 4))
        tk.Label(f, text=t("pg_proj_sub"),
                 font=(UI_FONT, 9), bg=BG, fg=SUB).pack(pady=(0, 8))

        # Canvas showing folder scan
        c = self._canvas(f, h=110)
        # GitHub folder
        c.create_rectangle(20, 20, 90, 65, fill="#FFB900", outline="#E6A800", width=1)
        c.create_rectangle(20, 35, 90, 65, fill="#E6A800", outline="")
        c.create_polygon(20, 35, 30, 35, 33, 28, 90, 28, 90, 35, fill="#FFD54F", outline="")
        c.create_text(55, 50, text="GitHub/", fill="white", font=(UI_FONT, 8, "bold"))
        # Sub-folders (projects)
        for i, (name, col, badge) in enumerate([
            ("Project-A", "#a6e3a1", "⚙"),
            ("My-Addon",  "#89b4fa", "↕"),
            ("Tools",     "#cba6f7", "↕"),
        ]):
            px, py = 110, 15 + i * 30
            c.create_rectangle(px, py, px+120, py+22, fill=SURFACE, outline=col, width=1)
            c.create_text(px+8, py+11, text=badge, fill=col, font=(UI_FONT, 8),
                          anchor="w")
            c.create_text(px+22, py+11, text=name, fill=TEXT, font=(UI_FONT, 8),
                          anchor="w")
        c.create_line(90, 42, 110, 42, fill=YELLOW, width=1, arrow=tk.LAST)
        # Pack Sync window mockup
        c.create_rectangle(250, 10, 490, 100, fill=SURFACE, outline=SURF2)
        c.create_rectangle(250, 10, 490, 26, fill=BG2, outline="")
        c.create_text(270, 18, text="Pack Sync", fill=TEXT, font=(UI_FONT, 7, "bold"), anchor="w")
        for i, (name, col) in enumerate([("Project-A  ⚙", PEACH), ("My-Addon  ↕", BLUE), ("Tools  ↕", BLUE)]):
            py = 34 + i * 22
            c.create_rectangle(258, py, 482, py+18, fill=BG2, outline=col, width=1)
            c.create_text(266, py+9, text=name, fill=TEXT, font=(UI_FONT, 7), anchor="w")
            c.create_rectangle(440, py+2, 480, py+16, fill=GREEN, outline="")
            c.create_text(460, py+9, text="Sync", fill=BG, font=(UI_FONT, 7, "bold"))
        c.create_text(370, 105, text=t("pg_proj_auto"),
                      fill=MUTED, font=(UI_FONT, 7))

        steps = [
            ("1", t("pg_proj_step1")),
            ("2", t("pg_proj_step2")),
            ("3", t("pg_proj_step3")),
        ]
        steps_f = tk.Frame(f, bg=BG2, padx=14, pady=8); steps_f.pack(fill=tk.X, padx=22, pady=6)
        for num, tip in steps:
            r = tk.Frame(steps_f, bg=BG2); r.pack(fill=tk.X, pady=2)
            tk.Label(r, text=num, font=(UI_FONT, 9, "bold"), bg=BLUE, fg=BG,
                     width=2, padx=2).pack(side=tk.LEFT)
            tk.Label(r, text=tip, font=(UI_FONT, 9), bg=BG2, fg=TEXT,
                     anchor="w", justify="left").pack(side=tk.LEFT, padx=8)
        self._nav(f, 1, 3, "Next  →", cur_page=2)

    # ── Page 3 — Sync in action ───────────────────────────────────────────────
    def _pg_sync_action(self):
        f = self._frame
        tk.Label(f, text=t("pg_sync_title"), font=(UI_FONT, 13, "bold"),
                 bg=BG, fg=TEXT).pack(pady=(14, 4))
        tk.Label(f, text=t("pg_sync_sub"),
                 font=(UI_FONT, 9), bg=BG, fg=SUB).pack(pady=(0, 8))

        c = self._canvas(f, h=120)
        # Left: repo folder
        c.create_rectangle(10, 30, 80, 70, fill=SURFACE, outline=SURF2)
        c.create_text(45, 45, text="📁", font=(UI_FONT, 16))
        c.create_text(45, 64, text="Repo", fill=MUTED, font=(UI_FONT, 7))
        # Middle: Pack Sync engine
        c.create_rectangle(160, 25, 340, 80, fill=BG2, outline=BLUE, width=2)
        c.create_arc(220, 35, 280, 70, start=30, extent=150, style="arc", outline=BLUE, width=3)
        c.create_arc(220, 35, 280, 70, start=210, extent=150, style="arc", outline=GREEN, width=3)
        c.create_text(250, 52, text="Pack Sync", fill=TEXT, font=(UI_FONT, 8, "bold"))
        c.create_text(250, 78, text="watches for file changes", fill=MUTED, font=(UI_FONT, 7))
        # Right: com.mojang
        c.create_rectangle(420, 30, 490, 70, fill=SURFACE, outline=SURF2)
        c.create_text(455, 45, text="🎮", font=(UI_FONT, 16))
        c.create_text(455, 64, text="com.mojang", fill=MUTED, font=(UI_FONT, 7))
        # Arrows one-way only (input → output)
        c.create_line(82, 52, 158, 52, fill=BLUE, width=2, arrow=tk.LAST)
        c.create_line(342, 52, 418, 52, fill=BLUE, width=2, arrow=tk.LAST)
        c.create_text(120, 38, text="repo→", fill=BLUE, font=(UI_FONT, 6, "bold"))
        c.create_text(380, 38, text="→game", fill=BLUE, font=(UI_FONT, 6, "bold"))
        # Progress bar mockup
        c.create_text(250, 100, text="████████████░░░░  syncing 8 / 12 files…",
                      fill=BLUE, font=("Consolas", 8))
        c.create_text(250, 112, text=t("pg_sync_newer"),
                      fill=MUTED, font=(UI_FONT, 7))

        facts = [
            ("●", GREEN,  t("pg_sync_fact1")),
            ("○", MUTED,  t("pg_sync_fact2")),
            ("↕", BLUE,   t("pg_sync_fact3")),
            ("⚡", YELLOW, t("pg_sync_fact4")),
        ]
        ff = tk.Frame(f, bg=BG2, padx=12, pady=8); ff.pack(fill=tk.X, padx=22, pady=6)
        for icon, col, tip in facts:
            r = tk.Frame(ff, bg=BG2); r.pack(fill=tk.X, pady=2)
            tk.Label(r, text=icon, font=(UI_FONT, 10), bg=BG2, fg=col,
                     width=2).pack(side=tk.LEFT)
            tk.Label(r, text=tip, font=(UI_FONT, 9), bg=BG2, fg=TEXT,
                     anchor="w", justify="left").pack(side=tk.LEFT, padx=6)
        self._nav(f, 2, 4, "Next  →", cur_page=3)

    # ── Page 4 — Regolith workflow ────────────────────────────────────────────
    def _pg_regolith_slide(self):
        f = self._frame
        tk.Label(f, text=t("pg_reg_title"), font=(UI_FONT, 13, "bold"),
                 bg=BG, fg=PEACH).pack(pady=(14, 4))
        tk.Label(f, text=t("pg_reg_sub"),
                 font=(UI_FONT, 9), bg=BG, fg=SUB).pack(pady=(0, 8))

        c = self._canvas(f, h=115)
        # Filter pipeline
        pipeline = [("apply_version", "Py", GREEN), ("armors_images", "JS", GREEN),
                    ("obfuscate_json", "JS", GREEN), ("gametests", "↓", BLUE)]
        px = 10
        for i, (name, rt, col) in enumerate(pipeline):
            c.create_rectangle(px, 20, px+82, 48, fill=BG2, outline=col, width=1)
            c.create_text(px+41, 30, text=name, fill=TEXT, font=(UI_FONT, 7), anchor="center")
            c.create_text(px+41, 42, text=rt, fill=col, font=(UI_FONT, 7, "bold"))
            if i < len(pipeline)-1:
                c.create_line(px+82, 34, px+94, 34, fill=MUTED, width=1, arrow=tk.LAST)
            px += 94
        c.create_text(200, 8, text="Filter Pipeline", fill=MUTED, font=(UI_FONT, 7))
        # Terminal mockup
        c.create_rectangle(10, 56, 490, 108, fill="#0D0D0D", outline="#333")
        term_lines = [
            ("$", "#CCC", " regolith run default"),
            ("✓", GREEN,   " Filters applied in 2.3s"),
            ("✓", GREEN,   " Exported → development_behavior_packs/BR BP - DEV"),
            ("✓", GREEN,   " Exported → development_resource_packs/BR RP - DEV"),
        ]
        for i, (sym, col, txt) in enumerate(term_lines):
            c.create_text(18, 64 + i*11, text=sym, fill=col, font=("Consolas", 8), anchor="w")
            c.create_text(28, 64 + i*11, text=txt, fill="#DDD", font=("Consolas", 8), anchor="w")

        facts = [
            ("▶", t("pg_reg_fact1")),
            ("↕", t("pg_reg_fact2")),
            ("⚙", t("pg_reg_fact3")),
            ("!", t("pg_reg_fact4")),
        ]
        ff = tk.Frame(f, bg=BG2, padx=12, pady=6); ff.pack(fill=tk.X, padx=22, pady=4)
        for icon, tip in facts:
            r = tk.Frame(ff, bg=BG2); r.pack(fill=tk.X, pady=1)
            tk.Label(r, text=icon, font=(UI_FONT, 9), bg=BG2, fg=PEACH,
                     width=2).pack(side=tk.LEFT)
            tk.Label(r, text=tip, font=(UI_FONT, 8), bg=BG2, fg=TEXT,
                     anchor="w", justify="left").pack(side=tk.LEFT, padx=6)
        self._nav(f, 3, 5, "Next  →", cur_page=4)

    # ── Page 5 — Tips & tricks ────────────────────────────────────────────────
    def _pg_tips(self):
        f = self._frame
        tk.Label(f, text=t("pg_tips_title"), font=(UI_FONT, 13, "bold"),
                 bg=BG, fg=YELLOW).pack(pady=(14, 4))

        tips = [
            ("🔍", BLUE,   t("pg_tip1_hdr"), t("pg_tip1_txt")),
            ("⎇",  YELLOW, t("pg_tip2_hdr"), t("pg_tip2_txt")),
            ("🌐", MUTED,  t("pg_tip3_hdr"), t("pg_tip3_txt")),
            ("🖱",  GREEN,  t("pg_tip4_hdr"), t("pg_tip4_txt")),
            ("?",  YELLOW, t("pg_tip5_hdr"), t("pg_tip5_txt")),
            ("⚡", MUTED,  t("pg_tip6_hdr"), t("pg_tip6_txt")),
        ]
        for icon, col, title, desc in tips:
            row = tk.Frame(f, bg=SURFACE, padx=12, pady=5)
            row.pack(fill=tk.X, padx=22, pady=2)
            ic = tk.Canvas(row, width=20, height=20, bg=SURFACE, highlightthickness=0)
            ic.pack(side=tk.LEFT, padx=(0, 8))
            ic.create_text(10, 10, text=icon, fill=col, font=(UI_FONT, 11))
            txt_f = tk.Frame(row, bg=SURFACE); txt_f.pack(side=tk.LEFT, fill=tk.X, expand=True)
            tk.Label(txt_f, text=title, font=(UI_FONT, 9, "bold"), bg=SURFACE,
                     fg=col, anchor="w").pack(anchor="w")
            tk.Label(txt_f, text=desc, font=(UI_FONT, 8), bg=SURFACE,
                     fg=SUB, anchor="w", justify="left").pack(anchor="w")
        self._nav(f, 4, 6, "Set Up  →", cur_page=5)

    # ── Page 6 — Setup form ───────────────────────────────────────────────────
    def _pg_setup(self):
        f = self._frame
        tk.Label(f, text=t("setup_title"), font=(UI_FONT, 14, "bold"),
                 bg=BG, fg=TEXT).pack(pady=(18, 4))
        tk.Label(f, text=t("setup_subtitle"), font=(UI_FONT, 9),
                 bg=BG, fg=SUB).pack(pady=(0, 10))
        tk.Label(f, text=t("setup_project_type"), font=(UI_FONT, 10),
                 bg=BG, fg=TEXT).pack(anchor="w", padx=36)
        for label, val in [("🎮  Minecraft Bedrock", "minecraft"),
                            ("📁  Custom folder",     "custom")]:
            tk.Radiobutton(f, text=label, variable=self._ptype, value=val,
                           bg=BG, fg=TEXT, selectcolor=SURFACE,
                           activebackground=BG, activeforeground=TEXT,
                           command=self._on_type, font=(UI_FONT, 10)).pack(anchor="w", padx=56)
        self._row(f, t("setup_github_folder"), self._gv)
        self._row(f, t("setup_dest_folder"),   self._dv)
        self._nav(f, 5, None, t("btn_get_started"), cur_page=6)

    def _row(self, parent, label, var):
        fr = tk.Frame(parent, bg=BG); fr.pack(fill=tk.X, padx=36, pady=5)
        tk.Label(fr, text=label, bg=BG, fg=TEXT, font=(UI_FONT, 10)).pack(anchor="w")
        r = tk.Frame(fr, bg=BG); r.pack(fill=tk.X)
        tk.Entry(r, textvariable=var, bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                 relief=tk.FLAT, font=("Consolas", 9)).pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(r, text="…", command=lambda v=var: self._browse(v),
                  bg=SURF2, fg=TEXT, relief=tk.FLAT, padx=6).pack(side=tk.LEFT, padx=3)

    def _on_type(self):
        if self._ptype.get() == "minecraft": self._dv.set(str(DEFAULT_MOJANG))

    def _browse(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or str(HOME))
        if d: var.set(d)

    def _confirm(self):
        self.result = {"project_type": self._ptype.get(),
                       "github_dir": self._gv.get(), "mojang_dir": self._dv.get()}
        self.destroy()


# ─── Setup wizard (legacy — kept for compatibility) ────────────────────────────────────────────────────────────────
class SetupDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent); self.title("Welcome to Pack Sync")
        self.geometry("500x395"); self.configure(bg=BG)
        self.grab_set(); self.resizable(False, False)
        self.result: dict | None = None; self._build()

    def _build(self):
        tk.Label(self, text="Welcome to Pack Sync", font=("Segoe UI",15,"bold"),
                 bg=BG, fg=TEXT).pack(pady=(22,4))
        tk.Label(self, text="One-time setup — change later in Settings.",
                 font=("Segoe UI",9), bg=BG, fg=SUB).pack(pady=(0,14))
        tk.Label(self, text="Project type:", font=("Segoe UI",10),
                 bg=BG, fg=TEXT).pack(anchor="w", padx=36)
        self._ptype = tk.StringVar(value="minecraft")
        for label, val in [("Minecraft Bedrock","minecraft"),("Custom","custom")]:
            tk.Radiobutton(self, text=label, variable=self._ptype, value=val,
                           bg=BG, fg=TEXT, selectcolor=SURFACE,
                           activebackground=BG, activeforeground=TEXT,
                           command=self._on_type).pack(anchor="w", padx=56)
        self._gv = tk.StringVar(value=str(DEFAULT_GITHUB))
        self._dv = tk.StringVar(value=str(DEFAULT_MOJANG))
        self._row("GitHub folder:", self._gv)
        self._row("Destination folder:", self._dv)
        tk.Button(self, text="Get Started", command=self._confirm,
                  bg=BLUE, fg=BG, font=("Segoe UI",11,"bold"),
                  relief=tk.FLAT, padx=28, pady=7, cursor="hand2").pack(pady=22)

    def _row(self, label, var):
        f = tk.Frame(self, bg=BG); f.pack(fill=tk.X, padx=36, pady=5)
        tk.Label(f, text=label, bg=BG, fg=TEXT, font=("Segoe UI",10)).pack(anchor="w")
        r = tk.Frame(f, bg=BG); r.pack(fill=tk.X)
        tk.Entry(r, textvariable=var, bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                 relief=tk.FLAT, font=("Consolas",9)).pack(
                     side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(r, text="…", command=lambda v=var: self._browse(v),
                  bg=SURF2, fg=TEXT, relief=tk.FLAT, padx=6).pack(side=tk.LEFT, padx=3)

    def _on_type(self):
        if self._ptype.get() == "minecraft": self._dv.set(str(DEFAULT_MOJANG))

    def _browse(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or str(HOME))
        if d: var.set(d)

    def _confirm(self):
        self.result = {"project_type": self._ptype.get(),
                       "github_dir": self._gv.get(), "mojang_dir": self._dv.get()}
        self.destroy()


# ─── Settings dialog ──────────────────────────────────────────────────────────
class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, cfg):
        super().__init__(parent); self.title(t("settings_title"))
        self.configure(bg=BG)
        self.grab_set(); self.resizable(False, True)
        self.result: dict | None = None; self._cfg = cfg
        self._filter_rows: list[dict] = []
        self._build()
        _center_on(self, parent, 540, 580)

    def _build(self):
        tk.Label(self, text=t("settings_title"), font=(UI_FONT,13,"bold"),
                 bg=BG, fg=TEXT).pack(pady=(18,14))
        self._gv = tk.StringVar(value=self._cfg.get("github_dir", str(DEFAULT_GITHUB)))
        self._dv = tk.StringVar(value=self._cfg.get("mojang_dir", str(DEFAULT_MOJANG)))
        for label, var in [(t("setup_github_folder"), self._gv),
                            (t("setup_dest_folder"),   self._dv)]:
            f = tk.Frame(self, bg=BG); f.pack(fill=tk.X, padx=28, pady=5)
            tk.Label(f, text=label, bg=BG, fg=TEXT, font=(UI_FONT,10)).pack(anchor="w")
            r = tk.Frame(f, bg=BG); r.pack(fill=tk.X)
            tk.Entry(r, textvariable=var, bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                     relief=tk.FLAT, font=("Consolas",9),
                     width=44).pack(side=tk.LEFT)
            tk.Button(r, text="…", command=lambda v=var: self._browse(v),
                      bg=SURF2, fg=TEXT, relief=tk.FLAT, padx=6).pack(side=tk.LEFT, padx=3)
        self._sv = tk.BooleanVar(value=is_startup_enabled())
        tk.Checkbutton(self, text=t("settings_startup"),
                       variable=self._sv, bg=BG, fg=TEXT, selectcolor=SURFACE,
                       activebackground=BG, activeforeground=TEXT,
                       font=(UI_FONT,10)).pack(anchor="w", padx=28, pady=8)
        # Auto-update toggle + manual "check now"
        self._auv = tk.BooleanVar(value=self._cfg.get("auto_update", False))
        au_row = tk.Frame(self, bg=BG); au_row.pack(fill=tk.X, padx=28, pady=(0,4))
        tk.Checkbutton(au_row,
                       text="Check for Pack Sync updates on startup (auto-install)",
                       variable=self._auv, bg=BG, fg=TEXT, selectcolor=SURFACE,
                       activebackground=BG, activeforeground=TEXT,
                       font=(UI_FONT,10)).pack(side=tk.LEFT)
        tk.Button(au_row, text="Check now",
                  command=lambda: self.master._maybe_check_update(manual=True),
                  bg=SURF2, fg=TEXT, relief=tk.FLAT, padx=10, pady=2,
                  font=(UI_FONT,9), cursor="hand2").pack(side=tk.RIGHT)
        # Language selector
        lang_row = tk.Frame(self, bg=BG); lang_row.pack(fill=tk.X, padx=28, pady=4)
        tk.Label(lang_row, text=t("settings_language"), bg=BG, fg=TEXT,
                 font=(UI_FONT,10)).pack(side=tk.LEFT, padx=(0,8))
        langs = list(LANG_NAMES.keys())
        lang_names = [LANG_NAMES[c] for c in langs]
        cur_code   = self._cfg.get("language", _lang)
        cur_idx    = langs.index(cur_code) if cur_code in langs else 0
        self._lang_var = tk.StringVar(value=lang_names[cur_idx])
        om = tk.OptionMenu(lang_row, self._lang_var, *lang_names)
        om.config(bg=SURFACE, fg=TEXT, activebackground=SURF2, activeforeground=TEXT,
                  relief=tk.FLAT, font=(UI_FONT,9), highlightthickness=0)
        om["menu"].config(bg=SURFACE, fg=TEXT, activebackground=BLUE, activeforeground=BG)
        om.pack(side=tk.LEFT)
        self._lang_codes = langs; self._lang_display = lang_names

        # ── Post-sync shell commands (non-Regolith projects) ──────────────────
        tk.Frame(self, bg=SURF2, height=1).pack(fill=tk.X, padx=28, pady=(10, 0))
        adv_hdr = tk.Frame(self, bg=BG); adv_hdr.pack(fill=tk.X, padx=28, pady=2)
        self._adv_open = tk.BooleanVar(value=False)
        self._adv_lbl  = tk.Label(adv_hdr,
                                   text="▶  Post-Sync Commands  (non-Regolith projects)",
                                   bg=BG, fg=MUTED, font=(UI_FONT, 9, "bold"),
                                   cursor="hand2")
        self._adv_lbl.pack(side=tk.LEFT)
        tk.Label(adv_hdr, text="ℹ", bg=BG, fg=BLUE, font=(UI_FONT, 9),
                 cursor="hand2").pack(side=tk.LEFT, padx=4)
        self._adv_frame = tk.Frame(self, bg=BG)
        self._adv_lbl.bind("<Button-1>", self._toggle_adv)
        self._build_adv()

        tk.Frame(self, bg=SURF2, height=1).pack(fill=tk.X, padx=28, pady=(8, 2))
        tk.Button(self, text="🗂  Show Hidden Repositories",
                  command=lambda: HiddenReposDialog(self.master),
                  bg=SURF2, fg=TEXT, relief=tk.FLAT, padx=16, pady=5,
                  font=(UI_FONT, 9), cursor="hand2").pack(pady=(4, 0))
        tk.Button(self, text=t("btn_save"), command=self._save, bg=BLUE, fg=BG,
                  font=(UI_FONT,10,"bold"), relief=tk.FLAT, padx=24, pady=6,
                  cursor="hand2").pack(pady=10)

    def _toggle_adv(self, _=None):
        if self._adv_open.get():
            self._adv_frame.pack_forget()
            self._adv_lbl.config(text="▶  Advanced — Regolith Filters")
            self._adv_open.set(False)
        else:
            self._adv_frame.pack(fill=tk.X, padx=28)
            self._adv_lbl.config(text="▼  Advanced — Regolith Filters")
            self._adv_open.set(True)

    def _build_adv(self):
        f = self._adv_frame
        for w in f.winfo_children(): w.destroy()
        self._filter_rows.clear()
        tk.Label(f, text=(
            "Run shell commands on the destination pack folder after each file sync.\n"
            "For Regolith projects, use the Build button on the project card instead —\n"
            "these commands are for non-Regolith projects only.\n"
            "Commands run in the destination pack folder (com.mojang/development_*_packs/…)."),
            bg=BG, fg=SUB, font=(UI_FONT, 8), justify="left").pack(anchor="w", pady=(4, 6))
        self._filter_list = tk.Frame(f, bg=BG); self._filter_list.pack(fill=tk.X)
        for flt in self._cfg.get("regolith_filters", []):
            self._add_filter_row(flt.get("name",""), flt.get("cmd",""), flt.get("enabled", True))
        add_btn = tk.Button(f, text="＋  Add Filter", command=self._add_filter_row,
                            bg=SURF2, fg=TEXT, relief=tk.FLAT, padx=10, pady=3,
                            font=(UI_FONT, 9), cursor="hand2")
        add_btn.pack(anchor="w", pady=4)

    def _add_filter_row(self, name="", cmd="", enabled=True):
        fr = tk.Frame(self._filter_list, bg=BG, pady=2); fr.pack(fill=tk.X)
        en_var  = tk.BooleanVar(value=enabled)
        name_var = tk.StringVar(value=name)
        cmd_var  = tk.StringVar(value=cmd)
        tk.Checkbutton(fr, variable=en_var, bg=BG, fg=TEXT,
                       selectcolor=SURFACE, activebackground=BG).pack(side=tk.LEFT)
        tk.Entry(fr, textvariable=name_var, width=12, bg=SURFACE, fg=TEXT,
                 insertbackground=TEXT, relief=tk.FLAT,
                 font=("Consolas", 9)).pack(side=tk.LEFT, padx=2)
        tk.Entry(fr, textvariable=cmd_var, width=26, bg=SURFACE, fg=TEXT,
                 insertbackground=TEXT, relief=tk.FLAT,
                 font=("Consolas", 9)).pack(side=tk.LEFT, padx=2)
        row = {"enabled": en_var, "name": name_var, "cmd": cmd_var, "frame": fr}
        self._filter_rows.append(row)
        def _remove(r=row):
            r["frame"].destroy(); self._filter_rows.remove(r)
        tk.Button(fr, text="✕", command=_remove, bg=BG, fg=RED,
                  relief=tk.FLAT, font=(UI_FONT, 8), cursor="hand2").pack(side=tk.LEFT)

    def _browse(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or str(HOME))
        if d: var.set(d)

    def _save(self):
        try: set_startup(self._sv.get())
        except Exception as e:
            messagebox.showwarning("Startup", f"Could not update startup entry:\n{e}")
        sel_name = self._lang_var.get()
        sel_code = self._lang_codes[self._lang_display.index(sel_name)] \
                   if sel_name in self._lang_display else _lang
        apply_lang(sel_code)
        filters = [{"name": r["name"].get(), "cmd": r["cmd"].get(),
                    "enabled": r["enabled"].get()}
                   for r in self._filter_rows if r["cmd"].get().strip()]
        self.result = {"github_dir": self._gv.get(), "mojang_dir": self._dv.get(),
                       "language": sel_code, "regolith_filters": filters,
                       "auto_update": self._auv.get()}
        self.destroy()


class HiddenReposDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.title("Hidden Repositories")
        self.configure(bg=BG)
        self.resizable(False, True)
        self._app    = app
        self._checks: dict[str, tk.BooleanVar] = {}
        self._build()
        _center_on(self, app, 440, 380)
        self.grab_set()

    def _build(self):
        tk.Label(self, text="Hidden Repositories", font=(UI_FONT, 13, "bold"),
                 bg=BG, fg=TEXT).pack(pady=(18, 10))

        hidden = self._app.cfg.get("hidden_repos", [])

        if not hidden:
            tk.Label(self, text="No hidden repositories.", bg=BG, fg=MUTED,
                     font=(UI_FONT, 11)).pack(pady=40)
        else:
            scroll_frame = tk.Frame(self, bg=BG)
            scroll_frame.pack(fill=tk.BOTH, expand=True, padx=28, pady=4)
            for name in sorted(hidden):
                v = tk.BooleanVar(value=False)
                self._checks[name] = v
                row = tk.Frame(scroll_frame, bg=BG)
                row.pack(fill=tk.X, pady=3)
                tk.Checkbutton(row, variable=v, bg=BG, fg=TEXT,
                               selectcolor=SURFACE, activebackground=BG,
                               relief=tk.FLAT, cursor="hand2").pack(side=tk.LEFT)
                tk.Label(row, text=name, bg=BG, fg=TEXT,
                         font=(UI_FONT, 10)).pack(side=tk.LEFT, padx=6)
            tk.Frame(self, bg=SURF2, height=1).pack(fill=tk.X, padx=28, pady=(8, 4))

        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(pady=10)
        if hidden:
            tk.Button(btn_row, text="Unhide Selected",
                      command=self._unhide_selected,
                      bg=SURF2, fg=TEXT, relief=tk.FLAT, padx=14, pady=6,
                      font=(UI_FONT, 9), cursor="hand2").pack(side=tk.LEFT, padx=6)
            tk.Button(btn_row, text="Unhide All",
                      command=self._unhide_all,
                      bg=BLUE, fg=BG, relief=tk.FLAT, padx=14, pady=6,
                      font=(UI_FONT, 9, "bold"), cursor="hand2").pack(side=tk.LEFT, padx=6)
        tk.Button(btn_row, text="Close", command=self.destroy,
                  bg=SURF2, fg=TEXT, relief=tk.FLAT, padx=14, pady=6,
                  font=(UI_FONT, 9), cursor="hand2").pack(side=tk.LEFT, padx=6)

    def _unhide_selected(self):
        to_show = {name for name, v in self._checks.items() if v.get()}
        if not to_show:
            return
        hidden = set(self._app.cfg.get("hidden_repos", []))
        hidden -= to_show
        self._app.cfg["hidden_repos"] = sorted(hidden)
        save_cfg(self._app.cfg)
        self._show_frames(to_show)
        self.destroy()

    def _unhide_all(self):
        all_hidden = set(self._app.cfg.get("hidden_repos", []))
        self._app.cfg["hidden_repos"] = []
        save_cfg(self._app.cfg)
        self._show_frames(all_hidden)
        self.destroy()

    def _show_frames(self, names: set):
        """Instantly re-pack pre-built card frames for the given project names."""
        for name in sorted(names):
            frame = self._app._all_card_frames.get(name)
            if frame:
                frame.pack(fill=tk.X, padx=14, pady=6)
                self._app._card_frames.append((frame, name.lower()))
                proj = next((p for p in self._app._all_projects
                             if p["name"] == name), None)
                if proj:
                    self._app._watchers.restart(
                        proj, self._app._get_sync_pairs(proj))


def _refuse_second_instance():
    """A Pack Sync instance is already running. Tell the user and exit. We avoid
    spinning up the full Tk app; a tiny transient popup is enough."""
    try:
        warn = tk.Tk(); warn.title("Pack Sync")
        warn.geometry("360x120"); warn.configure(bg=BG)
        warn.eval('tk::PlaceWindow . center')
        tk.Label(warn, text="Pack Sync is already running.",
                 font=("Segoe UI", 11, "bold"), bg=BG, fg=TEXT).pack(pady=(24, 4))
        tk.Label(warn, text="Check the system tray.",
                 font=("Segoe UI", 9), bg=BG, fg=SUB).pack()
        tk.Button(warn, text="OK", command=warn.destroy, bg=SURF2, fg=TEXT,
                  relief="flat", padx=24, pady=4).pack(pady=14)
        warn.after(4000, warn.destroy)
        warn.mainloop()
    except Exception:
        pass

if __name__ == "__main__":
    # Enforce a single running instance. When relaunched by the self-updater
    # (--minimized), the old process may not have released its mutex yet, so
    # retry briefly before giving up rather than refusing a legitimate restart.
    _retries = 25 if "--minimized" in sys.argv else 1  # ~5s grace on update-restart
    _got_lock = False
    for _i in range(_retries):
        if acquire_single_instance():
            _got_lock = True
            break
        time.sleep(0.2)
    if not _got_lock:
        _refuse_second_instance()
        sys.exit(0)
    App().mainloop()
