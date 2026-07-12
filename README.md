# Ripped Record Formatter

You record a whole side of a record in one pass and end up with a single enormous WAV. Ripped Record Formatter turns that into a finished album: it cleans the audio (rumble, mains hum, surface noise, clicks), works out where one track ends and the next begins, looks the release up on MusicBrainz to get the real tracklist, per-side structure and cover art, and encodes tagged FLACs — one side at a time or a whole multi-disc album in one run. It is a desktop app built for the actual shape of the job, which is why it asks you where a split goes when it genuinely cannot tell, instead of guessing and quietly mangling a track.

![The Full Rip tab](docs/screenshot.png)

<!-- TODO(screenshot): docs/screenshot.png does not exist. An earlier capture was
     committed by accident and removed again — it showed the pre-rework UI (a
     standalone "Side-long WAV" field, an "Album mode" checkbox, a separate
     "Encode tracks" button), none of which exist any more. Capture the current
     Full Rip tab (folder selected, release looked up so the preview shows, one
     side mid-review with markers on the waveform), save it to
     docs/screenshot.png, and delete this comment. Until then this image is
     broken — do not make the repo public with it missing. -->

## Features

- **Restoration pipeline.** An ordered chain of independent stages — rumble filter (zero-phase subsonic high-pass), mains-hum removal (notch at the mains frequency and its harmonics), spectral-gating noise reduction profiled from the lead-in groove, and click/pop removal. Everything between stages is held as float, so a filter that overshoots full scale is not hard-clipped on the way to the next stage; the single conversion back to the source bit depth happens at the very end, with a headroom check that attenuates rather than clips.
- **Duration-anchored splitting.** Given the expected track durations from the release, the splitter predicts roughly where each gap should fall, searches a window around that prediction for the real silence, and re-anchors the next prediction on the gap it just confirmed — so turntable speed error, unknown lead-out deadspace and approximate CD-sourced durations never accumulate down the side. **When a window contains no convincing silence — a segue, a crossfade, a genuinely gapless transition — it does not invent a split. It hands you that one gap, with the search window highlighted on the waveform, and asks you to place it; the rest of the side is still resolved automatically.**
- **MusicBrainz lookup.** Search a release, pick the right pressing (vinyl is ranked above CD and studio albums above compilations, so the 1959 LP beats a later best-of), and pull down the per-side tracklist, track durations, MusicBrainz IDs and the front cover from the Cover Art Archive.
- **Album orchestration.** Point it at the folder of side WAVs, map each file to its side, and the whole record goes through in one run — sides analyse in the background while you review the one in front of you, and accepting a side starts its encode immediately. Tracks land flat in one output folder, numbered continuously across the album, while the *tags* keep per-side TRACKNUMBER/DISCNUMBER (the Picard vinyl convention).
- **Network-share friendly.** Rips usually live on a NAS. Every operation stages through a local temp directory — copy in, work locally, write results back — and cleans up after itself even on failure or cancellation.

## Installation

**From source — the only supported path today.** Requires **Python 3.14**. There is no packaged build yet; see [build.md](build.md).

```bash
git clone https://github.com/Conrad451/ripped-record-formatter.git
cd ripped-record-formatter
python -m venv .venv
.venv\Scripts\activate        # Windows;  source .venv/bin/activate on Unix
pip install -r requirements.txt
python app.py
```

ffmpeg is fetched automatically on first use — see [Runtime requirements](build.md#runtime-requirements).

## Usage

The happy path, in the **Full Rip** tab:

1. **Select the folder holding this record's side WAVs.** One row appears per WAV found. Set the side for each file that belongs to this record and leave the rest on **— skip —** — a folder can hold more than one album, and anything the app isn't sure about it leaves alone rather than guessing.
2. **Look up the release.** The preview shows the cover art — or warns you, loudly, if the release has none, while you can still do something about it.
3. **Press Start album.** Sides analyse in the background: restoration runs, then the splitter proposes cut points.
4. **Click a ready side to review it.** Adjust the split markers, and edit titles or per-track artists directly in the table.
5. **Press Accept side.** The side is cut, tagged and starts encoding immediately — while you get on with reviewing the next one.
6. **Collect your album.** Every track from every side lands flat in your output folder, numbered continuously.

A single WAV is just a one-row mapping table — use **Add single WAV…** instead of selecting a folder; everything after that is identical.

**Convert** and **Re-tag** are the simpler tabs for WAVs that are already one-file-per-track, or FLACs that just need their tags rewritten.

## How it works

Everything that matters lives in `core/`, which is completely UI-agnostic: no printing, no prompting, no Qt. It exposes plain dataclasses and functions that take callbacks for progress, and the Qt layer in `gui/` is a thin driver on top — which is what makes the core testable against synthetic rips and reusable from a future CLI.

The splitting design is the interesting part. Expected durations only locate a gap to within *seconds* — turntable speed drifts by a percent or two and it compounds, deadspace between tracks is unknown, and CD-sourced durations do not exactly match a vinyl pressing — so durations are used to define a **search window** rather than a cut point, and energy analysis finds the true silence inside it. Every confirmed gap becomes the anchor for the next prediction, which is what stops the error from accumulating across a side; a window with no qualifying silence is reported as unresolved rather than forced.

## Tags written

Written as FLAC Vorbis comments. **A field that is absent writes no tag at all** — never an empty string. The base four are always present; the rest appear only when a release has been looked up.

| Tag | Source | Notes |
| --- | --- | --- |
| `ARTIST` | Per-track artist, falling back to the release artist | Handles splits and various-artists releases |
| `ALBUM` | Release title | |
| `TITLE` | Track title | |
| `TRACKNUMBER` | Position within the side | Resets on each side (Picard vinyl convention) |
| `ALBUMARTIST` | Release artist | |
| `DATE` | Release year | |
| `TRACKTOTAL` | Number of tracks **on that side** | Per-side, not the whole release |
| `DISCNUMBER` | Side / medium position | Side A = 1, side B = 2, … |
| `DISCTOTAL` | Number of sides in the release | |
| `MUSICBRAINZ_ALBUMID` | Release MBID | |
| `MUSICBRAINZ_ARTISTID` | Track artist MBID, falling back to the release artist MBID | |
| `MUSICBRAINZ_RECORDINGID` | Recording MBID | |
| `MUSICBRAINZ_TRACKID` | Release-track MBID | |
| Front cover image | Cover Art Archive | Embedded as a FLAC picture block, type 3 (front cover) |

## Roadmap & known limits

- **Gapless sides need you.** A side mixed as a continuous piece — segues, crossfades, live recordings with applause bridging tracks — has no silence to find. The splitter will tell you which gaps it could not resolve and let you place them by hand, but it will not place them for you. This is deliberate: a wrong automatic cut in the middle of a track is worse than being asked.
- **Defaults are conservative.** Noise reduction in particular is tuned to under-process rather than risk the gurgling artefacts of an aggressive spectral gate. If your pressing is rough, turn it up in **Settings** — every threshold, cutoff and weight is exposed there rather than buried in the code.
- **From source is the only supported path.** There is no packaged build yet — see [build.md](build.md).
- **No command-line interface.** The app is GUI-only for now; `core/` is deliberately UI-agnostic so a CLI can be added without touching the logic.

## License

[GNU AGPLv3](LICENSE). In plain English: you can use, modify and share this freely, but if you distribute it — or run a modified version as a network service — you have to make your source available under the same terms.

## Acknowledgments

Release metadata comes from [MusicBrainz](https://musicbrainz.org/) and cover art from the [Cover Art Archive](https://coverartarchive.org/); please respect their terms of use and rate limits. Audio decoding and click removal lean on [ffmpeg](https://ffmpeg.org/). The app stands on [PySide6](https://doc.qt.io/qtforpython/) (Qt), [NumPy](https://numpy.org/), [SciPy](https://scipy.org/), [soundfile](https://python-soundfile.readthedocs.io/), [noisereduce](https://github.com/timsainb/noisereduce), [mutagen](https://mutagen.readthedocs.io/), [pydub](https://github.com/jiaaro/pydub), [musicbrainzngs](https://python-musicbrainzngs.readthedocs.io/), and [pyqtgraph](https://www.pyqtgraph.org/).
