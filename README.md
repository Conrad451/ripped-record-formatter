# Ripped Record Formatter

You play a record into it and get a finished album back. Ripped Record Formatter records the side, then turns that single enormous WAV into tagged tracks: it cleans the audio (rumble, mains hum, surface noise, clicks), works out where one track ends and the next begins, looks the release up on MusicBrainz to get the real tracklist, per-side structure and cover art, and encodes tagged FLACs — one side at a time or a whole multi-disc album in one run. It is a desktop app built for the actual shape of the job, which is why it asks you where a split goes when it genuinely cannot tell, instead of guessing and quietly mangling a track.

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

- **Recording.** Capture straight from any input device — turntable into a preamp into a line-in, or a USB interface — with live stereo level meters, peak-hold ticks and a clip indicator that *latches*, so you can set your input gain before you commit and still see afterwards that something clipped. Captures stream to disk (a side never sits in RAM), record to local staging and move to your folder on stop, and say so loudly if the audio has a dropout. Naming is side-aware: the next-file field pre-fills `SideA.wav` and advances to `SideB.wav` after each stop, because the thing you do between takes is flip the record, not type. Capture runs at whatever rate the device is really set to and the 44.1k conversion happens at encode, so there is no rate to pin; a full-width input-gain fader sits beneath the meters with its own level ribbon; the exact file the next take will be written to is shown before you press Record; and naming the release up front carries the album's identity across to Full Rip with the audio.
- **Restoration pipeline.** An ordered chain of independent stages — rumble filter (zero-phase subsonic high-pass), mains-hum removal (notch at the mains frequency and its harmonics), spectral-gating noise reduction profiled from the lead-in groove, and click/pop removal. Everything between stages is held as float, so a filter that overshoots full scale is not hard-clipped on the way to the next stage; the single conversion back to the source bit depth happens at the very end, with a headroom check that attenuates rather than clips.
- **Duration-anchored splitting.** Given the expected track durations from the release, the splitter predicts roughly where each gap should fall, searches a window around that prediction for the real silence, and re-anchors the next prediction on the gap it just confirmed — so turntable speed error, unknown lead-out deadspace and approximate CD-sourced durations never accumulate down the side. **When a window contains no convincing silence — a segue, a crossfade, a genuinely gapless transition — it does not invent a split. It hands you that one gap, with the search window highlighted on the waveform, and asks you to place it; the rest of the side is still resolved automatically.**
- **MusicBrainz lookup.** Search a release, pick the right pressing (vinyl is ranked above CD and studio albums above compilations, so the 1959 LP beats a later best-of), and pull down the per-side tracklist, track durations, MusicBrainz IDs and the front cover from the Cover Art Archive.
- **Album orchestration.** Point it at the folder of side WAVs, map each file to its side, and the whole record goes through in one run — sides analyse in the background while you review the one in front of you, and accepting a side starts its encode immediately. Tracks land flat in one output folder, numbered continuously across the album, while the *tags* keep per-side TRACKNUMBER/DISCNUMBER (the Picard vinyl convention). A side you just recorded appears in the mapping table on its own, already assigned to its side — record A, flip, record B, and the album job is mapped without you touching it. And a finished album shows a receipt: sides, sizes, warnings, and a link to the output folder.
- **Export to MP3.** A copy for the phone, the car or the gym, made from the FLACs without touching them — V0 by default, 320 CBR for devices that dislike VBR, V2 for when space is tight. Tags and cover art carry across into ID3, MusicBrainz IDs included, using Picard's spelling so other taggers find them.
- **One pipeline, not five tools.** The tabs run left to right in the order you actually work — Record, Full Rip, Convert, Re-tag, Settings — the app opens on Record, and a one-line status strip along the bottom says what is happening right now in plain words, turning amber or red when something wants you.
- **Network-share friendly.** Rips usually live on a NAS. Every operation stages through a local temp directory — copy in, work locally, write results back — and cleans up after itself even on failure or cancellation.

## Installation

**Download it.** Grab the release zip, extract it anywhere, and run `RippedRecordFormatter.exe`. No Python, no ffmpeg, no installer — ffmpeg is bundled inside, and nothing is written outside the folder you extract.

The build is not code-signed, so Windows SmartScreen will warn you the first time: click *More info* → *Run anyway*. The download is around 188 MB and extracts to about 470 MB, most of which is ffmpeg and Qt.

It is unsigned because code-signing economics are absurd for a personal tool — a certificate costs more per year than this project will ever cost to run. Verify your download against the **SHA-256 published with each release** instead: `Get-FileHash RippedRecordFormatter-<version>-win64.zip -Algorithm SHA256`, and check it matches the hash in the release notes.

**Or run it from source.** Requires **Python 3.14**.

```bash
git clone https://github.com/Conrad451/ripped-record-formatter.git
cd ripped-record-formatter
python -m venv .venv
.venv\Scripts\activate        # Windows;  source .venv/bin/activate on Unix
pip install -r requirements.txt
python app.py
```

From source, ffmpeg is fetched automatically on first use — see [Runtime requirements](build.md#runtime-requirements). To build the standalone bundle yourself, see [build.md](build.md).

## Usage

The tabs are the pipeline, left to right: **Record → Full Rip → Convert → Re-tag → Settings**. The app opens on **Record**, at the beginning of the story. If you already have the WAVs, start one tab along at **Full Rip**.

Along the bottom, a one-line status strip says what the app is doing right now — *"Recording SideB — 2:14, peaks −8.1"*, *"Encoding — 3 of 5 tracks"*, *"Ready"* — and turns amber or red if something needs your attention. **Show details** opens the full log when you want it; everything is recorded either way.

1. **Record the sides** (optional). In the **Record** tab, pick your input, optionally look up the release so the sides are named and the identity travels with them, watch the meters while you set gain on the fader beneath them, and press Record. The exact file the next take will be written to is shown under **Recordings save to:**, so where it lands is never a guess. Stop at the end of the side, flip the record, press Record again — the file name advances from `SideA.wav` to `SideB.wav` on its own, and each finished side appears in the Full Rip mapping table already assigned. When the record is done, **Done recording — process this album** takes you across to Full Rip with the session in hand. *Or start from an existing folder of WAVs:* in **Full Rip**, select the folder holding this record's side WAVs. One row appears per WAV found. Set the side for each file that belongs to this record and leave the rest on **— skip —** — a folder can hold more than one album, and anything the app isn't sure about it leaves alone rather than guessing.
2. **Look up the release.** The preview shows the cover art — or warns you, loudly, if the release has none, while you can still do something about it.
3. **Press Start album.** Sides analyse in the background: restoration runs, then the splitter proposes cut points.
4. **Click a ready side to review it.** Adjust the split markers, and edit titles or per-track artists directly in the table.
5. **Press Accept side.** The side is cut, tagged and starts encoding immediately — while you get on with reviewing the next one.
6. **Collect your album.** Every track from every side lands flat in your output folder, numbered continuously.

A single WAV is just a one-row mapping table — use **Add single WAV…** instead of selecting a folder; everything after that is identical.

**Convert** and **Re-tag** are the simpler tabs for WAVs that are already one-file-per-track, or FLACs that just need their tags rewritten. Convert also carries **Export to MP3** for making a copy for a phone or a car; Re-tag has its own release lookup, so a folder of untagged FLACs can be given a tracklist without leaving the tab.

## Setting up your turntable

Getting a record into the computer needs one amplifier between the turntable and the PC — **exactly one**, and that is the thing people most often get wrong.

- **One preamp, not two.** A record needle puts out a tiny "phono" signal that has to be boosted to normal "line" level before recording. That boost must happen *once* — either inside the turntable (many have a PHONO/LINE switch on the back), or in the little USB box you plug into, or in a separate phono preamp — but never two in a row. Two boosts make the sound painfully loud and distorted; none makes it far too quiet. If your turntable has the switch and your USB box also amplifies, set the turntable to **LINE** so only one of them is boosting.
- **A simple USB box works well.** A Behringer **UCA202** or **UFO202** is an inexpensive, well-behaved way to get a turntable into a computer over USB. The UFO202 has a built-in phono preamp and a headphone jack, so it can be the "one preamp" on its own.
- **You do not need to set the sample rate.** Capture happens at whatever rate your device is already set to in Windows — 48,000 or even 192,000 Hz is fine — and your FLACs are saved at 44,100 Hz automatically. **Check my setup** says so rather than nagging you to change anything. (The library rate is configurable under **Settings**, including "keep source" if you would rather not resample at all.)
- **Then set the volume.** Play the loudest song on the record and drag the input-gain fader until the peaks sit just below the −3 dBFS mark — then you're ready to record.

## Recording

An appliance, not an editor. Beyond choosing your input once and pressing Record, the whole interface is **level awareness** and **file naming** — the two things that quietly ruin a rip. No live waveform, no editing; monitoring is optional and off by default (see below).

The meters run whenever the Record tab is open — which, since it is the tab the app opens on, means from the moment you start it — so you set gain against real bars before you commit to a take. The **input gain fader** sits directly beneath them, full width, with its own level ribbon showing what the knob is producing: drag until the ribbon sits just under the −3 dBFS mark. (The mark is on the ribbon, not on the fader's own scale, because which knob position gives you −3 dBFS depends entirely on how hot your source is.) Each channel gets a **substantial bar with its own numbers beside it** — the level right now in dBFS to one decimal, and that channel's max-hold — because a single shared "max" cannot tell you *which* side is hot, which is the thing you need in order to do anything about it. Under the bars runs a **calibrated rule** marked 0 / −3 / −6 / −12 / −20 dBFS, with the zone above −3 shaded red on the bar's own track so the ceiling is visible before anything reaches it. The rule is the point: on an honest linear-in-dB scale −7 dBFS genuinely fills 88% of the bar, which looks alarming until you can see it sitting comfortably below the −6 and −3 marks. Under them, a **level history strip** shows the last 30 seconds of peak in **one lane per channel, stacked** — L above R, each on the same shared dBFS scale so a level lands in the same place in both lanes and in the bars above them — against gridlines at 0 / −3 / −6 / −12 / −20 dBFS, with clip events marked in red at the top edge where they persist as the strip scrolls — so the loudest passage of the record is still on screen when you look, rather than something you had to catch in the act. The max peak is stated with the margin it leaves — *“max −4.2 dBFS (4.2 dB headroom)”* — and coloured green, amber or red as that margin runs out. The gain ritual is simply: play the loudest passage of the record, watch the strip and keep peaks below the −3 dBFS line. Clipping **latches** with a count — it is still lit when you look up — and a capture that clipped says so in the log: *"The sound was too loud and distorted in 7 spots. Turn the input volume down and record this side again."* A capture with a dropout is never shipped silently. A **Check my setup** link beside the device picker (and an automatic pass when you first choose a device) inspects the rate, the signal level and whether anything is coming in at all, and says in plain words what to fix. It is diagnostics rather than a step, which is why it sits next to the thing it inspects instead of in the middle of the flow.

**The sample rate takes care of itself.** Windows' WASAPI shared mode records at the rate your device is *actually configured at* — often **192 kHz** on a line input — and it will not let an application ask for a different one. So the app no longer pretends otherwise: the rate box shows what the device really reports (probed, not assumed from a fixed menu), capture runs at that rate, and the **conversion to 44,100 Hz happens at encode time**, where it always could have. There is nothing to pin and nothing to get wrong. The library rate lives under **Settings → Output sample rate** if you want 48,000 or "keep source" instead.

**Input gain, without leaving the app.** A vertical **Input** slider sits beside the meters and drives the Windows capture level for the selected device, so the knob and the bars it moves are in one place. It hides itself when that endpoint can't be reached. One caveat, which the tooltip also states: this adjusts the *Windows* input level — if the signal distorts even at low settings, the distortion is happening upstream and you need to turn the source down, not this.

**Monitoring (optional).** When your listening rig isn't within a headphone cable of the deck, turn on **Monitor** and pick an output device: the input is passed straight through to it so you can hear the record as it captures, with no Windows dialogs. It is off by default and completely independent of the recording — toggling it can never touch the file. The **capture box's own headphone jack is the zero-delay alternative**; the software path adds a little delay (Windows shared-mode audio, around 150 ms here), so it is for convenience, not for playing along in time.

**Turn off Windows' "Listen to this device".** If you have that enabled *and* this monitor on, you are hearing the same signal twice down two paths of different length — which sounds like an echo, or like the channels have come apart. One monitor at a time.

**Judge levels by the meters, not by ear.** The monitor is for hearing that the record is playing, not for deciding whether it is too loud. Two reasons it will understate clipping. It passes the captured samples through **bit-exactly** — no scaling of any kind — but what you hear afterwards goes through your output device's *own* volume, so the monitor's loudness tells you nothing about the recording's level. And with around 150 ms of delay in the path, your ears and the meters are never auditing the same instant anyway. The meters and the clip counter measure the file; your monitor volume knob does not.

**Say what is on the platter, before you play it.** The Record tab has an optional **Album** row: press **Look up release…**, pick the pressing, and the cover appears beside the button. Naming the record *before* the capture rather than after is what lets the identity ride along with the audio — it suggests the output folder (`Artist/Album`), names the sides from the release's own layout rather than a generic `SideA`/`SideB`, and hands the release to Full Rip together with the WAVs, so the album arrives already identified. It is entirely optional and entirely reversible: leave it alone and an anonymous session behaves exactly as it always has, or press **Clear** to drop back to one. Nothing is auto-filled over a folder path you typed yourself.

**"Done recording — process this album."** When a recording lands in Full Rip's mapping table, the bridge button at the bottom of the Record tab arms. Press it and you are taken to **Full Rip** with the session carried over — the sides mapped, the release attached. It is a *bridge, not a trigger*: it moves you to the tab and stops there, with Start album still yours to press. The flow it closes is the one that used to dead-end at Stop: record A, flip, record B, press Done, and you are standing in front of the album job ready to run. Each saved side also says in the log where it went — *"saved SideB.wav (19:42). Loudest point: −4.2 dBFS — mapped to Side B of Songs From the Big Chair in Full Rip."*

**The monitor is a session feature.** Once it is on, it stays on — switching to another tab, or clicking away from the window entirely, does not stop it. Only you, closing the app, or the output device disappearing will. Because that means audio can be running while you are looking at a tab that shows no sign of it, the window title says **— MONITORING** and the Record tab carries a ♪ from wherever you are.

**Record into a running album.** While an album job is running, a side recorded into its folder and assigned a side is admitted straight into the running job's analysis queue — it appears in the side list as queued→analyzing like any other, and the job now waits for it before concluding. Admission is open only while the job has not yet concluded. A finished album is finished: if every side had already reached a terminal state and the job concluded before the recording landed, the late side is simply mapped into the table (exactly as when no job is running) and the log says to press Start album to run again including it — jobs are never held open speculatively on the chance another side is coming. If no job is running at all, a completed recording is only mapped; the app never auto-starts a job the user didn't start. A recording that ended with warnings (a dropout, clipping) still admits normally, and the warning is carried forward into the admitted side's log line. Cancelling the album also cancels any admitted side still in flight.

**Sides map themselves when the answer is knowable.** Mapping re-runs whenever the WAV list or the release changes — including a release looked up *after* you scanned the folder — and only ever fills rows still on skip, so a choice you made by hand is never overwritten. It works down a confidence ladder and never guesses: an explicit side in the filename (`SideA`, `side_2`, a lone `A`); failing that, an unambiguous count-and-order (exactly as many unmapped WAVs as unmapped sides, with sortable names, mapped in order); failing that, a duration match (a WAV within ~5% of exactly one side's expected length, with no closer competitor — shown with a tooltip explaining why). Anything still ambiguous stays on skip for you to place.

**A clean slate between albums.** When an album finishes, the app clears its identity for the next record — the Artist and Album fields, the looked-up release and its cover, the side layout and the mapping table all reset, because no default is safe for identity: inheriting the last album's artist or release would quietly mistag the next one. The source and output folders are the exception — each follows a policy you set under **Settings → Default folders**: keep the last-used folder (the default), reset it to a folder you nominate, or clear it, so a location can persist across records even though identity never does. The finished-album receipt card carries a **Run this album again** button that restores everything just cleared in one click and re-arms Start, so redoing the record you just finished never means re-entering it. And if you are part-way through recording the next side when the album concludes, the clear waits until that capture has landed — a recording in flight is never orphaned by the reset.

## One file of state

Everything the app remembers lives in a single file, **`rrf.db`**, in your
per-user config folder. Settings, the journal of the album you are working on,
releases already fetched, and your collection list. One file: back it up, move
it to a new machine, done.

**It is never the source of truth for your library.** The FLACs and the tags
inside them are that. Everything in the database that describes the world — a
folder path, a finished album — is a *claim about the filesystem*, and where the
two disagree the filesystem wins: an album whose folder has moved shows as
"files not found" rather than insisting it is ripped. Delete `rrf.db` and the
app starts with defaults, an empty ledger, and every one of your FLACs perfectly
intact. You lose preferences, not music.

Your old `settings.json` is read once on first launch, written into the
database, and renamed to `settings.json.migrated` — kept, not deleted, because
at that moment it is the only copy of your preferences.

## If it stops part-way

Turntable time is unrepeatable, so an interrupted rip is worth picking up rather
than starting over. The app writes down what it is doing as it goes, and if it
finds a job still open on the next launch it offers a bar — not a dialog you
have to dismiss before you can do anything:

> You were working on Discovery — Side B needs to be prepared again before
> review. **[Resume] [Discard]**

Sides that finished are left exactly as they are; their files are on disk.
Anything unfinished is prepared again from its WAV, because the working files
were temporary and are genuinely gone — the app will not pretend otherwise. If
a WAV has moved since, it is left out and says so: the files on disk are what
count.

Because the release you looked up is remembered too, resuming brings back the
tracklist and cover art without asking MusicBrainz again — as does re-doing a
side, or running an album a second time.

## Your collection

A list of what you have ripped and what you still mean to. Albums add
themselves when a rip finishes; add a record you own but have not ripped by
hand. Reach it from **Collection** in the bar along the bottom, or from the
link on an album's receipt.

It reconciles against your disk every time you open it, so a record whose folder
you have since moved shows as *files not found* rather than quietly claiming to
be done. No playback, no shelf management, no Discogs — just the answer to
"have I done this one yet?".

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
| `RRF_VERSION` | App version (`core/version.py`) at encode time | Provenance: records which build wrote the file |
| `RRF_RESTORATION` | Stable, parseable summary of the restoration applied to the audio, or `none` | Provenance: records how the audio was restored; format is documented as stable in `core/restoration.format_restoration` |
| Front cover image | Cover Art Archive | Embedded as a FLAC picture block, type 3 (front cover) |

The two `RRF_*` fields are written only when the app *encodes* the audio (Full Rip, plain Convert) — re-tagging carries forward whatever the original encode stamped and never rewrites them, so a re-tag cannot erase or falsify provenance.

## Export to MP3

FLAC is the library; MP3 is a copy for a device. The **Convert** tab has an **Export to MP3** section that turns a folder of FLACs into a folder of tagged MP3s, and **Use the album just finished** points it at the album you have this moment ripped. Sources are never modified, moved or deleted.

Three qualities, because the reason you want an MP3 varies: **V0** (VBR ~245 kbps, the default — transparent for practical purposes and smaller than 320), **320 kbps CBR** (for devices and car decks that get unhappy with VBR headers), and **V2** (VBR ~190 kbps, for when the device is small and the commute is long). Encoding is a direct ffmpeg subprocess per track rather than a decode-to-RAM round trip, so an album of 24-bit sides does not need an album of 24-bit sides' worth of memory.

Tags carry across. A field the FLAC does not have produces no frame at all, never an empty one:

| FLAC (Vorbis comment) | MP3 (ID3v2.4) | Notes |
| --- | --- | --- |
| `TITLE` | `TIT2` | |
| `ARTIST` | `TPE1` | |
| `ALBUM` | `TALB` | |
| `ALBUMARTIST` | `TPE2` | |
| `DATE` | `TDRC` | Year as-is |
| `TRACKNUMBER` + `TRACKTOTAL` | `TRCK` | Packed as `N/T` |
| `DISCNUMBER` + `DISCTOTAL` | `TPOS` | Packed as `N/T` |
| `MUSICBRAINZ_ALBUMID` | `TXXX:MusicBrainz Album Id` | Picard's spelling, so Picard and beets find them |
| `MUSICBRAINZ_ARTISTID` | `TXXX:MusicBrainz Artist Id` | |
| `MUSICBRAINZ_RECORDINGID` | `TXXX:MusicBrainz Recording Id` | |
| `MUSICBRAINZ_TRACKID` | `TXXX:MusicBrainz Release Track Id` | |
| `RRF_VERSION` | `TXXX:RRF_VERSION` | Provenance travels with the copy |
| `RRF_RESTORATION` | `TXXX:RRF_RESTORATION` | |
| Front cover picture | `APIC` type 3 | Description "front cover" |

`TRACKTOTAL`/`DISCTOTAL` are also read as `TOTALTRACKS`/`TOTALDISCS`, which is how some other taggers spell them — the source folder is whatever you point at, not necessarily something this app wrote. A total with no corresponding number is dropped rather than written as `/5`.

## Re-tagging: the tagging stage, pointed at FLACs you already have

**Re-tag writes everything Full Rip writes.** It is the pipeline's tagging stage retargeted at an existing library, which is how a folder of pre-app rips becomes first-class rather than something the good tools cannot reach.

Choose a folder and it loads — no separate Load step. If the folder sits under your FLAC root, Artist and Album are read back out of `{FLAC root}/{Artist}/{Album}` and offered, never forced, and never over something you typed.

**Look up release…** is on the tab, and the selection feeds everything below it: titles, album artist, date, and the MusicBrainz IDs for each track.

**Define sides…** splits a flat folder into sides using the same partition editor Full Rip uses. Track numbers then restart on each side, TRACKTOTAL is that side's count, and DISCNUMBER/DISCTOTAL follow — the Picard vinyl convention, identical to an album rip. Your filename convention setting governs the renames here too (`[A01]` per side, or `[01]` continuous), composed with the prefix strip so a file already called `[01] - Song` re-stamps once rather than twice.

**The table is the preview of the write.** Every field that will land in the file is a column: number, title, artist, album, album artist, date, disc, and whether MusicBrainz IDs are going in. The ones you can type are editable; the ones that are derived (the number, the disc) or identifying (the IDs) are shown read-only, because they are answers to questions asked elsewhere — an ID changes by choosing a different release, not by typing over hex digits.

**Apply to all** saves typing the same album artist fourteen times: right-click a cell in Artist, Album, Album Artist or Date and give every row that value. Deliberately not offered on Title or the number — those are per-track by definition.

Provenance is never touched. `RRF_VERSION` and `RRF_RESTORATION` are read from the source and carried forward unchanged, so a re-tag can never erase or falsify how a file was made.

## Cover art you supply yourself

Plenty of records have nothing in the Cover Art Archive — private presses, reissues, anything obscure. Everywhere the app says it has no art, it now offers **Choose cover image…**: the Re-tag tab, Full Rip's release row, the Record tab's album row, and the lookup dialog. Pick a JPEG or PNG and it is embedded exactly like fetched art.

Two sanity limits, both stated when they bite: **10 MB** and **5000×5000**. A cover is embedded in *every* track, so a large scan multiplies across the album, and nothing displays a cover bigger than that. Files over either limit are refused rather than silently resized — re-encoding your artwork behind your back is not the app's business.

## Roadmap & known limits

- **Gapless sides need you.** A side mixed as a continuous piece — segues, crossfades, live recordings with applause bridging tracks — has no silence to find. The splitter will tell you which gaps it could not resolve and let you place them by hand, but it will not place them for you. This is deliberate: a wrong automatic cut in the middle of a track is worse than being asked.
- **Defaults are conservative.** Noise reduction in particular is tuned to under-process rather than risk the gurgling artefacts of an aggressive spectral gate. If your pressing is rough, turn it up in **Settings** — every threshold, cutoff and weight is exposed there rather than buried in the code.
- **Recording is Windows-first in practice.** The capture layer is PortAudio (via `sounddevice`) and is not OS-specific, but the device quirks documented above — and the testing — are Windows/WASAPI.
- **The build is not code-signed.** Windows SmartScreen warns on first run until it is.
- **No command-line interface.** The app is GUI-only for now; `core/` is deliberately UI-agnostic so a CLI can be added without touching the logic.

## License

[GNU AGPLv3](LICENSE). In plain English: you can use, modify and share this freely, but if you distribute it — or run a modified version as a network service — you have to make your source available under the same terms.

## Acknowledgments

Release metadata comes from [MusicBrainz](https://musicbrainz.org/) and cover art from the [Cover Art Archive](https://coverartarchive.org/). Lookups use MusicBrainz's free service under [its terms of use](https://musicbrainz.org/doc/MusicBrainz_API), which ask that clients identify themselves and stay within one request per second — this app throttles itself accordingly. **If you do a lot of lookups, set your own contact in Settings** ("MusicBrainz contact"): it tells MusicBrainz who to reach about your traffic, rather than leaving it anonymous, and it is the courteous thing to do when you are leaning on somebody's free service. Audio decoding and click removal lean on [ffmpeg](https://ffmpeg.org/). The app stands on [PySide6](https://doc.qt.io/qtforpython/) (Qt), [NumPy](https://numpy.org/), [SciPy](https://scipy.org/), [soundfile](https://python-soundfile.readthedocs.io/), [noisereduce](https://github.com/timsainb/noisereduce), [mutagen](https://mutagen.readthedocs.io/), [pydub](https://github.com/jiaaro/pydub), [musicbrainzngs](https://python-musicbrainzngs.readthedocs.io/), and [pyqtgraph](https://www.pyqtgraph.org/).
