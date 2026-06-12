# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for FLINT — "Quantum Console".

Produces a one-folder build at dist/FLINT/ (FLINT.exe + _internal/). One-folder
is deliberate: unsigned one-FILE PyInstaller exes get flagged and deleted by
Windows Defender as false positives; the one-folder layout (plus the icon and
version resource below) is flagged far less. Build with:

    pip install -r requirements-build.txt
    pyinstaller flint.spec --noconfirm

Notes
  • The Supabase URL + publishable key travel inside config/app_config.json,
    so accounts work on any machine the .exe is copied to.
  • Browser automation (Playwright) needs Chromium, which PyInstaller does NOT
    bundle. On a fresh machine those specific features stay dormant until the
    user runs `playwright install`; everything else works out of the box.
"""

import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

# ── the app's own data files ─────────────────────────────────────────────────
for src, dst in [
    ("core/prompt.txt",         "core"),
    ("config/app_config.json",  "config"),
    ("mobile_ui",               "mobile_ui"),
    ("face.png",                "."),
    ("pet_memory.json",         "."),
]:
    if os.path.exists(src):
        datas.append((src, dst))

# ── third-party packages that ship data files or use dynamic imports ─────────
for pkg in [
    "google", "supabase", "supabase_auth", "gotrue", "postgrest", "realtime",
    "storage3", "supafunc", "pycaw", "comtypes", "win10toast", "playwright",
    "duckduckgo_search", "youtube_transcript_api", "pywinauto", "mss",
    "sounddevice", "pyaudio",
]:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# Our own packages are imported dynamically (cloud bridge resolves actions by
# name), so make sure every submodule is pulled in.
for pkg in ("actions", "agent", "core", "memory"):
    hiddenimports += collect_submodules(pkg)

hiddenimports += [
    "cv2", "numpy", "psutil", "PIL", "bs4", "requests", "websockets",
    "pyperclip", "pygetwindow", "send2trash",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

# One-FOLDER build: the small launcher exe + its dependencies live side by side
# in dist/FLINT/. Heuristic AV flags this far less than a single self-extracting
# one-file exe. The icon + version resource further lower false-positive odds.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,               # one-folder: keep deps outside the exe
    name="FLINT",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                           # UPX compression spikes AV detections
    console=False,                       # GUI app — no console window
    icon="face.ico" if os.path.exists("face.ico") else None,
    version="version_info.txt" if os.path.exists("version_info.txt") else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="FLINT",
)
