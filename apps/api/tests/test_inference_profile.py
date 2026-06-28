"""Tests for yukar.llm.inference_profile — Bedrock application inference profile
resolution.

Coverage:
1. Fast path: already-priced model ID returns immediately without calling boto3.
2. Resolution: application inference profile ARN → foundation model ID via
   bedrock.get_inference_profile.
3. Failure: get_inference_profile raises an exception → original model_id returned,
   no crash.
4. Cache: two calls to the same ARN result in get_inference_profile being called
   exactly once.
5. provider_is_bedrock=False: boto3 is never called.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_APP_PROFILE_ARN = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"
_FOUNDATION_MODEL_ARN = (
    "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-opus-4-8-20250101-v1:0"
)
_FOUNDATION_MODEL_ID = "anthropic.claude-opus-4-8-20250101-v1:0"

# A model ID that is already in the pricing table (fast path).
_KNOWN_MODEL_ID = "us.anthropic.claude-sonnet-4-6-20250514-v1:0"


def _make_get_inference_profile_resp(
    model_arn: str = _FOUNDATION_MODEL_ARN,
    profile_type: str = "APPLICATION",
) -> dict:
    return {
        "models": [{"modelArn": model_arn}],
        "type": profile_type,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_inference_cache():  # type: ignore[return]
    """Clear the module-level cache before each test so tests are isolated."""
    import yukar.llm.inference_profile as ip

    ip._resolved_cache.clear()
    ip._warned.clear()
    yield
    ip._resolved_cache.clear()
    ip._warned.clear()


# ---------------------------------------------------------------------------
# 1. Fast path
# ---------------------------------------------------------------------------


class TestFastPath:
    """Already-priced model IDs must not trigger a boto3 call."""

    @pytest.mark.asyncio
    async def test_known_model_id_no_boto3_call(self) -> None:
        with patch("boto3.client") as mock_client:
            from yukar.llm.inference_profile import resolve_model_id_for_pricing

            result = await resolve_model_id_for_pricing(
                _KNOWN_MODEL_ID,
                region="us-east-1",
                provider_is_bedrock=True,
            )

        assert result == _KNOWN_MODEL_ID
        mock_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_short_model_name_no_boto3_call(self) -> None:
        """Short names like 'claude-opus-4-8' are priced directly."""
        with patch("boto3.client") as mock_client:
            from yukar.llm.inference_profile import resolve_model_id_for_pricing

            result = await resolve_model_id_for_pricing(
                "claude-opus-4-8",
                region=None,
                provider_is_bedrock=True,
            )

        assert result == "claude-opus-4-8"
        mock_client.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Resolution
# ---------------------------------------------------------------------------


class TestResolution:
    """Application inference profile ARN must resolve to the foundation model ID."""

    @pytest.mark.asyncio
    async def test_resolves_app_profile_to_foundation_model(self) -> None:
        mock_bedrock = MagicMock()
        mock_bedrock.get_inference_profile.return_value = _make_get_inference_profile_resp()

        with patch("boto3.client", return_value=mock_bedrock):
            from yukar.llm.inference_profile import resolve_model_id_for_pricing

            result = await resolve_model_id_for_pricing(
                _APP_PROFILE_ARN,
                region="us-east-1",
                provider_is_bedrock=True,
            )

        assert result == _FOUNDATION_MODEL_ID
        mock_bedrock.get_inference_profile.assert_called_once_with(
            inferenceProfileIdentifier=_APP_PROFILE_ARN
        )

    @pytest.mark.asyncio
    async def test_region_extracted_from_arn_when_none(self) -> None:
        """If region=None and the ARN contains a region, it is auto-extracted."""
        mock_bedrock = MagicMock()
        mock_bedrock.get_inference_profile.return_value = _make_get_inference_profile_resp()

        with patch("boto3.client", return_value=mock_bedrock) as mock_client:
            from yukar.llm.inference_profile import resolve_model_id_for_pricing

            result = await resolve_model_id_for_pricing(
                _APP_PROFILE_ARN,
                region=None,
                provider_is_bedrock=True,
            )

        assert result == _FOUNDATION_MODEL_ID
        # boto3.client should have been called with the region from the ARN
        mock_client.assert_called_once_with("bedrock", region_name="us-east-1")

    @pytest.mark.asyncio
    async def test_model_arn_without_foundation_model_prefix_returns_original(self) -> None:
        """If the resolved model ARN lacks 'foundation-model/', return original model_id."""
        bad_resp = {"models": [{"modelArn": "arn:aws:bedrock:us-east-1::some-other/thing"}]}
        mock_bedrock = MagicMock()
        mock_bedrock.get_inference_profile.return_value = bad_resp

        with patch("boto3.client", return_value=mock_bedrock):
            from yukar.llm.inference_profile import resolve_model_id_for_pricing

            result = await resolve_model_id_for_pricing(
                _APP_PROFILE_ARN,
                region="us-east-1",
                provider_is_bedrock=True,
            )

        # The resolved model arn is not in the pricing table, so original is returned.
        assert result == _APP_PROFILE_ARN

    @pytest.mark.asyncio
    async def test_empty_models_list_returns_original(self) -> None:
        mock_bedrock = MagicMock()
        mock_bedrock.get_inference_profile.return_value = {"models": [], "type": "APPLICATION"}

        with patch("boto3.client", return_value=mock_bedrock):
            from yukar.llm.inference_profile import resolve_model_id_for_pricing

            result = await resolve_model_id_for_pricing(
                _APP_PROFILE_ARN,
                region="us-east-1",
                provider_is_bedrock=True,
            )

        assert result == _APP_PROFILE_ARN


# ---------------------------------------------------------------------------
# 3. Failure
# ---------------------------------------------------------------------------


class TestFailure:
    """Exceptions from get_inference_profile must not propagate."""

    @pytest.mark.asyncio
    async def test_access_denied_returns_original_model_id(self) -> None:
        import botocore.exceptions

        mock_bedrock = MagicMock()
        mock_bedrock.get_inference_profile.side_effect = botocore.exceptions.ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "Access Denied"}},
            "GetInferenceProfile",
        )

        with patch("boto3.client", return_value=mock_bedrock):
            from yukar.llm.inference_profile import resolve_model_id_for_pricing

            result = await resolve_model_id_for_pricing(
                _APP_PROFILE_ARN,
                region="us-east-1",
                provider_is_bedrock=True,
            )

        assert result == _APP_PROFILE_ARN

    @pytest.mark.asyncio
    async def test_generic_exception_returns_original_model_id(self) -> None:
        mock_bedrock = MagicMock()
        mock_bedrock.get_inference_profile.side_effect = RuntimeError("network error")

        with patch("boto3.client", return_value=mock_bedrock):
            from yukar.llm.inference_profile import resolve_model_id_for_pricing

            result = await resolve_model_id_for_pricing(
                _APP_PROFILE_ARN,
                region="us-east-1",
                provider_is_bedrock=True,
            )

        assert result == _APP_PROFILE_ARN

    @pytest.mark.asyncio
    async def test_failure_warning_logged_once(self, caplog: pytest.LogCaptureFixture) -> None:
        """Warning for a failed resolution must be emitted only once per model_id."""
        import logging

        mock_bedrock = MagicMock()
        mock_bedrock.get_inference_profile.side_effect = RuntimeError("error")

        with patch("boto3.client", return_value=mock_bedrock):
            from yukar.llm.inference_profile import resolve_model_id_for_pricing

            with caplog.at_level(logging.WARNING, logger="yukar.llm.inference_profile"):
                await resolve_model_id_for_pricing(
                    _APP_PROFILE_ARN,
                    region="us-east-1",
                    provider_is_bedrock=True,
                )
                # Second call — cache is populated from first failure, no second warning
                # (Note: _warned set prevents re-logging even on cache miss path)
                import yukar.llm.inference_profile as ip

                ip._resolved_cache.clear()  # force re-resolve but _warned is intact
                await resolve_model_id_for_pricing(
                    _APP_PROFILE_ARN,
                    region="us-east-1",
                    provider_is_bedrock=True,
                )

        # Warning should appear exactly once
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1


# ---------------------------------------------------------------------------
# 4. Cache
# ---------------------------------------------------------------------------


class TestCache:
    """get_inference_profile must be called exactly once per unique ARN."""

    @pytest.mark.asyncio
    async def test_second_call_uses_cache(self) -> None:
        mock_bedrock = MagicMock()
        mock_bedrock.get_inference_profile.return_value = _make_get_inference_profile_resp()

        with patch("boto3.client", return_value=mock_bedrock):
            from yukar.llm.inference_profile import resolve_model_id_for_pricing

            first = await resolve_model_id_for_pricing(
                _APP_PROFILE_ARN,
                region="us-east-1",
                provider_is_bedrock=True,
            )
            second = await resolve_model_id_for_pricing(
                _APP_PROFILE_ARN,
                region="us-east-1",
                provider_is_bedrock=True,
            )

        assert first == second == _FOUNDATION_MODEL_ID
        # boto3 client was constructed, but get_inference_profile only once
        assert mock_bedrock.get_inference_profile.call_count == 1

    @pytest.mark.asyncio
    async def test_concurrent_calls_resolve_once(self) -> None:
        """Concurrent coroutines for the same ARN must resolve exactly once."""
        import asyncio

        call_count = 0

        def fake_get_inference_profile(**kwargs: object) -> dict:
            nonlocal call_count
            call_count += 1
            return _make_get_inference_profile_resp()

        mock_bedrock = MagicMock()
        mock_bedrock.get_inference_profile.side_effect = fake_get_inference_profile

        with patch("boto3.client", return_value=mock_bedrock):
            from yukar.llm.inference_profile import resolve_model_id_for_pricing

            results = await asyncio.gather(
                *[
                    resolve_model_id_for_pricing(
                        _APP_PROFILE_ARN,
                        region="us-east-1",
                        provider_is_bedrock=True,
                    )
                    for _ in range(5)
                ]
            )

        assert all(r == _FOUNDATION_MODEL_ID for r in results)
        # Due to the lock, only one actual boto3 call is made
        assert call_count == 1


# ---------------------------------------------------------------------------
# 5. provider_is_bedrock=False
# ---------------------------------------------------------------------------


class TestNonBedrockProvider:
    """Non-Bedrock providers must never trigger boto3 calls."""

    @pytest.mark.asyncio
    async def test_anthropic_provider_no_boto3(self) -> None:
        with patch("boto3.client") as mock_client:
            from yukar.llm.inference_profile import resolve_model_id_for_pricing

            result = await resolve_model_id_for_pricing(
                _APP_PROFILE_ARN,
                region=None,
                provider_is_bedrock=False,
            )

        assert result == _APP_PROFILE_ARN
        mock_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_fake_provider_no_boto3(self) -> None:
        with patch("boto3.client") as mock_client:
            from yukar.llm.inference_profile import resolve_model_id_for_pricing

            result = await resolve_model_id_for_pricing(
                "some-fake-model-id",
                region=None,
                provider_is_bedrock=False,
            )

        assert result == "some-fake-model-id"
        mock_client.assert_not_called()


# ---------------------------------------------------------------------------
# 6. arbiter role in factory and settings
# ---------------------------------------------------------------------------


class TestArbiterRole:
    """create_model must accept role='arbiter' and fall back to global model_id."""

    def _make_settings(
        self,
        arbiter_model_id: str | None = None,
        worker_model_id: str | None = None,
    ):  # type: ignore[return]
        from yukar.config.settings import LLMRoleSettings, LLMRolesSettings, LLMSettings

        return LLMSettings(
            provider="bedrock",
            model_id="us.anthropic.claude-sonnet-4-6-20250514-v1:0",
            roles=LLMRolesSettings(
                arbiter=LLMRoleSettings(model_id=arbiter_model_id),
                worker=LLMRoleSettings(model_id=worker_model_id),
            ),
        )

    def test_arbiter_override_respected(self) -> None:
        from strands.models.bedrock import BedrockModel

        from yukar.llm.factory import create_model

        settings = self._make_settings(arbiter_model_id="anthropic.claude-opus-4-8-v1:0")
        model = create_model(settings, role="arbiter")
        assert isinstance(model, BedrockModel)
        cfg = model.get_config()
        assert cfg["model_id"] == "anthropic.claude-opus-4-8-v1:0"

    def test_arbiter_fallback_to_global_when_not_set(self) -> None:
        from strands.models.bedrock import BedrockModel

        from yukar.llm.factory import create_model

        settings = self._make_settings(arbiter_model_id=None)
        model = create_model(settings, role="arbiter")
        assert isinstance(model, BedrockModel)
        cfg = model.get_config()
        # No override → falls back to global model_id
        assert cfg["model_id"] == "us.anthropic.claude-sonnet-4-6-20250514-v1:0"

    def test_arbiter_not_affected_by_worker_override(self) -> None:
        """Worker override must not bleed into arbiter when arbiter has no override."""
        from strands.models.bedrock import BedrockModel

        from yukar.llm.factory import create_model

        settings = self._make_settings(
            arbiter_model_id=None,
            worker_model_id="anthropic.claude-haiku-4-5-v1:0",
        )
        model = create_model(settings, role="arbiter")
        assert isinstance(model, BedrockModel)
        cfg = model.get_config()
        # arbiter has no override → global, NOT worker's model
        assert cfg["model_id"] == "us.anthropic.claude-sonnet-4-6-20250514-v1:0"

    def test_worker_override_not_affected_by_arbiter(self) -> None:
        """Arbiter override must not bleed into worker."""
        from strands.models.bedrock import BedrockModel

        from yukar.llm.factory import create_model

        settings = self._make_settings(
            arbiter_model_id="anthropic.claude-opus-4-8-v1:0",
            worker_model_id=None,
        )
        model = create_model(settings, role="worker")
        assert isinstance(model, BedrockModel)
        cfg = model.get_config()
        # worker has no override → global
        assert cfg["model_id"] == "us.anthropic.claude-sonnet-4-6-20250514-v1:0"


# ---------------------------------------------------------------------------
# 7. Application inference profile — fast-path bypass hardening
# ---------------------------------------------------------------------------


class TestAppProfileFastPathBypass:
    """Application inference profile ARNs must always bypass the fast-path
    even when the opaque ID accidentally contains a pricing-key substring
    (e.g. "haiku").  Hardening 1 from the 2026-06-19 code review.
    """

    # ARN whose opaque ID embeds the FULL pricing key "haiku-4-5" so that
    # get_pricing(arn) actually matches.  This makes the test discriminate the
    # is_app_profile bypass: without the guard the fast-path would short-circuit
    # (get_pricing != None → return the ARN, boto3 NOT called) and the assertions
    # below would fail; with the guard it always resolves via boto3.
    _COLLIDING_ARN = (
        "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/p-haiku-4-5-x"
    )
    _HAIKU_FOUNDATION_ARN = (
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-haiku-4-5-20250101-v1:0"
    )
    _HAIKU_FOUNDATION_ID = "anthropic.claude-haiku-4-5-20250101-v1:0"

    @pytest.mark.asyncio
    async def test_colliding_arn_calls_get_inference_profile(self) -> None:
        """ARN whose opaque ID contains 'haiku' must NOT be short-circuited by fast-path.

        The fix ensures get_inference_profile (boto3) is always called for
        application-inference-profile ARNs, regardless of substring collisions.
        """
        mock_bedrock = MagicMock()
        mock_bedrock.get_inference_profile.return_value = {
            "models": [{"modelArn": self._HAIKU_FOUNDATION_ARN}],
            "type": "APPLICATION",
        }

        with patch("boto3.client", return_value=mock_bedrock) as mock_client:
            from yukar.llm.inference_profile import resolve_model_id_for_pricing

            result = await resolve_model_id_for_pricing(
                self._COLLIDING_ARN,
                region="us-east-1",
                provider_is_bedrock=True,
            )

        # Must resolve to the actual foundation model, not short-circuit on "haiku"
        assert result == self._HAIKU_FOUNDATION_ID
        # boto3 must have been called (fast-path bypassed)
        mock_client.assert_called_once()
        mock_bedrock.get_inference_profile.assert_called_once_with(
            inferenceProfileIdentifier=self._COLLIDING_ARN
        )

    @pytest.mark.asyncio
    async def test_app_profile_arn_always_calls_boto3_when_bedrock(self) -> None:
        """Any application-inference-profile ARN must call boto3 when provider_is_bedrock=True."""
        mock_bedrock = MagicMock()
        mock_bedrock.get_inference_profile.return_value = _make_get_inference_profile_resp()

        with patch("boto3.client", return_value=mock_bedrock):
            from yukar.llm.inference_profile import resolve_model_id_for_pricing

            await resolve_model_id_for_pricing(
                _APP_PROFILE_ARN,
                region="us-east-1",
                provider_is_bedrock=True,
            )

        mock_bedrock.get_inference_profile.assert_called_once()

    @pytest.mark.asyncio
    async def test_app_profile_arn_non_bedrock_no_boto3(self) -> None:
        """provider_is_bedrock=False must prevent boto3 call even for app profile ARNs."""
        with patch("boto3.client") as mock_client:
            from yukar.llm.inference_profile import resolve_model_id_for_pricing

            result = await resolve_model_id_for_pricing(
                _APP_PROFILE_ARN,
                region=None,
                provider_is_bedrock=False,
            )

        assert result == _APP_PROFILE_ARN
        mock_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_cross_region_profile_still_uses_fast_path(self) -> None:
        """Cross-region / system inference profiles (us.anthropic.…) must keep fast-path.

        They don't contain 'application-inference-profile', so the fast-path
        must remain for them (zero network calls when pricing is known).
        """
        _cross_region_id = "us.anthropic.claude-sonnet-4-6-20250514-v1:0"

        with patch("boto3.client") as mock_client:
            from yukar.llm.inference_profile import resolve_model_id_for_pricing

            result = await resolve_model_id_for_pricing(
                _cross_region_id,
                region="us-east-1",
                provider_is_bedrock=True,
            )

        assert result == _cross_region_id
        mock_client.assert_not_called()


# ---------------------------------------------------------------------------
# 8. AgentUsageRecorder — one-shot resolution integration tests
# ---------------------------------------------------------------------------


class TestAgentUsageRecorderResolution:
    """Verify that AgentUsageRecorder._record resolves the model ID exactly once
    (one-shot) and passes the resolved model ID to the tracker on every call.
    Hardening 2 from the 2026-06-19 code review.
    """

    _APP_PROFILE_ARN = (
        "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/recorder-test"
    )
    _RESOLVED_MODEL_ID = "anthropic.claude-opus-4-8-20250101-v1:0"

    def _make_fake_agent(self, model_id: str, region: str = "us-east-1") -> MagicMock:
        """Build a minimal fake Strands Agent whose model looks like BedrockModel."""
        fake_meta = MagicMock()
        fake_meta.region_name = region

        fake_client = MagicMock()
        fake_client.meta = fake_meta

        fake_model = MagicMock()
        fake_model.__class__.__name__ = "BedrockModel"
        fake_model.client = fake_client
        fake_model.get_config.return_value = {"model_id": model_id}

        fake_metrics = MagicMock()
        fake_metrics.accumulated_usage = {
            "inputTokens": 0,
            "outputTokens": 0,
            "cacheReadInputTokens": 0,
            "cacheWriteInputTokens": 0,
        }

        fake_agent = MagicMock()
        fake_agent.model = fake_model
        fake_agent.event_loop_metrics = fake_metrics
        fake_agent.callback_handler = MagicMock()

        return fake_agent

    @pytest.mark.asyncio
    async def test_first_record_resolves_model_id(self) -> None:
        """On the first _record call, resolve_model_id_for_pricing is awaited
        and the resolved model ID is passed to tracker.record.
        """
        from yukar.agents.streaming.usage_recorder import AgentUsageRecorder
        from yukar.usage.tracker import UsageDelta

        recorder = AgentUsageRecorder(
            project_id="p1",
            epic_id="e1",
            run_id="r1",
            role="manager",
        )

        fake_agent = self._make_fake_agent(self._APP_PROFILE_ARN)
        # Prime the snapshot with non-zero usage so the delta is non-zero.
        fake_agent.event_loop_metrics.accumulated_usage = {
            "inputTokens": 1000,
            "outputTokens": 200,
            "cacheReadInputTokens": 0,
            "cacheWriteInputTokens": 0,
        }

        recorder.bind(fake_agent)

        # Patch the resolver so we can track calls.  Capture region and
        # provider_is_bedrock too, so a regression in bind()'s BedrockModel
        # detection or region extraction (which would silently disable
        # resolution and record app-profile cost as 0.0) is caught here.
        resolve_calls: list[str] = []
        resolve_kwargs: list[tuple[str | None, bool]] = []

        async def fake_resolve(
            model_id: str, *, region: str | None, provider_is_bedrock: bool
        ) -> str:
            resolve_calls.append(model_id)
            resolve_kwargs.append((region, provider_is_bedrock))
            return self._RESOLVED_MODEL_ID

        # Capture what model_id gets passed to tracker.record.
        recorded_model_ids: list[str] = []

        async def fake_tracker_record(
            *,
            project_id: str,
            epic_id: str,
            run_id: str,
            role: str,
            model_id: str,
            delta: UsageDelta,
        ) -> None:
            recorded_model_ids.append(model_id)

        fake_tracker = MagicMock()
        fake_tracker.record = fake_tracker_record

        with (
            patch(
                "yukar.llm.inference_profile.resolve_model_id_for_pricing",
                side_effect=fake_resolve,
            ),
            patch(
                "yukar.usage.tracker.get_tracker",
                return_value=fake_tracker,
            ),
        ):
            delta = UsageDelta(input_tokens=1000, output_tokens=200)
            await recorder._record(delta)

        # Resolver must have been called once with the raw ARN.
        assert resolve_calls == [self._APP_PROFILE_ARN]
        # bind() must have detected BedrockModel and extracted the region from
        # model.client.meta.region_name, then passed both to the resolver.
        assert resolve_kwargs == [("us-east-1", True)]
        # tracker.record must have received the resolved model ID.
        assert recorded_model_ids == [self._RESOLVED_MODEL_ID]

    @pytest.mark.asyncio
    async def test_second_record_does_not_re_resolve(self) -> None:
        """After the first _record resolves the model ID, _model_resolved is True
        and subsequent _record calls must not invoke the resolver again.
        """
        from yukar.agents.streaming.usage_recorder import AgentUsageRecorder
        from yukar.usage.tracker import UsageDelta

        recorder = AgentUsageRecorder(
            project_id="p1",
            epic_id="e1",
            run_id="r2",
            role="worker",
        )

        fake_agent = self._make_fake_agent(self._APP_PROFILE_ARN)
        fake_agent.event_loop_metrics.accumulated_usage = {
            "inputTokens": 500,
            "outputTokens": 100,
            "cacheReadInputTokens": 0,
            "cacheWriteInputTokens": 0,
        }
        recorder.bind(fake_agent)

        resolve_calls: list[str] = []

        async def fake_resolve(
            model_id: str, *, region: str | None, provider_is_bedrock: bool
        ) -> str:
            resolve_calls.append(model_id)
            return self._RESOLVED_MODEL_ID

        recorded_model_ids: list[str] = []

        async def fake_tracker_record(
            *,
            project_id: str,
            epic_id: str,
            run_id: str,
            role: str,
            model_id: str,
            delta: UsageDelta,
        ) -> None:
            recorded_model_ids.append(model_id)

        fake_tracker = MagicMock()
        fake_tracker.record = fake_tracker_record

        delta = UsageDelta(input_tokens=500, output_tokens=100)

        with (
            patch(
                "yukar.llm.inference_profile.resolve_model_id_for_pricing",
                side_effect=fake_resolve,
            ),
            patch(
                "yukar.usage.tracker.get_tracker",
                return_value=fake_tracker,
            ),
        ):
            # First call: resolver runs, _model_resolved becomes True.
            await recorder._record(delta)
            assert recorder._model_resolved is True
            assert len(resolve_calls) == 1

            # Second call: resolver must NOT run again.
            await recorder._record(delta)

        assert len(resolve_calls) == 1, (
            f"resolver called {len(resolve_calls)} times; expected exactly 1"
        )
        # Both records must carry the resolved model ID.
        assert all(m == self._RESOLVED_MODEL_ID for m in recorded_model_ids)
        assert len(recorded_model_ids) == 2
