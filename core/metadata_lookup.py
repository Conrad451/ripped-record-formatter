"""Online album-metadata lookup, shaped as a swappable *provider*.

This module lets the app enrich a rip with real release metadata -- the proper
tracklist, per-side structure, durations, and front cover art -- looked up from
an online database. It is deliberately an *enhancement*: every network call has
a timeout and raises a small set of typed errors, and nothing in the core
conversion path depends on it. If the network is down, lookups fail cleanly and
the rest of the app keeps working.

Design
------
The public surface is the :class:`MetadataProvider` abstract interface::

    provider.search_releases(artist, album) -> list[ReleaseResult]
    provider.get_release(release_id)        -> ReleaseDetail

:class:`ReleaseResult` carries just enough to disambiguate one pressing from
another in a results table (title, artist, year, country, format), so a user can
tell the 1959 vinyl from a later CD reissue. :class:`ReleaseDetail` carries the
full per-medium tracklist plus optional cover-art bytes.

:class:`MusicBrainzProvider` is the only implementation today. A Discogs provider
can slot in behind the same interface later without touching callers.

Terms of service
----------------
MusicBrainz *requires* a descriptive ``User-Agent`` (application name, version,
and contact) and rate-limits anonymous clients to one request per second. Both
are enforced here (see :class:`MusicBrainzProvider`).
"""

from __future__ import annotations

import socket
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator, Sequence

# ---------------------------------------------------------------------------
# Typed errors -- callers catch MetadataError (or a subclass) and degrade.
# ---------------------------------------------------------------------------


class MetadataError(Exception):
    """Base class for every failure raised out of this module."""


class MetadataNetworkError(MetadataError):
    """A network-level failure: timeout, DNS, connection refused, etc.

    Signals *try again later*; it is never the caller's fault.
    """


class MetadataResponseError(MetadataError):
    """The service responded but the request could not be satisfied.

    Covers a missing release (404), a malformed response, or a bad id.
    """


class MetadataConfigError(MetadataError):
    """The provider was used before it was configured correctly.

    For MusicBrainz this means the required User-Agent was not set.
    """


# ---------------------------------------------------------------------------
# Value objects -- plain, provider-agnostic data the GUI and callers consume.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseResult:
    """One search hit -- the minimum needed to pick the right pressing.

    ``release_id`` is opaque and provider-specific; pass it back to
    :meth:`MetadataProvider.get_release`.
    """

    release_id: str
    title: str
    artist: str
    year: str = ""          # "" when unknown; first 4 chars of the date
    country: str = ""       # e.g. "US", "GB", "XW" (worldwide)
    formats: str = ""       # e.g. "Vinyl", "2xVinyl", "CD"
    track_count: int = 0
    disambiguation: str = ""  # MusicBrainz free-text note, if any
    primary_type: str = ""    # release-group primary type ("Album", "Single", ...)
    secondary_types: tuple[str, ...] = ()  # e.g. ("Compilation", "Live")

    @property
    def is_vinyl(self) -> bool:
        return "vinyl" in self.formats.lower()

    @property
    def is_compilation(self) -> bool:
        return any(t.lower() == "compilation" for t in self.secondary_types)

    def label(self) -> str:
        """A one-line human summary for a results row / log line."""
        bits = [self.title, self.artist]
        tail = ", ".join(b for b in (self.year, self.country, self.formats) if b)
        if tail:
            bits.append(f"({tail})")
        if self.disambiguation:
            bits.append(f"[{self.disambiguation}]")
        return " - ".join(bits)


@dataclass(frozen=True)
class TrackInfo:
    """A single track within a release's tracklist."""

    position: int             # 1-based position within its medium
    number: str               # printed track number ("A1", "3", ...)
    title: str
    length_ms: int | None = None      # duration in milliseconds, if known

    def length_display(self) -> str:
        """``m:ss`` for the duration, or ``""`` when unknown."""
        if self.length_ms is None:
            return ""
        total_seconds = round(self.length_ms / 1000)
        minutes, seconds = divmod(total_seconds, 60)
        return f"{minutes}:{seconds:02d}"


@dataclass(frozen=True)
class MediumInfo:
    """One physical medium (a vinyl side, a disc) and its tracks."""

    position: int             # 1-based medium/side index
    format: str = ""          # "Vinyl", "CD", ...
    title: str = ""           # medium subtitle, if any
    tracks: tuple[TrackInfo, ...] = ()


@dataclass(frozen=True)
class CoverArt:
    """Front cover image bytes plus its MIME type."""

    data: bytes
    mime: str                 # "image/jpeg", "image/png", ...


@dataclass(frozen=True)
class ReleaseDetail:
    """Full detail for a chosen release: metadata, tracklist, and cover art."""

    release_id: str
    title: str
    artist: str
    year: str = ""
    country: str = ""
    media: tuple[MediumInfo, ...] = ()
    cover: CoverArt | None = None

    @property
    def tracks(self) -> list[TrackInfo]:
        """Every track across all media, flattened into playing order."""
        return [t for medium in self.media for t in medium.tracks]

    @property
    def track_count(self) -> int:
        return sum(len(m.tracks) for m in self.media)


# ---------------------------------------------------------------------------
# Provider interface.
# ---------------------------------------------------------------------------


class MetadataProvider(ABC):
    """A source of album metadata (MusicBrainz today, Discogs later).

    Implementations must enforce whatever rate limits and identification their
    service's terms require, and must raise only :class:`MetadataError`
    subclasses out of the two public methods.
    """

    #: Short human name for the provider, for UI/logging.
    name: str = "provider"

    @abstractmethod
    def search_releases(self, artist: str, album: str, *, limit: int = 25) -> list[ReleaseResult]:
        """Return candidate releases matching ``artist`` + ``album``.

        Returns an empty list when nothing matches -- that is *not* an error.
        Raises :class:`MetadataNetworkError` / :class:`MetadataResponseError`
        on failure.
        """

    @abstractmethod
    def get_release(self, release_id: str, *, with_cover: bool = True) -> ReleaseDetail:
        """Return the full tracklist (+ optional cover art) for one release.

        Raises :class:`MetadataResponseError` if the id is unknown/malformed and
        :class:`MetadataNetworkError` on a network failure. Absent cover art is
        *not* an error -- ``detail.cover`` is simply ``None``.
        """


# ---------------------------------------------------------------------------
# Rate limiting -- shared, injectable, testable.
# ---------------------------------------------------------------------------


class RateLimiter:
    """Serialises calls so consecutive ones are >= ``min_interval`` apart.

    Thread-safe: the GUI runs lookups on background threads, so several may hit
    the limiter at once. A lock is held across the wait, which means calls are
    also serialised (one in flight at a time) -- exactly what a 1 req/s budget
    wants. The clock and sleep function are injectable so tests can assert the
    spacing without real wall-clock delays.
    """

    def __init__(
        self,
        min_interval: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval = float(min_interval)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        """Block until it is permissible to make the next request."""
        with self._lock:
            now = self._clock()
            wait = self._next_allowed - now
            if wait > 0:
                self._sleep(wait)
                now = now + wait
            self._next_allowed = now + self._min_interval


# ---------------------------------------------------------------------------
# MusicBrainz implementation.
# ---------------------------------------------------------------------------


def _sniff_mime(data: bytes) -> str:
    """Best-effort image MIME from magic bytes; defaults to JPEG."""
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] in (b"GIF8",):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


@contextmanager
def _socket_timeout(timeout: float) -> Iterator[None]:
    """Temporarily set the default socket timeout, then restore it.

    musicbrainzngs and the Cover Art Archive both make their HTTP calls through
    :mod:`urllib`, which honours the process-wide default socket timeout when a
    request does not specify its own. Setting it around each call is the one
    lever that reliably bounds *every* network operation this client makes.
    Restoring the previous value keeps us from stomping on the rest of the app.
    """
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        yield
    finally:
        socket.setdefaulttimeout(previous)


class MusicBrainzProvider(MetadataProvider):
    """MusicBrainz + Cover Art Archive, via :mod:`musicbrainzngs`.

    A descriptive User-Agent is mandatory (MusicBrainz rejects anonymous
    clients) and requests are throttled to one per second -- both required by
    the MusicBrainz web-service terms of use. Every call is bounded by
    ``timeout`` seconds and raises only :class:`MetadataError` subclasses.
    """

    name = "MusicBrainz"

    def __init__(
        self,
        app_name: str = "RippedRecordFormatter",
        app_version: str = "2.0",
        contact: str = "https://github.com/Conrad451/ripped-record-formatter",
        *,
        timeout: float = 15.0,
        rate_limiter: RateLimiter | None = None,
        client=None,
    ) -> None:
        # ``client`` injection keeps the network library out of unit tests.
        if client is None:
            import musicbrainzngs as client  # local import: optional dependency
        self._mb = client
        self._timeout = float(timeout)
        self._limiter = rate_limiter or RateLimiter(1.0)

        if not contact:
            raise MetadataConfigError(
                "MusicBrainz requires a contact (URL or email) in the User-Agent."
            )
        # ToS requirement: identify the application. Raised as our error type so
        # callers never see a musicbrainzngs-specific exception.
        try:
            self._mb.set_useragent(app_name, app_version, contact)
        except self._mb.UsageError as exc:  # pragma: no cover - defensive
            raise MetadataConfigError(str(exc)) from exc
        # Belt-and-suspenders: also arm the library's own throttle at 1 req/s.
        self._mb.set_rate_limit(1.0, 1)

    # -- network plumbing ---------------------------------------------------
    def _call(self, func: Callable, *args, **kwargs):
        """Run one throttled, timeout-bounded network call, mapping errors."""
        self._limiter.acquire()
        try:
            with _socket_timeout(self._timeout):
                return func(*args, **kwargs)
        except self._mb.NetworkError as exc:
            raise MetadataNetworkError(str(exc)) from exc
        except self._mb.ResponseError as exc:
            raise MetadataResponseError(str(exc)) from exc
        except self._mb.WebServiceError as exc:
            # AuthenticationError / anything else web-service related.
            raise MetadataResponseError(str(exc)) from exc
        except (socket.timeout, TimeoutError, OSError) as exc:
            raise MetadataNetworkError(str(exc)) from exc

    # -- MetadataProvider ---------------------------------------------------
    def search_releases(self, artist: str, album: str, *, limit: int = 25) -> list[ReleaseResult]:
        artist = (artist or "").strip()
        album = (album or "").strip()
        if not artist and not album:
            return []
        fields: dict[str, str] = {}
        if artist:
            fields["artist"] = artist
        if album:
            fields["release"] = album
        data = self._call(self._mb.search_releases, limit=limit, **fields)
        releases = data.get("release-list", []) if isinstance(data, dict) else []
        results = [_parse_search_release(r) for r in releases]
        # This is a vinyl tool: surface the pressing the user actually wants.
        # Studio albums outrank compilations (the reported failure mode), and
        # vinyl outranks CD within the same album type. sorted() is stable, so
        # MusicBrainz's own relevance order breaks any remaining ties.
        results.sort(key=_rank_key)
        return results

    def get_release(self, release_id: str, *, with_cover: bool = True) -> ReleaseDetail:
        if not release_id:
            raise MetadataResponseError("empty release id")
        data = self._call(
            self._mb.get_release_by_id,
            release_id,
            includes=["recordings", "artist-credits", "media"],
        )
        release = data.get("release") if isinstance(data, dict) else None
        if not release:
            raise MetadataResponseError(f"no release found for id {release_id!r}")
        cover = self._fetch_cover(release_id) if with_cover else None
        return _parse_release_detail(release, cover)

    def _fetch_cover(self, release_id: str) -> CoverArt | None:
        """Fetch the front cover, returning ``None`` when there is no art.

        A missing image surfaces from musicbrainzngs as a ``ResponseError``
        (HTTP 404); that is an expected, non-fatal outcome -- not every release
        has cover art -- so we swallow it and return ``None``. Network failures
        still propagate as :class:`MetadataNetworkError`.
        """
        try:
            data = self._call(self._mb.get_image_front, release_id)
        except MetadataResponseError:
            return None
        if not data:
            return None
        return CoverArt(data=bytes(data), mime=_sniff_mime(bytes(data)))


# ---------------------------------------------------------------------------
# Parsing helpers -- isolate the messy musicbrainzngs dict shapes here.
# ---------------------------------------------------------------------------


def _artist_from_credit(entry: dict) -> str:
    """Extract a display artist from a musicbrainzngs release/entry dict."""
    phrase = entry.get("artist-credit-phrase")
    if phrase:
        return phrase
    parts: list[str] = []
    for credit in entry.get("artist-credit", []):
        if isinstance(credit, str):
            parts.append(credit)              # a join phrase like " & "
        elif isinstance(credit, dict):
            artist = credit.get("artist", {})
            parts.append(artist.get("name", ""))
    joined = "".join(parts).strip()
    return joined or "Unknown Artist"


def _formats_from_media(media: Sequence[dict]) -> str:
    """Summarise media formats, e.g. two vinyl discs -> ``"2xVinyl"``."""
    counts: dict[str, int] = {}
    order: list[str] = []
    for medium in media:
        fmt = medium.get("format")
        if not fmt:
            continue
        if fmt not in counts:
            order.append(fmt)
        counts[fmt] = counts.get(fmt, 0) + 1
    pieces = []
    for fmt in order:
        n = counts[fmt]
        pieces.append(f"{n}x{fmt}" if n > 1 else fmt)
    return " + ".join(pieces)


def _rank_key(r: ReleaseResult) -> tuple[int, int]:
    """Sort key: studio albums before compilations, then vinyl before CD.

    Album type dominates (picking the right *release* matters most -- a
    compilation ranking first was the reported bug); format is the tiebreak so a
    vinyl pressing of the wanted album beats its CD.
    """
    return (1 if r.is_compilation else 0, 0 if r.is_vinyl else 1)


def _parse_search_release(r: dict) -> ReleaseResult:
    media = r.get("medium-list", []) or []
    track_count = 0
    for medium in media:
        tc = medium.get("track-count")
        if tc is not None:
            try:
                track_count += int(tc)
            except (TypeError, ValueError):
                pass
    date = r.get("date", "") or ""
    group = r.get("release-group", {}) or {}
    primary_type = group.get("type") or group.get("primary-type") or ""
    secondary_types = tuple(group.get("secondary-type-list", []) or ())
    return ReleaseResult(
        release_id=r.get("id", ""),
        title=r.get("title", "") or "",
        artist=_artist_from_credit(r),
        year=date[:4],
        country=r.get("country", "") or "",
        formats=_formats_from_media(media),
        track_count=track_count,
        disambiguation=r.get("disambiguation", "") or "",
        primary_type=primary_type,
        secondary_types=secondary_types,
    )


def _parse_length(track: dict) -> int | None:
    """Track duration in ms: prefer the track length, fall back to recording."""
    raw = track.get("length")
    if raw in (None, ""):
        raw = track.get("recording", {}).get("length")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _parse_track(track: dict, index: int) -> TrackInfo:
    recording = track.get("recording", {}) or {}
    title = track.get("title") or recording.get("title") or ""
    try:
        position = int(track.get("position", index))
    except (TypeError, ValueError):
        position = index
    number = str(track.get("number", position))
    return TrackInfo(
        position=position,
        number=number,
        title=title,
        length_ms=_parse_length(track),
    )


def _parse_release_detail(release: dict, cover: CoverArt | None) -> ReleaseDetail:
    media: list[MediumInfo] = []
    for m_index, medium in enumerate(release.get("medium-list", []) or [], start=1):
        tracks = tuple(
            _parse_track(t, i)
            for i, t in enumerate(medium.get("track-list", []) or [], start=1)
        )
        try:
            m_position = int(medium.get("position", m_index))
        except (TypeError, ValueError):
            m_position = m_index
        media.append(
            MediumInfo(
                position=m_position,
                format=medium.get("format", "") or "",
                title=medium.get("title", "") or "",
                tracks=tracks,
            )
        )
    date = release.get("date", "") or ""
    return ReleaseDetail(
        release_id=release.get("id", ""),
        title=release.get("title", "") or "",
        artist=_artist_from_credit(release),
        year=date[:4],
        country=release.get("country", "") or "",
        media=tuple(media),
        cover=cover,
    )
