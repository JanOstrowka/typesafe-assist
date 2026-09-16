"""Minimal async client for the TypeSafe System One HTTP API.

The API is a single endpoint (``POST /v1/systemone``), so we talk to it with
Home Assistant's shared aiohttp session instead of pulling in the vendor SDK
(which is only published on TypeSafe's own package index).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .const import DEFAULT_BASE_URL, DEFAULT_MODEL, DEFAULT_TIMEOUT

_LOGGER = logging.getLogger(__name__)

RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504, 529}


class TypeSafeError(Exception):
    """Base error for the TypeSafe client."""


class TypeSafeAuthError(TypeSafeError):
    """API key rejected."""


class TypeSafeConnectionError(TypeSafeError):
    """Could not reach the API or it timed out."""


class TypeSafeRequestError(TypeSafeError):
    """The API rejected the request body (422) or returned another 4xx."""

    def __init__(self, status: int, body: Any) -> None:
        """Initialize."""
        super().__init__(f"TypeSafe API returned {status}: {body}")
        self.status = status
        self.body = body


@dataclass(slots=True)
class ChoiceAnswer:
    """A Choice answer."""

    choice: str
    probabilities: dict[str, float]
    confidence: float

    def probability(self, option: str) -> float:
        """Return the probability of an option."""
        return self.probabilities.get(option, 0.0)

    def ranked(self) -> list[tuple[str, float]]:
        """Return options sorted by probability, highest first."""
        return sorted(self.probabilities.items(), key=lambda kv: -kv[1])


@dataclass(slots=True)
class ScoreAnswer:
    """A Score answer."""

    score: float
    legend: dict[str, str]
    probabilities: dict[str, float]
    confidence: float


@dataclass(slots=True)
class NoulAnswer:
    """A Noul (yes/no probability) answer."""

    noul: float


Answer = ChoiceAnswer | ScoreAnswer | NoulAnswer


@dataclass(slots=True)
class SystemOneResponse:
    """Parsed response of a System One evaluation."""

    model: str
    answers: dict[str, Answer]
    usage: dict[str, int] = field(default_factory=dict)

    def choice(self, key: str) -> ChoiceAnswer | None:
        """Return a choice answer or None."""
        answer = self.answers.get(key)
        return answer if isinstance(answer, ChoiceAnswer) else None

    def noul(self, key: str, default: float = 0.0) -> float:
        """Return a noul probability."""
        answer = self.answers.get(key)
        return answer.noul if isinstance(answer, NoulAnswer) else default


def _parse_answer(raw: dict[str, Any]) -> Answer | None:
    kind = raw.get("type")
    if kind == "choice":
        return ChoiceAnswer(
            choice=str(raw.get("choice")),
            probabilities={
                str(k): float(v) for k, v in (raw.get("probabilities") or {}).items()
            },
            confidence=float(raw.get("confidence", 0.0)),
        )
    if kind == "score":
        return ScoreAnswer(
            score=float(raw.get("score", 0.0)),
            legend={str(k): str(v) for k, v in (raw.get("legend") or {}).items()},
            probabilities={
                str(k): float(v) for k, v in (raw.get("probabilities") or {}).items()
            },
            confidence=float(raw.get("confidence", 0.0)),
        )
    if kind == "noul":
        return NoulAnswer(noul=float(raw.get("noul", 0.0)))
    return None


class TypeSafeClient:
    """Tiny client for ``POST /v1/systemone``."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 1,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self.model = model
        self._timeout = timeout
        self._max_retries = max_retries

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def system_one(
        self,
        state: Any,
        questions: dict[str, dict[str, Any]],
        *,
        model: str | None = None,
    ) -> SystemOneResponse:
        """Evaluate ``questions`` against ``state``."""
        payload = {
            "state": state,
            "model": model or self.model,
            "questions": questions,
        }
        url = f"{self._base_url}/v1/systemone"

        attempt = 0
        while True:
            try:
                async with asyncio.timeout(self._timeout):
                    async with self._session.post(
                        url, json=payload, headers=self._headers
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            break
                        body: Any
                        try:
                            body = await resp.json()
                        except (aiohttp.ContentTypeError, ValueError):
                            body = await resp.text()
                        if resp.status in (401, 403):
                            raise TypeSafeAuthError(f"Rejected API key ({resp.status})")
                        if (
                            resp.status in RETRYABLE_STATUSES
                            and attempt < self._max_retries
                        ):
                            attempt += 1
                            _LOGGER.debug(
                                "TypeSafe returned %s, retrying (%s/%s)",
                                resp.status,
                                attempt,
                                self._max_retries,
                            )
                            await asyncio.sleep(0.4 * attempt)
                            continue
                        raise TypeSafeRequestError(resp.status, body)
            except TimeoutError as err:
                if attempt < self._max_retries:
                    attempt += 1
                    continue
                raise TypeSafeConnectionError("Timed out talking to TypeSafe") from err
            except aiohttp.ClientError as err:
                raise TypeSafeConnectionError(str(err)) from err

        answers: dict[str, Answer] = {}
        for key, raw in (data.get("answers") or {}).items():
            if isinstance(raw, dict) and (answer := _parse_answer(raw)) is not None:
                answers[str(key)] = answer

        return SystemOneResponse(
            model=str(data.get("model", payload["model"])),
            answers=answers,
            usage={
                str(k): int(v)
                for k, v in (data.get("usage") or {}).items()
                if isinstance(v, (int, float))
            },
        )

    async def validate(self) -> None:
        """Make the cheapest possible request to verify the API key works."""
        await self.system_one(
            state="ping",
            questions={
                "ok": {"type": "noul", "instructions": "Is this the word 'ping'?"}
            },
        )
