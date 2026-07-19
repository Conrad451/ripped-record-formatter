"""Album-level orchestration over the single-side restore/split/encode pipeline.

Two pieces, both GUI-agnostic:

* :func:`propose_wav_side_map` -- filename heuristics proposing a side for each
  WAV in a folder (``SideA.wav``, ``side_a``, ``A.wav``, ``01 - side 1``). It only
  proposes, never guesses: a file with no side hint is left unmapped for the user
  to place or skip. :func:`sides_from_proposal` turns a confirmed mapping into a
  job, dropping the skipped rows.
* :class:`AlbumController` -- a small state machine + thread pools that pipelines
  the sides: analysis of side k+1 runs in the background while side k waits for
  the human to review it. Analysis concurrency is bounded (default 1 -- each
  in-flight analysis holds a whole side in RAM and rips usually sit on a network
  share); accepted sides encode on a separate pool while later sides are still
  being reviewed. One side failing (bad WAV, sanity-guard trip) parks that side
  in :attr:`SideState.ERROR` and never stops the others. A side whose analysis
  *worked* but tripped a sanity guard is parked in
  :attr:`SideState.NEEDS_ATTENTION` instead, keeping its proposal, because that
  is a review request rather than a failure.

The controller is deliberately injected with ``analyze_fn`` / ``encode_fn`` so it
can be driven by real DSP in the GUI or by instrumented fakes in tests. State
changes are pushed through ``on_state_change(side)`` (called from worker threads;
a GUI marshals it onto its own thread).
"""

from __future__ import annotations

import re
import threading
import traceback
import atexit
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Callable


class SideState(str, Enum):
    QUEUED = "queued"
    ANALYZING = "analyzing"
    READY = "ready"            # analysis done, awaiting human review
    NEEDS_ATTENTION = "needs attention"
    """Analysis succeeded, but a sanity guard says the result should not be
    trusted unreviewed -- too few boundaries confirmed for the expected track
    count. That is a *review request*, not a failure: the analysis is intact and
    the side opens for review like a READY one, just with a diagnosis banner. It
    is emphatically not ERROR, whose only exit is Retry -- and retrying a guard
    trip re-runs the same deterministic analysis on the same input and trips the
    same guard."""
    RESOLVING = "resolving"    # human is reviewing / placing markers
    ACCEPTED = "accepted"      # cuts confirmed, queued to encode
    ENCODING = "encoding"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


_TERMINAL = {SideState.DONE, SideState.ERROR, SideState.CANCELLED}

#: States a side can be re-run from.
#:
#: DONE is in here deliberately. A side that failed and a side that came out
#: wrong are the same gesture with different starting states -- "the splitter
#: missed a track and Accept locked the door" is exactly as much a reason to
#: re-run as "the share blinked". A tool that shows its work has to accept
#: appeals, and the raw WAVs survive by design precisely so it can.
#:
#: CANCELLED is not: a cancelled side may have been cancelled *because* the
#: album was being torn down, and re-running it would resurrect work the user
#: just stopped.
_RETRYABLE = {SideState.ERROR, SideState.NEEDS_ATTENTION, SideState.DONE}


@dataclass(frozen=True)
class SideSummary:
    """A finished side's receipt -- what was written, how big, how long, and any
    warnings. Captured once at side completion (see :func:`measure_outputs`), so
    the GUI card is a pure view and never re-walks the output folder. Qt-free.

    An errored/cancelled side that never wrote anything still gets a
    :class:`SideSummary`, carrying only its ``index``/``label``/``state`` -- the
    card shows it honestly rather than omitting it.
    """

    index: int
    label: str
    state: "SideState"
    track_count: int = 0
    output_paths: tuple[Path, ...] = ()
    total_bytes: int = 0
    duration_s: float = 0.0
    warnings: tuple[str, ...] = ()
    warned_tracks: int = 0
    """How many *tracks* carried at least one warning (not the warning count)."""
    declick_repaired_samples: int | None = None
    declick_total_samples: int | None = None
    """adeclick's repaired/examined sample tally for this side, carried through
    from :class:`~core.restoration.RestorationResult`. ``None`` when declick was
    off for the run, or when ffmpeg printed no stat we could read. Samples, not
    clicks -- the card must say so."""


@dataclass(frozen=True)
class AlbumSummary:
    """How an album ended. Every side is counted exactly once.

    The three integer counts and :meth:`describe` are the original contract (the
    one-line log). :attr:`sides` adds the per-side receipts the summary card
    renders; the roll-up properties derive album totals from them.
    """

    done: int = 0
    error: int = 0
    cancelled: int = 0
    sides: tuple[SideSummary, ...] = ()

    @property
    def total(self) -> int:
        return self.done + self.error + self.cancelled

    @property
    def total_bytes(self) -> int:
        return sum(s.total_bytes for s in self.sides)

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(w for s in self.sides for w in s.warnings)

    @property
    def warned_tracks(self) -> int:
        return sum(s.warned_tracks for s in self.sides)

    def describe(self) -> str:
        """One line for the log. Says what happened, not merely that it stopped.

        A clean run reads "2 sides done."; anything else itemises, because
        "finished" over a failed side is a lie of omission.
        """
        if self.total == 0:
            return "Album complete: no sides."
        if self.error == 0 and self.cancelled == 0:
            noun = "side" if self.done == 1 else "sides"
            return f"Album complete: {self.done} {noun} done."
        parts = []
        if self.done:
            parts.append(f"{self.done} done")
        if self.error:
            parts.append(f"{self.error} error")
        if self.cancelled:
            parts.append(f"{self.cancelled} cancelled")
        return f"Album complete: {', '.join(parts)}."


def measure_outputs(paths) -> tuple[int, float]:
    """Total size on disk (bytes) and total audio duration (seconds) for ``paths``.

    Measured once, at side completion, while the files are fresh -- so the
    summary card reads captured numbers rather than re-walking the output folder.
    A missing or unreadable file is skipped, never fatal: a finished-album receipt
    must not crash on one quirky file. ``soundfile`` is imported lazily so the
    controller module stays cheap to import.
    """
    total_bytes = 0
    total_s = 0.0
    for path in paths:
        path = Path(path)
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass
        try:
            import soundfile as sf

            info = sf.info(str(path))
            if info.samplerate:
                total_s += float(info.frames) / info.samplerate
        except Exception:
            pass
    return total_bytes, total_s


class NeedsAttention(Exception):
    """Raised by ``analyze_fn`` when a sanity guard trips on a *usable* result.

    Carries the analysis that was produced, so the controller can park the side
    for review instead of throwing the work away. Anything else raised out of
    ``analyze_fn`` is a real failure and still lands in ERROR.
    """

    def __init__(self, reason: str, analysis: object) -> None:
        super().__init__(reason)
        self.reason = reason
        self.analysis = analysis


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
    artists: list[str] = field(default_factory=list)
    """Per-track artists as the reviewer left them. Snapshotted at accept time
    alongside :attr:`titles`, so the review area can be handed to the next side
    immediately without the pending edits living on in the UI."""

    result: "SideSummary | None" = None
    """The side's encode receipt (output paths, sizes, duration, warnings),
    populated by the encode callback at completion. ``None`` until it finishes,
    and stays ``None`` for a side that errored or was cancelled before writing.
    :meth:`AlbumController.summary` re-stamps its ``state`` from the authoritative
    :attr:`state` at finish time."""

    # --- failure detail -----------------------------------------------------
    # An ERROR state that only says "error" is useless. Every failure records
    # what actually went wrong, which phase it went wrong in, and the traceback,
    # so the UI can show a cause instead of a colour.
    attention: str = ""
    """Why a NEEDS_ATTENTION side wants review. Not an error message."""
    error: str = ""
    """One-line cause, e.g. ``PermissionError: [Errno 13] Permission denied: ...``."""
    failed_phase: str = ""
    """Which phase raised: ``"setup"``, ``"analysis"`` or ``"encode"``."""
    error_detail: str = ""
    """Full traceback, for the log's detail level. Never the only record."""

    def clear_error(self) -> None:
        self.error = ""
        self.failed_phase = ""
        self.error_detail = ""
        self.attention = ""


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

    One entry per **WAV** rather than per side, because a folder may hold WAVs
    from several albums and the user works through one album at a time. Anything
    this function is not confident about is left unmapped, and unmapped rows are
    simply excluded from the job.

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


#: A WAV whose duration is within this fraction of a side's expected total is a
#: duration match. ~5%: turntable speed drift and lead-out deadspace move a side's
#: real length a percent or two off the release's sum; a tighter window would miss
#: real matches, a looser one would start pairing different-length sides.
DURATION_MATCH_TOLERANCE = 0.05


def probe_duration_ms(path) -> int:
    """A WAV's duration in milliseconds from its header only -- no decode.

    ``soundfile.info`` reads just the header, so this is cheap enough to run over
    a whole folder. Returns 0 when the file is missing or unreadable, so a quirky
    file never breaks auto-mapping -- it simply does not get a duration match.
    """
    try:
        import soundfile as sf

        info = sf.info(str(path))
        if info.samplerate:
            return int(round(1000.0 * info.frames / info.samplerate))
    except Exception:
        pass
    return 0


def _ordinal_key(name: str):
    """A sortable ordinal ``(kind, value)`` for count-and-order, or ``None``.

    A trailing/standalone side letter a-h, or the first run of digits in the stem.
    The caller requires a homogeneous *kind* across a group so letters and numbers
    are never ordered against each other.
    """
    stem = Path(name).stem
    match = _TRAILING_LETTER.search(stem)
    if match:
        return ("L", match.group(1).lower())
    match = re.search(r"\d+", stem)
    if match:
        return ("N", int(match.group()))
    return None


def propose_side_map(wav_names, num_sides: int, *, current=None, locked=None,
                     wav_durations_ms=None, side_totals_ms=None,
                     tolerance: float = DURATION_MATCH_TOLERANCE) -> list[int | None]:
    """Propose a side for each WAV with a confidence ladder -- never a guess.

    In descending confidence:

    a. **Filename patterns** (``SideA``, ``side_2``, a lone ``A``) --
       :func:`propose_wav_side_map`, unchanged and highest confidence.
    b. **Count-and-order**: exactly K unmapped WAVs for K unmapped sides whose
       names carry a homogeneous, distinct ordinal (all letters or all numbers) ->
       map in sorted order. The single-WAV/single-side album maps trivially (there
       is only one possibility).
    c. **Duration match**: an unmapped WAV within ``tolerance`` of exactly one free
       side's expected total, with no other free WAV competing for that side.
    d. Otherwise **skip** (``None``).

    ``current`` is the existing per-WAV mapping (parallel to ``wav_names``);
    ``locked`` is the set of row indices the user hand-set -- kept verbatim,
    including a hand-set skip. Only rows that are unlocked *and* currently ``None``
    are ever filled, so hand edits survive re-proposal and the exclusive-side rule
    (a side is claimed once, by the strongest signal) stands across every rung.
    Returns a new full mapping.
    """
    n = len(wav_names)
    result = list(current) if current is not None else [None] * n
    locked = set(locked or ())
    taken = {v for v in result if v is not None}

    def free_rows():
        return [i for i in range(n) if i not in locked and result[i] is None]

    def free_sides():
        return sorted(s for s in range(num_sides) if s not in taken)

    def claim(row, side):
        result[row] = side
        taken.add(side)

    # a. filename patterns
    for i, idx in enumerate(propose_wav_side_map(wav_names, num_sides)):
        if i not in locked and result[i] is None and idx is not None and idx not in taken:
            claim(i, idx)

    # b. count-and-order (and the trivial one-WAV/one-side album). Only from a
    # clean slate: every WAV unmapped, every side free, counts equal. A remainder
    # left after filename matching is not a clean ordered set -- its ordinal may
    # point at an already-claimed side -- so we never order-map a leftover.
    rows, sides = free_rows(), free_sides()
    if rows and len(rows) == n and len(sides) == num_sides and len(rows) == len(sides):
        if n == 1:
            claim(rows[0], sides[0])                 # one WAV, one side: only option
        else:
            keys = {i: _ordinal_key(wav_names[i]) for i in rows}
            vals = list(keys.values())
            kinds = {v[0] for v in vals if v is not None}
            if all(v is not None for v in vals) and len(kinds) == 1 and len(set(vals)) == len(vals):
                for row, side in zip(sorted(rows, key=lambda i: keys[i]), sides):
                    claim(row, side)

    # c. duration match -- unique for the WAV, uncontested for the side
    rows, sides = free_rows(), free_sides()
    if rows and wav_durations_ms and side_totals_ms:
        def within(i, s):
            d = wav_durations_ms[i] if i < len(wav_durations_ms) else 0
            t = side_totals_ms[s] if s < len(side_totals_ms) else 0
            return bool(d) and bool(t) and abs(d - t) <= tolerance * t

        for i in rows:
            cand = [s for s in sides if within(i, s)]
            if len(cand) == 1 and not any(j != i and within(j, cand[0]) for j in rows):
                claim(i, cand[0])
    return result


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #
# analyze_fn(side, should_cancel) -> analysis object (or raises)
AnalyzeFn = Callable[[SideJob, Callable[[], bool]], object]
# encode_fn(side, should_cancel) -> None (or raises)
EncodeFn = Callable[[SideJob, Callable[[], bool]], None]
StateCallback = Callable[[SideJob], None]
FinishedCallback = Callable[["AlbumSummary"], None]


#: Every controller that has not been shut down. Weak, so it never keeps one
#: alive; it exists purely so shutdown_all() can find pools nobody closed.
_LIVE_CONTROLLERS: "weakref.WeakSet" = weakref.WeakSet()


def shutdown_all(wait: bool = False) -> int:
    """Shut down every controller still holding pools. Returns how many.

    Registered with atexit, because the failure this prevents is *the process
    not exiting*: non-daemon pool threads are joined by Python's own atexit
    hook, so one controller nobody closed is enough to hang an application that
    has already closed its last window. Idempotent and never raises -- it runs
    during interpreter shutdown, where raising helps nobody.
    """
    closed = 0
    for controller in list(_LIVE_CONTROLLERS):
        try:
            controller.shutdown(wait=wait)
            closed += 1
        except Exception:
            continue
    return closed


# Registered before concurrent.futures' own hook would run for pools created
# later, and harmless when everything was closed properly.
atexit.register(shutdown_all)


class AlbumController:
    """Pipelines a list of :class:`SideJob` s through analyse -> review -> encode."""

    def __init__(
        self,
        sides: list[SideJob],
        analyze_fn: AnalyzeFn,
        encode_fn: EncodeFn,
        on_state_change: StateCallback | None = None,
        *,
        on_finished: FinishedCallback | None = None,
        max_analysis_workers: int = 1,
        max_encode_workers: int = 1,
    ) -> None:
        self.sides = sides
        self._analyze_fn = analyze_fn
        self._encode_fn = encode_fn
        self._on_state_change = on_state_change
        self._on_finished = on_finished
        self._lock = threading.Lock()
        self._cancel_all = threading.Event()
        self._side_cancel = {s.index: threading.Event() for s in sides}
        self._analysis_pool = ThreadPoolExecutor(max_workers=max(1, max_analysis_workers))
        self._encode_pool = ThreadPoolExecutor(max_workers=max(1, max_encode_workers))
        # ThreadPoolExecutor threads are non-daemon, and Python joins every one
        # of them from an atexit hook. A controller nobody shut down therefore
        # keeps the *interpreter* alive -- which is why closing the window
        # during an analysis left the process running with no window to show
        # for it. Registered here so shutdown_all() can reach it whatever
        # happens; a weak reference, so registration alone never keeps a
        # finished controller alive.
        _LIVE_CONTROLLERS.add(self)
        self._finished = False

    # -- completion ---------------------------------------------------------
    @property
    def finished(self) -> bool:
        """Every side has reached a terminal state. The job is over.

        An album that never concludes cannot be run again, which is the whole
        point of this: a controller with no terminal state left the GUI holding a
        spent object forever, so a completed album answered "already running" to
        a second Start.
        """
        with self._lock:
            return self._finished

    def summary(self) -> AlbumSummary:
        with self._lock:
            snapshot = [(s.index, s.label, s.state, s.result) for s in self.sides]
        side_summaries = []
        for index, label, state, result in snapshot:
            if result is not None:
                # Re-stamp the authoritative final state onto the receipt: the
                # side may have been cancelled after it captured partial output.
                side_summaries.append(replace(result, state=state))
            else:
                side_summaries.append(SideSummary(index=index, label=label, state=state))
        states = [state for _, _, state, _ in snapshot]
        return AlbumSummary(
            done=sum(1 for s in states if s == SideState.DONE),
            error=sum(1 for s in states if s == SideState.ERROR),
            cancelled=sum(1 for s in states if s == SideState.CANCELLED),
            sides=tuple(side_summaries),
        )

    def _claim_finish(self) -> bool:
        """True exactly once: for the caller that saw the last side go terminal.

        Sides finish on two different pools, so two threads can observe the final
        state at the same instant. The flag is claimed under the lock so the
        completion callback -- which re-arms Start and releases the pools -- runs
        once and not twice.
        """
        with self._lock:
            if self._finished:
                return False
            if not all(s.state in _TERMINAL for s in self.sides):
                return False
            self._finished = True
            return True

    # -- state helpers ------------------------------------------------------
    def _set_state(self, side: SideJob, state: SideState, error: str = "",
                   phase: str = "", detail: str = "") -> None:
        with self._lock:
            side.state = state
            if error:
                side.error = error
                side.failed_phase = phase
                side.error_detail = detail
        if self._on_state_change is not None:
            self._on_state_change(side)
        if state in _TERMINAL and self._claim_finish() and self._on_finished is not None:
            self._on_finished(self.summary())

    @staticmethod
    def _describe(exc: BaseException) -> str:
        """One line a human can act on. Never just the exception class."""
        text = str(exc).strip()
        return f"{type(exc).__name__}: {text}" if text else type(exc).__name__

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
    def retry_side(self, index: int) -> bool:
        """Re-run one side's analysis from scratch. Returns whether it ran.

        Two situations, one gesture. A transient failure -- the share blinked,
        the file was locked, ffmpeg was busy -- used to cost the whole album,
        because ERROR is terminal and the only way back was starting over. And a
        side that *succeeded* but came out wrong (the splitter missed a track,
        Accept was pressed, the door locked) had no way back at all: Re-tag
        cannot split a FLAC, and re-running the album re-did the sides that were
        already correct.

        Both are the same operation: re-queue this side and leave every other
        one untouched, including ones already encoding. Its error is cleared,
        its previous receipt is dropped so the card cannot show a stale track
        count, any cancel flag is reset (a side cancelled with the album is not
        re-runnable, but one that *errored* may have had the flag set by a later
        cancel_all), and analysis is submitted afresh.

        Deliberately no retry limit: if it comes out wrong again the user decides
        whether to try once more or give up.
        """
        side = self._by_index(index)
        with self._lock:
            if side.state not in _RETRYABLE:
                return False
            if side.wav_path is None:
                return False          # nothing to retry; it was never mapped
            # Retrying re-opens a job that may already have concluded (every side
            # terminal, one of them ERROR). It has to be able to conclude again,
            # or the second completion would never be announced.
            self._finished = False
        side.clear_error()
        side.analysis = None
        # Drop the old receipt: re-running a DONE side means its recorded track
        # count, size and declick figures describe files that are about to be
        # replaced, and a card showing them alongside the new run would be
        # reporting two different truths about one side.
        side.result = None
        self._side_cancel[index].clear()
        self._set_state(side, SideState.QUEUED)
        self._analysis_pool.submit(self._run_analysis, side)
        return True

    def admit_side(self, side: SideJob) -> bool:
        """Admit a late-arriving side into a still-running job's analysis queue.

        The live-session case: an album is analysing side A when side B's
        recording lands mapped. Rather than make the user restart, B joins the
        running job -- it queues for analysis like any other side, and the
        enlarged job now waits for it before concluding.

        Admission is open only while the job is *non-finished*. A concluded album
        is concluded: this returns ``False`` and the caller maps the side normally
        (the user re-runs to include it per the 8.4 semantics). It also returns
        ``False`` if the album is being cancelled, or if a side with this index is
        already in the job. On success the side is appended, given a cancel event
        (so ``cancel_all`` covers it), set ``QUEUED``, and submitted for analysis;
        it is counted in the final summary automatically.

        The finished-flag check, the append, and the cancel-event registration all
        happen together under the lock -- serialised against ``_claim_finish``,
        which also locks and re-reads ``self.sides`` -- so a last existing side
        going terminal can never conclude the album in the window between our
        admitting this side and queueing it.
        """
        with self._lock:
            if self._finished:
                return False
            if self._cancel_all.is_set():
                return False
            if any(s.index == side.index for s in self.sides):
                return False
            self._side_cancel[side.index] = threading.Event()
            self.sides.append(side)
        # Outside the lock: _set_state locks internally (non-reentrant Lock), and a
        # pool submit must never run under it.
        self._set_state(side, SideState.QUEUED)
        self._analysis_pool.submit(self._run_analysis, side)
        return True

    def start(self) -> None:
        """Queue every mapped side for analysis. Unmapped sides go to ERROR."""
        for side in self.sides:
            if side.wav_path is None:
                self._set_state(side, SideState.ERROR, "no WAV mapped to this side",
                                phase="setup")
                continue
            self._analysis_pool.submit(self._run_analysis, side)

    def _run_analysis(self, side: SideJob) -> None:
        if self._cancelled(side):
            self._set_state(side, SideState.CANCELLED)
            return
        self._set_state(side, SideState.ANALYZING)
        try:
            analysis = self._analyze_fn(side, self._should_cancel(side))
        except NeedsAttention as flag:
            # A guard trip, not a failure. Keep the analysis it produced.
            if self._cancelled(side):
                self._set_state(side, SideState.CANCELLED)
                return
            side.analysis = flag.analysis
            with self._lock:
                side.attention = flag.reason
            self._set_state(side, SideState.NEEDS_ATTENTION)
            return
        except Exception as exc:
            if self._cancelled(side):
                self._set_state(side, SideState.CANCELLED)
            else:
                self._set_state(side, SideState.ERROR, self._describe(exc),
                                phase="analysis", detail=traceback.format_exc())
            return
        side.analysis = analysis
        if self._cancelled(side):
            self._set_state(side, SideState.CANCELLED)
        else:
            self._set_state(side, SideState.READY)

    def mark_resolving(self, index: int) -> None:
        """The user opened a READY side for review."""
        side = self._by_index(index)
        if side.state in (SideState.READY, SideState.NEEDS_ATTENTION):
            self._set_state(side, SideState.RESOLVING)

    def accept_side(
        self,
        index: int,
        timestamps: list[float],
        titles: list[str] | None = None,
        artists: list[str] | None = None,
    ) -> None:
        """Confirm a side's cuts and queue it to encode in the background.

        Accepting *is* the commit: the reviewer's titles/artists are snapshotted
        onto the SideJob here and the encode is enqueued immediately, so there is
        no accepted-but-not-yet-encoded limbo for a UI to lose when the user
        moves on to the next side.
        """
        side = self._by_index(index)
        if side.state in _TERMINAL:
            return
        side.timestamps = list(timestamps)
        if titles is not None:
            side.titles = list(titles)
        if artists is not None:
            side.artists = list(artists)
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
                self._set_state(side, SideState.ERROR, self._describe(exc),
                                phase="encode", detail=traceback.format_exc())
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
        """Stop the pools, and ask any running task to return.

        ``cancel_futures`` only drops work that has not *started*: a task
        already running cannot be interrupted from outside, and a running
        analysis is exactly what used to keep non-daemon pool threads -- and so
        the whole interpreter -- alive after the window had closed. So the
        cancel flags the tasks poll are set here too.

        Deliberately the flags only, not :meth:`cancel_all`: shutting the pools
        down is a statement about the pools, and rewriting finished sides to
        CANCELLED would make "we stopped listening" indistinguishable from "the
        user cancelled it", including for anything reading state afterwards.
        """
        self._cancel_all.set()
        for event in self._side_cancel.values():
            event.set()
        self._analysis_pool.shutdown(wait=wait, cancel_futures=True)
        self._encode_pool.shutdown(wait=wait, cancel_futures=True)
        _LIVE_CONTROLLERS.discard(self)
