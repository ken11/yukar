"""Tests for token usage tracking: pricing, exchange, tracker, budget, API.

Coverage:
- pricing: all model tiers, cache tokens, embedding, unknown model
- exchange: API fetch, 12h cache hit, stale → re-fetch, fallback
- tracker: accumulate, persist/restore, budget stop trigger
- API: GET /api/usage, PUT /api/usage/budget
- SSE: TokenUsageEvent published when record() is called
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# 1. Pricing
# ---------------------------------------------------------------------------


class TestPricing:
    def test_opus_4_8(self) -> None:
        from yukar.usage.pricing import compute_cost_usd

        cost = compute_cost_usd("claude-opus-4-8", input_tokens=1_000_000, output_tokens=0)
        assert abs(cost - 5.0) < 1e-9

    def test_opus_4_8_output(self) -> None:
        from yukar.usage.pricing import compute_cost_usd

        cost = compute_cost_usd("claude-opus-4-8", input_tokens=0, output_tokens=1_000_000)
        assert abs(cost - 25.0) < 1e-9

    def test_sonnet_4_6_bedrock_arn(self) -> None:
        from yukar.usage.pricing import compute_cost_usd

        model_id = "anthropic.claude-sonnet-4-6-20250514-v1:0"
        cost = compute_cost_usd(model_id, input_tokens=1_000_000, output_tokens=0)
        assert abs(cost - 3.0) < 1e-9

    def test_sonnet_4_6_us_prefix(self) -> None:
        from yukar.usage.pricing import compute_cost_usd

        model_id = "us.anthropic.claude-sonnet-4-6-20250514-v1:0"
        cost = compute_cost_usd(model_id, input_tokens=1_000_000, output_tokens=0)
        assert abs(cost - 3.0) < 1e-9

    def test_haiku_4_5(self) -> None:
        from yukar.usage.pricing import compute_cost_usd

        cost = compute_cost_usd("claude-haiku-4-5", input_tokens=1_000_000, output_tokens=0)
        assert abs(cost - 1.0) < 1e-9

    def test_fable_5(self) -> None:
        from yukar.usage.pricing import compute_cost_usd

        cost = compute_cost_usd("claude-fable-5", input_tokens=1_000_000, output_tokens=0)
        assert abs(cost - 10.0) < 1e-9

    def test_sonnet_5(self) -> None:
        from yukar.usage.pricing import compute_cost_usd

        # Standard Sonnet 5 rate: input=3.0, output=15.0 per 1M.
        cost = compute_cost_usd("claude-sonnet-5", input_tokens=1_000_000, output_tokens=1_000_000)
        assert abs(cost - (3.0 + 15.0)) < 1e-9

    def test_sonnet_5_bedrock_arn_does_not_collide_with_sonnet_4(self) -> None:
        """A Sonnet 5 Bedrock id resolves to the sonnet-5 entry, not a sonnet-4-* one."""
        from yukar.usage.pricing import get_pricing

        five = get_pricing("us.anthropic.claude-sonnet-5-20260514-v1:0")
        four = get_pricing("us.anthropic.claude-sonnet-4-6-20251201-v1:0")
        # Both must resolve (not fall through to None → zeroed cost attribution).
        assert five is not None
        assert four is not None
        assert five.input == 3.0
        assert four.input == 3.0

    def test_cache_read_tokens(self) -> None:
        from yukar.usage.pricing import compute_cost_usd

        cost = compute_cost_usd("sonnet-4-6", cache_read_tokens=1_000_000)
        # 0.30 USD per 1M cache_read tokens for sonnet-4-6
        assert abs(cost - 0.30) < 1e-9

    def test_cache_write_tokens(self) -> None:
        from yukar.usage.pricing import compute_cost_usd

        cost = compute_cost_usd("sonnet-4-6", cache_write_tokens=1_000_000)
        # 3.75 USD per 1M cache_write tokens
        assert abs(cost - 3.75) < 1e-9

    def test_titan_embedding(self) -> None:
        from yukar.usage.pricing import compute_cost_usd

        cost = compute_cost_usd("amazon.titan-embed-text-v2:0", embedding_tokens=1_000_000)
        assert abs(cost - 0.02) < 1e-9

    def test_unknown_model_returns_zero(self, caplog: Any) -> None:
        import logging

        from yukar.usage.pricing import compute_cost_usd

        with caplog.at_level(logging.WARNING, logger="yukar.usage.pricing"):
            cost = compute_cost_usd("completely-unknown-model-xyz", input_tokens=100_000)
        assert cost == 0.0
        assert "Unknown model" in caplog.text

    def test_all_token_types_combined(self) -> None:
        from yukar.usage.pricing import compute_cost_usd

        # sonnet-4-6: input=3.0, output=15.0, cache_read=0.30, cache_write=3.75 per 1M
        cost = compute_cost_usd(
            "sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_write_tokens=1_000_000,
        )
        expected = 3.0 + 15.0 + 0.30 + 3.75
        assert abs(cost - expected) < 1e-9


# ---------------------------------------------------------------------------
# 2. Exchange rate
# ---------------------------------------------------------------------------


class TestExchangeRate:
    async def test_fallback_rate_when_no_cache_and_api_fails(self, tmp_path: Path) -> None:
        from yukar.usage.exchange import _FALLBACK_JPY, ExchangeRateProvider

        provider = ExchangeRateProvider(cache_path=tmp_path / "rate.yaml")
        with patch.object(provider, "_fetch_blocking", side_effect=RuntimeError("no net")):
            rate = await provider.get_rate()
        assert rate == _FALLBACK_JPY
        assert provider.get_rate_info()["source"] == "fallback"

    async def test_api_fetch_success(self, tmp_path: Path) -> None:
        from yukar.usage.exchange import ExchangeRateProvider

        provider = ExchangeRateProvider(cache_path=tmp_path / "rate.yaml")
        with patch.object(provider, "_fetch_blocking", return_value=150.5):
            rate = await provider.get_rate()
        assert abs(rate - 150.5) < 1e-9
        assert provider.get_rate_info()["source"] == "api"

    async def test_cache_used_within_ttl(self, tmp_path: Path) -> None:
        from yukar.usage.exchange import ExchangeRateProvider

        provider = ExchangeRateProvider(cache_path=tmp_path / "rate.yaml")
        # Prime in-memory state so it looks fresh.
        provider._rate = 145.0
        provider._fetched_at = datetime.now(UTC)
        provider._source = "api"
        provider._loaded = True

        with patch.object(provider, "_fetch_blocking", side_effect=AssertionError("no call")):
            rate = await provider.get_rate()
        assert abs(rate - 145.0) < 1e-9

    async def test_stale_cache_triggers_refresh(self, tmp_path: Path) -> None:
        from yukar.usage.exchange import _CACHE_TTL, ExchangeRateProvider

        provider = ExchangeRateProvider(cache_path=tmp_path / "rate.yaml")
        provider._rate = 140.0
        provider._fetched_at = datetime.now(UTC) - _CACHE_TTL - timedelta(minutes=1)
        provider._source = "cache"
        provider._loaded = True

        with patch.object(provider, "_fetch_blocking", return_value=155.0):
            rate = await provider.get_rate()
        assert abs(rate - 155.0) < 1e-9

    async def test_stale_cache_fallback_on_fail(self, tmp_path: Path) -> None:
        from yukar.usage.exchange import _CACHE_TTL, ExchangeRateProvider

        provider = ExchangeRateProvider(cache_path=tmp_path / "rate.yaml")
        provider._rate = 148.0
        provider._fetched_at = datetime.now(UTC) - _CACHE_TTL - timedelta(minutes=1)
        provider._source = "cache"
        provider._loaded = True

        with patch.object(provider, "_fetch_blocking", side_effect=RuntimeError("no net")):
            rate = await provider.get_rate()
        # Falls back to the existing cached value, not the hardcoded fallback.
        assert abs(rate - 148.0) < 1e-9

    async def test_cache_persisted_and_loaded(self, tmp_path: Path) -> None:
        from yukar.usage.exchange import ExchangeRateProvider

        cache_path = tmp_path / "rate.yaml"
        p1 = ExchangeRateProvider(cache_path=cache_path)
        with patch.object(p1, "_fetch_blocking", return_value=152.3):
            await p1.get_rate()
        assert cache_path.exists()

        # Second instance should load from disk cache (source = "cache").
        p2 = ExchangeRateProvider(cache_path=cache_path)
        p2._load_cache()
        assert abs(p2._rate - 152.3) < 0.01
        assert p2._source == "cache"

    async def test_fetch_disabled_skips_http(self, tmp_path: Path) -> None:
        """When fetch_enabled=False, _fetch_blocking must never be called (A4)."""
        from yukar.usage.exchange import _FALLBACK_JPY, ExchangeRateProvider

        provider = ExchangeRateProvider(
            cache_path=tmp_path / "rate.yaml",
            fetch_enabled=False,
        )
        with patch.object(
            provider, "_fetch_blocking", side_effect=AssertionError("HTTP fetch must not occur")
        ):
            rate = await provider.get_rate()
        # No cache, fetch disabled → must return fallback rate without raising.
        assert rate == _FALLBACK_JPY

    def test_fetch_blocking_parses_currency_api_json(self) -> None:
        """_fetch_blocking parses the new currency-api JSON shape {"usd": {"jpy": ...}}."""
        from unittest.mock import MagicMock, patch

        from yukar.usage.exchange import ExchangeRateProvider

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"date": "2026-06-16", "usd": {"jpy": 160.27, "eur": 0.9}}

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("httpx.Client", return_value=mock_client):
            result = ExchangeRateProvider._fetch_blocking()

        assert abs(result - 160.27) < 1e-9

    def test_fetch_blocking_falls_back_to_secondary_url(self) -> None:
        """_fetch_blocking tries the fallback URL when the primary raises."""
        from unittest.mock import MagicMock, call, patch

        from yukar.usage.exchange import (
            _API_URL_FALLBACK,
            _API_URL_PRIMARY,
            ExchangeRateProvider,
        )

        fallback_response = MagicMock()
        fallback_response.raise_for_status = MagicMock()
        fallback_response.json.return_value = {"date": "2026-06-16", "usd": {"jpy": 158.5}}

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        # Primary call raises; secondary call succeeds.
        mock_client.get.side_effect = [
            ConnectionError("primary down"),
            fallback_response,
        ]

        with patch("httpx.Client", return_value=mock_client):
            result = ExchangeRateProvider._fetch_blocking()

        assert abs(result - 158.5) < 1e-9
        # Verify both URLs were attempted in the correct order.
        assert mock_client.get.call_args_list == [
            call(_API_URL_PRIMARY),
            call(_API_URL_FALLBACK),
        ]


# ---------------------------------------------------------------------------
# 3. Tracker accumulation and persistence
# ---------------------------------------------------------------------------


class TestTracker:
    def _make_tracker(self, tmp_path: Path) -> Any:
        from yukar.usage.tracker import TokenUsageTracker

        ledger = tmp_path / "ledger.yaml"
        return TokenUsageTracker(ledger_path=ledger)

    async def test_record_accumulates_cost(self, tmp_path: Path) -> None:
        tracker = self._make_tracker(tmp_path)
        from yukar.usage.tracker import UsageDelta

        with patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)):
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="manager",
                model_id="sonnet-4-6",  # 3.0 USD / 1M input
                delta=UsageDelta(input_tokens=1_000_000),
            )

        rt = tracker.get_run_totals("r1")
        assert rt is not None
        assert rt.input_tokens == 1_000_000
        assert abs(rt.cost_usd - 3.0) < 1e-9
        assert abs(rt.cost_jpy - 3.0 * 150.0) < 1e-6

    async def test_persist_and_restore(self, tmp_path: Path) -> None:
        tracker = self._make_tracker(tmp_path)
        from yukar.usage.tracker import UsageDelta

        with patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=155.0)):
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="worker",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=500_000, output_tokens=100_000),
            )
        await tracker.flush()

        # Load a fresh tracker from the persisted ledger.
        tracker2 = self._make_tracker(tmp_path)
        await tracker2.load()
        rt = tracker2.get_run_totals("r1")
        assert rt is not None
        assert rt.input_tokens == 500_000
        assert rt.output_tokens == 100_000

    async def test_budget_exceeded_stops_runs(self, tmp_path: Path) -> None:
        tracker = self._make_tracker(tmp_path)

        mock_stop = AsyncMock()
        mock_supervisor = MagicMock()
        mock_supervisor.list_active_runs.return_value = [("root", "p1", "e1")]
        mock_supervisor.stop = mock_stop

        from yukar.usage.tracker import UsageDelta

        tracker._limit_usd = 1.0  # $1 USD limit — easily exceeded by 1M tokens ($3 USD)

        stop_called_with: list[tuple[str, str]] = []

        async def fake_stop_all_runs(
            triggering_project_id: str,
            triggering_epic_id: str,
            triggering_run_id: str,
            spent_usd: float,
            limit_usd: float,
        ) -> None:
            for _root, pid, eid in mock_supervisor.list_active_runs():
                await mock_supervisor.stop(pid, eid)
                stop_called_with.append((pid, eid))

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_stop_all_runs", side_effect=fake_stop_all_runs),
            patch.object(tracker, "_publish_sse"),
        ):
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="manager",
                # Cost: 1M tokens * 3 USD/M = 3.0 USD > 1.0 USD limit
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=1_000_000),
            )

        assert stop_called_with == [("p1", "e1")]

    async def test_concurrent_budget_breach_stops_runs_once(self, tmp_path: Path) -> None:
        tracker = self._make_tracker(tmp_path)

        from yukar.usage.tracker import UsageDelta

        tracker._limit_usd = 1.0  # $1 USD limit
        stop_calls = 0
        active_stops = 0
        max_active_stops = 0

        async def fake_stop_all_runs(*args: object, **kwargs: object) -> None:
            nonlocal stop_calls, active_stops, max_active_stops
            stop_calls += 1
            active_stops += 1
            max_active_stops = max(max_active_stops, active_stops)
            await asyncio.sleep(0)
            active_stops -= 1

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_stop_all_runs", side_effect=fake_stop_all_runs),
            patch.object(tracker, "_publish_sse"),
        ):
            await asyncio.gather(
                *[
                    tracker.record(
                        project_id="p1",
                        epic_id="e1",
                        run_id=f"r{i}",
                        role="manager",
                        model_id="sonnet-4-6",
                        delta=UsageDelta(input_tokens=1_000_000),
                    )
                    for i in range(3)
                ]
            )

        assert stop_calls == 1
        assert max_active_stops == 1

    async def test_budget_increase_rearms_enforcement(self, tmp_path: Path) -> None:
        tracker = self._make_tracker(tmp_path)

        from yukar.usage.tracker import UsageDelta

        # $2 USD limit — exceeded by 1M sonnet-4-6 tokens ($3 USD).
        await tracker.set_budget(2.0)
        stop = AsyncMock()
        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_stop_all_runs", stop),
            patch.object(tracker, "_publish_sse"),
        ):
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="manager",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=1_000_000),
            )
            # After first breach (month_spent=$3), raise the limit to $5.
            # Re-arms enforcement; month_spent($3) < new limit($5) so no breach yet.
            # Second record adds another $3 → month_spent=$6 > $5 → second breach.
            await tracker.set_budget(5.0)
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r2",
                role="manager",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=1_000_000),
            )

        assert stop.await_count == 2

    async def test_budget_not_triggered_below_limit(self, tmp_path: Path) -> None:
        tracker = self._make_tracker(tmp_path)

        mock_supervisor = MagicMock()
        mock_supervisor.list_active_runs.return_value = []

        from yukar.usage.tracker import UsageDelta

        tracker._limit_usd = 10.0  # $10 USD limit — not exceeded by 1000 tokens ($0.003 USD)

        stop_called = False

        async def fake_stop_all_runs(*args: object, **kwargs: object) -> None:
            nonlocal stop_called
            stop_called = True

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_stop_all_runs", side_effect=fake_stop_all_runs),
            patch.object(tracker, "_publish_sse"),
        ):
            # 1000 tokens * 3 USD/M = 0.003 USD << 10 USD limit
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="manager",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=1_000),
            )

        assert not stop_called

    async def test_global_summary(self, tmp_path: Path) -> None:
        tracker = self._make_tracker(tmp_path)
        from yukar.usage.tracker import UsageDelta

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch("yukar.usage.tracker.TokenUsageTracker._publish_sse"),
        ):
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="worker",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=1_000_000),
            )

        summary = tracker.get_global_summary()
        assert summary.total_input_tokens == 1_000_000
        assert abs(summary.total_cost_usd - 3.0) < 1e-9
        # by_project is a list — find the entry for "p1"
        assert any(p.project_id == "p1" for p in summary.by_project)

    async def test_breakdown_scoped_to_current_month(self, tmp_path: Path) -> None:
        """by_project / by_model include only runs active in the current JST month;
        the all-time totals still include prior-month runs."""
        from datetime import UTC, datetime, timedelta

        from yukar.usage.tracker import UsageDelta

        tracker = self._make_tracker(tmp_path)
        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch("yukar.usage.tracker.TokenUsageTracker._publish_sse"),
        ):
            await tracker.record(
                project_id="p-cur",
                epic_id="e1",
                run_id="r-cur",
                role="worker",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=1_000_000),
            )
            await tracker.record(
                project_id="p-old",
                epic_id="e2",
                run_id="r-old",
                role="worker",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=2_000_000),
            )

        # Backdate the old run's activity to ~40 days ago — always a prior month.
        old_rt = next(rt for rt in tracker._runs.values() if rt.run_id == "r-old")
        past = datetime.now(UTC) - timedelta(days=40)
        old_rt.updated_at = past
        old_rt.started_at = past

        summary = tracker.get_global_summary()
        proj_ids = {p.project_id for p in summary.by_project}
        assert "p-cur" in proj_ids  # active this month → in the breakdown
        assert "p-old" not in proj_ids  # last active last month → excluded
        # …but the all-time totals still count both runs.
        assert summary.total_input_tokens == 3_000_000

    async def test_global_summary_by_project_structure(self, tmp_path: Path) -> None:
        """by_project contains typed ProjectUsageSummary with nested epics and runs."""
        tracker = self._make_tracker(tmp_path)
        from yukar.usage.tracker import (
            EpicUsageSummary,
            ProjectUsageSummary,
            RunUsageSummary,
            UsageDelta,
        )

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch("yukar.usage.tracker.TokenUsageTracker._publish_sse"),
        ):
            await tracker.record(
                project_id="proj-a",
                epic_id="epic-1",
                run_id="run-x",
                role="manager",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=500_000, output_tokens=100_000),
            )

        summary = tracker.get_global_summary()
        assert len(summary.by_project) == 1
        proj = summary.by_project[0]
        assert isinstance(proj, ProjectUsageSummary)
        assert proj.project_id == "proj-a"
        assert proj.input_tokens == 500_000
        assert proj.output_tokens == 100_000

        assert len(proj.epics) == 1
        epic = proj.epics[0]
        assert isinstance(epic, EpicUsageSummary)
        assert epic.epic_id == "epic-1"

        assert len(epic.runs) == 1
        run = epic.runs[0]
        assert isinstance(run, RunUsageSummary)
        assert run.run_id == "run-x"

    async def test_global_summary_by_model(self, tmp_path: Path) -> None:
        """by_model aggregates token counts per model_id across all runs."""
        tracker = self._make_tracker(tmp_path)
        from yukar.usage.tracker import ModelUsageSummary, UsageDelta

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch("yukar.usage.tracker.TokenUsageTracker._publish_sse"),
        ):
            # Two runs using the same model.
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="manager",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=1_000_000),
            )
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r2",
                role="worker",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=500_000),
            )
            # One run using a different model.
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r3",
                role="evaluator",
                model_id="haiku-4-5",
                delta=UsageDelta(input_tokens=200_000),
            )

        summary = tracker.get_global_summary()
        assert len(summary.by_model) == 2

        model_map = {m.model_id: m for m in summary.by_model}
        assert "sonnet-4-6" in model_map
        assert "haiku-4-5" in model_map

        sonnet = model_map["sonnet-4-6"]
        assert isinstance(sonnet, ModelUsageSummary)
        assert sonnet.input_tokens == 1_500_000

        haiku = model_map["haiku-4-5"]
        assert haiku.input_tokens == 200_000

    async def test_persist_restore_v2_format(self, tmp_path: Path) -> None:
        """Ledger written in v2 format (by_model list) loads back correctly."""
        tracker = self._make_tracker(tmp_path)
        from yukar.usage.tracker import UsageDelta

        with patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=155.0)):
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="manager",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=1_000_000),
            )
        await tracker.flush()

        tracker2 = self._make_tracker(tmp_path)
        await tracker2.load()
        rt = tracker2.get_run_totals("r1")
        assert rt is not None
        assert len(rt.by_model) == 1
        mb = rt.by_model[0]
        assert mb.model_id == "sonnet-4-6"
        assert mb.role == "manager"
        assert mb.input_tokens == 1_000_000

    async def test_persist_restore_v1_migration(self, tmp_path: Path) -> None:
        """v1 ledger files with by_role composite keys are migrated transparently."""
        from yukar.storage.yaml_io import write_yaml

        # Write a v1-format ledger manually.
        ledger_path = tmp_path / "ledger.yaml"
        v1_data = {
            "limit_jpy": None,
            "reset_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "runs": [
                {
                    "project_id": "p1",
                    "epic_id": "e1",
                    "run_id": "r-v1",
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "input_tokens": 500_000,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "embedding_tokens": 0,
                    "total_tokens": 500_000,
                    "cost_usd": 1.5,
                    "cost_jpy": 232.5,
                    # v1 format: composite "role/model_id" key
                    "by_role": {
                        "manager/sonnet-4-6": {
                            "input_tokens": 500_000,
                            "output_tokens": 0,
                            "cache_read_tokens": 0,
                            "cache_write_tokens": 0,
                            "embedding_tokens": 0,
                            "cost_usd": 1.5,
                        }
                    },
                }
            ],
        }
        await write_yaml(ledger_path, v1_data)

        from yukar.usage.tracker import TokenUsageTracker

        tracker = TokenUsageTracker(ledger_path=ledger_path)
        await tracker.load()

        rt = tracker.get_run_totals("r-v1")
        assert rt is not None
        assert rt.input_tokens == 500_000
        # Migration should have produced one _ModelBreakdown entry.
        assert len(rt.by_model) == 1
        mb = rt.by_model[0]
        assert mb.model_id == "sonnet-4-6"
        assert mb.role == "manager"

    async def test_daily_bucket_increments_on_record(self, tmp_path: Path) -> None:
        """record() increments the JST daily bucket on each call."""
        from unittest.mock import patch
        from zoneinfo import ZoneInfo

        from yukar.usage.tracker import TokenUsageTracker, UsageDelta

        tracker = TokenUsageTracker(ledger_path=tmp_path / "ledger.yaml")
        _JST = ZoneInfo("Asia/Tokyo")

        today_str = datetime.now(_JST).date().isoformat()

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch("yukar.usage.tracker.TokenUsageTracker._publish_sse"),
        ):
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="manager",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=200_000, output_tokens=50_000),
            )
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="manager",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=100_000),
            )

        assert today_str in tracker._daily
        bucket = tracker._daily[today_str]
        assert bucket.input_tokens == 300_000
        assert bucket.output_tokens == 50_000
        assert bucket.total_tokens == 350_000

    async def test_daily_bucket_persisted_and_restored(self, tmp_path: Path) -> None:
        """Daily buckets survive flush → load cycle."""
        from unittest.mock import patch
        from zoneinfo import ZoneInfo

        from yukar.usage.tracker import TokenUsageTracker, UsageDelta

        _JST = ZoneInfo("Asia/Tokyo")
        today_str = datetime.now(_JST).date().isoformat()

        tracker = TokenUsageTracker(ledger_path=tmp_path / "ledger.yaml")
        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=155.0)),
            patch("yukar.usage.tracker.TokenUsageTracker._publish_sse"),
        ):
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="worker",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=1_000_000),
            )
        await tracker.flush()

        tracker2 = TokenUsageTracker(ledger_path=tmp_path / "ledger.yaml")
        await tracker2.load()
        assert today_str in tracker2._daily
        bucket = tracker2._daily[today_str]
        assert bucket.input_tokens == 1_000_000

    async def test_daily_migration_from_existing_runs(self, tmp_path: Path) -> None:
        """Existing ledger without 'daily' key gets daily buckets built from run started_at."""
        from yukar.storage.yaml_io import write_yaml
        from yukar.usage.tracker import TokenUsageTracker

        ledger_path = tmp_path / "ledger.yaml"
        existing_data = {
            "limit_jpy": None,
            "reset_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "runs": [
                {
                    "project_id": "p1",
                    "epic_id": "e1",
                    "run_id": "run-migrated",
                    # UTC midnight 2026-06-01 → JST 2026-06-01 (UTC+9, still same day)
                    "started_at": "2026-06-01T00:00:00+00:00",
                    "updated_at": "2026-06-01T01:00:00+00:00",
                    "input_tokens": 400_000,
                    "output_tokens": 80_000,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "embedding_tokens": 0,
                    "total_tokens": 480_000,
                    "cost_usd": 2.4,
                    "cost_jpy": 372.0,
                    "by_model": [
                        {
                            "model_id": "sonnet-4-6",
                            "role": "manager",
                            "input_tokens": 400_000,
                            "output_tokens": 80_000,
                            "cache_read_tokens": 0,
                            "cache_write_tokens": 0,
                            "embedding_tokens": 0,
                            "cost_usd": 2.4,
                        }
                    ],
                }
            ],
            # No "daily" key — migration should happen.
        }
        await write_yaml(ledger_path, existing_data)

        tracker = TokenUsageTracker(ledger_path=ledger_path)
        await tracker.load()

        # The run's JST date for 2026-06-01T00:00:00+00:00 is 2026-06-01 (UTC+9 = 09:00 JST).
        assert "2026-06-01" in tracker._daily
        bucket = tracker._daily["2026-06-01"]
        assert bucket.input_tokens == 400_000
        assert bucket.output_tokens == 80_000
        assert abs(bucket.cost_jpy - 372.0) < 1e-6

    async def test_load_skips_migration_when_daily_key_exists_but_empty(
        self, tmp_path: Path
    ) -> None:
        """Loading a ledger with daily: {} must not re-run migration from runs.

        The key-presence check (if "daily" in data) skips migration even when
        the value is an empty dict, preventing double-counting of run tokens.
        """
        from yukar.storage.yaml_io import write_yaml
        from yukar.usage.tracker import TokenUsageTracker

        ledger_path = tmp_path / "ledger.yaml"
        existing_data = {
            "limit_jpy": None,
            "reset_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "runs": [
                {
                    "project_id": "p1",
                    "epic_id": "e1",
                    "run_id": "run-no-migrate",
                    "started_at": "2026-06-01T00:00:00+00:00",
                    "updated_at": "2026-06-01T01:00:00+00:00",
                    "input_tokens": 100_000,
                    "output_tokens": 20_000,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "embedding_tokens": 0,
                    "total_tokens": 120_000,
                    "cost_usd": 0.6,
                    "cost_jpy": 93.0,
                    "by_model": [],
                }
            ],
            # "daily" key exists but is empty — migration must be skipped.
            "daily": {},
        }
        await write_yaml(ledger_path, existing_data)

        tracker = TokenUsageTracker(ledger_path=ledger_path)
        await tracker.load()

        # daily remains empty (tokens from runs must not be re-counted).
        assert len(tracker._daily) == 0, (
            f"migration must be skipped when 'daily' key exists (even empty); "
            f"got {list(tracker._daily.keys())}"
        )

    async def test_get_global_summary_includes_daily(self, tmp_path: Path) -> None:
        """get_global_summary() returns today/this_month/daily fields."""
        from unittest.mock import patch
        from zoneinfo import ZoneInfo

        from yukar.usage.tracker import PeriodTotals, TokenUsageTracker, UsageDelta

        _JST = ZoneInfo("Asia/Tokyo")
        today_str = datetime.now(_JST).date().isoformat()

        tracker = TokenUsageTracker(ledger_path=tmp_path / "ledger.yaml")
        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch("yukar.usage.tracker.TokenUsageTracker._publish_sse"),
        ):
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="manager",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=500_000),
            )

        summary = tracker.get_global_summary()
        assert summary.as_of_date == today_str
        assert isinstance(summary.today, PeriodTotals)
        assert summary.today.input_tokens == 500_000
        assert isinstance(summary.this_month, PeriodTotals)
        assert summary.this_month.input_tokens == 500_000
        assert len(summary.daily) >= 1
        # daily must be sorted by date.
        dates = [b.date for b in summary.daily]
        assert dates == sorted(dates)
        # The today bucket is in the list.
        assert any(b.date == today_str for b in summary.daily)

    # ------------------------------------------------------------------
    # Resilient load tests (regression for data-loss bug)
    # ------------------------------------------------------------------

    async def test_load_null_cache_tokens_does_not_drop_following_run(self, tmp_path: Path) -> None:
        """cache_read_tokens=null (None) in one run must not prevent subsequent runs from loading.

        Regression test: previously int(None) raised TypeError inside the shared
        try/except, causing all runs after the corrupt record to be lost and then
        permanently deleted on the next flush.
        """
        from yukar.storage.yaml_io import write_yaml
        from yukar.usage.tracker import TokenUsageTracker

        ledger_path = tmp_path / "ledger.yaml"
        data = {
            "limit_jpy": None,
            "reset_at": "2026-01-01T00:00:00+00:00",
            "baseline_jpy": 0.0,
            "updated_at": "2026-01-01T00:00:00+00:00",
            "runs": [
                {
                    "project_id": "p1",
                    "epic_id": "e1",
                    "run_id": "run-null-cache",
                    "started_at": "2026-06-01T00:00:00+00:00",
                    "updated_at": "2026-06-01T01:00:00+00:00",
                    "input_tokens": 100,
                    "output_tokens": 20,
                    # null cache fields — should be treated as 0, not raise TypeError
                    "cache_read_tokens": None,
                    "cache_write_tokens": None,
                    "embedding_tokens": 0,
                    "total_tokens": 120,
                    "cost_usd": 0.001,
                    "cost_jpy": 0.15,
                    "by_model": [],
                },
                {
                    "project_id": "p1",
                    "epic_id": "e1",
                    "run_id": "run-ep6-normal",
                    "started_at": "2026-06-02T00:00:00+00:00",
                    "updated_at": "2026-06-02T01:00:00+00:00",
                    "input_tokens": 500_000,
                    "output_tokens": 100_000,
                    "cache_read_tokens": 200_000,
                    "cache_write_tokens": 50_000,
                    "embedding_tokens": 0,
                    "total_tokens": 850_000,
                    "cost_usd": 2.0,
                    "cost_jpy": 310.0,
                    "by_model": [
                        {
                            "model_id": "sonnet-4-6",
                            "role": "manager",
                            "input_tokens": 500_000,
                            "output_tokens": 100_000,
                            "cache_read_tokens": 200_000,
                            "cache_write_tokens": 50_000,
                            "embedding_tokens": 0,
                            "cost_usd": 2.0,
                        }
                    ],
                },
            ],
            "daily": {},
        }
        await write_yaml(ledger_path, data)

        tracker = TokenUsageTracker(ledger_path=ledger_path)
        await tracker.load()

        # Both runs must be present.
        assert tracker.get_run_totals("run-null-cache") is not None, (
            "null-cache run must load (0-coerced)"
        )
        assert tracker.get_run_totals("run-ep6-normal") is not None, (
            "EP-6 run must not be dropped by a preceding null-cache run"
        )

        # Null cache tokens should be coerced to 0.
        null_run = tracker.get_run_totals("run-null-cache")
        assert null_run is not None
        assert null_run.cache_read_tokens == 0
        assert null_run.cache_write_tokens == 0

        # The normal run must retain its values.
        ep6 = tracker.get_run_totals("run-ep6-normal")
        assert ep6 is not None
        assert ep6.input_tokens == 500_000
        assert ep6.cache_read_tokens == 200_000

    async def test_load_bad_started_at_does_not_drop_following_run(self, tmp_path: Path) -> None:
        """A run with an invalid started_at string must not cause the next run to be lost.

        The bad timestamp falls back to datetime.now(UTC) and the run is loaded
        (not quarantined).  The following run loads normally.
        """
        from yukar.storage.yaml_io import write_yaml
        from yukar.usage.tracker import TokenUsageTracker

        ledger_path = tmp_path / "ledger.yaml"
        data = {
            "limit_jpy": None,
            "reset_at": "2026-01-01T00:00:00+00:00",
            "baseline_jpy": 0.0,
            "updated_at": "2026-01-01T00:00:00+00:00",
            "runs": [
                {
                    "project_id": "p1",
                    "epic_id": "e1",
                    "run_id": "run-bad-date",
                    # Invalid ISO string — should fall back to now(UTC) without raising.
                    "started_at": "NOT-A-DATE",
                    "updated_at": "ALSO-NOT-A-DATE",
                    "input_tokens": 50,
                    "output_tokens": 10,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "embedding_tokens": 0,
                    "total_tokens": 60,
                    "cost_usd": 0.0,
                    "cost_jpy": 0.0,
                    "by_model": [],
                },
                {
                    "project_id": "p1",
                    "epic_id": "e1",
                    "run_id": "run-after-bad-date",
                    "started_at": "2026-06-10T00:00:00+00:00",
                    "updated_at": "2026-06-10T01:00:00+00:00",
                    "input_tokens": 1_000,
                    "output_tokens": 200,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "embedding_tokens": 0,
                    "total_tokens": 1_200,
                    "cost_usd": 0.005,
                    "cost_jpy": 0.75,
                    "by_model": [],
                },
            ],
            "daily": {},
        }
        await write_yaml(ledger_path, data)

        tracker = TokenUsageTracker(ledger_path=ledger_path)
        await tracker.load()

        # Both runs must survive — bad date is a fallback, not an error.
        assert tracker.get_run_totals("run-bad-date") is not None, (
            "bad-date run must be loaded (with fallback timestamp)"
        )
        assert tracker.get_run_totals("run-after-bad-date") is not None, (
            "run after bad-date run must not be dropped"
        )

    async def test_completely_unparseable_run_quarantined_and_preserved(
        self, tmp_path: Path
    ) -> None:
        """A run dict that cannot be deserialized at all is quarantined in _unparsed_runs.

        After flush() → load(), the quarantined record must still be present
        in _unparsed_runs (verbatim round-trip), and the preceding normal run
        must also survive.
        """
        from yukar.storage.yaml_io import write_yaml
        from yukar.usage.tracker import TokenUsageTracker

        ledger_path = tmp_path / "ledger.yaml"
        # A valid run followed by a record whose int() coercion we make fail
        # by monkey-patching RunTotals.from_dict after loading the file.
        # Instead we construct a record that has a by_model entry with an
        # uncoercible value at the RunTotals level to force the per-run except.
        #
        # To reliably trigger from_dict failure without relying on from_dict
        # internals, we patch from_dict for a specific run_id marker.
        normal_run = {
            "project_id": "p1",
            "epic_id": "e1",
            "run_id": "run-normal",
            "started_at": "2026-06-01T00:00:00+00:00",
            "updated_at": "2026-06-01T01:00:00+00:00",
            "input_tokens": 1_000,
            "output_tokens": 200,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "embedding_tokens": 0,
            "total_tokens": 1_200,
            "cost_usd": 0.003,
            "cost_jpy": 0.46,
            "by_model": [],
        }
        # Craft a record that will raise inside from_dict: by_model contains an
        # object (not a dict) which causes AttributeError in _ModelBreakdown.from_dict.
        bad_run: dict[str, Any] = {
            "project_id": "p1",
            "epic_id": "e1",
            "run_id": "run-corrupt",
            "started_at": "2026-06-02T00:00:00+00:00",
            "updated_at": "2026-06-02T01:00:00+00:00",
            "input_tokens": 999,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "embedding_tokens": 0,
            "total_tokens": 999,
            "cost_usd": 0.0,
            "cost_jpy": 0.0,
            # by_model contains a non-dict item — will raise in _ModelBreakdown.from_dict
            # when it tries d.get("model_id") on a string.
            "by_model": ["not-a-dict"],
        }
        data = {
            "limit_jpy": None,
            "reset_at": "2026-01-01T00:00:00+00:00",
            "baseline_jpy": 0.0,
            "updated_at": "2026-01-01T00:00:00+00:00",
            "runs": [normal_run, bad_run],
            "daily": {},
        }
        await write_yaml(ledger_path, data)

        tracker = TokenUsageTracker(ledger_path=ledger_path)

        # Patch from_dict so that "run-corrupt" always raises.
        from unittest.mock import patch

        from yukar.usage import tracker as tracker_mod

        original = tracker_mod.RunTotals.from_dict

        def patched_from_dict(d: dict[str, Any]) -> tracker_mod.RunTotals:
            if d.get("run_id") == "run-corrupt":
                raise ValueError("simulated complete parse failure")
            return original(d)

        with patch.object(tracker_mod.RunTotals, "from_dict", staticmethod(patched_from_dict)):
            await tracker.load()

        # Normal run must be loaded.
        assert tracker.get_run_totals("run-normal") is not None

        # Corrupt run must be quarantined — not in _runs.
        assert tracker.get_run_totals("run-corrupt") is None
        assert len(tracker._unparsed_runs) == 1
        assert tracker._unparsed_runs[0]["run_id"] == "run-corrupt"

        # After flush() → reload, the quarantined record must still be present.
        await tracker.flush()

        tracker2 = TokenUsageTracker(ledger_path=ledger_path)
        # Patch again for the reload so it stays quarantined.
        with patch.object(tracker_mod.RunTotals, "from_dict", staticmethod(patched_from_dict)):
            await tracker2.load()

        assert tracker2.get_run_totals("run-normal") is not None
        assert len(tracker2._unparsed_runs) == 1, (
            "quarantined run must survive flush → load (verbatim write-back)"
        )
        assert tracker2._unparsed_runs[0]["run_id"] == "run-corrupt"

    async def test_normal_cache_run_round_trips_correctly(self, tmp_path: Path) -> None:
        """A run with all cache fields (by_model with cacheRead/cacheWrite) survives round-trip.

        This is the existing-behaviour preservation test: ensures the resilience
        changes do not alter handling of fully-correct ledger records.
        """
        from yukar.storage.yaml_io import write_yaml
        from yukar.usage.tracker import TokenUsageTracker

        ledger_path = tmp_path / "ledger.yaml"
        data = {
            "limit_jpy": None,
            "reset_at": "2026-01-01T00:00:00+00:00",
            "baseline_jpy": 0.0,
            "updated_at": "2026-01-01T00:00:00+00:00",
            "runs": [
                {
                    "project_id": "proj-cache",
                    "epic_id": "epic-cache",
                    "run_id": "run-with-cache",
                    "started_at": "2026-06-15T10:00:00+00:00",
                    "updated_at": "2026-06-15T10:30:00+00:00",
                    "input_tokens": 800_000,
                    "output_tokens": 120_000,
                    "cache_read_tokens": 300_000,
                    "cache_write_tokens": 60_000,
                    "embedding_tokens": 5_000,
                    "total_tokens": 1_285_000,
                    "cost_usd": 3.5,
                    "cost_jpy": 542.5,
                    "by_model": [
                        {
                            "model_id": "claude-sonnet-4-6",
                            "role": "manager",
                            "input_tokens": 800_000,
                            "output_tokens": 120_000,
                            "cache_read_tokens": 300_000,
                            "cache_write_tokens": 60_000,
                            "embedding_tokens": 0,
                            "cost_usd": 3.5,
                        },
                        {
                            "model_id": "amazon.titan-embed-text-v2:0",
                            "role": "worker",
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "cache_read_tokens": 0,
                            "cache_write_tokens": 0,
                            "embedding_tokens": 5_000,
                            "cost_usd": 0.0,
                        },
                    ],
                }
            ],
            "daily": {},
        }
        await write_yaml(ledger_path, data)

        tracker = TokenUsageTracker(ledger_path=ledger_path)
        await tracker.load()

        rt = tracker.get_run_totals("run-with-cache")
        assert rt is not None
        assert rt.project_id == "proj-cache"
        assert rt.epic_id == "epic-cache"
        assert rt.input_tokens == 800_000
        assert rt.output_tokens == 120_000
        assert rt.cache_read_tokens == 300_000
        assert rt.cache_write_tokens == 60_000
        assert rt.embedding_tokens == 5_000
        assert rt.total_tokens == 1_285_000
        assert abs(rt.cost_usd - 3.5) < 1e-9
        assert abs(rt.cost_jpy - 542.5) < 1e-6
        assert len(rt.by_model) == 2

        # Flush and reload to verify round-trip.
        await tracker.flush()
        tracker2 = TokenUsageTracker(ledger_path=ledger_path)
        await tracker2.load()

        rt2 = tracker2.get_run_totals("run-with-cache")
        assert rt2 is not None
        assert rt2.cache_read_tokens == 300_000
        assert rt2.cache_write_tokens == 60_000
        assert len(rt2.by_model) == 2
        model_ids = {mb.model_id for mb in rt2.by_model}
        assert "claude-sonnet-4-6" in model_ids
        assert "amazon.titan-embed-text-v2:0" in model_ids
        # No quarantined runs.
        assert len(tracker2._unparsed_runs) == 0


# ---------------------------------------------------------------------------
# 4. API endpoints
# ---------------------------------------------------------------------------


async def test_get_usage_summary(app_client: Any) -> None:
    """GET /api/usage returns 200 with expected schema including typed list fields."""
    resp = await app_client.get("/api/usage")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_cost_usd" in data
    assert "total_cost_jpy" in data
    assert "exchange_rate" in data
    assert "budget" in data
    # by_project and by_model must be lists (not dicts).
    assert "by_project" in data
    assert isinstance(data["by_project"], list)
    assert "by_model" in data
    assert isinstance(data["by_model"], list)
    # New daily/period fields.
    assert "today" in data
    assert "input_tokens" in data["today"]
    assert "cost_usd" in data["today"]
    assert "this_month" in data
    assert "input_tokens" in data["this_month"]
    assert "daily" in data
    assert isinstance(data["daily"], list)
    assert "as_of_date" in data


async def test_set_budget(app_client: Any) -> None:
    """PUT /api/usage/budget sets the limit (USD)."""
    resp = await app_client.put("/api/usage/budget", json={"limit_usd": 50.0})
    assert resp.status_code == 200
    data = resp.json()
    assert data["limit_usd"] == 50.0


async def test_set_budget_null_clears(app_client: Any) -> None:
    """PUT /api/usage/budget with null clears the limit."""
    await app_client.put("/api/usage/budget", json={"limit_usd": 50.0})
    resp = await app_client.put("/api/usage/budget", json={"limit_usd": None})
    assert resp.status_code == 200
    assert resp.json()["limit_usd"] is None


async def test_budget_blocks_new_run(app_client: Any) -> None:
    """A regular run cannot start after the configured budget is reached."""
    await app_client.post("/api/projects", json={"id": "budget-run", "name": "P", "repos": []})
    await app_client.post("/api/projects/budget-run/epics", json={"title": "Epic"})
    await app_client.put("/api/usage/budget", json={"limit_usd": 0.0})

    resp = await app_client.post("/api/projects/budget-run/epics/EP-1/run")

    assert resp.status_code == 409
    assert resp.json() == {"detail": "Budget limit reached"}


async def test_budget_blocks_resolve_run(
    app_client: Any,
    fixture_git_repo: Path,
) -> None:
    """A conflict-resolution run cannot start after the budget is reached."""
    await app_client.post(
        "/api/projects",
        json={
            "id": "budget-resolve",
            "name": "P",
            "repos": [{"name": "repo", "path": str(fixture_git_repo)}],
        },
    )
    await app_client.post("/api/projects/budget-resolve/epics", json={"title": "Epic"})
    await app_client.put("/api/usage/budget", json={"limit_usd": 0.0})

    resp = await app_client.post(
        "/api/projects/budget-resolve/epics/EP-1/git/resolve",
        json={"repo": "repo"},
    )

    assert resp.status_code == 409
    assert resp.json() == {"detail": "Budget limit reached"}


# ---------------------------------------------------------------------------
# 5. SSE: TokenUsageEvent published
# ---------------------------------------------------------------------------


async def test_token_usage_event_published(tmp_path: Path) -> None:
    """TokenUsageEvent is published to the event bus when record() is called."""
    from yukar.events import bus as event_bus
    from yukar.models.events import TokenUsageEvent
    from yukar.usage.tracker import TokenUsageTracker, UsageDelta

    ledger = tmp_path / "ledger.yaml"
    tracker = TokenUsageTracker(ledger_path=ledger)

    received: list[Any] = []

    async with event_bus.subscribe("p1", "e1") as q:

        async def record_and_wait() -> None:
            with patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)):
                await tracker.record(
                    project_id="p1",
                    epic_id="e1",
                    run_id="r1",
                    role="manager",
                    model_id="sonnet-4-6",
                    delta=UsageDelta(input_tokens=1000),
                )

        await record_and_wait()

        # Drain queue without blocking.
        while True:
            try:
                event = q.get_nowait()
                received.append(event)
            except asyncio.QueueEmpty:
                break

    usage_events = [e for e in received if isinstance(e, TokenUsageEvent)]
    assert len(usage_events) >= 1
    evt = usage_events[0]
    assert evt.role == "manager"
    assert evt.model_id == "sonnet-4-6"
    assert evt.delta["input"] == 1000


async def test_token_usage_event_published_to_global_stream(tmp_path: Path) -> None:
    """TokenUsageEvent reaches the global usage stream in addition to the
    (project, epic) scoped stream."""
    from yukar.events import bus as event_bus
    from yukar.models.events import TokenUsageEvent
    from yukar.usage.tracker import TokenUsageTracker, UsageDelta

    ledger = tmp_path / "ledger.yaml"
    tracker = TokenUsageTracker(ledger_path=ledger)

    epic_received: list[Any] = []
    global_received: list[Any] = []

    async with (
        event_bus.subscribe("p1", "e1") as eq,
        event_bus.subscribe_usage() as gq,
    ):
        with patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)):
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="worker",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=2000, output_tokens=500),
            )

        # Drain both queues without blocking.
        for q, bucket in ((eq, epic_received), (gq, global_received)):
            while True:
                try:
                    bucket.append(q.get_nowait())
                except asyncio.QueueEmpty:
                    break

    epic_usage = [e for e in epic_received if isinstance(e, TokenUsageEvent)]
    global_usage = [e for e in global_received if isinstance(e, TokenUsageEvent)]

    assert len(epic_usage) >= 1, "TokenUsageEvent must appear in (project, epic) stream"
    assert len(global_usage) >= 1, "TokenUsageEvent must appear in global usage stream"

    # Both queues receive the same event object.
    assert epic_usage[0] is global_usage[0]

    evt = global_usage[0]
    assert evt.role == "worker"
    assert evt.delta["input"] == 2000
    assert evt.delta["output"] == 500


async def test_budget_exceeded_event_published_to_global_stream(tmp_path: Path) -> None:
    """BudgetExceededEvent reaches the global usage stream when budget is breached."""
    from yukar.events import bus as event_bus
    from yukar.models.events import BudgetExceededEvent, TokenUsageEvent
    from yukar.usage.tracker import TokenUsageTracker, UsageDelta

    ledger = tmp_path / "ledger.yaml"
    tracker = TokenUsageTracker(ledger_path=ledger)
    tracker._limit_usd = 1.0  # $1 USD limit — exceeded by 1M tokens ($3 USD)

    mock_supervisor = MagicMock()
    mock_supervisor.list_active_runs_for_budget = AsyncMock(return_value=[])
    mock_supervisor.stop = AsyncMock()

    global_received: list[Any] = []

    async with event_bus.subscribe_usage() as gq:
        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch("yukar.runs.supervisor.get_supervisor", return_value=mock_supervisor),
        ):
            # 1M tokens * 3 USD/M = 3.0 USD >> 1.0 USD limit
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="manager",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=1_000_000),
            )

        # Drain queue without blocking.
        while True:
            try:
                global_received.append(gq.get_nowait())
            except asyncio.QueueEmpty:
                break

    token_events = [e for e in global_received if isinstance(e, TokenUsageEvent)]
    budget_events = [e for e in global_received if isinstance(e, BudgetExceededEvent)]

    assert len(token_events) >= 1, "TokenUsageEvent must appear in global stream"
    assert len(budget_events) >= 1, "BudgetExceededEvent must appear in global stream"
    assert budget_events[0].limit_usd == 1.0


async def test_usage_stream_sse_endpoint(app_client: Any) -> None:
    """GET /api/usage/stream returns 200 with text/event-stream content type."""
    import asyncio

    from yukar.events import bus as event_bus

    # Publish a sentinel to terminate the stream immediately after connect,
    # so the test does not hang waiting for the stream to close.
    async def _publish_sentinel() -> None:
        await asyncio.sleep(0.05)
        event_bus.publish_usage_sentinel()

    asyncio.create_task(_publish_sentinel())  # noqa: RUF006

    async with app_client.stream("GET", "/api/usage/stream") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]


# ---------------------------------------------------------------------------
# 6. Monthly budget helpers
# ---------------------------------------------------------------------------


class TestMonthlyBudget:
    """Tests for the monthly-budget semantics introduced in 2026-06."""

    def _make_tracker(self, tmp_path: Path) -> Any:
        from yukar.usage.tracker import TokenUsageTracker

        ledger = tmp_path / "ledger.yaml"
        return TokenUsageTracker(ledger_path=ledger)

    async def test_get_budget_state_with_limit(self, tmp_path: Path) -> None:
        """get_budget_state() returns correct daily_budget_usd, days_in_month, ratios."""
        import calendar
        from zoneinfo import ZoneInfo

        from yukar.usage.tracker import TokenUsageTracker, UsageDelta

        _JST = ZoneInfo("Asia/Tokyo")
        now_jst = datetime.now(_JST)
        days_in_month = calendar.monthrange(now_jst.year, now_jst.month)[1]

        tracker = TokenUsageTracker(ledger_path=tmp_path / "ledger.yaml")
        # $30 USD limit — 1M sonnet-4-6 tokens costs $3 USD (10% of limit)
        tracker._limit_usd = 30.0

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_publish_sse"),
        ):
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="manager",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=1_000_000),  # 3.0 USD
            )

        state = tracker.get_budget_state()

        assert state["limit_usd"] == 30.0
        assert state["days_in_month"] == days_in_month
        expected_daily_budget = 30.0 / days_in_month
        assert abs(state["daily_budget_usd"] - expected_daily_budget) < 1e-9
        # spent_usd == 3.0 USD (today's bucket cost_usd)
        assert abs(state["spent_usd"] - 3.0) < 1e-6
        assert abs(state["daily_spent_usd"] - 3.0) < 1e-6
        # month_ratio = 3.0 / 30.0
        assert state["month_ratio"] is not None
        assert abs(state["month_ratio"] - 3.0 / 30.0) < 1e-9
        # day_ratio = 3.0 / daily_budget
        assert state["day_ratio"] is not None
        assert abs(state["day_ratio"] - 3.0 / expected_daily_budget) < 1e-9
        assert state["over_budget"] is False

    async def test_get_budget_state_no_limit(self, tmp_path: Path) -> None:
        """When limit is None, daily_budget_usd / month_ratio / day_ratio are None."""
        from yukar.usage.tracker import TokenUsageTracker

        tracker = TokenUsageTracker(ledger_path=tmp_path / "ledger.yaml")
        # No limit set.
        state = tracker.get_budget_state()

        assert state["limit_usd"] is None
        assert state["daily_budget_usd"] is None
        assert state["month_ratio"] is None
        assert state["day_ratio"] is None
        assert state["over_budget"] is False

    async def test_day_ratio_can_exceed_1(self, tmp_path: Path) -> None:
        """day_ratio > 1.0 when today's spend exceeds the daily budget."""
        from yukar.usage.tracker import TokenUsageTracker, UsageDelta

        # Set limit to $2 USD so daily_budget = 2 / days_in_month.
        # 1M sonnet-4-6 tokens = $3 USD, which exceeds both daily_budget and the limit.
        # over_budget becomes True (month_spent >= limit), and day_ratio > 1.
        limit = 2.0
        tracker = TokenUsageTracker(ledger_path=tmp_path / "ledger.yaml")
        tracker._limit_usd = limit

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_publish_sse"),
            patch.object(tracker, "_check_budget", AsyncMock()),
        ):
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="manager",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=1_000_000),  # 3.0 USD
            )

        state = tracker.get_budget_state()
        day_ratio = state["day_ratio"]
        assert day_ratio is not None
        assert day_ratio > 1.0, f"day_ratio={day_ratio} should be > 1.0"
        # over_budget: month_spent(3.0) >= limit(2.0) → True
        assert state["over_budget"] is True

    async def test_day_ratio_exceeds_1_but_over_budget_false(self, tmp_path: Path) -> None:
        """Day ratio > 1 but over_budget False when month spend < monthly limit."""
        import calendar
        from zoneinfo import ZoneInfo

        from yukar.usage.tracker import DailyUsageBucket, TokenUsageTracker

        _JST = ZoneInfo("Asia/Tokyo")
        now_jst = datetime.now(_JST)
        days_in_month = calendar.monthrange(now_jst.year, now_jst.month)[1]
        today_str = now_jst.date().isoformat()

        tracker = TokenUsageTracker(ledger_path=tmp_path / "ledger.yaml")
        # Very large monthly limit: $10,000 USD
        # daily_budget = 10_000 / days_in_month
        # today_spent = daily_budget * 2 (double the daily budget) but still << monthly limit
        tracker._limit_usd = 10_000.0
        daily_budget = 10_000.0 / days_in_month
        today_spent = daily_budget * 2.0

        bucket = DailyUsageBucket(date=today_str)
        bucket.cost_usd = today_spent
        tracker._daily[today_str] = bucket

        state = tracker.get_budget_state()
        assert state["day_ratio"] is not None
        assert state["day_ratio"] > 1.0, f"day_ratio={state['day_ratio']} should be > 1.0"
        assert state["over_budget"] is False, "month_spent < limit so over_budget must be False"

    async def test_budget_exceeded_stops_runs_monthly_basis(self, tmp_path: Path) -> None:
        """record() stops runs when month-to-date USD spend exceeds the limit."""
        from yukar.usage.tracker import TokenUsageTracker, UsageDelta

        tracker = TokenUsageTracker(ledger_path=tmp_path / "ledger.yaml")
        # $1 USD limit — exceeded by 1M sonnet-4-6 tokens ($3 USD)
        tracker._limit_usd = 1.0

        stop_called_with: list[tuple[str, str]] = []
        mock_supervisor = MagicMock()
        mock_supervisor.list_active_runs.return_value = [("root", "p1", "e1")]
        mock_supervisor.stop = AsyncMock()

        async def fake_stop_all_runs(
            triggering_project_id: str,
            triggering_epic_id: str,
            triggering_run_id: str,
            spent_usd: float,
            limit_usd: float,
        ) -> None:
            for _root, pid, eid in mock_supervisor.list_active_runs():
                await mock_supervisor.stop(pid, eid)
                stop_called_with.append((pid, eid))

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_stop_all_runs", side_effect=fake_stop_all_runs),
            patch.object(tracker, "_publish_sse"),
        ):
            # 1M tokens * 3 USD/M = 3.0 USD > 1.0 USD limit
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="manager",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=1_000_000),
            )

        # Enforcement fires on monthly USD basis
        assert stop_called_with == [("p1", "e1")]

    async def test_monthly_auto_rearm(self, tmp_path: Path, monkeypatch: Any) -> None:
        """_budget_breach_claimed resets to False at month boundary (auto re-arm).

        Pins the current month to "2026-06" via monkeypatch to remove wall-clock
        dependency.  A bucket keyed "2026-05-31" does not match the current month
        prefix, so month-to-date USD spend is 0 and the breach claim clears.
        """
        from yukar.usage.tracker import DailyUsageBucket, TokenUsageTracker

        tracker = TokenUsageTracker(ledger_path=tmp_path / "ledger.yaml")
        tracker._limit_usd = 1.0

        # Pin current month to "2026-06" to eliminate wall-clock dependency.
        # _month_prefix() is used by _spent_this_month_usd() → _claim_budget_breach().
        monkeypatch.setattr(tracker, "_month_prefix", lambda: "2026-06")

        # Insert a previous-month bucket with cost_usd > limit.
        # "2026-05-31" does not match prefix "2026-06" so it is excluded from month spend.
        tracker._daily["2026-05-31"] = DailyUsageBucket(date="2026-05-31")
        tracker._daily["2026-05-31"].cost_usd = 200.0
        # Simulate a prior breach episode.
        tracker._budget_breach_claimed = True

        # _claim_budget_breach: current month spend (0) < limit → returns None and clears flag.
        result = await tracker._claim_budget_breach()
        assert result is None, "month-to-date spend is 0, no breach"
        assert tracker._budget_breach_claimed is False, "auto re-arm must clear the claimed flag"


# ---------------------------------------------------------------------------
# 7. Usage / cost attribution and ledger resilience (cross-file fixes)
# ---------------------------------------------------------------------------


class TestEmbeddingAttribution:
    """Embedding usage must be attributed to the real (project_id, epic_id, run_id)."""

    async def test_memory_embed_attributes_to_real_epic(self, tmp_path: Path) -> None:
        """The per-run memory embedder records embedding usage under the real epic_id.

        Regression: ensure_index_fresh runs before the Manager turn, so the
        embedding it triggers becomes the run's first usage event.  If epic_id
        were hardcoded to "", RunTotals.epic_id would freeze to "" and the whole
        run's cost would be mis-attributed to a phantom empty epic.
        """
        import asyncio

        from yukar.indexer.embedder import FakeEmbedder
        from yukar.usage.tracker import TokenUsageTracker, init_tracker

        tracker = TokenUsageTracker(ledger_path=tmp_path / "ledger.yaml")
        init_tracker(tracker)

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_publish_sse"),
        ):
            embedder = FakeEmbedder(project_id="proj-a", epic_id="EP-7", run_id="run-1")
            embedder.set_event_loop(asyncio.get_running_loop())
            embedder.embed_batch(["some content to embed"])
            # The usage record is scheduled via run_coroutine_threadsafe;
            # yield so the scheduled coroutine runs.
            await asyncio.sleep(0.05)

        rt = tracker.get_run_totals("run-1", project_id="proj-a")
        assert rt is not None, "embedding usage must create a run for the embedder's run_id"
        assert rt.project_id == "proj-a"
        assert rt.epic_id == "EP-7", "epic_id must be the real epic, not the empty phantom"
        assert rt.embedding_tokens > 0

    async def test_index_embed_attributes_to_indexed_project(self, tmp_path: Path) -> None:
        """IndexerService rebinds embedder attribution per project via set_context.

        Two projects sharing ONE embedder instance must each have their index
        embedding cost recorded under their own project, not collapsed into a
        single synthetic project_id="" run_id="embedding" run.
        """
        import asyncio

        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import _set_embedder_context, _set_embedder_loop
        from yukar.usage.tracker import TokenUsageTracker, init_tracker

        tracker = TokenUsageTracker(ledger_path=tmp_path / "ledger.yaml")
        init_tracker(tracker)

        # ONE shared embedder, as the IndexerService holds.
        shared = FakeEmbedder()
        loop = asyncio.get_running_loop()

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_publish_sse"),
        ):
            _set_embedder_loop(shared, loop)
            # Project A indexes.
            _set_embedder_context(shared, "proj-a", "index-proj-a")
            shared.embed_batch(["chunk from project a"])
            # Project B indexes on the SAME embedder instance.
            _set_embedder_context(shared, "proj-b", "index-proj-b")
            shared.embed_batch(["chunk from project b"])
            await asyncio.sleep(0.05)

        a = tracker.get_run_totals("index-proj-a", project_id="proj-a")
        b = tracker.get_run_totals("index-proj-b", project_id="proj-b")
        assert a is not None and a.project_id == "proj-a"
        assert b is not None and b.project_id == "proj-b"
        # Nothing should have landed under the synthetic empty project.
        assert tracker.get_run_totals("embedding", project_id="") is None


class TestLedgerResilience:
    """Top-level ledger corruption must never silently wipe usage history."""

    async def test_corrupt_top_level_ledger_not_overwritten_on_record(self, tmp_path: Path) -> None:
        """A corrupt top-level YAML is preserved; the next record() must not wipe it.

        Regression: load() swallowed the read_yaml raise, leaving _runs empty,
        then the next record()->_write_ledger() overwrote the file with
        runs:[] daily:{}, destroying all history.
        """
        from yukar.usage.tracker import TokenUsageTracker, UsageDelta

        ledger_path = tmp_path / "ledger.yaml"
        # Write bytes that read_yaml cannot parse into a mapping.
        corrupt_bytes = b"this: is: not: valid: yaml: : :\n  - [unbalanced\n"
        ledger_path.write_bytes(corrupt_bytes)

        tracker = TokenUsageTracker(ledger_path=ledger_path)
        await tracker.load()
        assert tracker._load_failed is True, "corrupt top-level YAML must set the load-failed flag"

        # A .corrupt-<ts> copy must have been written aside.
        backups = list(tmp_path.glob("ledger.yaml.corrupt-*"))
        assert backups, "a quarantine copy of the corrupt ledger must be written aside"
        assert backups[0].read_bytes() == corrupt_bytes

        # A record() must NOT overwrite the corrupt file (auto-write is refused).
        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_publish_sse"),
        ):
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="manager",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=1_000),
            )
        await tracker.flush()

        # The original corrupt bytes must be intact (not replaced with an empty ledger).
        assert ledger_path.read_bytes() == corrupt_bytes, (
            "corrupt ledger must be preserved, never overwritten with empty data"
        )

    async def test_empty_ledger_is_not_treated_as_corrupt(self, tmp_path: Path) -> None:
        """An empty ({} / absent) ledger is a normal fresh start, not a corruption."""
        from yukar.usage.tracker import TokenUsageTracker

        ledger_path = tmp_path / "ledger.yaml"  # does not exist
        tracker = TokenUsageTracker(ledger_path=ledger_path)
        await tracker.load()
        assert tracker._load_failed is False
        assert not list(tmp_path.glob("ledger.yaml.corrupt-*"))

    async def test_quarantined_run_tokens_counted_in_daily(self, tmp_path: Path) -> None:
        """Quarantined (unparsed) runs contribute their tokens to daily/global summaries."""
        from unittest.mock import patch

        from yukar.storage.yaml_io import write_yaml
        from yukar.usage import tracker as tracker_mod
        from yukar.usage.tracker import TokenUsageTracker

        ledger_path = tmp_path / "ledger.yaml"
        good_run = {
            "project_id": "p1",
            "epic_id": "e1",
            "run_id": "run-good",
            "started_at": "2026-06-01T00:00:00+00:00",
            "updated_at": "2026-06-01T01:00:00+00:00",
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "embedding_tokens": 0,
            "total_tokens": 120,
            "cost_usd": 0.0,
            "cost_jpy": 0.0,
            "by_model": [],
        }
        bad_run = {
            "project_id": "p1",
            "epic_id": "e1",
            "run_id": "run-corrupt",
            "started_at": "2026-06-01T00:00:00+00:00",
            "updated_at": "2026-06-01T01:00:00+00:00",
            "input_tokens": 7_000,
            "output_tokens": 3_000,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "embedding_tokens": 0,
            "total_tokens": 10_000,
            "cost_usd": 0.0,
            "cost_jpy": 0.0,
            "by_model": [],
        }
        # No "daily" key → migration runs (which is where quarantined runs are folded in).
        await write_yaml(ledger_path, {"limit_jpy": None, "runs": [good_run, bad_run]})

        tracker = TokenUsageTracker(ledger_path=ledger_path)

        original = tracker_mod.RunTotals.from_dict

        def patched_from_dict(d: dict[str, Any]) -> tracker_mod.RunTotals:
            if d.get("run_id") == "run-corrupt":
                raise ValueError("simulated parse failure")
            return original(d)

        with patch.object(tracker_mod.RunTotals, "from_dict", staticmethod(patched_from_dict)):
            await tracker.load()

        assert tracker.get_run_totals("run-corrupt") is None
        assert len(tracker._unparsed_runs) == 1
        # 2026-06-01T00:00:00+00:00 → JST 2026-06-01.
        bucket = tracker._daily.get("2026-06-01")
        assert bucket is not None
        # good (100) + quarantined (7000) input tokens must both be counted.
        assert bucket.input_tokens == 7_100, (
            "quarantined run tokens must be folded into the daily bucket, not dropped"
        )


class TestRememberEmbedFailureSurfaced:
    """remember() must surface a memory write that fails to embed/persist.

    Successor of the complete_epic learnings tests: complete_epic is gone
    (P3 — a conversation has no completion tool) and remember() is the only
    memory-write path, so the embed-failure visibility guarantee moves here.
    """

    async def test_remember_surfaces_embed_failure(self) -> None:
        """An add() that raises EmbedFailedError returns stored=False with
        reason=embed_failed instead of crashing the tool call."""
        from yukar.agents.orchestrator import _make_remember_tool
        from yukar.memory.store import EmbedFailedError

        class _BoomStore:
            async def add(self, content: str, metadata: dict[str, Any] | None = None) -> Any:
                raise EmbedFailedError("embed exploded")

        remember = _make_remember_tool(_BoomStore(), "EP-1")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        result = await remember._tool_func(fact="boom", category="lesson", repo=None)
        assert result == {"stored": False, "reason": "embed_failed"}

    async def test_remember_success_and_duplicate_counts(self) -> None:
        """Stored and duplicate outcomes are distinguishable to the Manager."""
        from yukar.agents.orchestrator import _make_remember_tool

        class _Store:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def add(self, content: str, metadata: dict[str, Any] | None = None) -> Any:
                self.calls.append(content)
                if content == "dup":
                    return None
                return "mem-0001"

        store = _Store()
        published: list[tuple[str, str]] = []
        remember = _make_remember_tool(
            store,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            "EP-1",
            lambda kind, name: published.append((kind, name)),
        )

        ok = await remember._tool_func(fact="good", category="lesson", repo=None)
        dup = await remember._tool_func(fact="dup", category="lesson", repo=None)
        assert ok == {"stored": True, "id": "mem-0001"}
        assert dup == {"stored": False, "reason": "duplicate"}
        # The sensitive-write event fires only for the successful store.
        assert published == [("memory", "lesson")]


class TestExchangeRobustness:
    """Naive fetched_at must not pin to fallback; concurrent first calls single-flight."""

    async def test_naive_fetched_at_does_not_pin_and_refetches(self, tmp_path: Path) -> None:
        """A legacy naive fetched_at that is stale must trigger a re-fetch, not raise."""
        from datetime import datetime, timedelta

        from yukar.usage.exchange import _CACHE_TTL, ExchangeRateProvider

        provider = ExchangeRateProvider(cache_path=tmp_path / "rate.yaml")
        provider._loaded = True
        provider._rate = 140.0
        provider._source = "cache"
        # NAIVE datetime (no tzinfo) on the UTC clock — simulates a legacy cache
        # written without an offset — older than the TTL → must be seen as stale.
        naive_utc_now = datetime.now(UTC).replace(tzinfo=None)
        provider._fetched_at = naive_utc_now - _CACHE_TTL - timedelta(hours=1)

        # _is_stale must not raise on the naive value and must report stale.
        assert provider._is_stale() is True

        with patch.object(provider, "_fetch_blocking", return_value=160.0):
            rate = await provider.get_rate()
        assert abs(rate - 160.0) < 1e-9, "stale naive cache must re-fetch instead of pinning"

    async def test_naive_fetched_at_loaded_from_cache_is_coerced(self, tmp_path: Path) -> None:
        """A persisted naive fetched_at is coerced to aware UTC on load."""
        from yukar.storage.yaml_io import write_yaml
        from yukar.usage.exchange import ExchangeRateProvider

        cache_path = tmp_path / "rate.yaml"
        # Persist a naive ISO string (no offset).
        await write_yaml(
            cache_path,
            {"rate_jpy": 151.0, "fetched_at": "2026-06-01T00:00:00", "source": "cache"},
        )
        provider = ExchangeRateProvider(cache_path=cache_path)
        provider._load_cache()
        assert provider._fetched_at is not None
        assert provider._fetched_at.tzinfo is not None, "naive cached timestamp must become aware"

    async def test_concurrent_first_calls_single_flight(self, tmp_path: Path) -> None:
        """Concurrent first get_rate() calls share one in-flight fetch (no double-fetch)."""
        import asyncio

        from yukar.usage.exchange import ExchangeRateProvider

        provider = ExchangeRateProvider(cache_path=tmp_path / "rate.yaml")
        fetch_count = 0

        def _slow_fetch() -> float:
            nonlocal fetch_count
            fetch_count += 1
            return 159.0

        with patch.object(provider, "_fetch_blocking", side_effect=_slow_fetch):
            results = await asyncio.gather(*[provider.get_rate() for _ in range(5)])

        assert all(abs(r - 159.0) < 1e-9 for r in results)
        assert fetch_count == 1, "concurrent first callers must share a single fetch"


# ---------------------------------------------------------------------------
# 9. Arbiter usage bucket
# ---------------------------------------------------------------------------


class TestArbiterUsageBucket:
    """Arbiter (batch-merge) runs must appear in a dedicated per-project bucket."""

    def _make_tracker(self, tmp_path: Path) -> Any:
        from yukar.usage.tracker import TokenUsageTracker

        ledger = tmp_path / "ledger.yaml"
        return TokenUsageTracker(ledger_path=ledger)

    async def test_arbiter_sentinel_lands_in_arbiter_bucket_not_epics(
        self, tmp_path: Path
    ) -> None:
        """Usage recorded with ARBITER_EPIC_SENTINEL must appear in project.arbiter,
        not in project.epics."""
        from yukar.usage.tracker import ARBITER_EPIC_SENTINEL, ArbiterUsageSummary, UsageDelta

        tracker = self._make_tracker(tmp_path)

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_publish_sse"),
        ):
            await tracker.record(
                project_id="p1",
                epic_id=ARBITER_EPIC_SENTINEL,
                run_id="arbiter-run-1",
                role="arbiter",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=500_000),
            )

        summary = tracker.get_global_summary()
        proj = next((p for p in summary.by_project if p.project_id == "p1"), None)
        assert proj is not None

        # Sentinel must NOT appear in epics.
        epic_ids = [e.epic_id for e in proj.epics]
        assert ARBITER_EPIC_SENTINEL not in epic_ids, (
            "sentinel epic_id must never appear in the epics list"
        )

        # Sentinel must appear in the arbiter bucket.
        assert proj.arbiter is not None, "arbiter bucket must be set when arbiter runs exist"
        assert isinstance(proj.arbiter, ArbiterUsageSummary)
        assert proj.arbiter.input_tokens == 500_000
        assert len(proj.arbiter.runs) == 1
        assert proj.arbiter.runs[0].run_id == "arbiter-run-1"

    async def test_mixed_epic_and_arbiter_runs_are_correctly_separated(
        self, tmp_path: Path
    ) -> None:
        """When a project has both regular epic runs and arbiter runs, they must be
        kept separate: epics list must not contain arbiter data, and the project
        total must equal Σ epics + arbiter."""
        from yukar.usage.tracker import ARBITER_EPIC_SENTINEL, UsageDelta

        tracker = self._make_tracker(tmp_path)

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_publish_sse"),
        ):
            # Regular epic run: 1M input → 3.0 USD → 450 JPY
            await tracker.record(
                project_id="proj",
                epic_id="EP-1",
                run_id="run-epic",
                role="manager",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=1_000_000),
            )
            # Arbiter run: 500K input → 1.5 USD → 225 JPY
            await tracker.record(
                project_id="proj",
                epic_id=ARBITER_EPIC_SENTINEL,
                run_id="run-arbiter",
                role="arbiter",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=500_000),
            )

        summary = tracker.get_global_summary()
        proj = next((p for p in summary.by_project if p.project_id == "proj"), None)
        assert proj is not None

        # epics must contain only the real epic.
        assert len(proj.epics) == 1
        assert proj.epics[0].epic_id == "EP-1"
        assert proj.epics[0].input_tokens == 1_000_000

        # arbiter bucket must contain only the arbiter run.
        assert proj.arbiter is not None
        assert proj.arbiter.input_tokens == 500_000

        # Project total = epics + arbiter.
        expected_input = 1_000_000 + 500_000
        assert proj.input_tokens == expected_input, (
            f"project.input_tokens ({proj.input_tokens}) != epics + arbiter ({expected_input})"
        )
        expected_cost_usd = proj.epics[0].cost_usd + proj.arbiter.cost_usd
        assert abs(proj.cost_usd - expected_cost_usd) < 1e-9, (
            f"project.cost_usd ({proj.cost_usd}) != epics + arbiter ({expected_cost_usd})"
        )

    async def test_arbiter_included_in_by_model_and_global_totals(
        self, tmp_path: Path
    ) -> None:
        """Arbiter costs must appear in by_model aggregates and global total_cost_*."""
        from yukar.usage.tracker import ARBITER_EPIC_SENTINEL, UsageDelta

        tracker = self._make_tracker(tmp_path)

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_publish_sse"),
        ):
            await tracker.record(
                project_id="p1",
                epic_id=ARBITER_EPIC_SENTINEL,
                run_id="arb-1",
                role="arbiter",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=1_000_000),  # 3.0 USD
            )

        summary = tracker.get_global_summary()

        # Global totals must include arbiter spend.
        assert abs(summary.total_cost_usd - 3.0) < 1e-9, (
            "total_cost_usd must include arbiter"
        )
        assert summary.total_input_tokens == 1_000_000, (
            "total_input_tokens must include arbiter"
        )

        # by_model must include arbiter.
        model_map = {m.model_id: m for m in summary.by_model}
        assert "sonnet-4-6" in model_map, "arbiter model must appear in by_model"
        assert model_map["sonnet-4-6"].input_tokens == 1_000_000

        # today bucket must include arbiter.
        assert summary.today.input_tokens == 1_000_000, (
            "today bucket must include arbiter"
        )

    async def test_project_with_only_arbiter_runs_appears_in_summary(
        self, tmp_path: Path
    ) -> None:
        """A project that has ONLY arbiter runs (no regular epic runs) must still
        appear in by_project with an arbiter bucket and an empty epics list."""
        from yukar.usage.tracker import ARBITER_EPIC_SENTINEL, UsageDelta

        tracker = self._make_tracker(tmp_path)

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_publish_sse"),
        ):
            await tracker.record(
                project_id="arb-only-project",
                epic_id=ARBITER_EPIC_SENTINEL,
                run_id="arb-only-run",
                role="arbiter",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=200_000),
            )

        summary = tracker.get_global_summary()
        proj = next(
            (p for p in summary.by_project if p.project_id == "arb-only-project"), None
        )
        assert proj is not None, "project with only arbiter runs must appear in by_project"
        assert proj.epics == [], "epics must be empty when there are no regular epic runs"
        assert proj.arbiter is not None
        assert proj.arbiter.input_tokens == 200_000

    async def test_existing_epic_summary_has_arbiter_none_by_default(
        self, tmp_path: Path
    ) -> None:
        """A project without any arbiter runs must have arbiter=None (backward-compatible)."""
        from yukar.usage.tracker import UsageDelta

        tracker = self._make_tracker(tmp_path)

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_publish_sse"),
        ):
            await tracker.record(
                project_id="normal-proj",
                epic_id="EP-1",
                run_id="run-1",
                role="manager",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=100_000),
            )

        summary = tracker.get_global_summary()
        proj = next(
            (p for p in summary.by_project if p.project_id == "normal-proj"), None
        )
        assert proj is not None
        assert proj.arbiter is None, (
            "arbiter must be None for projects with no arbiter runs (backward-compat)"
        )

    # ------------------------------------------------------------------
    # Regression tests for review-fix corrections
    # ------------------------------------------------------------------

    async def test_by_project_order_is_deterministic(self, tmp_path: Path) -> None:
        """by_project order matches first-seen insertion order (PYTHONHASHSEED-independent).

        Records three projects in p1→p2→p3 order and verifies that by_project
        project_id list preserves that order. Also verifies that p4 (no epic runs,
        arbiter runs only) follows after the projects that have epics.
        """
        from yukar.usage.tracker import ARBITER_EPIC_SENTINEL, UsageDelta

        tracker = self._make_tracker(tmp_path)

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_publish_sse"),
        ):
            # Record p1, p2, p3 with epic runs.
            for pid in ("p1", "p2", "p3"):
                await tracker.record(
                    project_id=pid,
                    epic_id="EP-1",
                    run_id=f"run-{pid}",
                    role="manager",
                    model_id="sonnet-4-6",
                    delta=UsageDelta(input_tokens=100_000),
                )
            # p4 has only arbiter runs (no epic runs).
            await tracker.record(
                project_id="p4",
                epic_id=ARBITER_EPIC_SENTINEL,
                run_id="arb-p4",
                role="arbiter",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=50_000),
            )

        summary = tracker.get_global_summary()
        project_ids = [p.project_id for p in summary.by_project]

        # Insertion order p1→p2→p3 is preserved.
        assert project_ids[:3] == ["p1", "p2", "p3"], (
            f"expected first-seen order ['p1','p2','p3'] but got {project_ids}"
        )
        # p4 (arbiter-only) comes last.
        assert project_ids[3] == "p4", (
            f"arbiter-only project must follow epic projects; got {project_ids}"
        )

    async def test_arbiter_multiple_records_same_run_collapse_to_single_run(
        self, tmp_path: Path
    ) -> None:
        """Multiple record() calls with the same (project_id, run_id) + ARBITER_EPIC_SENTINEL
        must collapse to a single entry in arbiter.runs with the total equal to the sum of
        all records. Also asserts that it does not appear on the epic side.
        """
        from yukar.usage.tracker import ARBITER_EPIC_SENTINEL, UsageDelta

        tracker = self._make_tracker(tmp_path)

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_publish_sse"),
        ):
            # Record 3 times with the same run_id (simulating arbitration of different real epics).
            for _ in range(3):
                await tracker.record(
                    project_id="proj",
                    epic_id=ARBITER_EPIC_SENTINEL,
                    run_id="arb-run-001",
                    role="arbiter",
                    model_id="sonnet-4-6",
                    delta=UsageDelta(input_tokens=200_000),
                )

        summary = tracker.get_global_summary()
        proj = next((p for p in summary.by_project if p.project_id == "proj"), None)
        assert proj is not None

        # The sentinel must not appear in epics.
        assert proj.epics == [], "arbiter sentinel must not leak into epics list"

        # arbiter.runs must collapse to 1 run.
        assert proj.arbiter is not None
        assert len(proj.arbiter.runs) == 1, (
            f"3 records with same run_id must collapse to 1 run, got {len(proj.arbiter.runs)}"
        )
        # Total = 3 × 200_000 = 600_000.
        assert proj.arbiter.input_tokens == 600_000, (
            f"arbiter.input_tokens must be 600_000 (3×200_000), got {proj.arbiter.input_tokens}"
        )
        assert proj.arbiter.runs[0].run_id == "arb-run-001"
        assert proj.arbiter.runs[0].input_tokens == 600_000


# ---------------------------------------------------------------------------
# 10. USD budget — backward-compat load and USD spend aggregation
# ---------------------------------------------------------------------------


class TestUsdBudget:
    """Tests for the USD-canonical budget introduced in 2026-06."""

    def _make_tracker(self, tmp_path: Path) -> Any:
        from yukar.usage.tracker import TokenUsageTracker

        return TokenUsageTracker(ledger_path=tmp_path / "ledger.yaml")

    async def test_backward_compat_load_limit_jpy(self, tmp_path: Path) -> None:
        """Legacy ledger with limit_jpy (not limit_usd) is migrated on load.

        The value is divided by the fallback rate (155 JPY/USD) to produce an
        approximate USD figure.  A WARNING is logged; the tracker must not crash.
        """
        import io
        import logging as _logging

        from yukar.storage.yaml_io import write_yaml
        from yukar.usage.tracker import TokenUsageTracker

        ledger_path = tmp_path / "ledger.yaml"
        legacy_data: dict[str, Any] = {
            "limit_jpy": 31_000.0,  # 31000 / 155 = 200.0 USD (exactly)
            "updated_at": "2026-06-01T00:00:00+00:00",
            "runs": [],
            "daily": {},
        }
        await write_yaml(ledger_path, legacy_data)

        tracker = TokenUsageTracker(ledger_path=ledger_path)
        log_stream = io.StringIO()
        handler = _logging.StreamHandler(log_stream)
        handler.setLevel(_logging.WARNING)
        _logging.getLogger("yukar.usage.tracker").addHandler(handler)
        try:
            await tracker.load()
        finally:
            _logging.getLogger("yukar.usage.tracker").removeHandler(handler)

        # limit_jpy=31000 / 155 = 200.0 USD (exactly divisible)
        assert tracker._limit_usd is not None
        assert abs(tracker._limit_usd - 200.0) < 1e-6, (
            f"legacy limit_jpy=31000 should convert to ~200 USD, got {tracker._limit_usd}"
        )
        # WARNING must have been emitted
        log_output = log_stream.getvalue()
        assert "limit_jpy" in log_output, "WARNING for legacy limit_jpy must be logged"
        assert "limit_usd" in log_output

    async def test_backward_compat_load_limit_jpy_none(self, tmp_path: Path) -> None:
        """Legacy ledger with limit_jpy=null loads without crash; _limit_usd becomes None."""
        from yukar.storage.yaml_io import write_yaml
        from yukar.usage.tracker import TokenUsageTracker

        ledger_path = tmp_path / "ledger.yaml"
        await write_yaml(
            ledger_path,
            {"limit_jpy": None, "updated_at": "2026-06-01T00:00:00+00:00", "runs": [], "daily": {}},
        )

        tracker = TokenUsageTracker(ledger_path=ledger_path)
        await tracker.load()

        assert tracker._limit_usd is None

    async def test_load_prefers_limit_usd_over_limit_jpy(self, tmp_path: Path) -> None:
        """When both limit_usd and limit_jpy are present, limit_usd takes precedence."""
        from yukar.storage.yaml_io import write_yaml
        from yukar.usage.tracker import TokenUsageTracker

        ledger_path = tmp_path / "ledger.yaml"
        # Theoretically impossible in production (write_ledger never writes limit_jpy),
        # but guard against hand-edited files.
        await write_yaml(
            ledger_path,
            {
                "limit_usd": 25.0,
                "limit_jpy": 99_999.0,
                "updated_at": "2026-06-01T00:00:00+00:00",
                "runs": [],
                "daily": {},
            },
        )

        tracker = TokenUsageTracker(ledger_path=ledger_path)
        await tracker.load()

        assert tracker._limit_usd is not None
        assert abs(tracker._limit_usd - 25.0) < 1e-9, (
            "limit_usd must take precedence over limit_jpy when both are present"
        )

    async def test_spent_usd_aggregates_from_daily_cost_usd(self, tmp_path: Path) -> None:
        """_spent_this_month_usd() sums daily bucket cost_usd (not cost_jpy)."""
        from zoneinfo import ZoneInfo

        from yukar.usage.tracker import TokenUsageTracker, UsageDelta

        _JST = ZoneInfo("Asia/Tokyo")
        tracker = TokenUsageTracker(ledger_path=tmp_path / "ledger.yaml")

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_publish_sse"),
        ):
            # sonnet-4-6: input=3 USD/M → 2M tokens = 6.0 USD
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="manager",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=2_000_000),
            )

        today_str = datetime.now(_JST).date().isoformat()
        bucket = tracker._daily.get(today_str)
        assert bucket is not None
        assert abs(bucket.cost_usd - 6.0) < 1e-9, (
            f"daily bucket cost_usd must be 6.0 for 2M sonnet input tokens, got {bucket.cost_usd}"
        )

        spent = tracker._spent_this_month_usd()
        assert abs(spent - 6.0) < 1e-9, (
            f"_spent_this_month_usd() must equal bucket.cost_usd=6.0, got {spent}"
        )

    async def test_set_budget_persists_limit_usd_key(self, tmp_path: Path) -> None:
        """set_budget() writes limit_usd (not limit_jpy) to the ledger YAML."""
        from yukar.storage.yaml_io import read_yaml
        from yukar.usage.tracker import TokenUsageTracker

        ledger_path = tmp_path / "ledger.yaml"
        tracker = TokenUsageTracker(ledger_path=ledger_path)
        await tracker.set_budget(50.0)

        data = read_yaml(ledger_path)
        assert data is not None
        assert "limit_usd" in data, "limit_usd key must be written to ledger YAML"
        assert abs(data["limit_usd"] - 50.0) < 1e-9
        assert "limit_jpy" not in data, "limit_jpy must NOT appear in new-format ledger"

    async def test_is_over_budget_uses_usd(self, tmp_path: Path) -> None:
        """is_over_budget() compares USD spend (cost_usd) against the USD limit."""
        from yukar.usage.tracker import TokenUsageTracker, UsageDelta

        tracker = TokenUsageTracker(ledger_path=tmp_path / "ledger.yaml")
        # $2 USD limit — not over budget before any spend
        tracker._limit_usd = 2.0
        assert tracker.is_over_budget() is False

        with (
            patch.object(tracker, "_get_jpy_rate", AsyncMock(return_value=150.0)),
            patch.object(tracker, "_publish_sse"),
            patch.object(tracker, "_check_budget", AsyncMock()),
        ):
            # 1M sonnet-4-6 tokens = $3 USD — exceeds $2 USD limit
            await tracker.record(
                project_id="p1",
                epic_id="e1",
                run_id="r1",
                role="manager",
                model_id="sonnet-4-6",
                delta=UsageDelta(input_tokens=1_000_000),
            )

        assert tracker.is_over_budget() is True
