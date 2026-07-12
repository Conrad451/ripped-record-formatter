"""Album-level orchestration over the single-side restore/split/encode pipeline.

Two pieces, both GUI-agnostic:

* :func:`map_wavs_to_sides` -- filename heuristics assigning a folder of WAVs to
  the sides of a release (``SideA.wav``, ``side_a``, ``A.wav``, ``01 - side 1``).
  It only proposes; the user confirms/corrects.
* :class:`AlbumController` -- a small state machine + thread pools that pipelines
  the sides: analysis of side k+1 runs in the background while side k waits for
  the human to review it. Analysis concurrency is bounded (default 1 -- each
  in-flight analysis holds a whole side in RAM and rips usually sit on a network
  share); accepted sides encode on a separate pool while later sides are still
  being reviewed. One side failing (bad WAV, sanity-guard trip) parks that side
  in :attr:`SideState.ERROR` and never stops the others.

The controller is deliberately injected with ``analyze_fn`` / ``encode_fn`` so it
can be driven by real DSP in the GUI or by instrumented fakes in tests. State
changes are pushed through ``on_state_change(side)`` (called from worker threads;
a GUI marshals it onto its own thread).
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable


class SideState(str, Enum):
    QUEUED = "queued"
    ANALYZING = "analyzing"
    READY = "ready"            # analysis done, awaiting human review
    RESOLVING = "resolving"    # human is reviewing / placing markers
    ACCEPTED = "accepted"      # cuts confirmed, queued to encode
    ENCODING = "encoding"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


_TERMINAL = {SideState.DONE, SideState.ERROR, SideState.CANCELLED}


@dataclass
class SideJob:
    """One side of the album as it moves through the pipeline."""

    index: int
    label: str
    wav_path: Path | None = None
    titles: list[str] = field(default_factory=list)
    durations_ms: list[int] = field(default_factory=list)
    state: SideState = SideState.QUEUED
    analysis: object | None = None      # whatever analyze_fn returns
    timestamps: list[float] = field(default_factory=list)
    segments: list[Path] = field(default_factory=list)
    """Cut track files, when the reviewer already produced them. Lets encode_fn
    reuse the reviewer's cut rather than splitting the side a second time."""
    error: str = ""


# --------------------------------------------------------------------------- #
# Filename -> side mapping
# --------------------------------------------------------------------------- #
_SIDE_WORD = re.compile(r"side[\s_\-]*([a-h]|\d{1,2})", re.IGNORECASE)
_TRAILING_LETTER = re.compile(r"(?:^|[\s_\-])([a-h])(?:[\s_\-.]|$)", re.IGNORECASE)


def _token_to_index(token: str) -> int | None:
    token = token.strip().lower()
    if not token:
        return None
    if token.isdigit():
        return int(token) - 1        # "1" -> side 0
    if len(token) == 1 and "a" <= token <= "h":
        return ord(token) - ord("a")  # "a" -> side 0
    return None


def _guess(stem: str) -> tuple[int | None, int]:
    """Return ``(side_index, strength)``; strength 2=explicit "side", 1=letter."""
    match = _SIDE_WORD.search(stem)
    if match:
        return _token_to_index(match.group(1)), 2
    match = _TRAILING_LETTER.search(stem)
    if match:
        return _token_to_index(match.group(1)), 1
    return None, 0


def guess_side_index(filename: str) -> int | None:
    """Best-effort side index from a filename, or ``None`` if unclear."""
    return _guess(Path(filename).stem)[0]


def propose_wav_side_map(wav_paths, num_sides: int) -> list[int | None]:
    """Propose a side for each WAV, in the order given; ``None`` means *skip*.

    The inverse of :func:`map_wavs_to_sides`: one entry per **WAV** rather than
    per side, because a folder may hold WAVs from several albums and the user
    works through one album at a time. Anything this function is not confident
    about is left unmapped, and unmapped rows are simply excluded from the job.

    Confident means the filename actually names a side -- ``SideA``, ``side_b``,
    ``side-2``, or a lone ``A``. A file with no side hint (``bonus.wav``,
    ``track01.wav``) is **never** guessed into a slot: it stays ``None``. If two
    files claim the same side, the stronger hint wins ("SideA" beats a bare "A")
    and the loser is left unmapped for the user to resolve -- we do not silently
    bump it elsewhere.
    """
    paths = [Path(p) for p in wav_paths]
    proposal: list[int | None] = [None] * len(paths)
    taken: set[int] = set()

    # Strength-ordered so an explicit "SideA" claims the slot before a bare "A".
    # Ties inside a strength band break on filename, for determinism.
    ranked = sorted(
        ((i, *_guess(p.stem)) for i, p in enumerate(paths)),
        key=lambda t: (-t[2], paths[t[0]].name.lower()),
    )
    for i, idx, strength in ranked:
        if strength == 0 or idx is None:
            continue                      # no side hint -> skip, never guess
        if not 0 <= idx < num_sides or idx in taken:
            continue                      # out of range, or someone stronger took it
        proposal[i] = idx
        taken.add(idx)
    return proposal


def sides_from_proposal(wav_paths, proposal) -> dict[int, Path]:
    """``{side_index: wav}`` for mapped rows only -- skipped rows are dropped.

    This is what turns a mapping table into a job: a row left on "skip" (``None``)
    contributes nothing, so a folder holding two albums' worth of WAVs yields a
    job containing only the sides the user actually mapped.
    """
    return {
        idx: Path(path)
        for path, idx in zip(wav_paths, proposal)
        if idx is not None
    }


def map_wavs_to_sides(wav_paths, num_sides: int) -> list[Path | None]:
    """Propose one WAV per side (index 0..num_sides-1); ``None`` where unsure.

    Assignment is strength-ordered so an explicit ``SideA`` beats an incidental
    trailing ``A``: first files that name a side outright, then single-letter
    hints, then whatever's left filled into still-empty sides in sorted filename
    order (so a plain ``01.wav``/``02.wav`` pair still maps).
    """
    paths = [Path(p) for p in wav_paths]
    mapping: list[Path | None] = [None] * num_sides

    guesses = [(p, *_guess(p.stem)) for p in sorted(paths, key=lambda p: p.name.lower())]
    leftovers: list[Path] = []
    for strength in (2, 1):
        for path, idx, s in guesses:
            if s != strength or path in leftovers or path in mapping:
                continue
            if idx is not None and 0 <= idx < num_sides and mapping[idx] is None:
                mapping[idx] = path
    for path, idx, s in guesses:
        if path not in mapping:
            leftovers.append(path)

    empty = [i for i in range(num_sides) if mapping[i] is None]
    for slot, path in zip(empty, leftovers):
        mapping[slot] = path
    return mapping


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #
# analyze_fn(side, should_cancel) -> analysis object (or raises)
AnalyzeFn = Callable[[SideJob, Callable[[], bool]], object]
# encode_fn(side, should_cancel) -> None (or raises)
EncodeFn = Callable[[SideJob, Callable[[], bool]], None]
StateCallback = Callable[[SideJob], None]


class AlbumController:
    """Pipelines a list of :class:`SideJob` s through analyse -> review -> encode."""

    def __init__(
        self,
        sides: list[SideJob],
        analyze_fn: AnalyzeFn,
        encode_fn: EncodeFn,
        on_state_change: StateCallback | None = None,
        *,
        max_analysis_workers: int = 1,
        max_encode_workers: int = 1,
    ) -> None:
        self.sides = sides
        self._analyze_fn = analyze_fn
        self._encode_fn = encode_fn
        self._on_state_change = on_state_change
        self._lock = threading.Lock()
        self._cancel_all = threading.Event()
        self._side_cancel = {s.index: threading.Event() for s in sides}
        self._analysis_pool = ThreadPoolExecutor(max_workers=max(1, max_analysis_workers))
        self._encode_pool = ThreadPoolExecutor(max_workers=max(1, max_encode_workers))

    # -- state helpers ------------------------------------------------------
    def _set_state(self, side: SideJob, state: SideState, error: str = "") -> None:
        with self._lock:
            side.state = state
            if error:
                side.error = error
        if self._on_state_change is not None:
            self._on_state_change(side)

    def _by_index(self, index: int) -> SideJob:
        for side in self.sides:
            if side.index == index:
                return side
        raise KeyError(index)

    def _should_cancel(self, side: SideJob) -> Callable[[], bool]:
        event = self._side_cancel[side.index]
        return lambda: self._cancel_all.is_set() or event.is_set()

    def _cancelled(self, side: SideJob) -> bool:
        return self._should_cancel(side)()

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Queue every mapped side for analysis. Unmapped sides go to ERROR."""
        for side in self.sides:
            if side.wav_path is None:
                self._set_state(side, SideState.ERROR, "no WAV mapped to this side")
                continue
            self._analysis_pool.submit(self._run_analysis, side)

    def _run_analysis(self, side: SideJob) -> None:
        if self._cancelled(side):
            self._set_state(side, SideState.CANCELLED)
            return
        self._set_state(side, SideState.ANALYZING)
        try:
            analysis = self._analyze_fn(side, self._should_cancel(side))
        except Exception as exc:
            if self._cancelled(side):
                self._set_state(side, SideState.CANCELLED)
            else:
                self._set_state(side, SideState.ERROR, f"{type(exc).__name__}: {exc}")
            return
        side.analysis = analysis
        if self._cancelled(side):
            self._set_state(side, SideState.CANCELLED)
        else:
            self._set_state(side, SideState.READY)

    def mark_resolving(self, index: int) -> None:
        """The user opened a READY side for review."""
        side = self._by_index(index)
        if side.state == SideState.READY:
            self._set_state(side, SideState.RESOLVING)

    def accept_side(self, index: int, timestamps: list[float], titles: list[str] | None = None) -> None:
        """Confirm a side's cuts and queue it to encode in the background."""
        side = self._by_index(index)
        if side.state in _TERMINAL:
            return
        side.timestamps = list(timestamps)
        if titles is not None:
            side.titles = list(titles)
        self._set_state(side, SideState.ACCEPTED)
        self._encode_pool.submit(self._run_encode, side)

    def _run_encode(self, side: SideJob) -> None:
        if self._cancelled(side):
            self._set_state(side, SideState.CANCELLED)
            return
        self._set_state(side, SideState.ENCODING)
        try:
            self._encode_fn(side, self._should_cancel(side))
        except Exception as exc:
            if self._cancelled(side):
                self._set_state(side, SideState.CANCELLED)
            else:
                self._set_state(side, SideState.ERROR, f"{type(exc).__name__}: {exc}")
            return
        if self._cancelled(side):
            self._set_state(side, SideState.CANCELLED)
        else:
            self._set_state(side, SideState.DONE)

    # -- cancel -------------------------------------------------------------
    def cancel_side(self, index: int) -> None:
        side = self._by_index(index)
        self._side_cancel[index].set()
        with self._lock:
            terminal = side.state in _TERMINAL
            waiting = side.state in (SideState.QUEUED, SideState.READY, SideState.RESOLVING,
                                     SideState.ACCEPTED)
        if not terminal and waiting:
            self._set_state(side, SideState.CANCELLED)

    def cancel_all(self) -> None:
        self._cancel_all.set()
        for side in self.sides:
            with self._lock:
                waiting = side.state not in _TERMINAL and side.state in (
                    SideState.QUEUED, SideState.READY, SideState.RESOLVING, SideState.ACCEPTED)
            if waiting:
                self._set_state(side, SideState.CANCELLED)

    def shutdown(self, wait: bool = False) -> None:
        self._analysis_pool.shutdown(wait=wait, cancel_futures=True)
        self._encode_pool.shutdown(wait=wait, cancel_futures=True)
