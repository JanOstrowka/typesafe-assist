# TypeSafe Conversation (Jev) for Home Assistant

A conversation agent for Home Assistant Assist that uses [TypeSafe's Jev](https://docs.typesafe.ai/introduction) to interpret commands.

Jev is a System One model: it does not generate text. It answers typed questions about a piece of state and returns choices with calibrated probabilities. This integration sends each Assist command, together with the entities exposed to Assist, to Jev in a single request containing a speculative fan-out of questions (intent, target, device, room, brightness, and so on). The answers are mapped onto Home Assistant's built-in intents (`HassTurnOn`, `HassLightSet`, `HassGetState`, ...), which do the actual device control. Spoken confirmations come from templates.

When Jev is unsure, the request is conversational, or it contains several actions, the original text is handed to an optional fallback conversation agent (for example an OpenAI, Anthropic or Ollama agent).

## Installation

### HACS (Recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=JanOstrowka&repository=typesafe-assist&category=integration)

1. Make sure [HACS](https://hacs.xyz/) is installed.
2. Click the badge above, or go to **HACS → Integrations → ⋮ → Custom repositories** and add:
   ```
   https://github.com/JanOstrowka/typesafe-assist
   ```
   with category **Integration**.
3. Download "TypeSafe Conversation (Jev)" and restart Home Assistant.

### Manual

Copy `custom_components/typesafe_conversation` into your `config/custom_components/` directory and restart Home Assistant.

## Setup

1. Settings → Devices & services → Add integration → **TypeSafe Conversation (Jev)**.
2. Enter your TypeSafe API key.
3. Open the entry's options and set a **fallback conversation agent** (recommended), plus confidence thresholds if needed.
4. Settings → Voice assistants → pick or create a pipeline and set its conversation agent to the new **Jev** entity. Keep "Prefer handling commands locally" enabled so exact sentence matches stay on-device.

Every request is visible in the Assist debug view with Jev's latency, top intent probabilities, confidence, and the slots sent to Home Assistant.

## Notes

- Only entities exposed to Assist are sent to TypeSafe.
- Spoken responses are English.
- Intents that need free text (shopping list, broadcast) are routed to the fallback agent, since Jev cannot extract free text.
- Requires Home Assistant 2025.6 or newer.
