"""One bounded-parallel batch runner, shared by every per-track pipeline.

:mod:`core.converter` (WAV -> FLAC) and :mod:`core.mp3_export` (FLAC -> MP3) do
the same thing to different payloads: run an independent ffmpeg subprocess per
track, report "N of M" as each *completes*, poll for cancellation before each
submission and let in-flight work finish, and hand back outcomes in input order
however they finished. They carried a copy each, and the copies noted that they
should be unified if a third caller appeared -- but the real cost of two copies
is not the duplication, it is that cancellation and ordering semantics are the
easy things to fix in one and forget in the other.

So the concurrency lives here once. Callers keep their own result types: what
differs between them is genuinely the payload, not the batching.
"""

from __future__ import annotations

from typing import Callable, Iterable


def run_batch(
    items: Iterable,
    work: Callable,
    *,
    name_of: Callable,
    on_progress=None,
    max_workers: int = 1,
    should_cancel=None,
) -> list:
    """Run ``work(item)`` over ``items``, serially or on a bounded thread pool.

    ``on_progress`` fires once per *completed* item with
    ``(completed_count, total, name)`` -- order-independent "N of M", never
    "track K", because with ``max_workers > 1`` completion order is not input
    order. ``name_of(outcome)`` supplies that name, which is the one thing the
    two pipelines genuinely disagree about.

    ``should_cancel`` is polled before each submission; work already in flight is
    allowed to finish and a partial result comes back. Returns the outcomes in
    **input** order, with never-started slots dropped.
    """
    items = list(items)
    total = len(items)
    outcomes: list = [None] * total

    if max_workers and max_workers > 1 and total > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            for i, item in enumerate(items):
                if should_cancel is not None and should_cancel():
                    break
                futures[pool.submit(work, item)] = i
            completed = 0
            for future in as_completed(futures):
                outcome = future.result()
                outcomes[futures[future]] = outcome
                completed += 1
                if on_progress is not None:
                    on_progress(completed, total, name_of(outcome))
    else:
        completed = 0
        for i, item in enumerate(items):
            if should_cancel is not None and should_cancel():
                break
            outcomes[i] = work(item)
            completed += 1
            if on_progress is not None:
                on_progress(completed, total, name_of(outcomes[i]))

    return [o for o in outcomes if o is not None]
