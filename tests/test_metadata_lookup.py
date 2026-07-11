"""Tests for core.metadata_lookup -- fully mocked, no live network.

A ``FakeMusicBrainz`` stands in for the ``musicbrainzngs`` module: it exposes the
same functions and exception classes the provider uses, records the calls it
received, and returns canned dict shapes matching real MusicBrainz responses.
That lets us assert parsing, the User-Agent/rate-limit setup, cover-art handling,
and error mapping without ever touching the network.
"""

from __future__ import annotations

import pytest

from core.metadata_lookup import (
    CoverArt,
    MetadataConfigError,
    MetadataNetworkError,
    MetadataResponseError,
    MusicBrainzProvider,
    RateLimiter,
    ReleaseResult,
    _sniff_mime,
)


# ---------------------------------------------------------------------------
# Fake musicbrainzngs.
# ---------------------------------------------------------------------------


class _UsageError(Exception):
    pass


class _WebServiceError(Exception):
    pass


class _NetworkError(_WebServiceError):
    pass


class _ResponseError(_WebServiceError):
    pass


_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 8
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def _release_search_payload():
    return {
        "release-list": [
            {
                "id": "mbid-vinyl",
                "title": "Kind of Blue",
                "artist-credit-phrase": "Miles Davis",
                "date": "1959-08-17",
                "country": "US",
                "disambiguation": "180g reissue",
                "medium-list": [{"format": "Vinyl", "track-count": 5}],
            },
            {
                "id": "mbid-2xcd",
                "title": "Kind of Blue",
                "artist-credit": [{"artist": {"name": "Miles Davis"}}],
                "date": "1997",
                "country": "XW",
                "medium-list": [
                    {"format": "CD", "track-count": 5},
                    {"format": "CD", "track-count": 3},
                ],
            },
        ],
        "release-count": 2,
    }


def _release_detail_payload():
    return {
        "release": {
            "id": "mbid-vinyl",
            "title": "Kind of Blue",
            "artist-credit-phrase": "Miles Davis",
            "date": "1959",
            "country": "US",
            "medium-list": [
                {
                    "position": "1",
                    "format": "Vinyl",
                    "track-list": [
                        {"position": "1", "number": "A1", "title": "So What", "length": "545000"},
                        {
                            "position": "2",
                            "number": "A2",
                            "recording": {"title": "Freddie Freeloader", "length": "586000"},
                        },
                    ],
                },
                {
                    "position": "2",
                    "format": "Vinyl",
                    "track-list": [
                        {"position": "1", "number": "B1", "title": "Blue in Green"},  # no length
                    ],
                },
            ],
        }
    }


class FakeMusicBrainz:
    """Stand-in for the musicbrainzngs module."""

    UsageError = _UsageError
    WebServiceError = _WebServiceError
    NetworkError = _NetworkError
    ResponseError = _ResponseError

    def __init__(self, *, search=None, detail=None, image=_JPEG, raises=None):
        self._search = search if search is not None else _release_search_payload()
        self._detail = detail if detail is not None else _release_detail_payload()
        self._image = image
        self._raises = raises or {}
        self.calls: list[tuple] = []
        self.useragent = None
        self.rate_limit = None

    def set_useragent(self, app, version, contact=None):
        if not app:
            raise _UsageError("empty app name")
        self.useragent = (app, version, contact)

    def set_rate_limit(self, limit_or_interval=1.0, new_requests=1):
        self.rate_limit = (limit_or_interval, new_requests)

    def _maybe_raise(self, key):
        exc = self._raises.get(key)
        if exc is not None:
            raise exc

    def search_releases(self, limit=25, **fields):
        self.calls.append(("search", fields, limit))
        self._maybe_raise("search")
        return self._search

    def get_release_by_id(self, rid, includes=None, **kw):
        self.calls.append(("detail", rid, tuple(includes or ())))
        self._maybe_raise("detail")
        return self._detail

    def get_image_front(self, rid, size=None):
        self.calls.append(("image", rid))
        self._maybe_raise("image")
        return self._image


def make_provider(mb):
    """Provider wired to a fake client and a no-wait rate limiter."""
    limiter = RateLimiter(1.0, clock=lambda: 0.0, sleep=lambda s: None)
    return MusicBrainzProvider(client=mb, rate_limiter=limiter)


# ---------------------------------------------------------------------------
# Construction / ToS.
# ---------------------------------------------------------------------------


def test_sets_useragent_and_rate_limit_on_construction():
    mb = FakeMusicBrainz()
    make_provider(mb)
    assert mb.useragent is not None
    app, version, contact = mb.useragent
    assert app and version and contact  # all three ToS fields present
    assert mb.rate_limit == (1.0, 1)


def test_empty_contact_rejected():
    with pytest.raises(MetadataConfigError):
        MusicBrainzProvider(client=FakeMusicBrainz(), contact="")


# ---------------------------------------------------------------------------
# Search parsing.
# ---------------------------------------------------------------------------


def test_search_parses_disambiguating_fields():
    provider = make_provider(FakeMusicBrainz())
    results = provider.search_releases("Miles Davis", "Kind of Blue")
    assert [r.release_id for r in results] == ["mbid-vinyl", "mbid-2xcd"]

    vinyl = results[0]
    assert vinyl.artist == "Miles Davis"
    assert vinyl.year == "1959"            # truncated from 1959-08-17
    assert vinyl.country == "US"
    assert vinyl.formats == "Vinyl"
    assert vinyl.track_count == 5
    assert vinyl.disambiguation == "180g reissue"

    # Multi-disc CD collapses to "2xCD"; artist derived from artist-credit list.
    cd = results[1]
    assert cd.formats == "2xCD"
    assert cd.artist == "Miles Davis"
    assert cd.track_count == 8


def test_search_passes_both_fields_to_client():
    mb = FakeMusicBrainz()
    make_provider(mb).search_releases("Miles Davis", "Kind of Blue")
    _, fields, _ = mb.calls[0]
    assert fields == {"artist": "Miles Davis", "release": "Kind of Blue"}


def _ranking_payload():
    def rel(rid, title, fmt, tc, secondary=None):
        rg = {"type": "Album"}
        if secondary:
            rg["secondary-type-list"] = secondary
        return {
            "id": rid, "title": title, "artist-credit-phrase": "Nirvana",
            "medium-list": [{"format": fmt, "track-count": tc}],
            "release-group": rg,
        }
    return {"release-list": [
        rel("studio-cd", "In Utero", "CD", 12),
        rel("comp-vinyl", "Nirvana", "Vinyl", 14, ["Compilation"]),
        rel("studio-vinyl", "In Utero", "Vinyl", 12),
        rel("comp-cd", "Nirvana", "CD", 14, ["Compilation"]),
    ], "release-count": 4}


def test_search_ranks_studio_and_vinyl_first():
    mb = FakeMusicBrainz(search=_ranking_payload())
    results = make_provider(mb).search_releases("Nirvana", "In Utero")
    # Studio albums before compilations; vinyl before CD within the same type.
    assert [r.release_id for r in results] == [
        "studio-vinyl", "studio-cd", "comp-vinyl", "comp-cd",
    ]
    # ...and both search terms still constrained the query.
    _, fields, _ = mb.calls[0]
    assert fields == {"artist": "Nirvana", "release": "In Utero"}
    assert results[0].is_vinyl and not results[0].is_compilation
    assert results[2].is_compilation


def test_search_with_no_query_short_circuits_without_network():
    mb = FakeMusicBrainz()
    results = make_provider(mb).search_releases("  ", "")
    assert results == []
    assert not mb.calls  # never hit the network


def test_search_no_results_returns_empty_list():
    mb = FakeMusicBrainz(search={"release-list": [], "release-count": 0})
    assert make_provider(mb).search_releases("nobody", "nothing") == []


# ---------------------------------------------------------------------------
# Detail parsing.
# ---------------------------------------------------------------------------


def test_get_release_builds_per_medium_tracklist():
    provider = make_provider(FakeMusicBrainz())
    detail = provider.get_release("mbid-vinyl")

    assert detail.title == "Kind of Blue"
    assert len(detail.media) == 2
    assert detail.media[0].format == "Vinyl"
    assert detail.track_count == 3

    tracks = detail.tracks  # flattened across sides
    assert [t.number for t in tracks] == ["A1", "A2", "B1"]
    # Title falls back to the recording title when the track has none.
    assert tracks[1].title == "Freddie Freeloader"
    # Durations parsed to ms and rendered m:ss; unknown stays blank.
    assert tracks[0].length_ms == 545000
    assert tracks[0].length_display() == "9:05"
    assert tracks[2].length_ms is None
    assert tracks[2].length_display() == ""


def test_get_release_requests_needed_includes():
    mb = FakeMusicBrainz()
    make_provider(mb).get_release("mbid-vinyl")
    detail_call = next(c for c in mb.calls if c[0] == "detail")
    _, rid, includes = detail_call
    assert rid == "mbid-vinyl"
    assert {"recordings", "media", "artist-credits"} <= set(includes)


def test_detail_parses_mbids_and_per_track_artist():
    detail = {"release": {
        "id": "rel-id", "title": "Split LP", "date": "2000",
        "artist-credit-phrase": "Various", "artist-credit": [{"artist": {"id": "va-id", "name": "Various"}}],
        "medium-list": [{"position": "1", "format": "Vinyl", "track-list": [
            {"position": "1", "number": "A1", "title": "T1", "length": "100000",
             "recording": {"id": "rec-1"},
             "artist-credit-phrase": "Band A", "artist-credit": [{"artist": {"id": "a-id", "name": "Band A"}}]},
            {"position": "2", "number": "A2", "recording": {"id": "rec-2", "title": "T2"}},
        ]}],
    }}
    d = make_provider(FakeMusicBrainz(detail=detail)).get_release("rel-id")
    assert d.artist_id == "va-id"
    t0, t1 = d.tracks
    assert t0.recording_id == "rec-1"
    assert t0.artist == "Band A" and t0.artist_id == "a-id"   # per-track credit
    assert t1.recording_id == "rec-2"
    assert t1.artist == "" and t1.artist_id == ""             # no per-track credit -> blank


def test_empty_release_id_raises_response_error():
    provider = make_provider(FakeMusicBrainz())
    with pytest.raises(MetadataResponseError):
        provider.get_release("")


def test_missing_release_in_payload_raises_response_error():
    mb = FakeMusicBrainz(detail={})
    with pytest.raises(MetadataResponseError):
        make_provider(mb).get_release("mbid-vinyl")


# ---------------------------------------------------------------------------
# Cover art.
# ---------------------------------------------------------------------------


def test_cover_art_fetched_with_mime():
    detail = make_provider(FakeMusicBrainz(image=_PNG)).get_release("mbid-vinyl")
    assert isinstance(detail.cover, CoverArt)
    assert detail.cover.mime == "image/png"
    assert detail.cover.data == _PNG


def test_missing_cover_art_is_not_an_error():
    # A 404 from the Cover Art Archive surfaces as ResponseError -> cover None.
    mb = FakeMusicBrainz(raises={"image": _ResponseError("404")})
    detail = make_provider(mb).get_release("mbid-vinyl")
    assert detail.cover is None
    assert detail.title == "Kind of Blue"  # the rest still parsed fine


def test_with_cover_false_skips_the_image_request():
    mb = FakeMusicBrainz()
    make_provider(mb).get_release("mbid-vinyl", with_cover=False)
    assert not any(c[0] == "image" for c in mb.calls)


# ---------------------------------------------------------------------------
# Error mapping / offline resilience.
# ---------------------------------------------------------------------------


def test_network_error_on_search_is_typed():
    mb = FakeMusicBrainz(raises={"search": _NetworkError("timed out")})
    with pytest.raises(MetadataNetworkError):
        make_provider(mb).search_releases("Miles", "Blue")


def test_response_error_on_detail_is_typed():
    mb = FakeMusicBrainz(raises={"detail": _ResponseError("bad id")})
    with pytest.raises(MetadataResponseError):
        make_provider(mb).get_release("mbid-vinyl")


def test_low_level_oserror_maps_to_network_error():
    mb = FakeMusicBrainz(raises={"search": OSError("connection refused")})
    with pytest.raises(MetadataNetworkError):
        make_provider(mb).search_releases("Miles", "Blue")


# ---------------------------------------------------------------------------
# Rate limiter.
# ---------------------------------------------------------------------------


def test_rate_limiter_spaces_calls_by_interval():
    now = {"t": 0.0}
    slept: list[float] = []

    def clock():
        return now["t"]

    def sleep(seconds):
        slept.append(seconds)
        now["t"] += seconds  # simulate time advancing while asleep

    limiter = RateLimiter(1.0, clock=clock, sleep=sleep)
    limiter.acquire()          # first call: no wait
    assert slept == []
    limiter.acquire()          # immediately after: must wait a full interval
    assert slept == [1.0]

    now["t"] += 5.0            # plenty of time passes
    limiter.acquire()          # no wait needed
    assert slept == [1.0]


def test_rate_limiter_is_used_between_search_and_cover():
    slept: list[float] = []
    now = {"t": 0.0}

    def sleep(s):
        slept.append(s)
        now["t"] += s

    limiter = RateLimiter(1.0, clock=lambda: now["t"], sleep=sleep)
    provider = MusicBrainzProvider(client=FakeMusicBrainz(), rate_limiter=limiter)
    # detail fetch makes two network calls (release + cover) -> one throttle wait.
    provider.get_release("mbid-vinyl")
    assert slept == [1.0]


# ---------------------------------------------------------------------------
# MIME sniffing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data,expected",
    [
        (_JPEG, "image/jpeg"),
        (_PNG, "image/png"),
        (b"GIF89a....", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPxxxx", "image/webp"),
        (b"random-bytes", "image/jpeg"),  # default
    ],
)
def test_sniff_mime(data, expected):
    assert _sniff_mime(data) == expected


# ---------------------------------------------------------------------------
# ReleaseResult convenience.
# ---------------------------------------------------------------------------


def test_release_result_label_is_readable():
    r = ReleaseResult("id", "Kind of Blue", "Miles Davis", "1959", "US", "Vinyl", 5, "reissue")
    label = r.label()
    assert "Kind of Blue" in label and "Miles Davis" in label
    assert "1959" in label and "US" in label and "Vinyl" in label
    assert "reissue" in label
