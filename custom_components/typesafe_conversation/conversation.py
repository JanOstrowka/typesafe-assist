"""Conversation agent backed by TypeSafe's Jev (System One) model.

Flow for every Assist command:

1. Snapshot the entities exposed to Assist plus areas/floors.
2. Send the command and that snapshot to Jev as *one* request containing a
   speculative fan-out of typed questions (intent, target, device, values...).
3. Read the answers, map them onto a Home Assistant intent and slots, and run
   the normal intent handler so the same code path as the built-in agent does
   the actual device control.
4. If Jev is unsure, the request is conversational, or it bundles several
   actions, hand the *original* text to an optional fallback agent (e.g. your
   Claude/OpenAI agent) so nothing is lost.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from homeassistant.components import conversation
from homeassistant.components.intent.timers import async_device_supports_timers
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import intent, llm
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import SystemOneResponse, TypeSafeClient, TypeSafeError
from .const import (
    CLARIFICATION_TTL_SECONDS,
    CONF_FALLBACK_AGENT,
    CONF_INCLUDE_STATE,
    CONF_INTENT_CONFIDENCE,
    CONF_TARGET_CONFIDENCE,
    DEFAULT_INCLUDE_STATE,
    DEFAULT_INTENT_CONFIDENCE,
    DEFAULT_TARGET_CONFIDENCE,
    DOMAIN,
    OPTION_ALL,
    OPTION_NONE,
    Q_PICK,
)
from .home_state import ASSISTANT, async_snapshot_home
from .questions import (
    Interpretation,
    build_pick_questions,
    build_questions,
    interpret,
    pick_state,
)
from .speech import (
    ERROR_SPEECH,
    clarification_question,
    describe_target,
    speech_for,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PendingClarification:
    """A command waiting for the user to say which device they meant."""

    interpretation: Interpretation
    question: str
    asked_at: float


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the conversation entity."""
    async_add_entities([TypeSafeConversationEntity(config_entry)])


class TypeSafeConversationEntity(
    conversation.ConversationEntity, conversation.AbstractConversationAgent
):
    """Assist agent that interprets commands with Jev."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = conversation.ConversationEntityFeature.CONTROL

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize."""
        self.entry = entry
        self._pending: dict[str, PendingClarification] = {}
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="TypeSafe AI",
            model="Jev (System One)",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    def _client(self) -> TypeSafeClient:
        return self.entry.runtime_data

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Jev is language agnostic; spoken confirmations are English."""
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        """Register as agent."""
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self.entry, self)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister as agent."""
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    # ------------------------------------------------------------------
    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Interpret and execute one command."""
        options = self.entry.options
        language = user_input.language or self.hass.config.language
        fallback_agent: str | None = options.get(CONF_FALLBACK_AGENT) or None
        if fallback_agent == self.entity_id:
            fallback_agent = None
        target_confidence = options.get(
            CONF_TARGET_CONFIDENCE, DEFAULT_TARGET_CONFIDENCE
        )

        # Is this the answer to a "which one?" question we asked a moment ago?
        if (pending := self._pop_pending(chat_log.conversation_id)) is not None:
            resolved = await self._async_resolve_clarification(
                user_input, pending, target_confidence
            )
            if resolved is not None:
                return await self._async_execute(
                    user_input, chat_log, resolved, language
                )
            # Not an answer to our question: treat it as a fresh command.

        snapshot = async_snapshot_home(
            self.hass,
            include_state=options.get(CONF_INCLUDE_STATE, DEFAULT_INCLUDE_STATE),
            device_id=user_input.device_id,
            satellite_id=user_input.satellite_id,
        )
        available_intents = {h.intent_type for h in intent.async_get(self.hass)}
        supports_timers = bool(
            user_input.device_id
            and async_device_supports_timers(self.hass, user_input.device_id)
        )
        question_set = build_questions(
            snapshot,
            available_intents=available_intents,
            supports_timers=supports_timers,
        )

        started = time.monotonic()
        try:
            response = await self._client.system_one(
                snapshot.as_state(
                    user_input.text,
                    language,
                    options.get(CONF_INCLUDE_STATE, DEFAULT_INCLUDE_STATE),
                ),
                question_set.questions,
            )
        except TypeSafeError as err:
            _LOGGER.warning("TypeSafe request failed: %s", err)
            if fallback_agent:
                return await self._async_fallback(
                    user_input, fallback_agent, "api_error"
                )
            return self._error_result(user_input, chat_log, "api_error", language)
        elapsed = time.monotonic() - started

        interpretation = interpret(
            user_input.text,
            response,
            snapshot,
            question_set,
            intent_confidence=options.get(
                CONF_INTENT_CONFIDENCE, DEFAULT_INTENT_CONFIDENCE
            ),
            target_confidence=target_confidence,
        )
        self._trace(response, interpretation, elapsed, len(question_set.questions))
        _LOGGER.debug(
            "Jev (%.0f ms, %s questions): intent=%s reason=%s confidence=%.2f slots=%s",
            elapsed * 1000,
            len(question_set.questions),
            interpretation.intent,
            interpretation.reason,
            interpretation.confidence,
            {k: v.get("value") for k, v in interpretation.slots.items()},
        )

        if interpretation.is_compound and fallback_agent:
            return await self._async_fallback(user_input, fallback_agent, "compound")
        if interpretation.reason == "ambiguous_target" and interpretation.candidates:
            return self._ask_clarification(
                user_input, chat_log, interpretation, language
            )
        if not interpretation.actionable:
            reason = interpretation.reason or "no_answer"
            if fallback_agent:
                return await self._async_fallback(user_input, fallback_agent, reason)
            if interpretation.is_compound:
                reason = "compound"
            return self._error_result(user_input, chat_log, reason, language)

        return await self._async_execute(user_input, chat_log, interpretation, language)

    # ------------------------------------------------------------------
    # Clarification ("Which fan: BlueAir Fan or fan socket?")
    # ------------------------------------------------------------------
    def _pop_pending(self, conversation_id: str | None) -> PendingClarification | None:
        """Return and clear the pending question for a conversation, if still fresh."""
        now = time.monotonic()
        for key in [
            k
            for k, p in self._pending.items()
            if now - p.asked_at > CLARIFICATION_TTL_SECONDS
        ]:
            self._pending.pop(key, None)
        if conversation_id is None:
            return None
        return self._pending.pop(conversation_id, None)

    def _ask_clarification(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
        interpretation: Interpretation,
        language: str,
    ) -> conversation.ConversationResult:
        """Ask which candidate the user meant and remember the command."""
        question = clarification_question(interpretation)
        self._pending[chat_log.conversation_id] = PendingClarification(
            interpretation=interpretation, question=question, asked_at=time.monotonic()
        )
        conversation.async_conversation_trace_append(
            conversation.ConversationTraceEventType.AGENT_DETAIL,
            {
                "typesafe_clarification": {
                    "intent": interpretation.intent,
                    "candidates": [e.entity_id for e in interpretation.candidates],
                    "question": question,
                }
            },
        )
        _LOGGER.debug("Asking for clarification: %s", question)
        intent_response = intent.IntentResponse(language=language)
        intent_response.response_type = intent.IntentResponseType.QUERY_ANSWER
        intent_response.async_set_speech(question)
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(
                agent_id=user_input.agent_id, content=question
            )
        )
        return conversation.ConversationResult(
            response=intent_response,
            conversation_id=chat_log.conversation_id,
            continue_conversation=True,
        )

    async def _async_resolve_clarification(
        self,
        user_input: conversation.ConversationInput,
        pending: PendingClarification,
        target_confidence: float,
    ) -> Interpretation | None:
        """Map the user's reply onto one of the candidates.

        Returns None when the reply is not an answer to our question, in which
        case the caller processes it as a brand-new command.
        """
        candidates = pending.interpretation.candidates
        started = time.monotonic()
        try:
            response = await self._client.system_one(
                pick_state(pending.question, user_input.text, candidates),
                build_pick_questions(candidates),
            )
        except TypeSafeError as err:
            _LOGGER.warning("TypeSafe clarification request failed: %s", err)
            return None
        answer = response.choice(Q_PICK)
        elapsed = time.monotonic() - started
        _LOGGER.debug(
            "Jev clarification (%.0f ms): pick=%s confidence=%.2f for %r",
            elapsed * 1000,
            answer.choice if answer else None,
            answer.confidence if answer else 0.0,
            user_input.text,
        )
        conversation.async_conversation_trace_append(
            conversation.ConversationTraceEventType.AGENT_DETAIL,
            {
                "typesafe_clarification_reply": {
                    "latency_ms": round(elapsed * 1000),
                    "pick": answer.choice if answer else None,
                    "confidence": round(answer.confidence, 3) if answer else None,
                    "probabilities": (
                        {k: round(v, 3) for k, v in answer.ranked()[:5]}
                        if answer
                        else {}
                    ),
                }
            },
        )
        if answer is None or answer.confidence < target_confidence:
            return None
        if answer.choice == OPTION_ALL:
            return pending.interpretation.with_all_candidates()
        if answer.choice == OPTION_NONE:
            return None
        for entity in candidates:
            if entity.entity_id == answer.choice:
                return pending.interpretation.with_target(entity)
        return None

    # ------------------------------------------------------------------
    async def _async_execute(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
        interpretation: Interpretation,
        language: str,
    ) -> conversation.ConversationResult:
        """Run the Home Assistant intent handler."""
        assert interpretation.intent is not None
        tool_input = llm.ToolInput(
            tool_name=interpretation.intent,
            tool_args={k: v.get("value") for k, v in interpretation.slots.items()},
            external=True,
        )
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(
                agent_id=user_input.agent_id, content=None, tool_calls=[tool_input]
            )
        )

        try:
            intent_response = await intent.async_handle(
                self.hass,
                DOMAIN,
                interpretation.intent,
                interpretation.slots,
                user_input.text,
                user_input.context,
                language,
                assistant=ASSISTANT,
                device_id=user_input.device_id,
                satellite_id=user_input.satellite_id,
                conversation_agent_id=user_input.agent_id,
            )
        except intent.MatchFailedError as err:
            intent_response = intent.IntentResponse(language=language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.NO_VALID_TARGETS,
                self._match_failed_speech(err, interpretation),
            )
        except intent.InvalidSlotInfo as err:
            _LOGGER.warning("Invalid slots for %s: %s", interpretation.intent, err)
            intent_response = intent.IntentResponse(language=language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.FAILED_TO_HANDLE,
                ERROR_SPEECH["missing_value"],
            )
        except intent.IntentHandleError as err:
            _LOGGER.warning("Intent %s failed: %s", interpretation.intent, err)
            intent_response = intent.IntentResponse(language=language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.FAILED_TO_HANDLE,
                f"Sorry, I couldn't do that: {err}",
            )
        except intent.IntentError:
            _LOGGER.exception("Unexpected error handling %s", interpretation.intent)
            intent_response = intent.IntentResponse(language=language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "Sorry, something went wrong.",
            )

        if (
            intent_response.response_type != intent.IntentResponseType.ERROR
            and not intent_response.speech
        ):
            intent_response.async_set_speech(
                speech_for(interpretation, intent_response)
            )

        chat_log.async_add_assistant_content_without_tools(
            conversation.ToolResultContent(
                agent_id=user_input.agent_id,
                tool_call_id=tool_input.id,
                tool_name=tool_input.tool_name,
                tool_result=llm.IntentResponseDict(intent_response),
            )
        )
        speech = intent_response.speech.get("plain", {}).get("speech", "")
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=user_input.agent_id, content=speech)
        )
        return conversation.ConversationResult(
            response=intent_response, conversation_id=chat_log.conversation_id
        )

    async def _async_fallback(
        self,
        user_input: conversation.ConversationInput,
        agent_id: str,
        reason: str,
    ) -> conversation.ConversationResult:
        """Hand the untouched command to another conversation agent."""
        _LOGGER.debug("Handing off to %s (%s): %s", agent_id, reason, user_input.text)
        conversation.async_conversation_trace_append(
            conversation.ConversationTraceEventType.AGENT_DETAIL,
            {"typesafe_fallback": {"agent_id": agent_id, "reason": reason}},
        )
        return await conversation.async_converse(
            self.hass,
            text=user_input.text,
            conversation_id=user_input.conversation_id,
            context=user_input.context,
            language=user_input.language,
            agent_id=agent_id,
            device_id=user_input.device_id,
            satellite_id=user_input.satellite_id,
            extra_system_prompt=user_input.extra_system_prompt,
        )

    def _error_result(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
        reason: str,
        language: str,
    ) -> conversation.ConversationResult:
        message = ERROR_SPEECH.get(reason, ERROR_SPEECH["no_answer"])
        code = {
            "api_error": intent.IntentResponseErrorCode.UNKNOWN,
            "no_target": intent.IntentResponseErrorCode.NO_VALID_TARGETS,
            "ambiguous_domain": intent.IntentResponseErrorCode.NO_VALID_TARGETS,
        }.get(reason, intent.IntentResponseErrorCode.NO_INTENT_MATCH)
        intent_response = intent.IntentResponse(language=language)
        intent_response.async_set_error(code, message)
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=user_input.agent_id, content=message)
        )
        return conversation.ConversationResult(
            response=intent_response, conversation_id=chat_log.conversation_id
        )

    @staticmethod
    def _match_failed_speech(
        err: intent.MatchFailedError, interpretation: Interpretation
    ) -> str:
        target = describe_target(interpretation)
        reason = err.result.no_match_reason
        if reason is intent.MatchFailedReason.NAME:
            return f"Sorry, I couldn't find a device called {target}."
        if reason is intent.MatchFailedReason.AREA:
            return f"Sorry, I couldn't find {target}."
        if reason is intent.MatchFailedReason.DUPLICATE_NAME:
            return f"Sorry, there is more than one {target}. Which room?"
        if reason is intent.MatchFailedReason.STATE:
            return f"{target} is already like that."
        if reason is intent.MatchFailedReason.FEATURE:
            return f"Sorry, {target} can't do that."
        if reason is intent.MatchFailedReason.ASSISTANT:
            return f"Sorry, {target} isn't exposed to Assist."
        return f"Sorry, I couldn't find any matching {target}."

    @staticmethod
    def _trace(
        response: SystemOneResponse,
        interpretation: Interpretation,
        elapsed: float,
        question_count: int,
    ) -> None:
        """Expose the raw judgement in the Assist debug trace."""
        detail: dict[str, Any] = {
            "model": response.model,
            "latency_ms": round(elapsed * 1000),
            "questions": question_count,
            "usage": response.usage,
            "intent": interpretation.intent,
            "reason": interpretation.reason,
            "confidence": round(interpretation.confidence, 3),
            "is_compound": interpretation.is_compound,
            "needs_conversation": interpretation.needs_conversation,
            "intent_probabilities": {
                k: round(v, 3)
                for k, v in sorted(
                    interpretation.intent_probabilities.items(), key=lambda kv: -kv[1]
                )[:5]
            },
            "slots": {k: v.get("value") for k, v in interpretation.slots.items()},
        }
        conversation.async_conversation_trace_append(
            conversation.ConversationTraceEventType.AGENT_DETAIL, {"typesafe": detail}
        )
