"""Speculative fan-out: one TypeSafe request per command, many typed questions.

Every question is asked up front (including ones that will be irrelevant for
most commands) and code afterwards decides which answers matter. This is the
pattern TypeSafe recommends for smart-home assistants: batching is far faster
than a chain of dependent calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any

from .api import ChoiceAnswer, SystemOneResponse
from .const import (
    INTENT_OTHER,
    MAX_CHOICE_OPTIONS,
    OPTION_ALL,
    OPTION_NONE,
    Q_AREA,
    Q_BRIGHTNESS,
    Q_COLOR,
    Q_COLOR_TEMP,
    Q_COMPOUND,
    Q_DOMAIN,
    Q_ENTITY_PREFIX,
    Q_FLOOR,
    Q_INTENT,
    Q_LEVEL,
    Q_NEEDS_CONVERSATION,
    Q_PICK,
    Q_STATE_FILTER,
    Q_TARGET_TYPE,
)
from .home_state import DOMAIN_DESCRIPTIONS, ExposedEntity, HomeSnapshot
from .parsing import (
    find_duration,
    find_numbers,
    pick_percentage,
    pick_temperature,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class IntentSpec:
    """How a Home Assistant intent maps onto the question set."""

    name: str
    description: str
    needs_target: bool = True
    optional_target: bool = False
    domains: frozenset[str] | None = None
    numeric: str | None = None  # percent | position | temperature | duration | volume
    requires_timers: bool = False


INTENT_SPECS: dict[str, IntentSpec] = {
    spec.name: spec
    for spec in (
        IntentSpec(
            "HassTurnOn",
            "Turn on, switch on, activate, enable, open (a cover/blind), lock (a lock), "
            "start (a vacuum), run (a script) or activate (a scene). Any 'make it on/open/active' command.",
        ),
        IntentSpec(
            "HassTurnOff",
            "Turn off, switch off, deactivate, disable, close (a cover/blind), unlock (a lock), "
            "stop or dock (a vacuum). Any 'make it off/closed/inactive' command.",
        ),
        IntentSpec(
            "HassToggle",
            "Toggle: flip a device to the opposite of its current state.",
        ),
        IntentSpec(
            "HassLightSet",
            "Adjust a light that stays on: set brightness/dim/brighten, set a color, or set warm/cool white. "
            "Not a plain on/off.",
            domains=frozenset({"light"}),
        ),
        IntentSpec(
            "HassSetPosition",
            "Move a cover, blind, shutter, curtain or valve to a specific position or percentage "
            "(halfway, 30%, all the way up).",
            domains=frozenset({"cover", "valve"}),
            numeric="position",
        ),
        IntentSpec(
            "HassClimateSetTemperature",
            "Set the target temperature of an air conditioner, thermostat or heater to a value.",
            domains=frozenset({"climate"}),
            numeric="temperature",
            optional_target=True,
        ),
        IntentSpec(
            "HassClimateGetTemperature",
            "Ask what the current temperature is in a room or on a thermostat.",
            domains=frozenset({"climate"}),
            needs_target=False,
            optional_target=True,
        ),
        IntentSpec(
            "HassFanSetSpeed",
            "Set a fan's speed or percentage.",
            domains=frozenset({"fan"}),
            numeric="percent",
        ),
        IntentSpec(
            "HassSetVolume",
            "Set the volume of a speaker or media player to a level.",
            domains=frozenset({"media_player"}),
            numeric="volume",
        ),
        IntentSpec(
            "HassMediaPause",
            "Pause music, video or media playback.",
            domains=frozenset({"media_player"}),
            optional_target=True,
        ),
        IntentSpec(
            "HassMediaUnpause",
            "Resume or continue paused music/media playback.",
            domains=frozenset({"media_player"}),
            optional_target=True,
        ),
        IntentSpec(
            "HassMediaNext",
            "Skip to the next track, song or item.",
            domains=frozenset({"media_player"}),
            optional_target=True,
        ),
        IntentSpec(
            "HassMediaPrevious",
            "Go back to the previous track or song.",
            domains=frozenset({"media_player"}),
            optional_target=True,
        ),
        IntentSpec(
            "HassMediaPlayerMute",
            "Mute a speaker or media player.",
            domains=frozenset({"media_player"}),
            optional_target=True,
        ),
        IntentSpec(
            "HassMediaPlayerUnmute",
            "Unmute a speaker or media player.",
            domains=frozenset({"media_player"}),
            optional_target=True,
        ),
        IntentSpec(
            "HassVacuumStart",
            "Start the robot vacuum cleaning.",
            domains=frozenset({"vacuum"}),
            optional_target=True,
        ),
        IntentSpec(
            "HassVacuumReturnToBase",
            "Send the robot vacuum back to its dock / base.",
            domains=frozenset({"vacuum"}),
            optional_target=True,
        ),
        IntentSpec(
            "HassGetState",
            "A question about the current state of a device or sensor: is it on/off/open, what is the "
            "temperature/humidity reading, which lights are on, how many doors are open.",
        ),
        IntentSpec(
            "HassGetWeather",
            "Ask about the weather or forecast.",
            needs_target=False,
        ),
        IntentSpec(
            "HassGetCurrentTime",
            "Ask what time it is.",
            needs_target=False,
        ),
        IntentSpec(
            "HassGetCurrentDate",
            "Ask what today's date or day of the week is.",
            needs_target=False,
        ),
        IntentSpec(
            "HassStartTimer",
            "Set or start a timer for a duration (e.g. 'set a timer for 10 minutes').",
            needs_target=False,
            numeric="duration",
            requires_timers=True,
        ),
        IntentSpec(
            "HassCancelTimer",
            "Cancel or stop the running timer.",
            needs_target=False,
            requires_timers=True,
        ),
        IntentSpec(
            "HassCancelAllTimers",
            "Cancel all running timers.",
            needs_target=False,
            requires_timers=True,
        ),
        IntentSpec(
            "HassPauseTimer",
            "Pause the running timer.",
            needs_target=False,
            requires_timers=True,
        ),
        IntentSpec(
            "HassUnpauseTimer",
            "Resume the paused timer.",
            needs_target=False,
            requires_timers=True,
        ),
        IntentSpec(
            "HassTimerStatus",
            "Ask how much time is left on the timer.",
            needs_target=False,
            requires_timers=True,
        ),
        IntentSpec(
            "HassNevermind",
            "The user cancels: 'never mind', 'forget it', 'stop', 'cancel that'.",
            needs_target=False,
        ),
    )
}

OTHER_DESCRIPTION = (
    "Anything else: general conversation, small talk, a question needing general knowledge "
    "or reasoning, adding items to lists, sending messages, or a request not covered above."
)

TARGET_TYPE_OPTIONS: dict[str, str] = {
    "entity": "One specific, named device, scene, script or sensor",
    "area": "Everything of a kind in one room / area (e.g. 'the bedroom lights')",
    "floor": "Everything of a kind on a whole floor",
    "everything": "All devices of a kind in the whole home (e.g. 'all the lights')",
    OPTION_NONE: "No device is targeted (time, weather, timers, chit-chat)",
}

BRIGHTNESS_OPTIONS: dict[str, tuple[str, int | None]] = {
    "stated_number": ("The user gives a specific number or percentage", None),
    "maximum": ("Full brightness, maximum, brightest, all the way up", 100),
    "bright": ("Bright / brighter, but not explicitly maximum", 75),
    "medium": ("Medium, half, normal", 50),
    "dim": ("Dim, low, soft, night light, cozy", 20),
    "minimum": ("Lowest possible while still on, barely lit", 5),
    OPTION_NONE: ("Brightness is not mentioned", None),
}

COLOR_OPTIONS: dict[str, str] = {
    "red": "Red",
    "orange": "Orange",
    "yellow": "Yellow",
    "green": "Green",
    "cyan": "Cyan / turquoise / teal",
    "blue": "Blue",
    "purple": "Purple / violet",
    "magenta": "Magenta",
    "pink": "Pink",
    "white": "White (plain white, not warm or cool)",
    OPTION_NONE: "No color is mentioned",
}

COLOR_TEMP_OPTIONS: dict[str, tuple[str, int | None]] = {
    "warm": ("Warm white, cozy, candle-like, yellowish white", 2700),
    "neutral": ("Neutral white", 4000),
    "cool": ("Cool white, daylight, bluish white, bright white", 6000),
    OPTION_NONE: ("Color temperature is not mentioned", None),
}

LEVEL_OPTIONS: dict[str, str] = {
    "maximum": "Fully / all the way / maximum / 100%",
    "high": "High / most of the way / loud / fast",
    "medium": "Medium / half / halfway",
    "low": "Low / a little / quiet / slow",
    "minimum": "Minimum / lowest / all the way down but not off",
    OPTION_NONE: "A specific number is given, or no level is mentioned",
}

LEVEL_VALUES: dict[str, dict[str, int]] = {
    "position": {"maximum": 100, "high": 75, "medium": 50, "low": 25, "minimum": 0},
    "percent": {"maximum": 100, "high": 100, "medium": 66, "low": 33, "minimum": 10},
    "volume": {"maximum": 100, "high": 80, "medium": 50, "low": 20, "minimum": 5},
}

STATE_FILTER_OPTIONS: dict[str, str] = {
    "on": "Which/how many are on / active / lit",
    "off": "Which/how many are off / inactive",
    "open": "Which/how many are open (doors, windows, covers)",
    "closed": "Which/how many are closed",
    "locked": "Which are locked",
    "unlocked": "Which are unlocked",
    OPTION_NONE: "Not asking about a particular state, or asking for a value/reading",
}


@dataclass(slots=True)
class QuestionSet:
    """Questions plus the metadata needed to read the answers."""

    questions: dict[str, dict[str, Any]]
    entity_domains: list[str] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)
    has_lights: bool = False
    has_levels: bool = False


def _choice(instructions: str, criteria: dict[str, str | None]) -> dict[str, Any]:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def _noul(
    instructions: str, true: str | None = None, false: str | None = None
) -> dict[str, Any]:
    question: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if true or false:
        question["criteria"] = {"true": true, "false": false}
    return question


def build_questions(
    snapshot: HomeSnapshot,
    *,
    available_intents: set[str],
    supports_timers: bool,
) -> QuestionSet:
    """Build the speculative question set for a command."""
    domains = snapshot.domains
    domain_set = set(domains)

    intents: list[str] = []
    intent_criteria: dict[str, str | None] = {}
    for spec in INTENT_SPECS.values():
        if spec.name not in available_intents:
            continue
        if spec.requires_timers and not supports_timers:
            continue
        if spec.domains is not None and not (spec.domains & domain_set):
            continue
        intents.append(spec.name)
        intent_criteria[spec.name] = spec.description
    intent_criteria[INTENT_OTHER] = OTHER_DESCRIPTION

    questions: dict[str, dict[str, Any]] = {
        Q_INTENT: _choice(
            "What is the user asking the smart home to do?", intent_criteria
        ),
        Q_TARGET_TYPE: _choice(
            "What kind of target does the command refer to?", TARGET_TYPE_OPTIONS
        ),
        Q_DOMAIN: _choice(
            "Which kind of device does the command refer to? Pick 'none' if no device kind is "
            "implied (time, weather, timers, conversation).",
            {
                **{
                    domain: DOMAIN_DESCRIPTIONS.get(domain, domain.replace("_", " "))
                    for domain in domains
                },
                OPTION_NONE: "No particular kind of device",
            },
        ),
        Q_COMPOUND: _noul(
            "Does the command ask for more than one distinct action (e.g. two different devices "
            "or two different changes joined by 'and'/'then')?",
            true="Two or more separate actions requested",
            false="A single action, possibly with several details",
        ),
        Q_NEEDS_CONVERSATION: _noul(
            "Does a good reply require generating free-form text (an explanation, opinion, joke, "
            "recipe, general knowledge) rather than performing a smart-home action or reading a device?",
            true="Needs a conversational, generated answer",
            false="A device action or a device/sensor reading is enough",
        ),
        Q_STATE_FILTER: _choice(
            "If the user asks which or how many devices are in a certain state, which state?",
            STATE_FILTER_OPTIONS,
        ),
    }

    if snapshot.areas:
        questions[Q_AREA] = _choice(
            "Which room or area does the command name? Pick 'none' if no room is mentioned.",
            {
                **{
                    name: (f"Also called {', '.join(aliases)}" if aliases else None)
                    for name, aliases in list(snapshot.areas.items())[
                        : MAX_CHOICE_OPTIONS - 1
                    ]
                },
                OPTION_NONE: "No room or area is mentioned",
            },
        )
    if len(snapshot.floors) >= 2:
        questions[Q_FLOOR] = _choice(
            "Which floor does the command name? Pick 'none' if no floor is mentioned.",
            {
                **{
                    name: (f"Also called {', '.join(aliases)}" if aliases else None)
                    for name, aliases in list(snapshot.floors.items())[
                        : MAX_CHOICE_OPTIONS - 1
                    ]
                },
                OPTION_NONE: "No floor is mentioned",
            },
        )

    entity_domains: list[str] = []
    for domain in domains:
        entities = snapshot.entities_in_domain(domain)
        if not entities:
            continue
        if len(entities) > MAX_CHOICE_OPTIONS - 1:
            _LOGGER.warning(
                "%s exposed %s entities exceed the %s-option limit; only the first %s "
                "are offered to the model",
                len(entities),
                domain,
                MAX_CHOICE_OPTIONS,
                MAX_CHOICE_OPTIONS - 1,
            )
            entities = entities[: MAX_CHOICE_OPTIONS - 1]
        label = (
            DOMAIN_DESCRIPTIONS.get(domain, domain.replace("_", " "))
            .split(",")[0]
            .lower()
        )
        questions[f"{Q_ENTITY_PREFIX}{domain}"] = _choice(
            f"If the command refers to one specific {domain.replace('_', ' ')} ({label}), which one? "
            "Match on the name, its aliases and the room. Pick 'none' if the command does not "
            "single out one of these.",
            {
                **{entity.entity_id: entity.describe() for entity in entities},
                OPTION_NONE: "None of these is specifically referred to",
            },
        )
        entity_domains.append(domain)

    has_lights = "light" in domain_set
    if has_lights:
        questions[Q_BRIGHTNESS] = _choice(
            "If the command sets a light's brightness, to what level?",
            {key: desc for key, (desc, _) in BRIGHTNESS_OPTIONS.items()},
        )
        questions[Q_COLOR] = _choice(
            "If the command sets a light color, which color?", COLOR_OPTIONS
        )
        questions[Q_COLOR_TEMP] = _choice(
            "If the command asks for warm or cool white light, which?",
            {key: desc for key, (desc, _) in COLOR_TEMP_OPTIONS.items()},
        )

    has_levels = bool(domain_set & {"cover", "valve", "fan", "media_player"})
    if has_levels:
        questions[Q_LEVEL] = _choice(
            "If the command describes a level in words instead of a number (for a blind position, "
            "fan speed or volume), which level?",
            LEVEL_OPTIONS,
        )

    return QuestionSet(
        questions=questions,
        entity_domains=entity_domains,
        intents=intents,
        has_lights=has_lights,
        has_levels=has_levels,
    )


@dataclass(slots=True)
class Interpretation:
    """What we decided to do with a command."""

    intent: str | None
    slots: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    reason: str | None = None  # set when we cannot / should not act
    target_entity: ExposedEntity | None = None
    target_area: str | None = None
    target_floor: str | None = None
    target_domain: str | None = None
    target_all: bool = False
    values: dict[str, Any] = field(default_factory=dict)
    is_compound: bool = False
    needs_conversation: bool = False
    intent_probabilities: dict[str, float] = field(default_factory=dict)
    candidates: list[ExposedEntity] = field(default_factory=list)
    """Possible targets when the command was ambiguous (reason == ambiguous_target)."""

    @property
    def actionable(self) -> bool:
        """True when an intent should be executed."""
        return self.intent is not None and self.reason is None

    def with_target(self, entity: ExposedEntity) -> Interpretation:
        """Return a copy of this interpretation targeting one specific entity."""
        slots = {
            k: v for k, v in self.slots.items() if k not in ("name", "area", "floor")
        }
        slots["name"] = _slot(entity.entity_id, entity.name)
        slots["domain"] = _slot([entity.domain], entity.domain)
        return replace(
            self,
            slots=slots,
            reason=None,
            target_entity=entity,
            target_domain=entity.domain,
            target_all=False,
            candidates=[],
        )

    def with_all_candidates(self) -> Interpretation:
        """Return a copy targeting every candidate (all devices of the domain)."""
        slots = {
            k: v for k, v in self.slots.items() if k not in ("name", "area", "floor")
        }
        slots["name"] = _slot("all")
        domain = self.target_domain or (
            self.candidates[0].domain if self.candidates else None
        )
        if domain:
            slots["domain"] = _slot([domain], domain)
        area = self.candidates[0].area if self.candidates else None
        if area and all(e.area == area for e in self.candidates):
            slots["area"] = _slot(area)
        return replace(
            self,
            slots=slots,
            reason=None,
            target_entity=None,
            target_domain=domain,
            target_area=slots.get("area", {}).get("value"),
            target_all="area" not in slots,
            candidates=[],
        )


def _slot(value: Any, text: str | None = None) -> dict[str, Any]:
    return {"value": value, "text": text if text is not None else str(value)}


def _best_non_none(answer: ChoiceAnswer | None) -> tuple[str | None, float]:
    """Return the most probable option that isn't the 'none' sentinel."""
    if answer is None:
        return None, 0.0
    for option, prob in answer.ranked():
        if option != OPTION_NONE:
            return option, prob
    return None, 0.0


def _resolve_entity(
    response: SystemOneResponse,
    snapshot: HomeSnapshot,
    question_set: QuestionSet,
    preferred_domains: list[str],
    target_confidence: float,
) -> tuple[ExposedEntity | None, float]:
    """Find the entity the user most likely named."""
    # First pass: trust the answer for the domain(s) we believe in.
    for domain in preferred_domains:
        answer = response.choice(f"{Q_ENTITY_PREFIX}{domain}")
        if answer is None or answer.choice == OPTION_NONE:
            continue
        if answer.confidence >= target_confidence and (
            entity := snapshot.entity(answer.choice)
        ):
            return entity, answer.confidence

    # Second pass: whichever per-domain question is most sure about a non-none pick.
    # This is a guess, so don't guess an unavailable device.
    best: tuple[ExposedEntity | None, float] = (None, 0.0)
    for domain in question_set.entity_domains:
        answer = response.choice(f"{Q_ENTITY_PREFIX}{domain}")
        option, prob = _best_non_none(answer)
        if (
            option
            and prob > best[1]
            and (entity := snapshot.entity(option))
            and entity.available
        ):
            best = (entity, prob)
    if best[0] is not None and best[1] >= target_confidence:
        return best
    return None, best[1]


def _disambiguate(
    snapshot: HomeSnapshot,
    domain: str,
) -> list[ExposedEntity]:
    """Narrow "the fan" down using availability and the speaker's location."""
    pool = [e for e in snapshot.entities_in_domain(domain) if e.available]
    if not pool:
        return []
    if snapshot.device_area and any(e.area == snapshot.device_area for e in pool):
        pool = [e for e in pool if e.area == snapshot.device_area]
    return pool


def build_pick_questions(
    candidates: list[ExposedEntity],
) -> dict[str, dict[str, Any]]:
    """Question set for resolving the reply to a clarification question."""
    criteria: dict[str, str | None] = {
        entity.entity_id: entity.describe()
        for entity in candidates[: MAX_CHOICE_OPTIONS - 2]
    }
    criteria[OPTION_ALL] = "All of them / both / every one"
    criteria[OPTION_NONE] = (
        "The reply does not pick any of these: it cancels, changes the subject, "
        "or is an unrelated new command"
    )
    return {
        Q_PICK: _choice(
            "The assistant asked which device the user meant. Which option does the "
            "user's reply refer to? Match on name, alias, room, or position in the list "
            "(e.g. 'the first one', 'the second').",
            criteria,
        )
    }


def pick_state(
    question: str, reply: str, candidates: list[ExposedEntity]
) -> dict[str, Any]:
    """State payload for the clarification follow-up."""
    return {
        "assistant_question": question,
        "user_reply": reply,
        "options": [
            {
                "position": index + 1,
                "id": entity.entity_id,
                "name": entity.name,
                **({"aliases": entity.aliases} if entity.aliases else {}),
                **({"area": entity.area} if entity.area else {}),
            }
            for index, entity in enumerate(candidates)
        ],
    }


def interpret(
    text: str,
    response: SystemOneResponse,
    snapshot: HomeSnapshot,
    question_set: QuestionSet,
    *,
    intent_confidence: float,
    target_confidence: float,
) -> Interpretation:
    """Turn the answers into a Home Assistant intent and slots."""
    intent_answer = response.choice(Q_INTENT)
    result = Interpretation(intent=None)
    result.is_compound = response.noul(Q_COMPOUND) >= 0.7
    result.needs_conversation = response.noul(Q_NEEDS_CONVERSATION) >= 0.7

    if intent_answer is None:
        result.reason = "no_answer"
        return result

    result.intent_probabilities = intent_answer.probabilities
    result.confidence = intent_answer.confidence

    if intent_answer.choice == INTENT_OTHER or intent_answer.choice not in INTENT_SPECS:
        result.reason = "other"
        return result
    if intent_answer.confidence < intent_confidence:
        result.intent = intent_answer.choice
        result.reason = "low_confidence"
        return result

    intent_name = intent_answer.choice
    spec = INTENT_SPECS[intent_name]
    result.intent = intent_name
    slots: dict[str, Any] = {}
    numbers = find_numbers(text)

    # --- Target resolution -------------------------------------------------
    target_type_answer = response.choice(Q_TARGET_TYPE)
    target_type = target_type_answer.choice if target_type_answer else OPTION_NONE
    domain_answer = response.choice(Q_DOMAIN)

    def domain_candidates() -> list[str]:
        ranked = [d for d, _ in domain_answer.ranked()] if domain_answer else []
        ranked = [d for d in ranked if d != OPTION_NONE]
        if spec.domains is not None:
            ranked = [d for d in ranked if d in spec.domains] or sorted(
                spec.domains & set(snapshot.domains)
            )
        return ranked

    candidates = domain_candidates()
    chosen_domain: str | None = None
    if domain_answer and domain_answer.choice != OPTION_NONE:
        chosen_domain = domain_answer.choice
        if spec.domains is not None and chosen_domain not in spec.domains:
            chosen_domain = candidates[0] if candidates else None
    elif spec.domains is not None and len(candidates) == 1:
        chosen_domain = candidates[0]

    area_answer = response.choice(Q_AREA)
    area = (
        area_answer.choice
        if area_answer and area_answer.choice != OPTION_NONE
        else None
    )
    floor_answer = response.choice(Q_FLOOR)
    floor = (
        floor_answer.choice
        if floor_answer and floor_answer.choice != OPTION_NONE
        else None
    )

    requires_target = spec.needs_target and not spec.optional_target
    if spec.needs_target or spec.optional_target:
        entity: ExposedEntity | None = None
        entity_conf = 0.0
        if target_type == "entity" or (target_type == OPTION_NONE and requires_target):
            entity, entity_conf = _resolve_entity(
                response,
                snapshot,
                question_set,
                candidates or snapshot.domains,
                target_confidence,
            )

        if entity is not None:
            result.target_entity = entity
            result.target_domain = entity.domain
            # The intent matcher accepts an entity_id in the name slot, which
            # sidesteps duplicate names and alias resolution entirely.
            slots["name"] = _slot(entity.entity_id, entity.name)
            slots["domain"] = _slot([entity.domain], entity.domain)
            result.confidence = min(result.confidence, entity_conf)
        elif target_type in ("area", "floor", "everything") or area or floor:
            # Group target: needs a domain to be meaningful for on/off.
            group_domain = chosen_domain or (candidates[0] if candidates else None)
            if group_domain is None and intent_name in (
                "HassTurnOn",
                "HassTurnOff",
                "HassToggle",
            ):
                group_domain = "light" if "light" in snapshot.domains else None
            if target_type == "floor" and floor:
                slots["floor"] = _slot(floor)
                result.target_floor = floor
            elif area and target_type != "everything":
                slots["area"] = _slot(area)
                result.target_area = area
            elif floor:
                slots["floor"] = _slot(floor)
                result.target_floor = floor
            else:
                slots["name"] = _slot("all")
                result.target_all = True
            if group_domain:
                slots["domain"] = _slot([group_domain], group_domain)
                result.target_domain = group_domain
            elif intent_name != "HassGetState":
                result.reason = "ambiguous_domain"
            if area_answer and target_type in ("area", OPTION_NONE):
                result.confidence = min(result.confidence, area_answer.confidence)
        elif requires_target:
            # No specific device was named ("turn off the fan"). If we at least
            # know the kind of device, narrow it down in code before giving up.
            group_domain = (
                chosen_domain
                if chosen_domain
                and domain_answer is not None
                and domain_answer.confidence >= target_confidence
                else None
            )
            pool = _disambiguate(snapshot, group_domain) if group_domain else []
            everything_prob = (
                target_type_answer.probability("everything")
                if target_type_answer
                else 0.0
            )
            if len(pool) == 1:
                only = pool[0]
                result.target_entity = only
                result.target_domain = only.domain
                slots["name"] = _slot(only.entity_id, only.name)
                slots["domain"] = _slot([only.domain], only.domain)
            elif pool and (target_type == "everything" or everything_prob >= 0.35):
                slots["name"] = _slot("all")
                slots["domain"] = _slot([group_domain], group_domain)
                result.target_domain = group_domain
                result.target_all = True
            elif pool:
                result.reason = "ambiguous_target"
                result.target_domain = group_domain
                result.candidates = pool
            else:
                result.reason = "no_target"
        elif chosen_domain:
            # Optional target with only a domain (e.g. "pause the music").
            result.target_domain = chosen_domain

    if snapshot.device_area_id:
        slots["preferred_area_id"] = {"value": snapshot.device_area_id}
    if snapshot.device_floor_id:
        slots["preferred_floor_id"] = {"value": snapshot.device_floor_id}

    # --- Values ------------------------------------------------------------
    if intent_name == "HassLightSet":
        brightness_answer = response.choice(Q_BRIGHTNESS)
        brightness: int | None = None
        if brightness_answer and brightness_answer.choice != OPTION_NONE:
            if brightness_answer.choice == "stated_number":
                brightness = pick_percentage(numbers)
            else:
                brightness = BRIGHTNESS_OPTIONS[brightness_answer.choice][1]
        if brightness is None and numbers:
            brightness = pick_percentage(numbers)
        color_answer = response.choice(Q_COLOR)
        color = (
            color_answer.choice
            if color_answer
            and color_answer.choice != OPTION_NONE
            and color_answer.confidence >= 0.4
            else None
        )
        temp_answer = response.choice(Q_COLOR_TEMP)
        kelvin = (
            COLOR_TEMP_OPTIONS[temp_answer.choice][1]
            if temp_answer
            and temp_answer.choice != OPTION_NONE
            and temp_answer.confidence >= 0.4
            else None
        )
        if brightness is not None:
            slots["brightness"] = _slot(brightness, f"{brightness}%")
            result.values["brightness"] = brightness
        if color:
            slots["color"] = _slot(color)
            result.values["color"] = color
        if kelvin and not color:
            slots["temperature"] = _slot(kelvin, f"{kelvin}K")
            result.values["color_temp"] = temp_answer.choice if temp_answer else None
        if not (brightness is not None or color or kelvin):
            # Nothing to set: the user most likely just wants it on.
            result.intent = intent_name = "HassTurnOn"
            slots.pop("domain", None)
            if result.target_entity:
                slots["domain"] = _slot(
                    [result.target_entity.domain], result.target_entity.domain
                )
            elif result.target_domain:
                slots["domain"] = _slot([result.target_domain], result.target_domain)

    elif spec.numeric in ("position", "percent", "volume"):
        value = pick_percentage(numbers)
        if value is None:
            level_answer = response.choice(Q_LEVEL)
            if level_answer and level_answer.choice != OPTION_NONE:
                value = LEVEL_VALUES[spec.numeric].get(level_answer.choice)
        if value is None:
            result.reason = "missing_value"
        else:
            slot_name = {
                "position": "position",
                "percent": "percentage",
                "volume": "volume_level",
            }[spec.numeric]
            slots[slot_name] = _slot(value, f"{value}%")
            result.values[slot_name] = value

    elif spec.numeric == "temperature":
        temperature = pick_temperature(numbers)
        if temperature is None:
            result.reason = "missing_value"
        else:
            if temperature == int(temperature):
                temperature = int(temperature)
            slots["temperature"] = _slot(temperature)
            result.values["temperature"] = temperature

    elif spec.numeric == "duration":
        duration = find_duration(text)
        if duration is None or duration.total_seconds <= 0:
            result.reason = "missing_value"
        else:
            if duration.hours:
                slots["hours"] = _slot(duration.hours)
            if duration.minutes:
                slots["minutes"] = _slot(duration.minutes)
            if duration.seconds:
                slots["seconds"] = _slot(duration.seconds)
            result.values["duration"] = duration

    if intent_name == "HassGetState":
        state_answer = response.choice(Q_STATE_FILTER)
        if (
            state_answer
            and state_answer.choice != OPTION_NONE
            and state_answer.confidence >= 0.5
        ):
            slots["state"] = _slot([state_answer.choice], state_answer.choice)
            result.values["state"] = state_answer.choice
        if "name" not in slots and "area" not in slots and "floor" not in slots:
            if result.target_domain or chosen_domain:
                slots["name"] = _slot("all")
                slots["domain"] = _slot(
                    [result.target_domain or chosen_domain],
                    result.target_domain or chosen_domain,
                )
                result.target_all = True
                result.reason = None
            else:
                result.reason = "no_target"

    result.slots = slots
    return result
