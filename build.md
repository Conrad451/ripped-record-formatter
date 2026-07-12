# Build

How to reproduce a working build from a clean clone.

**From source is the only supported way to run the app today** — there is no
packaged build (see [Packaging a standalone executable](#packaging-a-standalone-executable)),
and no command-line interface. The README says the same.

## Deferred work

Tracked here so it is not rediscovered later:

- **Packaging.** No PyInstaller spec for the Qt app yet — details below.
- **CLI.** Deferred by decision. `core/` is UI-agnostic precisely so one can be
  added later without touching the logic.
- **`requirements.txt` is not pruned.** It still carries the legacy interactive
  script's dependencies (`tk`/`Tcl`, `tqdm`, `alive-progress`, `colorama`), which
  the Qt app does not import. They stay until `v2/` and its pins are retired
  *together* — pruning them first would break the legacy script, which is still
  on `main`.

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

<!-- TODO(packaging): No PyInstaller spec exists for the PySide6 app yet. The
     .gitignore already reserves *.spec (it is deliberately NOT ignored) so a
     hand-tuned spec can be committed once written. Work still to do:
       - author a .spec for app.py covering PySide6 plugins and the scipy/numpy
         binary deps that PyInstaller's analysis routinely misses
       - decide whether ffmpeg is bundled into the frozen app or left to the
         per-user ffmpeg-downloader path (ffmpeg_locator.py is the single place
         that would change — it was written with exactly this in mind)
       - AGPLv3 means the corresponding source must be offered with any binary
         you distribute
       - then measure and record: bundle size, cold-start time, and a smoke test
         of the frozen exe on a machine with no Python installed
     The legacy v2 terminal app's spec is preserved at tag archive/v3-exe
     (v2/ripped_record_formatter.spec) for reference only — it targets the old
     console script, not the current Qt app. -->

**TODO** — not implemented yet. `pyinstaller` is already pinned in
`requirements.txt`, and `.gitignore` deliberately does *not* ignore `*.spec` so a
hand-tuned spec can be versioned once it exists. Until then the from-source path
above is the only supported way to run the app.

Note that AGPLv3 obliges you to offer the corresponding source alongside any
binary you distribute.
