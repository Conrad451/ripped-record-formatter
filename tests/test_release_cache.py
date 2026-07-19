"""The release cache: an optimisation that is never a dependency.

Every re-process, resume and re-run used to re-query MusicBrainz for a release
it had already downloaded. These cover the saving *and* the boundary: a cache
that cannot make anything wrong, only faster.
"""

from __future__ import annotations

import json

import pytest

from core import release_cache
from core.metadata_lookup import (
    CoverArt,
    MediumInfo,
    MetadataNetworkError,
    MetadataProvider,
    ReleaseDetail,
    TrackInfo,
)
from core.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "rrf.db")
    yield s
    s.close()


def _detail(mbid="mbid-1", *, cover=True, tracks=2):
    def track(pos):
        return TrackInfo(position=pos, number=str(pos), title=f"Track {pos}",
                         length_ms=1000 * pos, artist="A", artist_id="aid",
                         recording_id=f"rec{pos}", track_mbid=f"trk{pos}")

    return ReleaseDetail(
        release_id=mbid, title="Discovery", artist="Daft Punk", year="2001",
        country="FR", artist_id="artist-mbid",
        media=(MediumInfo(1, "Vinyl", "", tuple(track(i) for i in range(1, tracks + 1))),),
        cover=CoverArt(data=b"\xff\xd8jpegbytes", mime="image/jpeg") if cover else None,
    )


class _Counting(MetadataProvider):
    """An inner provider that records how often it was actually called."""

    name = "counting"

    def __init__(self, detail=None, raises=None):
        self.detail = detail if detail is not None else _detail()
        self.raises = raises
        self.get_calls: list[str] = []
        self.search_calls: list[tuple] = []

    def search_releases(self, artist, album, *, limit=25):
        self.search_calls.append((artist, album))
        return []

    def get_release(self, release_id, *, with_cover=True):
        self.get_calls.append(release_id)
        if self.raises is not None:
            raise self.raises
        return self.detail


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #
def test_a_release_survives_the_round_trip_intact(store):
    original = _detail()
    assert release_cache.put(store, original) is True

    back = release_cache.get(store, "mbid-1")

    assert back is not None
    assert back.release_id == original.release_id
    assert back.title == original.title
    assert back.artist_id == original.artist_id
    assert back.track_count == 2
    assert [t.title for t in back.tracks] == ["Track 1", "Track 2"]
    # The identifiers that end up in tags are what make this worth caching.
    assert [t.recording_id for t in back.tracks] == ["rec1", "rec2"]
    assert [t.track_mbid for t in back.tracks] == ["trk1", "trk2"]
    assert back.cover is not None
    assert back.cover.data == b"\xff\xd8jpegbytes"
    assert back.cover.mime == "image/jpeg"


def test_re_fetching_updates_rather_than_duplicates(store):
    release_cache.put(store, _detail(tracks=2))
    release_cache.put(store, _detail(tracks=5))

    assert release_cache.get(store, "mbid-1").track_count == 5
    row = store.read().execute("SELECT COUNT(*) AS n FROM releases").fetchone()
    assert row["n"] == 1


# --------------------------------------------------------------------------- #
# Completeness gate
# --------------------------------------------------------------------------- #
def test_a_fetch_without_cover_is_not_remembered(store):
    """Not having *asked* for the cover is not the same as there being none."""
    assert release_cache.put(store, _detail(cover=False), with_cover=False) is False
    assert release_cache.get(store, "mbid-1") is None


def test_a_release_with_no_tracks_is_not_remembered(store):
    """A release with no tracks is not a tracklist."""
    empty = ReleaseDetail(release_id="mbid-2", title="X", artist="Y")

    assert release_cache.put(store, empty) is False
    assert release_cache.get(store, "mbid-2") is None


def test_a_release_that_genuinely_has_no_cover_is_still_complete(store):
    """Absent cover art is not an error, and must not be a permanent miss."""
    assert release_cache.put(store, _detail(cover=False), with_cover=True) is True

    back = release_cache.get(store, "mbid-1")
    assert back is not None and back.cover is None


# --------------------------------------------------------------------------- #
# The boundary: a cache may only ever be faster, never wrong
# --------------------------------------------------------------------------- #
def test_a_corrupt_row_reads_as_a_miss(store):
    release_cache.put(store, _detail())
    with store.write() as connection:
        connection.execute("UPDATE releases SET payload='{not json' WHERE mbid='mbid-1'")

    assert release_cache.get(store, "mbid-1") is None


def test_a_row_from_an_unknown_shape_reads_as_a_miss(store):
    """A half-decoded release would end up tagged into somebody's files."""
    release_cache.put(store, _detail())
    with store.write() as connection:
        connection.execute(
            "UPDATE releases SET payload=? WHERE mbid='mbid-1'",
            (json.dumps({"release_id": "mbid-1", "title": "X"}),))     # no media key

    assert release_cache.get(store, "mbid-1") is None


def test_no_store_at_all_is_simply_a_miss(store):
    assert release_cache.get(None, "mbid-1") is None
    assert release_cache.put(None, _detail()) is False


# --------------------------------------------------------------------------- #
# The wrapper
# --------------------------------------------------------------------------- #
def test_a_cached_release_costs_no_network_call(store):
    inner = _Counting()
    provider = release_cache.CachingProvider(inner, store)

    first = provider.get_release("mbid-1")
    second = provider.get_release("mbid-1")

    assert inner.get_calls == ["mbid-1"], "the second fetch went to the network"
    assert second.title == first.title
    assert second.track_count == first.track_count


def test_a_cache_hit_says_so_once(store):
    inner = _Counting()
    logged: list[str] = []
    provider = release_cache.CachingProvider(inner, store, on_log=logged.append)

    provider.get_release("mbid-1")
    assert logged == []                        # the fetch was live; nothing to say

    provider.get_release("mbid-1")
    assert len(logged) == 1
    assert "saved copy" in logged[0]
    assert "Discovery" in logged[0]


def test_a_miss_falls_through_and_is_then_remembered(store):
    inner = _Counting()
    provider = release_cache.CachingProvider(inner, store)

    provider.get_release("mbid-1")

    assert release_cache.get(store, "mbid-1") is not None


def test_search_is_never_cached(store):
    """Finding a release you have not seen is inherently an online act."""
    inner = _Counting()
    provider = release_cache.CachingProvider(inner, store)

    provider.search_releases("Daft Punk", "Discovery")
    provider.search_releases("Daft Punk", "Discovery")

    assert len(inner.search_calls) == 2


def test_a_network_failure_still_raises_through_the_wrapper(store):
    """The cache must not swallow an error into a silent empty answer."""
    inner = _Counting(raises=MetadataNetworkError("the share blinked"))
    provider = release_cache.CachingProvider(inner, store)

    with pytest.raises(MetadataNetworkError):
        provider.get_release("mbid-1")


def test_a_broken_store_does_not_break_fetching(store, tmp_path):
    """The fallback is structural: if the cache misbehaves, the provider is
    right there."""
    class Broken:
        path = tmp_path / "rrf.db"

        def read(self):
            raise RuntimeError("disk I/O error")

        def write(self):
            raise RuntimeError("disk I/O error")

    inner = _Counting()
    provider = release_cache.CachingProvider(inner, Broken())

    detail = provider.get_release("mbid-1")

    assert detail.title == "Discovery"
    assert inner.get_calls == ["mbid-1"]


def test_forgetting_a_release_sends_the_next_read_to_the_network(store):
    inner = _Counting()
    provider = release_cache.CachingProvider(inner, store)
    provider.get_release("mbid-1")

    release_cache.forget(store, "mbid-1")
    provider.get_release("mbid-1")

    assert inner.get_calls == ["mbid-1", "mbid-1"]
