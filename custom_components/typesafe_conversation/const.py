"""Constants for the TypeSafe Conversation (Jev) integration."""

from __future__ import annotations

DOMAIN = "typesafe_conversation"

DEFAULT_NAME = "Jev"
DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT = 8.0

CONF_BASE_URL = "base_url"
CONF_MODEL = "model"
CONF_FALLBACK_AGENT = "fallback_agent"
CONF_INTENT_CONFIDENCE = "intent_confidence"
CONF_TARGET_CONFIDENCE = "target_confidence"
CONF_INCLUDE_STATE = "include_state"
CONF_TIMEOUT = "timeout"

DEFAULT_INTENT_CONFIDENCE = 0.5
DEFAULT_TARGET_CONFIDENCE = 0.4
DEFAULT_INCLUDE_STATE = False

# Sentinel option used in every Choice question to let the model opt out.
OPTION_NONE = "none"
# Sentinel intent meaning "hand this to a generative agent / can't handle".
INTENT_OTHER = "other"

# Hard limit from the TypeSafe API on Choice cardinality.
MAX_CHOICE_OPTIONS = 255

# Keys of questions in the speculative fan-out request.
Q_INTENT = "intent"
Q_TARGET_TYPE = "target_type"
Q_DOMAIN = "domain"
Q_AREA = "area"
Q_FLOOR = "floor"
Q_ENTITY_PREFIX = "entity__"
Q_BRIGHTNESS = "brightness_level"
Q_COLOR = "color"
Q_COLOR_TEMP = "color_temperature"
Q_LEVEL = "level_word"
Q_COMPOUND = "is_compound"
Q_NEEDS_CONVERSATION = "needs_conversation"
Q_STATE_FILTER = "state_filter"
