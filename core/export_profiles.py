"""What the app can export a FLAC *to*, as data rather than as code paths.

FLAC is the library. An export is a copy for somewhere the library cannot go,
which is a much smaller set of places than it used to be: Navidrome now streams
FLAC to every device the stakeholder owns, CarPlay included, so the lossy
formats that once justified themselves are answering a question nobody is
asking any more.

So the menu is deliberately two entries, not five:

* **ALAC/M4A** -- the Apple-native lossless bridge. iTunes, Files, somebody
  else's Mac. Lossless, so it is a *copy* of the library rather than a
  degradation of it.
* **WAV 16/44.1** -- the universal escape hatch. CD burning, hardware old
  enough to predate every codec in this file.

MP3 stays because it already shipped, as one profile family rather than a
special case in the code.

**AAC and Opus are designed and not built.** Both encoders are present in the
bundled ffmpeg (verified), so adding either is data entry: append an
:class:`ExportProfile` to :data:`PROFILES` below and it inherits the encoder
check, the batching, the tag strategy dispatch and -- this is the important one
-- the decode invariant, because none of those are per-format code. Nothing else
needs touching. That is the whole reason this file is a table.

A profile that skipped the decode check would have to be expressible, and it is
not: verification happens in the shared export path, keyed off the profile, with
no per-profile hook that could opt out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Tag strategies. The container decides how metadata is written, not the codec.
TAG_ID3 = "id3"          # MP3: ID3v2.4 frames
TAG_MP4 = "mp4"          # M4A: MP4 atoms (©nam/©ART/aART/©alb/trkn/disk/©day/covr)
TAG_NONE = "none"        # WAV: nothing, honestly


@dataclass(frozen=True)
class ExportProfile:
    """One thing a FLAC can become."""

    key: str
    label: str
    suffix: str
    #: ffmpeg encoder name, verified before a batch runs. Empty means none is
    #: needed (a raw PCM muxer is not an optional component).
    encoder: str
    #: Codec arguments, before the destination.
    args: tuple[str, ...]
    tag_strategy: str
    #: Shown in the UI when the format costs the user something.
    caveat: str = ""
    #: Named quality variants, for formats that have them. The default is first.
    variants: dict[str, tuple[str, ...]] = field(default_factory=dict)
    variant_labels: dict[str, str] = field(default_factory=dict)

    def encode_args(self, variant: str = "") -> tuple[str, ...]:
        """Codec arguments for this profile, plus the chosen variant's."""
        if not self.variants:
            return self.args
        chosen = variant or self.default_variant
        if chosen not in self.variants:
            raise ValueError(
                f"Unknown {self.label} quality {chosen!r}. "
                f"Expected one of: {', '.join(self.variants)}.")
        return self.args + self.variants[chosen]

    @property
    def default_variant(self) -> str:
        return next(iter(self.variants), "")

    def output_name(self, source) -> str:
        from pathlib import Path

        return Path(source).stem + self.suffix


MP3 = ExportProfile(
    key="mp3",
    label="MP3",
    suffix=".mp3",
    encoder="libmp3lame",
    args=("-codec:a", "libmp3lame"),
    tag_strategy=TAG_ID3,
    caveat="Lossy. Fine for a phone; not a copy of your library.",
    variants={
        "V0": ("-q:a", "0"),        # VBR ~245 kbps
        "320": ("-b:a", "320k"),    # CBR, for decks unhappy with VBR headers
        "V2": ("-q:a", "2"),        # VBR ~190 kbps
    },
    variant_labels={
        "V0": "V0 (VBR ~245 kbps, default)",
        "320": "320 kbps CBR",
        "V2": "V2 (VBR ~190 kbps)",
    },
)

ALAC = ExportProfile(
    key="alac",
    label="ALAC (Apple Lossless)",
    suffix=".m4a",
    encoder="alac",
    # No bitrate to choose: lossless is lossless. -vn keeps the FLAC's picture
    # out of the audio pass; it is re-attached as a proper cover atom after.
    args=("-codec:a", "alac", "-vn"),
    tag_strategy=TAG_MP4,
    caveat="Lossless — same audio as your FLAC, in the container Apple devices "
           "expect.",
)

WAV = ExportProfile(
    key="wav",
    label="WAV (16-bit / 44.1 kHz)",
    suffix=".wav",
    # No encoder to verify: PCM is a muxer, not an optional build component.
    encoder="",
    args=("-codec:a", "pcm_s16le", "-ar", "44100", "-vn"),
    tag_strategy=TAG_NONE,
    caveat="No tags and no cover art — WAV has nowhere honest to put them. "
           "For CD burning and hardware that predates everything else.",
)

#: The menu, in the order it is offered. Append here to add a format.
PROFILES: tuple[ExportProfile, ...] = (ALAC, WAV, MP3)

_BY_KEY = {profile.key: profile for profile in PROFILES}

DEFAULT_PROFILE = ALAC.key


def get(key: str) -> ExportProfile:
    """The profile named ``key``. Raises rather than guessing at a default."""
    try:
        return _BY_KEY[key]
    except KeyError:
        raise ValueError(
            f"Unknown export format {key!r}. "
            f"Expected one of: {', '.join(_BY_KEY)}.") from None
