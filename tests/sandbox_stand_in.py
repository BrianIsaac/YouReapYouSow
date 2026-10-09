"""The agentic mock behind the sandbox client, standing in for Reap's sandbox.

``build_runtime`` builds ``ReapSandbox`` when ``REAP_BACKEND=sandbox``; here that client
talks HTTP to the agentic mock in process, so the runtime takes its sandbox code paths
(no card step: an enrolment it creates waits on its hosted page, as on Reap) without
leaving the process. The operator's enrolment is created and completed on the mock
beforehand, as ``swap_check --enrol`` and Reap's hosted page do against the real
sandbox.
"""

import httpx
import pytest
from pydantic import SecretStr

from youreapyousow.clock import ManualClock
from youreapyousow.reap.client import MOCK_BASE_URL, ReapHttpClient
from youreapyousow.reap.mock.agentic import TEST_CARDS, TEST_OTP, AgenticMockEngine
from youreapyousow.reap.mock.engine import MockReapEngine
from youreapyousow.reap.mock.server import create_mock_app
from youreapyousow.reap.models import (
    ClientReferenceOwner,
    CreateExternalEnrollmentRequest,
    Presentation,
)

STAND_IN_KEY = SecretStr("stand-in-key")


def stand_in_sandbox(monkeypatch: pytest.MonkeyPatch, clock: ManualClock) -> str:
    """Point the runtime's sandbox client at the agentic mock and enrol the operator there.

    Args:
        monkeypatch: Replaces ``ReapSandbox`` where ``build_runtime`` builds it.
        clock: The runtime's clock, shared by the mock.

    Returns:
        The operator's ``ACTIVE`` enrolment, the value ``REAP_ENROLLMENT_ID`` holds.
    """
    engine = AgenticMockEngine(clock=clock)
    app = create_mock_app(MockReapEngine(clock=clock), clock, agentic=engine)

    def sandbox(*, api_key: SecretStr, base_url: str) -> ReapHttpClient:
        return ReapHttpClient(
            base_url=MOCK_BASE_URL,
            api_key=api_key,
            backend="sandbox",
            transport=httpx.ASGITransport(app=app),
        )

    monkeypatch.setattr("youreapyousow.api.app.ReapSandbox", sandbox)
    created = engine.create_enrollment(
        CreateExternalEnrollmentRequest(
            owner=ClientReferenceOwner(id="operator", email="operator@example.com"),
            presentation=Presentation(return_url="https://example.com/return"),
        )
    )
    number, (cvc, expiry) = next(iter(TEST_CARDS.items()))
    engine.submit_card(created.id, number=number, cvc=cvc, expiry=expiry, otp=TEST_OTP)
    return created.id
