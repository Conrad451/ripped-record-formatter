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

The contact is the *user's*, not the maintainer's -- see :func:`user_agent`.
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator, Sequence
from urllib.parse import urlsplit

from core.version import __version__

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

    For MusicBrainz this means the client library rejected our User-Agent.
    """


class MetadataSecurityError(MetadataError):
    """A fetch was refused because the transport would have been unsafe.

    Raised when a redirect tries to move us off HTTPS. Never swallowed: an
    https -> http downgrade on the cover-art path is either an attacker or a
    service regression, and both deserve to be seen.
    """


# ---------------------------------------------------------------------------
# Identity -- who this traffic says it is.
# ---------------------------------------------------------------------------
#
# MusicBrainz asks API clients to identify themselves with a contact address so
# they can reach whoever is generating the traffic. That contact must therefore
# be the *user's*, not the maintainer's: shipping the maintainer's URL as the
# contact for every download of this app would name one person as responsible
# for strangers' request volume, which is neither accurate nor fair.
#
# So: a configured contact is used verbatim (after sanitising). An unconfigured
# one produces a string that is honest about being nobody -- the repository URL
# stays, but only as *provenance* ("this is what the app is"), explicitly not as
# a contact.

APP_NAME = "RippedRecordFormatter"
SOURCE_URL = "github.com/Conrad451/ripped-record-formatter"
UNCONFIGURED_CONTACT = f"unconfigured; source: {SOURCE_URL}"

CONTACT_NUDGE = (
    "Tip: set a MusicBrainz contact in Settings -- your lookups currently "
    "identify only the app."
)

#: Longest contact we will put in a header. A contact is an email or a URL; a
#: kilobyte of one is a mistake or an attack, and either way it is not going out
#: over the wire on the user's behalf.
MAX_CONTACT_LENGTH = 120


def sanitize_contact(contact: str, *, max_length: int = MAX_CONTACT_LENGTH) -> str:
    """Reduce a user-supplied contact to something safe to put in a header.

    This value comes from a config file and ends up inside an HTTP request
    header, so a bare newline in it would be *header injection* -- the classic
    ``foo\\r\\nX-Evil: 1`` splitting one header into two. We do not escape; we
    drop. Anything outside printable ASCII goes (headers are latin-1 on the wire
    and a control character has no business in a contact address), runs of
    whitespace collapse to one space, and the result is capped.

    Returns ``""`` for anything that sanitises down to nothing -- which callers
    treat exactly like "not configured".
    """
    if not contact:
        return ""
    kept: list[str] = []
    for ch in contact:
        if ch in "\t\r\n":
            kept.append(" ")          # a line break becomes a space, never a break
        elif " " <= ch <= "~":        # printable ASCII survives
            kept.append(ch)
        # anything else -- C0/C1 controls, NUL, non-ASCII -- is dropped outright
    collapsed = " ".join("".join(kept).split())
    return collapsed[:max_length].strip()


def user_agent(
    contact: str = "",
    *,
    app_name: str = APP_NAME,
    version: str = __version__,
    max_length: int = MAX_CONTACT_LENGTH,
) -> str:
    """The ``User-Agent`` this app identifies itself with.

    Configured::

        RippedRecordFormatter/2.2.1 (you@example.com)

    Unconfigured -- a deliberate non-identity, not a stand-in maintainer::

        RippedRecordFormatter/2.2.1 (unconfigured; source: github.com/...)

    The version is always :data:`core.version.__version__`; nothing here is
    hardcoded to a release.
    """
    clean = sanitize_contact(contact, max_length=max_length) or UNCONFIGURED_CONTACT
    return f"{app_name}/{version} ({clean})"


# One nudge per process. The user is told once that their lookups are anonymous;
# telling them on every search would be nagging, and a dialog would be worse.
_nudged = False


def take_contact_nudge(contact: str) -> str | None:
    """Return the nudge text the *first* time a lookup runs unconfigured.

    ``None`` every other time -- because a contact is set, or because it has
    already been said once this session.
    """
    global _nudged
    if sanitize_contact(contact) or _nudged:
        return None
    _nudged = True
    return CONTACT_NUDGE


def reset_contact_nudge() -> None:
    """Forget that the nudge was shown. For tests; a process is one session."""
    global _nudged
    _nudged = False


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
    artist: str = ""          # per-track artist (splits/VA); "" -> use release
    artist_id: str = ""       # per-track artist MBID
    recording_id: str = ""    # MUSICBRAINZ_RECORDINGID source (recording MBID)
    track_mbid: str = ""      # MUSICBRAINZ_TRACKID source (release-track MBID)

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
    artist_id: str = ""       # MUSICBRAINZ_ARTISTID (release artist)

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


# ---------------------------------------------------------------------------
# Cover Art Archive fetch -- ours, so the transport is ours to pin.
# ---------------------------------------------------------------------------
#
# The CAA does not serve image bytes itself: it answers with a redirect to the
# Internet Archive, which redirects again to whichever node holds the file. Today
# every hop is https (verified against the live service). But *nothing enforced
# that*: urllib's HTTPRedirectHandler happily follows an https -> http Location,
# so the guarantee was the servers' good behaviour, not our policy.
#
# musicbrainzngs builds its own opener internally, with no seam to pass a redirect
# policy through -- so we make this one request ourselves. It costs us a small
# amount of URL construction and buys an explicit refusal to be downgraded.

CAA_BASE_URL = "https://coverartarchive.org"


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but only ever to HTTPS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        scheme = urlsplit(newurl).scheme.lower()
        if scheme != "https":
            raise MetadataSecurityError(
                f"refusing to follow a {scheme or 'scheme-less'} redirect from "
                f"{req.full_url} -- cover art must stay on HTTPS."
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_front_cover(release_id: str, agent: str, timeout: float) -> bytes:
    """GET the front cover for ``release_id`` from the Cover Art Archive.

    Returns the image bytes. Raises :class:`MetadataResponseError` when there is
    no art (404) or the service refuses, :class:`MetadataNetworkError` on a
    transport failure, and :class:`MetadataSecurityError` if a redirect tries to
    take us off HTTPS.
    """
    url = f"{CAA_BASE_URL}/release/{release_id}/front"
    if urlsplit(url).scheme != "https":  # pragma: no cover - guards the constant
        raise MetadataSecurityError(f"cover-art URL is not HTTPS: {url!r}")
    opener = urllib.request.build_opener(_HttpsOnlyRedirectHandler)
    request = urllib.request.Request(url, headers={"User-Agent": agent})
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.read()
    except MetadataError:
        raise
    except urllib.error.HTTPError as exc:
        # 404 is the common one: this release simply has no art.
        raise MetadataResponseError(f"cover art unavailable (HTTP {exc.code})") from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        raise MetadataNetworkError(str(exc)) from exc


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

    ``contact`` is the *user's* -- an email or URL they set in Settings. Left
    empty the client still works, but identifies itself as having no contact
    (see :func:`user_agent`) and asks, once, for one via ``notice``.
    """

    name = "MusicBrainz"

    def __init__(
        self,
        app_name: str = APP_NAME,
        app_version: str = __version__,
        contact: str = "",
        *,
        timeout: float = 15.0,
        rate_limiter: RateLimiter | None = None,
        client=None,
        cover_fetcher: Callable[[str, str, float], bytes] | None = None,
        notice: Callable[[str], None] | None = None,
    ) -> None:
        # ``client`` injection keeps the network library out of unit tests.
        if client is None:
            import musicbrainzngs as client  # local import: optional dependency
        self._mb = client
        self._timeout = float(timeout)
        self._limiter = rate_limiter or RateLimiter(1.0)
        self._contact = contact or ""
        self._cover_fetcher = cover_fetcher or fetch_front_cover
        self._notice = notice

        # ToS requirement: identify the application. The library composes its own
        # header (``app/version python-musicbrainzngs/x.y ( contact )``), so the
        # contact we hand it is what MusicBrainz sees. Raised as our error type
        # so callers never see a musicbrainzngs-specific exception.
        try:
            self._mb.set_useragent(app_name, app_version, self._ua_contact)
        except self._mb.UsageError as exc:  # pragma: no cover - defensive
            raise MetadataConfigError(str(exc)) from exc
        # Belt-and-suspenders: also arm the library's own throttle at 1 req/s.
        self._mb.set_rate_limit(1.0, 1)

    @property
    def _ua_contact(self) -> str:
        """The contact that goes on the wire: the user's, or an explicit none."""
        return sanitize_contact(self._contact) or UNCONFIGURED_CONTACT

    @property
    def user_agent(self) -> str:
        """The User-Agent used for the cover-art fetch we make ourselves."""
        return user_agent(self._contact)

    def _nudge(self) -> None:
        """Once per session, if unconfigured, ask for a contact. Never blocks."""
        if self._notice is None:
            return
        text = take_contact_nudge(self._contact)
        if text:
            self._notice(text)

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
        self._nudge()
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
        self._nudge()
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

        A release with no art answers 404, which the fetcher reports as a
        :class:`MetadataResponseError`; that is an expected, non-fatal outcome
        -- not every release has cover art -- so we swallow it and return
        ``None``. Network failures still propagate as
        :class:`MetadataNetworkError`, and a refused transport as
        :class:`MetadataSecurityError`; neither is silently turned into
        "no cover".
        """
        try:
            data = self._call(self._cover_fetcher, release_id,
                              self.user_agent, self._timeout)
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


def _artist_id_from_credit(entry: dict) -> str:
    """First artist's MBID from an artist-credit list, or ``""``."""
    for credit in entry.get("artist-credit", []):
        if isinstance(credit, dict):
            artist = credit.get("artist", {})
            mbid = artist.get("id")
            if mbid:
                return mbid
    return ""


def _track_artist(track: dict) -> tuple[str, str]:
    """Per-track ``(artist, artist_id)`` -- only when the track names its own."""
    if track.get("artist-credit") or track.get("artist-credit-phrase"):
        return _artist_from_credit(track), _artist_id_from_credit(track)
    return "", ""


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
    artist, artist_id = _track_artist(track)
    return TrackInfo(
        position=position,
        number=number,
        title=title,
        length_ms=_parse_length(track),
        artist=artist,
        artist_id=artist_id,
        recording_id=recording.get("id", "") or "",
        track_mbid=track.get("id", "") or "",   # release-track MBID
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
        artist_id=_artist_id_from_credit(release),
    )
