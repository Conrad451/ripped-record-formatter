# Build

How to reproduce a working build from a clean clone.

Two ways to run it: **from source** (below), or the **packaged bundle** — a
self-contained `RippedRecordFormatter.exe` needing no Python, no ffmpeg and no
network. See [Packaging a standalone executable](#packaging-a-standalone-executable).
There is still no command-line interface.

## Deferred work

Tracked here so it is not rediscovered later:

- **CLI.** Deferred by decision. `core/` is UI-agnostic precisely so one can be
  added later without touching the logic.
- **`requirements.txt` is not pruned.** It still carries the legacy interactive
  script's dependencies (`tk`/`Tcl`, `tqdm`, `alive-progress`, `colorama`), which
  the Qt app does not import. They stay until `v2/` and its pins are retired
  *together* — pruning them first would break the legacy script, which is still
  on `main`. (The packaged bundle already excludes them — see the bundle diet.)
- **The bundle is not code-signed.** Windows SmartScreen will warn on first run.

## Prerequisites

- **Python 3.14** (the project is developed and tested only on 3.14; several pinned
  wheels — NumPy 2.5, SciPy 1.18, PySide6 6.11 — are resolved against it).
- **git**.
- A network connection on first run, so ffmpeg can be fetched (see
  [Runtime requirements](#runtime-requirements)).

Nothing else needs to be installed system-wide. In particular you do **not** need
to install ffmpeg yourself, and you do not need admin rights.

## From a clean clone

```bash
git clone https://github.com/Conrad451/ripped-record-formatter.git
cd ripped-record-formatter

python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux

pip install -r requirements.txt
```

For development work (test runner and friends) also install:

```bash
pip install -r requirements-dev.txt
```

## Verify

Run the test suite from the repo root:

```bash
python -m pytest
```

All tests should pass. The suite is self-contained — it generates its own
synthetic vinyl rips (tones separated by gaps sitting on a noise floor) and needs
no sample audio, no network, and no fixtures on disk. The only external
dependency it touches is ffmpeg, which the declick tests exercise for real.

Then launch the app:

```bash
python app.py
```

## Runtime requirements

The app resolves ffmpeg itself, through `core/ffmpeg_locator.py` — nothing else in
the codebase is allowed to reach for the binary directly. Resolution order:

0. **An ffmpeg bundled inside the frozen app.** Packaged builds only, and checked
   first — see [ffmpeg resolution: frozen vs. dev](#ffmpeg-resolution-frozen-vs-dev).
   Running from source this step is inert, so what follows is unchanged.
1. **An ffmpeg managed by the `ffmpeg-downloader` package** — a static build
   unpacked into the per-user data directory. This is the preferred path: no admin
   rights, no system `PATH` pollution.
2. **An `ffmpeg` already on the system `PATH`** — developer convenience and CI.

If neither is found, the app shells out to `python -m ffmpeg_downloader install -y`
to fetch one (this is the first-run network hit). If that also fails, it raises
`FFmpegNotAvailable` with instructions rather than dying obscurely.

To pre-seed ffmpeg yourself, either put it on `PATH` or run:

```bash
python -m ffmpeg_downloader install
```

Beyond ffmpeg, a target machine needs nothing but the Python runtime and the
pinned dependencies. Network access is optional and only used for two things:
fetching ffmpeg on first run, and MusicBrainz / Cover Art Archive lookups. Both
degrade cleanly when offline — metadata lookups raise a typed error and the rest
of the app keeps working.

## Packaging a standalone executable

A **onedir** bundle: `dist/RippedRecordFormatter/` containing
`RippedRecordFormatter.exe` (windowed) and `FrozenSmoke.exe` (the verification
harness — see [frozen-smoke.md](frozen-smoke.md)). It is **self-contained**: it
needs no Python, no ffmpeg and no network.

### Build it

From a clean clone, with the venv from [above](#from-a-clean-clone) active:

```bash
python scripts/fetch_ffmpeg.py                                   # once
python -m PyInstaller RippedRecordFormatter.spec --noconfirm
```

`build/` and `dist/` are gitignored; the spec is committed.

### The pinned ffmpeg

`scripts/fetch_ffmpeg.py` downloads one exact, immutable build into `vendor/`
(gitignored — a 200 MB binary does not belong in a source repo):

| | |
| --- | --- |
| Version | **ffmpeg 8.1.2-essentials_build** (gyan.dev static build) |
| URL | `https://github.com/GyanD/codexffmpeg/releases/download/8.1.2/ffmpeg-8.1.2-essentials_build.zip` |
| sha256 (zip) | `db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec` |
| Shipped | `ffmpeg.exe` (98 MB) + `ffprobe.exe` (97 MB). `ffplay` is not shipped |

The script refuses to proceed if the download's size does not match, so a moved
release cannot silently change what we ship.

**ffprobe is required, not optional.** pydub shells out to it to read a FLAC when
re-tagging; the re-tag path fails outright without it (verified by hiding it and
watching `test_retag_same_path_guard` fail).

We pin rather than reuse whatever `ffmpeg-downloader` left in the developer's
data dir, because that is "whatever was latest the day they first ran the app" —
not reproducible, and two machines would produce two different bundles.

### ffmpeg resolution: frozen vs. dev

`core/ffmpeg_locator.py` resolves in this order:

0. **Bundled** — `ffmpeg/ffmpeg.exe` inside the frozen app (`sys._MEIPASS`, or
   `_internal/` beside the exe). *Frozen only, and checked first*: the packaged
   app must work with no ffmpeg installed and no network, and must not be at the
   mercy of a stale ffmpeg on the user's `PATH`.
1. The `ffmpeg-downloader`-managed per-user copy — what a developer running from
   source gets.
2. `ffmpeg` on `PATH`.

Then, if `auto_download` is set, it fetches one. **A frozen app never gets past
step 0**, which is what makes it network-free. Running from source, step 0 is
inert (`sys.frozen` is unset) so dev behaviour is exactly as it always was.

### Bundle diet

`noisereduce` *declares* matplotlib as a dependency but only imports it inside
`noisereduce/plotting.py`, a debug-visualisation module this app never touches —
verified: a real `reduce_noise()` call imports none of the matplotlib family. The
spec excludes it, along with pillow / fontTools / contourpy, tkinter (the legacy
`v2/` script's dependency), and the Qt modules we never load.

| | Size |
| --- | --- |
| Without the diet | **489 MB** |
| With the diet | **454 MB** (app only) |
| Shipped bundle | **468 MB** (adds `FrozenSmoke.exe`) |

Cold start: **~0.9 s** to a visible window (3 runs: 0.98 / 0.85 / 0.82 s).

The bundle is dominated by things that cannot be dieted away: **ffmpeg 195 MB**
(two static binaries), **Qt 122 MB**, **SciPy 68 MB**, **NumPy 28 MB**.

### AGPL

The bundle ships `LICENSE` and a generated `SOURCE.txt` naming the repository and
the **exact commit** it was built from. Distributing the binary obliges us to
offer the corresponding source; `SOURCE.txt` is that offer, made concrete. It
also records that ffmpeg is redistributed under its own separate licence.

### Verify before shipping

Run [`frozen-smoke.md`](frozen-smoke.md)'s ritual. `FrozenSmoke.exe` exercises
every subsystem *in the frozen environment* — Qt, pyqtgraph, SciPy, soundfile,
noisereduce, mutagen, QtMultimedia, PortAudio, the bundled ffmpeg, and one full
restore → split → encode. Eleven checks; all must pass.

### Stakeholder checklist — what one machine cannot prove

A dev machine has Python, ffmpeg and audio drivers already installed, so it
**cannot** prove the bundle stands alone. On a second machine:

1. Copy **`dist\RippedRecordFormatter\`** across — that is the folder containing
   `_internal\`, `RippedRecordFormatter.exe` and `FrozenSmoke.exe`. It is the
   whole product. (`build\` is PyInstaller's scaffolding, not the product; ignore
   it.) Do not install anything.
2. Run `FrozenSmoke.exe` — **double-clicking it is fine**, the window now stays
   open until you press Enter. Expect **11/11 passed**; send the output back if
   not. On a machine with no sound card the two audio checks report `[SKIP]` and
   the summary reads *"9/11 passed, 2 skipped: no audio hardware"* — that is
   expected and is not a bundle defect. Only a `[FAIL]` is.
3. Launch `RippedRecordFormatter.exe`. Confirm the window opens and the title
   shows the version.
4. Time the launch. Confirm the delay to a visible window is acceptable
   (~1 s here; a slower disk or a cold file cache will be worse).
5. Open all six tabs; confirm none errors.
6. **Record** tab: confirm the device list is populated for *that* machine's
   hardware and the meters move when audio is playing in.
7. Record a few seconds, press Stop, confirm the WAV lands and the clip/peak
   readout looked sane.
8. **Full Rip** → *Add single WAV…* → point it at that recording → **Analyze**.
   Confirm the waveform draws and splits are proposed.
9. Accept a side and confirm a tagged FLAC is written.
10. Press **Play** / **Preview cut** on the audition player and confirm you hear
    audio through *that* machine's output.

If you cannot get a second machine, approximate it on this one: rename
`%LOCALAPPDATA%fmpegio` away and sanitise `PATH` before running (this is what
the dev build was verified against), but note that Qt/audio drivers already
present cannot be hidden this way.

