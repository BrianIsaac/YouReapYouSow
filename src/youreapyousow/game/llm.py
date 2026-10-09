"""The model chain: OpenAI first, Featherless after, each reply validated before it counts.

A link that times out, fails, or answers something that does not parse is passed over for
the next one; a reply is only ever advisory input to deterministic code.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import ValidationError

from youreapyousow.config import Settings
from youreapyousow.featherless import (
    FEATHERLESS_JSON_MODE,
    FEATHERLESS_REASONING_EFFORT,
    ChatClient,
    Message,
    ModelError,
    OpenAICompatibleClient,
)

type Purpose = Literal["coach", "vision"]


class ChainError(RuntimeError):
    """Raised when no link in the chain gave a usable answer."""


@dataclass(frozen=True)
class Link:
    """One model in the chain.

    Attributes:
        label: Its name on the record, such as ``openai:gpt-5.6-terra``.
        client: The client that asks it.
    """

    label: str
    client: ChatClient


async def ask[T](
    links: list[Link], messages: list[Message], parse: Callable[[str], T]
) -> tuple[T, str]:
    """Ask each link in turn until one answers something that parses.

    Args:
        links: The chain, in order.
        messages: The conversation.
        parse: Turns the reply text into the value, raising ``ValueError`` (or a
            pydantic ``ValidationError``) when it does not fit.

    Returns:
        The parsed value and the label of the link that gave it.

    Raises:
        ChainError: If every link failed, naming each and why.
    """
    errors: list[str] = []
    for link in links:
        try:
            text = await link.client.complete(messages)
            return parse(text), link.label
        except (ModelError, ValueError, ValidationError) as error:
            errors.append(f"{link.label}: {type(error).__name__}")
    raise ChainError("; ".join(errors) or "no model configured")


def build_links(settings: Settings, http: httpx.AsyncClient, purpose: Purpose) -> list[Link]:
    """Build the chain for the coach or the photo reader from the settings.

    OpenAI's two models come first when the provider is ``openai``, then Featherless when
    its key is set; ``featherless`` alone skips OpenAI; ``none`` is an empty chain.

    Args:
        settings: The configuration.
        http: The shared HTTP client.
        purpose: ``coach`` or ``vision``.

    Returns:
        The links, in order.
    """
    provider = settings.chat_provider
    links: list[Link] = []
    if provider == "openai" and settings.openai_api_key is not None:
        models = (
            (settings.coach_model, settings.coach_fallback_model)
            if purpose == "coach"
            else (settings.vision_model, settings.vision_fallback_model)
        )
        for model in dict.fromkeys(models):
            links.append(
                Link(
                    f"openai:{model}",
                    OpenAICompatibleClient(
                        http,
                        base_url=settings.openai_base_url,
                        model=model,
                        api_key=settings.openai_api_key,
                        timeout_s=settings.openai_timeout_s,
                        temperature=None,
                    ),
                )
            )
    if provider in ("openai", "featherless") and settings.featherless_api_key is not None:
        model = (
            settings.featherless_model if purpose == "coach" else settings.featherless_vision_model
        )
        links.append(
            Link(
                f"featherless:{model}",
                OpenAICompatibleClient(
                    http,
                    base_url=settings.featherless_base_url,
                    model=model,
                    api_key=settings.featherless_api_key,
                    timeout_s=max(settings.featherless_timeout_s, 20.0),
                    json_mode=FEATHERLESS_JSON_MODE,
                    reasoning_effort=FEATHERLESS_REASONING_EFFORT if purpose == "coach" else None,
                ),
            )
        )
    return links


def json_object(text: str) -> str:
    """Cut the JSON object out of a reply that may wrap it in prose or a code fence.

    Args:
        text: The reply.

    Returns:
        The text from the first ``{`` to the last ``}``.

    Raises:
        ValueError: If there is no object.
    """
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    return text[start : end + 1]
