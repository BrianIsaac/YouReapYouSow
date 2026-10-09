"""Chat completions over an OpenAI-compatible endpoint: Featherless, or any other.

The coach and the evidence reader call OpenAI first and Featherless after, both through
this client. The client only talks; what a reply may do is decided elsewhere.
"""

from typing import Literal, Protocol

import httpx
from pydantic import JsonValue, SecretStr

type JsonMode = Literal["structured", "prompt"]
type Message = dict[str, JsonValue]

FEATHERLESS_JSON_MODE: JsonMode = "prompt"
FEATHERLESS_REASONING_EFFORT = "low"


class ModelError(RuntimeError):
    """Raised when the model endpoint cannot give an answer."""


class ChatClient(Protocol):
    """One chat completion: messages in, the assistant's text out."""

    model: str

    async def complete(self, messages: list[Message]) -> str:
        """Complete a chat.

        Args:
            messages: The conversation, as ``{"role", "content"}`` dicts.

        Returns:
            The assistant's reply text.
        """
        ...


class OpenAICompatibleClient:
    """Chat completions over any OpenAI-compatible endpoint, through httpx."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str,
        model: str,
        api_key: SecretStr | None = None,
        timeout_s: float = 30.0,
        json_mode: JsonMode = "structured",
        reasoning_effort: str | None = None,
        temperature: float | None = 0,
        response_format: dict[str, JsonValue] | None = None,
    ) -> None:
        """Configure the client.

        Args:
            http: Shared HTTP client.
            base_url: The API root, such as ``https://api.example.com/v1``.
            model: The model name to request.
            api_key: Bearer key, if the endpoint needs one.
            timeout_s: Per-request timeout.
            json_mode: ``structured`` asks the endpoint for a JSON object through
                ``response_format``; ``prompt`` leaves the shape to the system prompt.
            reasoning_effort: Sent as ``reasoning_effort`` to a reasoning model when
                set; left out otherwise.
            temperature: Sent when set; left out for models that take only their own.
            response_format: Sent in place of the JSON-object mode when set, such as a
                ``json_schema`` for structured output.
        """
        self._http = http
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self.model = model
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._json_mode = json_mode
        self._reasoning_effort = reasoning_effort
        self._temperature = temperature
        self._response_format = response_format

    async def complete(self, messages: list[Message]) -> str:
        """Ask the endpoint for a JSON reply.

        Args:
            messages: The conversation.

        Returns:
            The reply text.

        Raises:
            ModelError: On a timeout, a transport error, a non-2xx status or an
                unexpected body.
        """
        headers: dict[str, str] = {}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key.get_secret_value()}"
        body: dict[str, object] = {"model": self.model, "messages": messages}
        if self._temperature is not None:
            body["temperature"] = self._temperature
        if self._response_format is not None:
            body["response_format"] = self._response_format
        elif self._json_mode == "structured":
            body["response_format"] = {"type": "json_object"}
        if self._reasoning_effort is not None:
            body["reasoning_effort"] = self._reasoning_effort
        try:
            response = await self._http.post(
                self._url, json=body, headers=headers, timeout=self._timeout_s
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        except httpx.TimeoutException as error:
            raise ModelError(f"timed out after {self._timeout_s:g} s") from error
        except httpx.HTTPStatusError as error:
            raise ModelError(f"HTTP {error.response.status_code}") from error
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as error:
            raise ModelError(f"{type(error).__name__}: {error}") from error
        if not isinstance(content, str) or not content.strip():
            raise ModelError("reply has no text content")
        return content
