# Frozen smoke

The ritual to run against a packaged bundle **before** it is handed to anyone.

Freezing a Qt + SciPy + PortAudio app does not fail at import time. It fails when
a media plugin, a codec DLL, a lazily-imported backend or a data file was never
collected — and the app starts perfectly, then dies the first time you actually
*use* the thing. So every check below is a **smoke exercise**, not an import:
does the subsystem do its job, in the frozen environment.

## Running it

`FrozenSmoke.exe` ships **inside the bundle**, next to the app. That is
deliberate: it loads the same DLLs, the same `_MEIPASS`, the same collected data
as the real exe, so a pass means something.

```
dist\RippedRecordFormatter\FrozenSmoke.exe
```

It prints a table and exits `0` (all passed) or `1` (something failed). Set
`SMOKE_TRACEBACK=1` for full tracebacks.

To prove the bundle is genuinely self-contained, run it with the machine's own
ffmpeg hidden — otherwise a bundled-ffmpeg "pass" proves nothing:

```powershell
# rename the per-user ffmpeg-downloader copy away, run, put it back
Rename-Item "$env:LOCALAPPDATA\ffmpegio" "$env:LOCALAPPDATA\ffmpegio_HIDDEN"
.\dist\RippedRecordFormatter\FrozenSmoke.exe
Rename-Item "$env:LOCALAPPDATA\ffmpegio_HIDDEN" "$env:LOCALAPPDATA\ffmpegio"
```

## What it checks, and why each one is here

| Check | Why it can break when frozen |
| --- | --- |
| version single-sourced | The spec must read `core/version.py`, never hardcode |
| **ffmpeg resolves to the BUNDLED copy** | The whole point of bundling. Asserts the resolved path is *inside* the bundle — not a per-user download, not something on `PATH` |
| PySide6: window + tabs render | Qt platform plugin not collected ⇒ no window at all |
| pyqtgraph: waveform draws | Resolves its Qt binding at runtime; templates can go missing |
| scipy+numpy+soundfile: a restoration stage runs | **SciPy resolves its array-API backend lazily, by string.** Imports fine, then dies on the first FFT. libsndfile ships as a data file and is easily dropped |
| noisereduce: a spectral-gate call | Same lazy-SciPy path; also proves the matplotlib exclusion didn't break it |
| ffmpeg+pydub+mutagen: WAV → tagged FLAC | Proves the bundled ffmpeg is actually *invocable*, and tags land |
| Full Rip end-to-end: restore → split → encode | The real chain, on a synthetic 2-track side |
| **QtMultimedia: audition player loads audio** | The classic freeze casualty — media plugins and codec DLLs. Asserts the media *loads, decodes and plays*, not just that the class constructs |
| **sounddevice: PortAudio enumerates devices** | PortAudio ships as a bundled DLL. Missing ⇒ zero devices ⇒ Record tab dead |
| AGPL: LICENSE + SOURCE.txt in the bundle | Distributing the binary obliges us to offer the source |

## Then, by hand

The harness cannot click. After it passes, launch
`RippedRecordFormatter.exe` and confirm:

1. The window opens and the title shows the version.
2. All six tabs open without error.
3. **Record** tab: the device list is populated and the meters move.

## What one machine cannot prove

See the *stakeholder checklist* in `build.md` — a machine that has the dev's
Python, ffmpeg and audio drivers installed cannot prove the bundle works on one
that doesn't.
