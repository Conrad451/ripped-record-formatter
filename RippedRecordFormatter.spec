# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: a self-contained, network-free RippedRecordFormatter.exe.

onedir, windowed. Build with:

    python -m PyInstaller RippedRecordFormatter.spec --noconfirm

Two things this spec exists to guarantee, beyond "it starts":

* **ffmpeg ships inside the bundle.** The frozen app must convert end to end on a
  machine with no ffmpeg and no network. The binaries are collected from whatever
  ``core.ffmpeg_locator`` resolves at *build* time, and land in ``ffmpeg/`` next
  to the exe; the locator checks that location first when frozen.
* **AGPL source offer.** LICENSE and a generated SOURCE.txt (repo URL + the exact
  commit this bundle was built from) ride along in the bundle, because
  distributing the binary obliges us to offer the corresponding source.

Every hidden import / binary collection below carries a one-line reason. None of
them is speculative -- each was added to fix an observed failure in the frozen
app, not defensively.
"""

import subprocess
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = Path(SPECPATH)
sys.path.insert(0, str(ROOT))

# --------------------------------------------------------------------------- #
# Version: single-sourced, never hardcoded here.
# --------------------------------------------------------------------------- #
version_ns: dict = {}
exec((ROOT / "core" / "version.py").read_text(encoding="utf-8"), version_ns)
VERSION = version_ns["__version__"]

# --------------------------------------------------------------------------- #
# AGPL: the binary must carry its source offer.
# --------------------------------------------------------------------------- #
REPO_URL = "https://github.com/Conrad451/ripped-record-formatter"


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


commit = _git("rev-parse", "HEAD")
describe = _git("describe", "--tags", "--always", "--dirty")
source_txt = ROOT / "SOURCE.txt"
source_txt.write_text(
    f"""Ripped Record Formatter {VERSION}
{'=' * 60}

This program is free software licensed under the GNU Affero General Public
License v3 (see LICENSE, distributed alongside this file).

The AGPL requires that the *corresponding source* for this binary is offered to
you. It is:

    Repository : {REPO_URL}
    Commit     : {commit}
    Describes  : {describe}

To obtain and build exactly this version:

    git clone {REPO_URL}
    cd ripped-record-formatter
    git checkout {commit}
    # then follow build.md

This bundle also redistributes ffmpeg (https://ffmpeg.org/), which is licensed
separately under the GPL/LGPL; see build.md for the exact build shipped.
""",
    encoding="utf-8",
)

# --------------------------------------------------------------------------- #
# ffmpeg: the pinned build from vendor/, shipped inside the bundle.
# --------------------------------------------------------------------------- #
# Sourced from vendor/ -- populated by scripts/fetch_ffmpeg.py from an immutable
# pinned release -- and NOT from whatever ffmpeg-downloader happens to have put
# in the developer's data dir, which is not reproducible across machines.
# ffprobe is required, not optional: pydub shells out to it to read a FLAC when
# re-tagging, and the re-tag path breaks outright without it (verified).
VENDOR_FFMPEG = ROOT / "vendor" / "ffmpeg"
ffmpeg_exe = VENDOR_FFMPEG / "ffmpeg.exe"
ffprobe_exe = VENDOR_FFMPEG / "ffprobe.exe"
for _needed in (ffmpeg_exe, ffprobe_exe):
    if not _needed.exists():
        raise SystemExit(
            f"{_needed} is missing. Run `python scripts/fetch_ffmpeg.py` first "
            "-- see build.md."
        )

# Land them in `ffmpeg/` beside the exe; ffmpeg_locator looks there first when
# frozen. Declared as *datas*, not *binaries*: they are standalone executables we
# shell out to, not libraries PyInstaller should scan and rewrite.
ffmpeg_datas = [(str(ffmpeg_exe), "ffmpeg"), (str(ffprobe_exe), "ffmpeg")]

# --------------------------------------------------------------------------- #
# Data / binary collection
# --------------------------------------------------------------------------- #
datas = [
    *ffmpeg_datas,
    (str(ROOT / "LICENSE"), "."),      # AGPL: ship the licence with the binary
    (str(source_txt), "."),            # AGPL: ...and the source offer
]

binaries = []

# soundfile ships libsndfile as a bundled DLL under _soundfile_data/. Without it
# the frozen app cannot read or write a single WAV.
datas += collect_data_files("soundfile")
binaries += collect_dynamic_libs("soundfile")

# sounddevice ships PortAudio the same way, under _sounddevice_data/. Without it
# device enumeration raises and the Record tab is dead.
datas += collect_data_files("sounddevice")
binaries += collect_dynamic_libs("sounddevice")

hiddenimports = [
    # soundfile and sounddevice reach libsndfile / PortAudio through cffi at
    # runtime; PyInstaller's static analysis never sees the backend being loaded.
    "_cffi_backend",
    # scipy resolves its array-API backend lazily, by string, the first time an
    # FFT runs. Without this the frozen app imports fine and then dies with
    # ModuleNotFoundError the moment ANY restoration stage or noisereduce call
    # touches scipy.signal -- i.e. the entire DSP path. Found by the frozen smoke
    # harness; PyInstaller's own analysis warns about the wrong path
    # (scipy._lib.array_api_compat...), which does not exist in SciPy 1.18.
    "scipy._external.array_api_compat.numpy.fft",
]

# --------------------------------------------------------------------------- #
# Bundle diet
# --------------------------------------------------------------------------- #
# noisereduce *declares* matplotlib as a dependency, but only imports it inside
# noisereduce/plotting.py -- a debug-visualisation module this app never touches.
# Verified: a real reduce_noise() call imports none of the matplotlib family.
# Excluding it drops matplotlib, pillow, fontTools and contourpy from the bundle.
excludes = [
    "matplotlib",
    "PIL",
    "Pillow",
    "fontTools",
    "contourpy",
    "kiwisolver",
    "cycler",
    # The legacy v2/ terminal script's deps. The Qt app imports none of them, and
    # tkinter in particular is a large, pointless payload next to Qt.
    "tkinter",
    "tcl",
    "tk",
    "IPython",
    "pytest",
    # PySide6 modules we never touch. Qt is the single biggest thing in here, and
    # WebEngine alone is >100 MB.
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtQuick3D",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtDesigner",
    "PySide6.QtTest",
    # QML/Quick: this is a QtWidgets app. Nothing here loads a .qml file.
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQml",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtSql",
    "PySide6.QtSvg",
    "PySide6.QtHelp",
    "PySide6.QtSerialPort",
    "PySide6.QtPositioning",
    "PySide6.QtSensors",
    "PySide6.QtWebSockets",
    "PySide6.QtWebChannel",
]

# Two entry points sharing ONE collected environment: the windowed app, and a
# console smoke harness. The harness therefore exercises exactly the DLLs, data
# files and _MEIPASS the real exe runs against -- which is the only way to prove
# a media plugin or a PortAudio DLL actually survived the freeze. It also gives
# the stakeholder something runnable on their own machine.
a = Analysis(
    ["app.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

smoke = Analysis(
    ["scripts/frozen_smoke.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

MERGE((a, "app", "RippedRecordFormatter"), (smoke, "frozen_smoke", "FrozenSmoke"))

pyz = PYZ(a.pure)
smoke_pyz = PYZ(smoke.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RippedRecordFormatter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX mangles Qt plugin DLLs; not worth the risk
    console=False,             # windowed: this is a GUI app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

smoke_exe = EXE(
    smoke_pyz,
    smoke.scripts,
    [],
    exclude_binaries=True,
    name="FrozenSmoke",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,              # the harness prints a report; it needs a console
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    smoke_exe,
    smoke.binaries,
    smoke.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="RippedRecordFormatter",
)
