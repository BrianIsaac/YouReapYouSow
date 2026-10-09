"""The connector contract shared by every market source."""

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from importlib import resources

import httpx
from pydantic import JsonValue

from youreapyousow.domain import ResourceSpec, Source


@dataclass(frozen=True)
class FetchMeta:
    """Where a raw response came from, stamped on everything parsed from it.

    Attributes:
        source: Live, cached or mock.
        fetched_at: When the response was fetched (for a fixture, when it was loaded).
        latency_ms: How long the fetch took, when it was live.
        raw_sha256: Hash of the raw bytes, the provenance reference.
    """

    source: Source
    fetched_at: datetime
    latency_ms: int | None
    raw_sha256: str

    def ref(self, index: int) -> str:
        """Build the provenance reference for one item of the response.

        Args:
            index: The item's position in the response.

        Returns:
            ``sha256:<digest>#<index>``.
        """
        return f"sha256:{self.raw_sha256}#{index}"


def sha256(raw: bytes) -> str:
    """Hash raw response bytes.

    Args:
        raw: The bytes.

    Returns:
        Hex digest.
    """
    return hashlib.sha256(raw).hexdigest()


class Connector[T](ABC):
    """A market source: how to ask it, and how to read its answer.

    Subclasses set ``name`` and ``fixture``; the fixture is a real response, so the mock
    path runs the same parser on the same shape. The GPU market's (the compute connectors
    and the references) are recorded with ``RECORD_FIXTURES=1 uv run pytest -m live
    tests/market``; the model sources' with ``RECORD_FIXTURES=1 uv run pytest -m live
    tests/ml/test_market_live.py``.
    """

    name: str
    fixture: str

    @abstractmethod
    def request(self, spec: ResourceSpec) -> httpx.Request:
        """Build the HTTP request for a spec.

        Args:
            spec: What is wanted; connectors push down what their API can filter.

        Returns:
            The request, unsent.
        """

    @abstractmethod
    def parse(self, payload: JsonValue, meta: FetchMeta) -> list[T]:
        """Normalise a raw response.

        Args:
            payload: The decoded JSON body.
            meta: Provenance to stamp on each item.

        Returns:
            The normalised items.
        """

    def cache_key(self, spec: ResourceSpec) -> str:
        """Key under which a response for ``spec`` is cached.

        Args:
            spec: The spec.

        Returns:
            A key; connectors whose request ignores the spec return a constant.
        """
        return f"{self.name}:{spec.model_dump_json()}"

    def fixture_bytes(self) -> bytes:
        """Load the recorded response shipped with the package.

        Returns:
            The raw fixture bytes.
        """
        return resources.files("youreapyousow.market.fixtures").joinpath(self.fixture).read_bytes()
