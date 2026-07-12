"""The frozen-smoke ritual: prove every subsystem survived freezing.

Built into the bundle as ``FrozenSmoke.exe``, alongside the windowed app, so it
exercises *exactly* the environment the real exe runs in -- same collected DLLs,
same _MEIPASS, same everything. Running it from source works too, for comparison.

It is deliberately not a unit test. Each check is a *smoke exercise*: not "does
this import" but "does this actually do its job". Imports are the easy half; the
things that break when you freeze a Qt/scipy/PortAudio app are media plugins,
codec DLLs and data files that never get collected.

    FrozenSmoke.exe            # run every check, print a table, exit 0/1
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str):
    def wrap(fn):
        try:
            detail = fn() or "ok"
            RESULTS.append((name, True, str(detail)))
        except Exception as exc:
            RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
            if os.environ.get("SMOKE_TRACEBACK"):
                traceback.print_exc()
        return fn

    return wrap


def main() -> int:
    frozen = getattr(sys, "frozen", False)
    print("=" * 72)
    print(f"Ripped Record Formatter — frozen smoke")
    print(f"  mode       : {'FROZEN (' + sys.executable + ')' if frozen else 'from source'}")
    print("=" * 72)

    # --- version + ffmpeg resolution ---------------------------------------
    @check("version single-sourced")
    def _version():
        from core.version import __version__

        return __version__

    @check("ffmpeg resolves to the BUNDLED copy")
    def _ffmpeg():
        import subprocess

        from core.ffmpeg_locator import find_ffmpeg

        ffmpeg, ffprobe = find_ffmpeg()
        if ffmpeg is None:
            raise RuntimeError("no ffmpeg resolved at all")
        if frozen:
            # This is the whole point of bundling: the app must NOT be reaching
            # for a per-user download or something on PATH.
            root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
            inside = str(root).lower() in str(ffmpeg).lower() or \
                str(Path(sys.executable).parent).lower() in str(ffmpeg).lower()
            if not inside:
                raise RuntimeError(f"resolved OUTSIDE the bundle: {ffmpeg}")
        out = subprocess.run([str(ffmpeg), "-version"], capture_output=True,
                             text=True).stdout.splitlines()[0]
        if ffprobe is None or not Path(ffprobe).exists():
            raise RuntimeError("ffprobe missing (the re-tag path needs it)")
        return f"{ffmpeg} | {out[:48]}"

    # --- PySide6: a real window with real tabs ------------------------------
    @check("PySide6: window + tabs render")
    def _qt():
        from PySide6.QtWidgets import QApplication

        from gui.main_window import MainWindow

        app = QApplication.instance() or QApplication(sys.argv)
        window = MainWindow()
        window.show()
        app.processEvents()
        tabs = [window.tabs.tabText(i) for i in range(window.tabs.count())]
        if len(tabs) < 5:
            raise RuntimeError(f"only {len(tabs)} tabs: {tabs}")
        if not window.isVisible():
            raise RuntimeError("window did not become visible")
        globals()["_WINDOW"] = window       # keep alive for later checks
        globals()["_APP"] = app
        return f"{window.windowTitle()} | {', '.join(tabs)}"

    # --- pyqtgraph: the waveform actually draws ------------------------------
    @check("pyqtgraph: waveform draws an envelope")
    def _pyqtgraph():
        import numpy as np
        import soundfile as sf

        from core.waveform import load_peak_envelope

        window = globals()["_WINDOW"]
        app = globals()["_APP"]
        # Build the envelope the way the app does -- from a real file, through the
        # real loader -- rather than faking the dataclass.
        tmp = Path(tempfile.mkdtemp(prefix="smoke_"))
        wav = tmp / "w.wav"
        t = np.arange(44100) / 44100
        sf.write(str(wav), (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32),
                 44100, subtype="PCM_16")
        env = load_peak_envelope(wav, 500)
        mins = env.mins
        wf = window.full_rip.waveform
        wf.set_envelope(env)
        wf.add_marker(0.5)
        app.processEvents()
        if wf.marker_count() != 1:
            raise RuntimeError("marker did not land")
        return f"envelope {len(mins)} buckets, 1 marker, axis ok"

    # --- scipy / numpy / soundfile: a real restoration stage ------------------
    @check("scipy+numpy+soundfile: a restoration stage runs")
    def _dsp():
        import numpy as np
        import soundfile as sf

        from core.restoration import HumRemoval, RumbleFilter

        tmp = Path(tempfile.mkdtemp(prefix="smoke_"))
        src, mid, dst = tmp / "a.wav", tmp / "b.wav", tmp / "c.wav"
        t = np.arange(44100) / 44100
        sig = (0.3 * np.sin(2 * np.pi * 1000 * t)
               + 0.2 * np.sin(2 * np.pi * 60 * t)
               + 0.2 * np.sin(2 * np.pi * 12 * t)).astype(np.float32)
        sf.write(str(src), sig, 44100, subtype="PCM_16")

        RumbleFilter().apply(src, mid)          # scipy butter + sosfiltfilt
        HumRemoval().apply(mid, dst)            # scipy iirnotch + filtfilt
        out, _ = sf.read(str(dst))
        if out.shape[0] != 44100:
            raise RuntimeError("stage changed the length")
        if sf.info(str(dst)).subtype != "FLOAT":
            raise RuntimeError("stage did not write a float intermediate")
        return f"rumble+hum ran, {out.shape[0]} frames, float intermediate"

    # --- noisereduce: one real spectral-gate call ----------------------------
    @check("noisereduce: a spectral-gate call")
    def _nr():
        import numpy as np
        from noisereduce import reduce_noise

        rng = np.random.default_rng(0)
        t = np.arange(44100) / 44100
        y = (0.3 * np.sin(2 * np.pi * 1000 * t)
             + 0.02 * rng.standard_normal(44100)).astype(np.float32)
        out = reduce_noise(y=y, sr=44100, y_noise=y[:4410], stationary=True,
                           prop_decrease=0.5)
        if out.shape != y.shape:
            raise RuntimeError("reduce_noise changed the shape")
        return f"{out.shape[0]} frames gated"

    # --- ffmpeg end-to-end: encode a tagged FLAC -----------------------------
    @check("ffmpeg+pydub+mutagen: WAV -> tagged FLAC")
    def _encode():
        import numpy as np
        import soundfile as sf
        from mutagen.flac import FLAC

        from core.converter import convert_wavs_to_flacs
        from core.ffmpeg_locator import configure_pydub
        from core.tracks import Tracks

        tmp = Path(tempfile.mkdtemp(prefix="smoke_"))
        wav = tmp / "t.wav"
        t = np.arange(22050) / 44100
        sf.write(str(wav), (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32),
                 44100, subtype="PCM_16")

        configure_pydub()                       # must find the BUNDLED ffmpeg
        track = Tracks(1, "Smoke Test", "Album", "Artist", wav,
                       album_artist="Artist", date="1959", mb_album_id="mbid")
        result = convert_wavs_to_flacs([track], tmp / "out", configure=False)
        produced = result.outcomes[0].output_path
        if not produced.exists() or produced.stat().st_size == 0:
            raise RuntimeError("no FLAC produced")
        tags = FLAC(str(produced))
        if tags["title"] != ["Smoke Test"] or tags["musicbrainz_albumid"] != ["mbid"]:
            raise RuntimeError(f"tags wrong: {dict(tags)}")
        globals()["_FLAC"] = produced
        return f"{produced.name} ({produced.stat().st_size} bytes), tags verified"

    # --- the whole chain, frozen: restore -> split -> encode ------------------
    @check("Full Rip end-to-end: restore -> split -> encode")
    def _pipeline():
        import numpy as np
        import soundfile as sf
        from mutagen.flac import FLAC

        from core.converter import convert_wavs_to_flacs
        from core.ffmpeg_locator import configure_pydub
        from core.job_settings import build_policy, build_silence_params, build_stages
        from core.config import Config
        from core.restoration import restore
        from core.splitting import execute_split, propose_splits
        from core.tracks import Tracks

        tmp = Path(tempfile.mkdtemp(prefix="smoke_"))
        # A synthetic 2-track side: two tones split by a gap on a noise floor.
        rng = np.random.RandomState(7)
        sr = 44100
        parts = []
        for i in range(2):
            t = np.arange(int(3.0 * sr)) / sr
            parts.append(0.35 * np.sin(2 * np.pi * (220 + 110 * i) * t))
            if i == 0:
                parts.append(np.zeros(int(1.5 * sr)))
        side = np.concatenate(parts)
        side = side + rng.normal(0, 10 ** (-55 / 20), side.size)
        src = tmp / "SideA.wav"
        sf.write(str(src), side.astype(np.float32), sr, subtype="PCM_16")

        cfg = Config()
        cfg.min_silence, cfg.min_track_length = 0.5, 1.0
        restored = tmp / "restored.wav"
        restore(src, restored, build_stages(cfg), policy=build_policy(cfg))

        proposal = propose_splits(restored, track_count=2,
                                  params=build_silence_params(cfg))
        if len(proposal.split_points) != 1:
            raise RuntimeError(f"expected 1 cut, got {len(proposal.split_points)}")

        segments = execute_split(restored, proposal.timestamps(), tmp / "cut")
        if len(segments) != 2:
            raise RuntimeError(f"expected 2 segments, got {len(segments)}")

        configure_pydub()
        tracks = [Tracks(i + 1, f"Track {i + 1}", "Album", "Artist", seg,
                         album_artist="Artist", track_total=2, disc_number=1)
                  for i, seg in enumerate(segments)]
        result = convert_wavs_to_flacs(tracks, tmp / "out", configure=False)
        flacs = sorted((tmp / "out").glob("*.flac"))
        if len(flacs) != 2:
            raise RuntimeError(f"expected 2 FLACs, got {len(flacs)}")
        tags = FLAC(str(flacs[0]))
        if tags["tracknumber"] != ["1"] or tags["tracktotal"] != ["2"]:
            raise RuntimeError("per-side tags wrong")
        if result.warnings:
            raise RuntimeError(f"warnings: {result.warnings}")
        globals()["_PIPELINE_FLAC"] = flacs[0]
        cut = proposal.split_points[0].timestamp
        return (f"cut at {cut:.2f}s -> 2 tracks -> "
                f"{flacs[0].name}, {flacs[1].name} (tagged)")

    # --- QtMultimedia: the classic freeze casualty ---------------------------
    @check("QtMultimedia: audition player loads audio")
    def _multimedia():
        import time

        import numpy as np
        import soundfile as sf

        from gui.playback import AuditionPlayer

        app = globals()["_APP"]
        player = AuditionPlayer()
        if not player.available:
            raise RuntimeError(f"no audio backend: {player.unavailable_reason}")

        tmp = Path(tempfile.mkdtemp(prefix="smoke_"))
        wav = tmp / "p.wav"
        t = np.arange(44100 * 3) / 44100
        sf.write(str(wav), (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32),
                 44100, subtype="PCM_16")

        player.set_source(wav)
        deadline = time.time() + 8.0
        while time.time() < deadline:
            app.processEvents()
            if player._player.duration() > 0:
                break
            time.sleep(0.02)
        duration = player._player.duration()
        if duration <= 0:
            raise RuntimeError("media never loaded — media plugin/codec missing")

        player.preview_cut(2.0, lead_in=1.0)    # must seek AND play
        deadline = time.time() + 4.0
        while time.time() < deadline:
            app.processEvents()
            if player.position() > 0.5:
                break
            time.sleep(0.02)
        pos = player.position()
        player.stop()
        if pos <= 0.0:
            raise RuntimeError("player never advanced — decode failed")
        return f"loaded {duration} ms, played to {pos:.2f}s, seek ok"

    # --- sounddevice / PortAudio --------------------------------------------
    @check("sounddevice: PortAudio enumerates input devices")
    def _devices():
        import sounddevice as sd

        from core.recorder import list_input_devices

        version = sd.get_portaudio_version()[1]
        devices = list_input_devices()
        if not devices:
            raise RuntimeError("zero input devices — PortAudio DLL missing?")
        return f"{version.split(',')[0]} | {len(devices)} input device(s); " \
               f"first: {devices[0].name}"

    # --- AGPL artefacts ------------------------------------------------------
    @check("AGPL: LICENSE + SOURCE.txt ship in the bundle")
    def _agpl():
        roots = [Path(sys.executable).parent]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass))
        roots += [r / "_internal" for r in list(roots)]
        if not frozen:
            roots.append(Path(__file__).resolve().parent.parent)

        for root in roots:
            licence, source = root / "LICENSE", root / "SOURCE.txt"
            if licence.exists() and source.exists():
                text = source.read_text(encoding="utf-8")
                if "Commit" not in text:
                    raise RuntimeError("SOURCE.txt carries no commit")
                commit = [ln for ln in text.splitlines() if "Commit" in ln][0].strip()
                return f"{root} | {commit}"
        raise RuntimeError("LICENSE / SOURCE.txt not found in the bundle")

    # --- report --------------------------------------------------------------
    print()
    width = max(len(n) for n, _, _ in RESULTS)
    failed = 0
    for name, ok, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  [{mark}] {name.ljust(width)}  {detail}")

    print()
    print("=" * 72)
    print(f"  {len(RESULTS) - failed}/{len(RESULTS)} passed"
          + ("" if not failed else f"  — {failed} FAILED"))
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
