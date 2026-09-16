"""Short spoken confirmations for executed intents.

Jev does not generate text, so the confirmation sentence has to come from
code. These mirror the tone of Home Assistant's built-in responses.
"""

from __future__ import annotations

from homeassistant.core import State
from homeassistant.helpers import intent
from homeassistant.util import dt as dt_util

from .home_state import DOMAIN_PLURALS
from .questions import Interpretation

_ON_VERBS = {
    "cover": "Opened",
    "lock": "Locked",
    "scene": "Activated",
    "script": "Ran",
    "vacuum": "Started",
    "valve": "Opened",
    "automation": "Enabled",
}
_OFF_VERBS = {
    "cover": "Closed",
    "lock": "Unlocked",
    "vacuum": "Stopped",
    "valve": "Closed",
    "automation": "Disabled",
    "script": "Stopped",
}

_STATE_WORDS = {
    "on": "on",
    "off": "off",
    "open": "open",
    "closed": "closed",
    "opening": "opening",
    "closing": "closing",
    "locked": "locked",
    "unlocked": "unlocked",
    "unavailable": "unavailable",
    "unknown": "in an unknown state",
    "home": "home",
    "not_home": "away",
    "docked": "docked",
    "cleaning": "cleaning",
    "idle": "idle",
    "playing": "playing",
    "paused": "paused",
    "cool": "cooling",
    "heat": "heating",
    "heat_cool": "in auto mode",
    "dry": "drying",
    "fan_only": "in fan-only mode",
}


def _plural(domain: str | None) -> str:
    if domain is None:
        return "devices"
    return DOMAIN_PLURALS.get(domain, domain.replace("_", " ") + "s")


def describe_target(result: Interpretation) -> str:
    """Return 'the bedroom lights', 'Kitchen lamp', 'all lights', ..."""
    if result.target_entity is not None:
        return result.target_entity.name
    plural = _plural(result.target_domain)
    if result.target_area:
        return f"the {result.target_area} {plural}"
    if result.target_floor:
        return f"the {plural} on the {result.target_floor} floor"
    if result.target_all:
        return f"all {plural}"
    if result.target_domain:
        return f"the {plural}"
    return "that"


def _state_word(state: State) -> str:
    unit = state.attributes.get("unit_of_measurement")
    if unit:
        value = state.state
        try:
            number = float(value)
            value = f"{number:g}"
        except ValueError:
            pass
        return f"{value}{'' if unit in ('°C', '°F', '%') else ' '}{unit}"
    return _STATE_WORDS.get(state.state, state.state.replace("_", " "))


def _list_names(states: list[State], limit: int = 6) -> str:
    names = [s.name for s in states[:limit]]
    if (extra := len(states) - limit) > 0:
        names.append(f"{extra} more")
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def speech_for(result: Interpretation, response: intent.IntentResponse) -> str:
    """Build a confirmation sentence for a successful intent."""
    if response.speech and (plain := response.speech.get("plain", {}).get("speech")):
        return plain

    name = result.intent
    target = describe_target(result)
    domain = (
        result.target_entity.domain if result.target_entity else result.target_domain
    )

    if name == "HassTurnOn":
        return f"{_ON_VERBS.get(domain or '', 'Turned on')} {target}."
    if name == "HassTurnOff":
        return f"{_OFF_VERBS.get(domain or '', 'Turned off')} {target}."
    if name == "HassToggle":
        return f"Toggled {target}."
    if name == "HassLightSet":
        parts: list[str] = []
        if (brightness := result.values.get("brightness")) is not None:
            parts.append(f"brightness to {brightness}%")
        if color := result.values.get("color"):
            parts.append(f"color to {color}")
        if temp := result.values.get("color_temp"):
            parts.append(f"{temp} white")
        return (
            f"Set {target} {' and '.join(parts)}." if parts else f"Adjusted {target}."
        )
    if name == "HassSetPosition":
        return f"Set {target} to {result.values.get('position')}%."
    if name == "HassFanSetSpeed":
        return f"Set {target} to {result.values.get('percentage')}%."
    if name == "HassSetVolume":
        return f"Set {target} volume to {result.values.get('volume_level')}%."
    if name == "HassClimateSetTemperature":
        return f"Set {target} to {result.values.get('temperature')} degrees."
    if name == "HassClimateGetTemperature":
        if response.matched_states:
            state = response.matched_states[0]
            current = state.attributes.get("current_temperature")
            if current is not None:
                where = state.name if result.target_entity else target
                return f"The temperature at {where} is {current:g} degrees."
        return "I couldn't read the temperature."
    if name == "HassGetWeather":
        if response.matched_states:
            state = response.matched_states[0]
            temp = state.attributes.get("temperature")
            condition = state.state.replace("-", " ").replace("_", " ")
            if temp is not None:
                return f"It's {condition} and {temp:g} degrees."
            return f"It's {condition}."
        return "I couldn't get the weather."
    if name == "HassGetCurrentTime":
        return f"It's {dt_util.now().strftime('%-I:%M %p')}."
    if name == "HassGetCurrentDate":
        return f"Today is {dt_util.now().strftime('%A, %B %-d')}."
    if name == "HassStartTimer":
        if duration := result.values.get("duration"):
            return f"Timer started for {duration.describe()}."
        return "Timer started."
    if name == "HassCancelTimer":
        return "Timer cancelled."
    if name == "HassCancelAllTimers":
        return "All timers cancelled."
    if name == "HassPauseTimer":
        return "Timer paused."
    if name == "HassUnpauseTimer":
        return "Timer resumed."
    if name == "HassTimerStatus":
        timers = response.speech_slots.get("timers") if response.speech_slots else None
        if timers:
            first = timers[0]
            remaining = []
            if first.get("rounded_hours_left"):
                remaining.append(f"{first['rounded_hours_left']} hours")
            if first.get("rounded_minutes_left"):
                remaining.append(f"{first['rounded_minutes_left']} minutes")
            if first.get("rounded_seconds_left") and not remaining:
                remaining.append(f"{first['rounded_seconds_left']} seconds")
            if remaining:
                return f"{' and '.join(remaining)} left."
        return "There are no timers running."
    if name in ("HassMediaPause",):
        return f"Paused {target}."
    if name == "HassMediaUnpause":
        return f"Resumed {target}."
    if name == "HassMediaNext":
        return "Skipped to the next track."
    if name == "HassMediaPrevious":
        return "Went back to the previous track."
    if name == "HassMediaPlayerMute":
        return f"Muted {target}."
    if name == "HassMediaPlayerUnmute":
        return f"Unmuted {target}."
    if name == "HassVacuumStart":
        return f"Started {target}."
    if name == "HassVacuumReturnToBase":
        return f"Sent {target} back to the dock."
    if name == "HassNevermind":
        return ""
    if name == "HassGetState":
        return _speech_for_state(result, response)
    return "Done."


def _speech_for_state(result: Interpretation, response: intent.IntentResponse) -> str:
    matched = list(response.matched_states)
    unmatched = list(response.unmatched_states)
    wanted = result.values.get("state")
    plural = _plural(result.target_domain)

    if wanted:
        total = len(matched) + len(unmatched)
        state_word = _STATE_WORDS.get(wanted, wanted)
        if result.target_entity is not None:
            entity_is = "Yes" if matched else "No"
            return f"{entity_is}, {result.target_entity.name} is {'' if matched else 'not '}{state_word}."
        if not matched:
            return f"None of the {plural} are {state_word}."
        if len(matched) == total and total > 1:
            return f"All {total} {plural} are {state_word}."
        return f"{_list_names(matched)} {'is' if len(matched) == 1 else 'are'} {state_word}."

    if not matched:
        return "I couldn't find anything matching that."
    if len(matched) == 1:
        state = matched[0]
        return f"{state.name} is {_state_word(state)}."
    lines = [f"{state.name} is {_state_word(state)}" for state in matched[:5]]
    tail = f", and {len(matched) - 5} more" if len(matched) > 5 else ""
    return "; ".join(lines) + tail + "."


ERROR_SPEECH = {
    "no_target": "Sorry, I couldn't tell which device you meant.",
    "ambiguous_domain": "Sorry, which kind of device did you mean?",
    "missing_value": "Sorry, I didn't catch the value to set.",
    "low_confidence": "Sorry, I'm not sure what you meant.",
    "other": "Sorry, I can't help with that.",
    "no_answer": "Sorry, I couldn't process that.",
    "compound": "Sorry, please ask for one thing at a time.",
    "conversation": "Sorry, I can only control the home.",
    "api_error": "Sorry, I couldn't reach the assistant service.",
}
