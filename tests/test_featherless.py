"""The Featherless chat client speaks OpenAI chat completions and names its failures."""

import json

import httpx
import pytest
from pydantic import SecretStr

from youreapyousow.featherless import ModelError, OpenAICompatibleClient

pytestmark = pytest.mark.anyio


async def test_http_client_speaks_openai_chat_completions() -> None:
    """One POST to /chat/completions with the model, JSON mode and the bearer key."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"a": 1}'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenAICompatibleClient(
            http, base_url="https://llm.example/v1/", model="m-1", api_key=SecretStr("k")
        )
        assert await client.complete([{"role": "user", "content": "hi"}]) == '{"a": 1}'
    request = seen[0]
    assert str(request.url) == "https://llm.example/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer k"
    body = json.loads(request.content)
    assert body["model"] == "m-1"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"error": "down"}),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
    ],
    ids=["server error", "no choices", "not json", "no text"],
)
async def test_http_client_failures_are_model_errors(response: httpx.Response) -> None:
    """Every way the endpoint can fail surfaces as one error type, without a key sent."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenAICompatibleClient(http, base_url="http://localhost:8000/v1", model="m")
        with pytest.raises(ModelError):
            await client.complete([])
    assert "authorization" not in seen[0].headers


async def test_http_client_names_its_timeout() -> None:
    """A timeout is a model error that says how long was waited."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenAICompatibleClient(
            http, base_url="http://127.0.0.1:8090/v1", model="m", timeout_s=2.5
        )
        with pytest.raises(ModelError, match=r"^timed out after 2\.5 s$"):
            await client.complete([])


async def test_http_client_in_prompt_mode_leaves_the_shape_to_the_prompt() -> None:
    """Prompt mode sends no ``response_format``; a reasoning effort is sent when set."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"a": 1}'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenAICompatibleClient(
            http,
            base_url="https://llm.example/v1",
            model="zai-org/GLM-5.3-Flash",
            api_key=SecretStr("k"),
            json_mode="prompt",
            reasoning_effort="low",
        )
        assert await client.complete([{"role": "user", "content": "hi"}]) == '{"a": 1}'
    body = json.loads(seen[0].content)
    assert "response_format" not in body
    assert body["reasoning_effort"] == "low"
    assert body["model"] == "zai-org/GLM-5.3-Flash"
    assert seen[0].headers["authorization"] == "Bearer k"


async def test_http_client_sends_no_reasoning_effort_unless_asked() -> None:
    """The local server gets the body it was measured with: no reasoning field."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenAICompatibleClient(http, base_url="http://127.0.0.1:8090/v1", model="m")
        await client.complete([])
    assert "reasoning_effort" not in json.loads(seen[0].content)


async def test_http_client_treats_an_empty_reply_as_no_answer() -> None:
    """A reasoning model that spends its reply thinking has not answered; the next may."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "", "reasoning": "hmm"}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenAICompatibleClient(http, base_url="http://127.0.0.1:8090/v1", model="m")
        with pytest.raises(ModelError, match="reply has no text content"):
            await client.complete([])


async def test_http_client_names_a_non_2xx_by_its_status_alone() -> None:
    """The rationale reads ``HTTP 401``, never the URL or the request behind it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenAICompatibleClient(http, base_url="https://llm.example/v1", model="m")
        with pytest.raises(ModelError, match=r"^HTTP 401$"):
            await client.complete([])
