"""Rollback and termination rules: when acquired capacity must stop costing money.

Pure functions over a deployment, its observations and ``now``. A purchase is never
assumed to have worked: unless the objective's constraints are seen to hold within the
verification window, the capacity is rolled back.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel

from youreapyousow.authority.grants import AuthorityGrant
from youreapyousow.domain import Deployment, DeploymentStatus, Objective, Observation


class LifecycleAction(StrEnum):
    """What should happen to a deployment."""

    KEEP = "keep"
    RELEASE = "release"
    ROLLBACK = "rollback"


class LifecycleRules(BaseModel):
    """The operator's termination and rollback settings for an objective.

    Attributes:
        max_lease_hours: Release unconditionally after this long.
        verify_within_s: The constraints must be seen to hold within this many seconds
            of the deployment starting, or it is rolled back as ineffective.
        load_metric: The demand metric, such as ``requests_per_min``.
        normal_below: Demand below this is normal.
        release_after_normal_s: Release once demand has been normal for this long.
    """

    max_lease_hours: Decimal = Decimal(2)
    verify_within_s: int = 120
    load_metric: str = "requests_per_min"
    normal_below: Decimal = Decimal(20)
    release_after_normal_s: int = 300


@dataclass(frozen=True)
class LifecycleDecision:
    """A lifecycle verdict naming the rule that decided it.

    Attributes:
        action: Keep, release or roll back.
        rule: The deciding rule's identifier.
        reason: Human-readable explanation.
    """

    action: LifecycleAction
    rule: str
    reason: str


def _verified(
    objective: Objective, observations: list[Observation], start: datetime, end: datetime
) -> bool:
    """Report whether every constraint was met at least once inside ``[start, end]``.

    Args:
        objective: Supplies the constraints.
        observations: Candidate observations.
        start: Window start.
        end: Window end.

    Returns:
        True when each constraint has a satisfying observation in the window.
    """
    met: dict[str, bool] = defaultdict(bool)
    for obs in observations:
        if start <= obs.at <= end:
            for constraint in objective.constraints:
                if constraint.metric == obs.metric and constraint.is_met(obs.value):
                    met[constraint.metric] = True
    return all(met[c.metric] for c in objective.constraints)


def _normal_since(
    rules: LifecycleRules, observations: list[Observation], start: datetime
) -> datetime | None:
    """Find when demand last became normal after ``start``.

    Args:
        rules: Supplies the load metric and threshold.
        observations: Candidate observations.
        start: Only observations from here on count.

    Returns:
        The time of the first normal reading after the last abnormal one, or None if
        the latest reading is not normal or there are none.
    """
    load = sorted(
        (o for o in observations if o.metric == rules.load_metric and o.at >= start),
        key=lambda o: o.at,
    )
    since: datetime | None = None
    for obs in load:
        normal = obs.value < rules.normal_below
        since = (since or obs.at) if normal else None
    return since


def evaluate_deployment(
    deployment: Deployment,
    *,
    objective: Objective,
    grant: AuthorityGrant | None,
    observations: list[Observation],
    rules: LifecycleRules,
    now: datetime,
) -> LifecycleDecision:
    """Decide whether a deployment is kept, released or rolled back.

    Rules, in order: protected baseline capacity is never touched; a stopped
    deployment is left alone; the maximum lease forces release; lapsed authority forces
    release; a failed post-action verification forces rollback; normalised demand
    releases; otherwise keep.

    Args:
        deployment: The deployment.
        objective: Its objective, for the constraints.
        grant: The objective's current grant, if any.
        observations: Observations for the objective.
        rules: The lifecycle settings.
        now: The time of evaluation.

    Returns:
        The verdict and the rule that produced it.
    """
    if deployment.protected:
        return LifecycleDecision(
            LifecycleAction.KEEP,
            "deployment.protected",
            "baseline capacity is never terminated by the agent",
        )
    if deployment.status != DeploymentStatus.RUNNING:
        return LifecycleDecision(LifecycleAction.KEEP, "deployment.not_running", "already released")
    age = now - deployment.started_at
    lease = timedelta(hours=float(rules.max_lease_hours))
    if age >= lease:
        return LifecycleDecision(
            LifecycleAction.RELEASE, "lease.expired", f"running {age}, maximum lease {lease}"
        )
    if grant is None or not grant.is_live(now):
        return LifecycleDecision(
            LifecycleAction.RELEASE,
            "grant.lapsed",
            "spending authority has expired or been revoked",
        )
    window_end = deployment.started_at + timedelta(seconds=rules.verify_within_s)
    if now >= window_end and not _verified(
        objective, observations, deployment.started_at, window_end
    ):
        return LifecycleDecision(
            LifecycleAction.ROLLBACK,
            "verification.failed",
            f"constraints not met within {rules.verify_within_s}s of start",
        )
    normal_since = _normal_since(rules, observations, deployment.started_at)
    if normal_since is not None:
        normal_for = now - normal_since
        if normal_for >= timedelta(seconds=rules.release_after_normal_s):
            return LifecycleDecision(
                LifecycleAction.RELEASE,
                "demand.normalised",
                f"{rules.load_metric} below {rules.normal_below} for {normal_for}",
            )
    return LifecycleDecision(LifecycleAction.KEEP, "slo.holding", "capacity still needed")
