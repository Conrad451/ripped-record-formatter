"""Remember releases we have already fetched, so we stop asking twice.

Every re-process, every resume and every "run this album again" used to re-query
MusicBrainz for a release it had already downloaded -- rate-limited, network
dependent, and for a record whose tracklist has not changed since the 1970s.

**A cache, and only a cache.** Every read path falls through to the provider
when the row is absent, unreadable, or not a full answer. Nothing here may turn
a network hiccup into a wrong tracklist, and deleting ``rrf.db`` must cost
nothing but a re-fetch. That is why the wrapper is the thing that has a
provider, rather than the provider having a cache: it can always just delegate.

**Only complete answers are stored** -- ``complete`` in the table means the
release was fetched *with* cover art and came back with at least one track. A
partial fetch is never written, so a hit is always as good as a live call and
callers never have to ask how good a hit was. (A release genuinely having no
cover art is complete; not having *asked* for the cover is not.)

Search is deliberately not cached. Finding a release you have never seen is
inherently an online act, and a stale search result is a wrong answer to a
question the user is asking right now.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from core.metadata_lookup import (
    CoverArt,
    MediumInfo,
    MetadataProvider,
    ReleaseDetail,
    TrackInfo,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def _track_to_dict(track: TrackInfo) -> dict:
    return {
        "position": track.position, "number": track.number, "title": track.title,
        "length_ms": track.length_ms, "artist": track.artist,
        "artist_id": track.artist_id, "recording_id": track.recording_id,
        "track_mbid": track.track_mbid,
    }


def _medium_to_dict(medium: MediumInfo) -> dict:
    return {
        "position": medium.position, "format": medium.format, "title": medium.title,
        "tracks": [_track_to_dict(t) for t in medium.tracks],
    }


def to_payload(detail: ReleaseDetail) -> dict:
    """The release as plain JSON-able data. Cover bytes are stored separately."""
    return {
        "release_id": detail.release_id, "title": detail.title,
        "artist": detail.artist, "year": detail.year, "country": detail.country,
        "artist_id": detail.artist_id,
        "cover_mime": detail.cover.mime if detail.cover else "",
        "media": [_medium_to_dict(m) for m in detail.media],
    }


def from_payload(payload: dict, cover_bytes: bytes | None) -> ReleaseDetail:
    """Rebuild a release. Raises on anything it cannot faithfully reconstruct.

    Deliberately strict: a half-decoded release is worse than a cache miss,
    because it would be tagged into somebody's files.
    """
    media = tuple(
        MediumInfo(
            position=int(m["position"]), format=m.get("format", ""),
            title=m.get("title", ""),
            tracks=tuple(
                TrackInfo(
                    position=int(t["position"]), number=str(t["number"]),
                    title=str(t["title"]), length_ms=t.get("length_ms"),
                    artist=t.get("artist", ""), artist_id=t.get("artist_id", ""),
                    recording_id=t.get("recording_id", ""),
                    track_mbid=t.get("track_mbid", ""),
                )
                for t in m.get("tracks", ())
            ),
        )
        for m in payload["media"]
    )
    cover = None
    if cover_bytes:
        cover = CoverArt(data=cover_bytes, mime=payload.get("cover_mime") or "image/jpeg")
    return ReleaseDetail(
        release_id=str(payload["release_id"]), title=str(payload["title"]),
        artist=str(payload["artist"]), year=payload.get("year", ""),
        country=payload.get("country", ""), media=media, cover=cover,
        artist_id=payload.get("artist_id", ""),
    )


def is_complete(detail: ReleaseDetail, *, with_cover: bool) -> bool:
    """Whether this answer is worth remembering.

    A release with no tracks is not a tracklist, and a fetch that never asked
    for the cover cannot stand in for one that did.
    """
    return bool(with_cover and detail.media and detail.track_count)


# --------------------------------------------------------------------------- #
# Store operations
# --------------------------------------------------------------------------- #
def put(store, detail: ReleaseDetail, *, with_cover: bool = True) -> bool:
    """Remember ``detail`` if it is a complete answer. Returns whether it was."""
    if store is None or not detail.release_id:
        return False
    if not is_complete(detail, with_cover=with_cover):
        return False
    try:
        with store.write() as connection:
            connection.execute(
                "INSERT INTO releases(mbid, payload, cover, complete, fetched_at) "
                "VALUES(?, ?, ?, 1, ?) "
                "ON CONFLICT(mbid) DO UPDATE SET payload=excluded.payload, "
                "cover=excluded.cover, complete=1, fetched_at=excluded.fetched_at",
                (detail.release_id, json.dumps(to_payload(detail)),
                 detail.cover.data if detail.cover else None,
                 datetime.now(timezone.utc).isoformat()))
        return True
    except Exception as exc:                    # a cache write must never fail a fetch
        log.info("Release cache: could not store %s (%s).", detail.release_id, exc)
        return False


def get(store, mbid: str) -> ReleaseDetail | None:
    """The remembered release, or None for any reason at all.

    Every failure mode -- missing row, incomplete row, corrupt JSON, a shape
    from some future schema -- returns None and lets the caller go to the
    network. A cache is not allowed to be the reason something breaks.
    """
    if store is None or not mbid:
        return None
    try:
        row = store.read().execute(
            "SELECT payload, cover, complete FROM releases WHERE mbid=?",
            (mbid,)).fetchone()
    except Exception as exc:
        log.info("Release cache: could not read %s (%s).", mbid, exc)
        return None
    if row is None or not row["complete"]:
        return None
    try:
        return from_payload(json.loads(row["payload"]), row["cover"])
    except Exception as exc:
        log.info("Release cache: %s is unreadable (%s); re-fetching.", mbid, exc)
        return None


def forget(store, mbid: str) -> None:
    """Drop a cached release. For when the stored copy is known to be wrong."""
    if store is None:
        return
    try:
        with store.write() as connection:
            connection.execute("DELETE FROM releases WHERE mbid=?", (mbid,))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# The wrapper
# --------------------------------------------------------------------------- #
class CachingProvider(MetadataProvider):
    """A provider that answers ``get_release`` from the store when it can.

    Wraps rather than replaces, so the fallback is structural: if anything at
    all goes wrong with the cache, the inner provider is right there.
    """

    def __init__(self, inner: MetadataProvider, store, *, on_log=None) -> None:
        self._inner = inner
        self._store = store
        self._on_log = on_log
        self.name = getattr(inner, "name", "provider")

    def _log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)

    def search_releases(self, artist: str, album: str, *, limit: int = 25):
        """Always live. Finding a release you have not seen is an online act."""
        return self._inner.search_releases(artist, album, limit=limit)

    def get_release(self, release_id: str, *, with_cover: bool = True) -> ReleaseDetail:
        if with_cover:
            cached = get(self._store, release_id)
            if cached is not None:
                self._log(f"Using the saved copy of “{cached.title}” — "
                          "no need to ask MusicBrainz again.")
                return cached

        detail = self._inner.get_release(release_id, with_cover=with_cover)
        put(self._store, detail, with_cover=with_cover)
        return detail
