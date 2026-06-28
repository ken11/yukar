"""arbiter-remainder-slice: Validates the remainder calculation bug after stop
when epic_ids contains duplicates.

finding: arbiter-remainder-slice
target code: apps/api/src/yukar/runs/arbiter_runner.py:160

   for remaining_id in self._epic_ids[self._epic_ids.index(real_epic_id) :]:

list.index() returns the position of the first occurrence.
When _epic_ids contains duplicates and stop is called while processing the second or later
occurrence of the same epic_id, index() returns the head position, so the slice starts
from the already-processed head element, causing a double-count.

Correct behavior: slice from the current loop position (enumerate index).

Test strategy:
- Run the stop path with _epic_ids = ["EP-A", "EP-A", "EP-B"] containing a duplicate.
- The first "EP-A" is treated as a non-existent epic and skipped (_process_epic early return).
- Arrange for _stopped=True to be set just before the second "EP-A" (at the loop head check).
- Expect the number of EpicMergeResult entries added as remaining to be
  "2 items remaining (2nd EP-A + EP-B)".
- With the index() bug, remaining would be 3 items (double-counting the head EP-A).

Status:
- Fixed — regression guard: the stop-path slice now uses the enumerate index instead
  of list.index(), so duplicate epic_ids are counted correctly.
"""

from __future__ import annotations

import asyncio
from typing import Any

# ---------------------------------------------------------------------------
# Logic isolation tests (zero coupling)
# ---------------------------------------------------------------------------


class TestSliceLogicIsolated:
    """Validate the stop-path slice logic of ArbiterRunner at the smallest unit.

    Because the actual ArbiterRunner._process_epic has heavy dependencies,
    only the stop-check block from start() is imitated as pure logic
    and tested directly to confirm the difference between index() vs enumerate.
    """

    def _simulate_buggy_slice(
        self,
        epic_ids: list[str],
        stopped_at_id: str,
    ) -> list[str]:
        """Current implementation: uses list.index() (buggy).

        Mimics the behavior at apps/api/src/yukar/runs/arbiter_runner.py:160:
          for remaining_id in self._epic_ids[self._epic_ids.index(real_epic_id) :]:
        """
        return epic_ids[epic_ids.index(stopped_at_id) :]

    def _simulate_correct_slice(
        self,
        epic_ids: list[str],
        stopped_at_idx: int,
    ) -> list[str]:
        """Correct implementation: uses enumerate index.

        Proposed fix:
          for idx, real_epic_id in enumerate(self._epic_ids):
              if self._stopped:
                  for remaining_id in self._epic_ids[idx:]:
                      ...
        """
        return epic_ids[stopped_at_idx:]

    # ------------------------------------------------------------------
    # No duplicates: verify both implementations agree (baseline test)
    # ------------------------------------------------------------------

    def test_no_duplicates_both_agree(self) -> None:
        """When there are no duplicates, index() and enumerate produce the same result
        (baseline)."""
        ids = ["EP-A", "EP-B", "EP-C"]
        stopped_idx = 1  # stopped at "EP-B"
        stopped_id = ids[stopped_idx]

        buggy = self._simulate_buggy_slice(ids, stopped_id)
        correct = self._simulate_correct_slice(ids, stopped_idx)

        assert buggy == correct == ["EP-B", "EP-C"]

    # ------------------------------------------------------------------
    # Duplicates: stopped at the 2nd element with the same name as the head
    # ------------------------------------------------------------------

    def test_duplicate_ids_buggy_slice_is_wrong(self) -> None:
        """With duplicates: regression test recording the difference between index() and
        enumerate implementations.

        _epic_ids = ["EP-A", "EP-A", "EP-B"]
        Assume the loop is stopped at the 2nd "EP-A" (idx=1).

        Correct remaining: ["EP-A", "EP-B"]  (from idx=1) — enumerate implementation
        Buggy  remaining: ["EP-A", "EP-A", "EP-B"]  (from idx=0) — index() implementation

        This test records both: "enumerate-based slice correctly returns 2 items"
        and "index()-based slice over-counts (3 items)".
        After the fix, ArbiterRunner uses enumerate so actual behavior matches correct.
        """
        ids = ["EP-A", "EP-A", "EP-B"]
        stopped_idx = 1  # stopped at 2nd "EP-A"

        correct = self._simulate_correct_slice(ids, stopped_idx)
        buggy = self._simulate_buggy_slice(ids, ids[stopped_idx])

        # enumerate-based (correct implementation) returns 2 items
        assert len(correct) == 2, f"correct slice should be 2 items, got {correct}"
        assert correct == ["EP-A", "EP-B"]
        # index()-based (old buggy implementation) returns 3 items (recorded for regression)
        assert len(buggy) == 3, (
            f"buggy slice (index-based) returns 3 items due to first-occurrence; got {buggy}"
        )
        # The two do not agree (enumerate is correct, index() over-counts)
        assert buggy != correct

    def test_duplicate_ids_correct_slice_length(self) -> None:
        """With duplicates: enumerate-based slice correctly returns 2 items (characterization)."""
        ids = ["EP-A", "EP-A", "EP-B"]
        stopped_idx = 1  # stopped at 2nd "EP-A"

        correct = self._simulate_correct_slice(ids, stopped_idx)

        assert correct == ["EP-A", "EP-B"]
        assert len(correct) == 2

    def test_duplicate_ids_buggy_slice_length(self) -> None:
        """With duplicates: index()-based slice incorrectly returns 3 items
        (characterization of current behavior)."""
        ids = ["EP-A", "EP-A", "EP-B"]
        stopped_at_id = ids[1]  # 2nd "EP-A"

        buggy = self._simulate_buggy_slice(ids, stopped_at_id)

        # Current incorrect behavior: double-counts the head "EP-A" and returns 3 items
        assert buggy == ["EP-A", "EP-A", "EP-B"]
        assert len(buggy) == 3  # should be 2 items

    def test_triplicate_ids_stopped_at_third(self) -> None:
        """Triple duplicates: characterize the index() bug when stopped at the 3rd occurrence."""
        ids = ["EP-X", "EP-X", "EP-X", "EP-Y"]
        stopped_idx = 2  # stopped at 3rd "EP-X"

        correct = self._simulate_correct_slice(ids, stopped_idx)
        buggy = self._simulate_buggy_slice(ids, ids[stopped_idx])

        # Correct: ["EP-X", "EP-Y"] — 2 items
        assert correct == ["EP-X", "EP-Y"]
        # Buggy: ["EP-X", "EP-X", "EP-X", "EP-Y"] — 4 items (3 too many)
        assert len(buggy) == 4
        assert buggy != correct

    def test_last_element_duplicate_no_bug(self) -> None:
        """Duplicates only at the tail: index() is correct when stopped at the head."""
        ids = ["EP-A", "EP-B", "EP-B"]
        stopped_idx = 0  # stopped at head "EP-A"

        correct = self._simulate_correct_slice(ids, stopped_idx)
        buggy = self._simulate_buggy_slice(ids, ids[stopped_idx])

        # "EP-A" is not duplicated so index() is also correct
        assert buggy == correct == ["EP-A", "EP-B", "EP-B"]


# ---------------------------------------------------------------------------
# ArbiterRunner integration tests: control when the _stopped flag is set
# ---------------------------------------------------------------------------


class TestArbiterRunnerStopRemainder:
    """Start ArbiterRunner.start() directly and verify the results count after stop.

    Because _process_epic is heavy, use the path where
    "epic not found → skipped, return immediately" to iterate the loop
    without touching LLM / git / worktree.
    """

    async def test_stop_mid_batch_duplicate_ids_remainder_count(self, tmp_path: Any) -> None:
        """With duplicate epic_ids, when _stopped=True is set at the loop head of the 2nd element,
        expect that remaining EpicMergeResult entries are correctly calculated using enumerate.

        Fixed — regression guard: the enumerate-based fix counts duplicates correctly.
        """
        from yukar.config.settings import LLMSettings
        from yukar.models.project import Project
        from yukar.runs.arbiter_runner import ArbiterRunner
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        pid = "proj"
        # Create the project but intentionally do not create epics (not found → skipped).
        await save_project(root, Project(id=pid, name=pid))

        # _epic_ids = ["EP-A", "EP-A", "EP-B"]
        # 1st "EP-A" → get_epic returns None → skipped
        # 2nd "EP-A" → stop() before the if self._stopped check at the loop head
        # "EP-B"     → should be added as remaining
        epic_ids = ["EP-A", "EP-A", "EP-B"]

        llm = LLMSettings(provider="fake")
        runner = ArbiterRunner(llm_settings=llm, epic_ids=epic_ids)

        # Stop after _process_epic has been called once (after processing the 1st EP-A).
        call_count = 0
        original_process = runner._process_epic

        async def _process_epic_with_stop(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Trigger stop after finishing processing of the 1st EP-A
                result = await original_process(*args, **kwargs)
                runner._stopped = True
                return result
            return await original_process(*args, **kwargs)

        runner._process_epic = _process_epic_with_stop  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]

        # Receive EpicMergeProgressEvent(phase="finished") and verify results.
        from yukar.events import bus as event_bus
        from yukar.models.events import EpicMergeProgressEvent

        finished_events: list[EpicMergeProgressEvent] = []

        async def _collect() -> None:
            async with event_bus.subscribe_project(pid) as q:
                while True:
                    import contextlib
                    with contextlib.suppress(TimeoutError):
                        ev = await asyncio.wait_for(q.get(), timeout=10.0)
                        if ev is None:
                            break
                        if isinstance(ev, EpicMergeProgressEvent) and ev.phase == "finished":
                            finished_events.append(ev)
                            break
                        continue
                    break

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        await runner.start(root=root, project_id=pid, epic_id="__merge__", run_id="run-t")

        event_bus.publish_project_sentinel(pid)
        import contextlib
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(collector, timeout=5.0)

        assert finished_events, "Did not receive a phase=finished event"
        final = finished_events[0]

        # Verify all results.
        # Expected:
        #   [0] EP-A (1st) → skipped (epic not found)
        #   [1] EP-A (2nd) → skipped (batch stopped) — added from remaining
        #   [2] EP-B       → skipped (batch stopped) — added from remaining
        # Should be 3 results total, but with the duplicate-count bug
        # the remaining slice at [1] becomes ["EP-A", "EP-A", "EP-B"] (3 items)
        # which together with [0]'s EP-A gives 4 items total.

        # Record the expected value for correct behavior as characterization:
        # remaining slice is 2 items (["EP-A", "EP-B"])
        # → total results is 3
        total_results = len(final.results)

        # First verify the breakdown of skipped epic_ids (as debug information)
        result_ids = [r.epic_id for r in final.results]
        result_statuses = [r.status for r in final.results]

        # 1st EP-A: _process_epic is called → skipped
        assert final.results[0].epic_id == "EP-A"
        assert final.results[0].status == "skipped"

        # With the current index() bug: remaining = ["EP-A", "EP-A", "EP-B"] = 3 items
        # → 1st EP-A (skipped from _process_epic) + 3 items = 4 total
        # After fix: remaining = ["EP-A", "EP-B"] = 2 items
        # → 1st EP-A (skipped) + 2 items = 3 total

        # After fix: enumerate gives correct 2 remaining items → 3 total
        assert total_results == 3, (
            f"expected total_results=3, got total_results={total_results}, "
            f"result_ids={result_ids}, statuses={result_statuses}"
        )

    async def test_stop_mid_batch_duplicate_ids_no_overcounting(self, tmp_path: Any) -> None:
        """With duplicate epic_ids, expect results after stop to have the correct count
        with no double-counting.

        Fixed — regression guard: the index() bug is resolved; no double-counting occurs.
        """
        from yukar.config.settings import LLMSettings
        from yukar.models.project import Project
        from yukar.runs.arbiter_runner import ArbiterRunner
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        pid = "proj-dup"
        await save_project(root, Project(id=pid, name=pid))

        epic_ids = ["EP-A", "EP-A", "EP-B"]

        llm = LLMSettings(provider="fake")
        runner = ArbiterRunner(llm_settings=llm, epic_ids=epic_ids)

        # Stop after processing the 1st EP-A
        call_count = 0
        original_process = runner._process_epic

        async def _process_with_stop(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            result = await original_process(*args, **kwargs)
            if call_count == 1:
                runner._stopped = True
            return result

        runner._process_epic = _process_with_stop  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]

        from yukar.events import bus as event_bus
        from yukar.models.events import EpicMergeProgressEvent

        finished: list[EpicMergeProgressEvent] = []

        async def _collect() -> None:
            async with event_bus.subscribe_project(pid) as q:
                import contextlib
                while True:
                    ev = None
                    with contextlib.suppress(TimeoutError):
                        ev = await asyncio.wait_for(q.get(), timeout=10.0)
                    if ev is None:
                        break
                    if isinstance(ev, EpicMergeProgressEvent) and ev.phase == "finished":
                        finished.append(ev)
                        break

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        await runner.start(root=root, project_id=pid, epic_id="__merge__", run_id="run-dup")

        event_bus.publish_project_sentinel(pid)
        import contextlib
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(collector, timeout=5.0)

        assert finished, "Did not receive a phase=finished event"
        final = finished[0]

        # Correct result:
        #   results[0]: EP-A skipped (epic not found, from _process_epic)
        #   results[1]: EP-A skipped (batch stopped, from remaining slice at idx=1)
        #   results[2]: EP-B skipped (batch stopped, from remaining slice at idx=2)
        # Total: 3 items
        assert len(final.results) == 3, (
            f"Expected 3 items, got {len(final.results)}: "
            f"{[(r.epic_id, r.status) for r in final.results]}"
        )
        # No double-count: EP-A appears twice, EP-B appears once
        ids = [r.epic_id for r in final.results]
        assert ids.count("EP-A") == 2, f"EP-A should appear twice: {ids}"
        assert ids.count("EP-B") == 1, f"EP-B should appear once: {ids}"
        # All statuses are skipped / error (due to stop)
        for r in final.results:
            assert r.status in ("skipped", "error"), f"Unexpected status: {r}"
