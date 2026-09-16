"""Pre-parse numbers and durations out of a command.

Jev cannot extract free text or numbers (it only answers typed questions), so
the "pre-parsed value extraction" pattern from the TypeSafe cookbook applies:
we pull candidate numbers out with plain code and let the model decide *what*
they mean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_UNITS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_SPECIAL = {
    "hundred": 100,
    "half": 50,
    "quarter": 25,
    "three quarters": 75,
    "a third": 33,
    "two thirds": 66,
}

_NUMBER_WORD = "|".join(sorted([*_UNITS, *_TENS, *_SPECIAL], key=len, reverse=True))
# "twenty two", "twenty-two", "seventy five percent", "22", "22.5", "22,5"
_NUMBER_RE = re.compile(
    rf"(?P<digits>\d+(?:[.,]\d+)?)|(?P<words>(?:{_NUMBER_WORD})(?:[\s-](?:{_NUMBER_WORD}))*)",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(r"\s*(%|percent|per cent)", re.IGNORECASE)
_DEGREE_RE = re.compile(
    r"\s*(°|degrees?|deg|celsius|fahrenheit|c\b|f\b)", re.IGNORECASE
)

_DURATION_RE = re.compile(
    rf"(?P<num>\d+(?:[.,]\d+)?|(?:{_NUMBER_WORD})(?:[\s-](?:{_NUMBER_WORD}))*|an?)\s*"
    r"(?P<unit>hours?|hrs?|h\b|minutes?|mins?|m\b|seconds?|secs?|s\b)",
    re.IGNORECASE,
)


@dataclass(slots=True, frozen=True)
class ParsedNumber:
    """A number found in the command."""

    value: float
    text: str
    is_percent: bool
    is_degrees: bool
    start: int


@dataclass(slots=True, frozen=True)
class Duration:
    """A duration found in the command."""

    hours: int = 0
    minutes: int = 0
    seconds: int = 0

    @property
    def total_seconds(self) -> int:
        """Total seconds."""
        return self.hours * 3600 + self.minutes * 60 + self.seconds

    def describe(self) -> str:
        """Human readable description."""
        parts: list[str] = []
        if self.hours:
            parts.append(f"{self.hours} hour{'s' if self.hours != 1 else ''}")
        if self.minutes:
            parts.append(f"{self.minutes} minute{'s' if self.minutes != 1 else ''}")
        if self.seconds:
            parts.append(f"{self.seconds} second{'s' if self.seconds != 1 else ''}")
        return " and ".join(parts) if parts else "0 seconds"


def words_to_number(text: str) -> float | None:
    """Convert a run of English number words ("twenty two") to a number."""
    text = text.lower().strip()
    if text in _SPECIAL:
        return float(_SPECIAL[text])
    total = 0
    current = 0
    for word in re.split(r"[\s-]+", text):
        if word in _UNITS:
            current += _UNITS[word]
        elif word in _TENS:
            current += _TENS[word]
        elif word == "hundred":
            current = (current or 1) * 100
        elif word in _SPECIAL:
            current += _SPECIAL[word]
        elif word in ("and",):
            continue
        else:
            return None
    total += current
    return float(total)


def find_numbers(text: str) -> list[ParsedNumber]:
    """Return every number in the text, in order of appearance."""
    found: list[ParsedNumber] = []
    for match in _NUMBER_RE.finditer(text):
        raw = match.group(0)
        if match.group("digits"):
            value = float(raw.replace(",", "."))
        else:
            parsed = words_to_number(raw)
            if parsed is None:
                continue
            value = parsed
        tail = text[match.end() :]
        found.append(
            ParsedNumber(
                value=value,
                text=raw,
                is_percent=bool(_PERCENT_RE.match(tail)),
                is_degrees=bool(_DEGREE_RE.match(tail)),
                start=match.start(),
            )
        )
    return found


def pick_percentage(numbers: list[ParsedNumber]) -> int | None:
    """Pick the most plausible 0-100 value from parsed numbers."""
    for number in numbers:
        if number.is_percent and 0 <= number.value <= 100:
            return int(round(number.value))
    for number in numbers:
        if not number.is_degrees and 0 <= number.value <= 100:
            return int(round(number.value))
    return None


def pick_temperature(numbers: list[ParsedNumber]) -> float | None:
    """Pick the most plausible temperature setpoint."""
    for number in numbers:
        if number.is_degrees:
            return number.value
    for number in numbers:
        if not number.is_percent and 5 <= number.value <= 40:
            return number.value
    for number in numbers:
        if not number.is_percent and 40 < number.value <= 100:
            return number.value  # Fahrenheit-ish
    return None


def find_duration(text: str) -> Duration | None:
    """Return the first duration ("5 minutes and 30 seconds") in the text."""
    hours = minutes = seconds = 0
    seen = False
    for match in _DURATION_RE.finditer(text):
        num_text = match.group("num").lower()
        if num_text in ("a", "an"):
            value = 1.0
        elif num_text[0].isdigit():
            value = float(num_text.replace(",", "."))
        else:
            parsed = words_to_number(num_text)
            if parsed is None:
                continue
            value = parsed
        unit = match.group("unit").lower()
        seen = True
        if unit.startswith("h"):
            hours += int(value)
            minutes += int(round((value - int(value)) * 60))
        elif unit.startswith("m"):
            minutes += int(value)
            seconds += int(round((value - int(value)) * 60))
        else:
            seconds += int(round(value))
    if not seen:
        return None
    minutes += seconds // 60
    seconds %= 60
    hours += minutes // 60
    minutes %= 60
    return Duration(hours=hours, minutes=minutes, seconds=seconds)
