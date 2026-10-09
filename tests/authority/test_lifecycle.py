"""Tests for the rollback and termination rules."""

from datetime import timedelta
from decimal import Decimal

from tests.conftest import START
from tests.factories import make_grant, make_objective, make_offer
from youreapyousow.authority.grants import AuthorityGrant
from youreapyousow.authority.lifecycle import (
    LifecycleAction,
    LifecycleRules,
    evaluate_deployment,
)
from youreapyousow.domain import Deployment, DeploymentStatus, Observation

RULES = LifecycleRules(
    max_lease_hours=Decimal(2),
    verify_within_s=120,
    load_metric="requests_per_min",
    normal_below=Decimal(20),
    release_after_normal_s=300,
)


def _deployment(**overrides: object) -> Deployment:
    base = Deployment(
        id="dep_1",
        objective_id="obj_1",
        intent_id="int_1",
        provider="vast",
        offer=make_offer(),
        started_at=START,
    )
    return base.model_copy(update=overrides)


def _obs(metric: str, value: int, seconds: int) -> Observation:
    return Observation(
        id=f"o_{metric}_{seconds}",
        objective_id="obj_1",
        metric=metric,
        value=Decimal(value),
        at=START + timedelta(seconds=seconds),
    )


GRANT = make_grant()


def _decide(
    deployment: Deployment,
    observations: list[Observation],
    seconds: float,
    grant: AuthorityGrant | None = GRANT,
) -> tuple[LifecycleAction, str]:
    decision = evaluate_deployment(
        deployment,
        objective=make_objective(),
        grant=grant,
        observations=observations,
        rules=RULES,
        now=START + timedelta(seconds=seconds),
    )
    return decision.action, decision.rule


RECOVERED = [_obs("p95_latency_ms", 418, 30), _obs("requests_per_min", 80, 30)]


def test_protected_baseline_is_never_released() -> None:
    """Baseline capacity is kept even when every release condition holds."""
    assert _decide(_deployment(protected=True), [], 99_999, grant=None) == (
        LifecycleAction.KEEP,
        "deployment.protected",
    )


def test_released_deployment_is_left_alone() -> None:
    """A stopped deployment needs no further action."""
    deployment = _deployment(status=DeploymentStatus.RELEASED)
    assert _decide(deployment, [], 10) == (LifecycleAction.KEEP, "deployment.not_running")


def test_lease_expiry_forces_release() -> None:
    """The maximum lease ends the spend regardless of load."""
    assert _decide(_deployment(), RECOVERED, 7200) == (LifecycleAction.RELEASE, "lease.expired")


def test_lapsed_authority_forces_release() -> None:
    """Revoked or expired authority stops the spend."""
    revoked = make_grant().model_copy(update={"revoked_at": START})
    assert _decide(_deployment(), RECOVERED, 60, grant=revoked) == (
        LifecycleAction.RELEASE,
        "grant.lapsed",
    )
    assert _decide(_deployment(), RECOVERED, 60, grant=None)[1] == "grant.lapsed"


def test_unverified_purchase_is_rolled_back() -> None:
    """No recovery inside the window means the purchase did not work."""
    still_slow = [_obs("p95_latency_ms", 1360, 60), _obs("requests_per_min", 80, 60)]
    assert _decide(_deployment(), still_slow, 120) == (
        LifecycleAction.ROLLBACK,
        "verification.failed",
    )
    assert _decide(_deployment(), [], 121)[1] == "verification.failed"


def test_recovery_only_counts_inside_the_window() -> None:
    """A late recovery does not retroactively verify the purchase."""
    late = [_obs("p95_latency_ms", 400, 200)]
    assert _decide(_deployment(), late, 200)[1] == "verification.failed"


def test_verification_window_still_open_keeps() -> None:
    """Before the window closes, an unrecovered deployment is kept."""
    assert _decide(_deployment(), [], 60) == (LifecycleAction.KEEP, "slo.holding")


def test_normalised_demand_releases_after_the_hold_period() -> None:
    """Demand normal for long enough releases; a fresh spike resets the clock."""
    calm = [
        *RECOVERED,
        _obs("requests_per_min", 14, 600),
        _obs("requests_per_min", 12, 700),
    ]
    assert _decide(_deployment(), calm, 899)[1] == "slo.holding"
    assert _decide(_deployment(), calm, 900) == (LifecycleAction.RELEASE, "demand.normalised")

    spiky = [*calm, _obs("requests_per_min", 90, 800)]
    assert _decide(_deployment(), spiky, 1000)[1] == "slo.holding"
