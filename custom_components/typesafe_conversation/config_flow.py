"""Config and options flow for TypeSafe Conversation (Jev)."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_API_KEY, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    ConversationAgentSelector,
    ConversationAgentSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    TypeSafeAuthError,
    TypeSafeClient,
    TypeSafeConnectionError,
    TypeSafeRequestError,
)
from .const import (
    CONF_FALLBACK_AGENT,
    CONF_INCLUDE_STATE,
    CONF_INTENT_CONFIDENCE,
    CONF_MODEL,
    CONF_TARGET_CONFIDENCE,
    CONF_TIMEOUT,
    DEFAULT_INCLUDE_STATE,
    DEFAULT_INTENT_CONFIDENCE,
    DEFAULT_MODEL,
    DEFAULT_NAME,
    DEFAULT_TARGET_CONFIDENCE,
    DEFAULT_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): TextSelector(),
    }
)
STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


class TypeSafeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    async def _async_validate(self, api_key: str) -> dict[str, str]:
        errors: dict[str, str] = {}
        client = TypeSafeClient(async_get_clientsession(self.hass), api_key)
        try:
            await client.validate()
        except TypeSafeAuthError:
            errors["base"] = "invalid_auth"
        except TypeSafeConnectionError:
            errors["base"] = "cannot_connect"
        except TypeSafeRequestError as err:
            _LOGGER.warning("TypeSafe rejected the validation request: %s", err)
            errors["base"] = "unknown"
        except Exception:
            _LOGGER.exception("Unexpected error validating TypeSafe API key")
            errors["base"] = "unknown"
        return errors

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the API key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            await self.async_set_unique_id(_key_fingerprint(api_key))
            self._abort_if_unique_id_configured()
            errors = await self._async_validate(api_key)
            if not errors:
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME) or DEFAULT_NAME,
                    data={CONF_API_KEY: api_key},
                    options={
                        CONF_MODEL: DEFAULT_MODEL,
                        CONF_INTENT_CONFIDENCE: DEFAULT_INTENT_CONFIDENCE,
                        CONF_TARGET_CONFIDENCE: DEFAULT_TARGET_CONFIDENCE,
                        CONF_INCLUDE_STATE: DEFAULT_INCLUDE_STATE,
                        CONF_TIMEOUT: DEFAULT_TIMEOUT,
                    },
                )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new API key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            errors = await self._async_validate(api_key)
            if not errors:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={CONF_API_KEY: api_key},
                    unique_id=_key_fingerprint(api_key),
                )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=STEP_REAUTH_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return TypeSafeOptionsFlow()


class TypeSafeOptionsFlow(OptionsFlow):
    """Tune the agent."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show/handle the options form."""
        if user_input is not None:
            if not user_input.get(CONF_FALLBACK_AGENT):
                user_input.pop(CONF_FALLBACK_AGENT, None)
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_MODEL, default=options.get(CONF_MODEL, DEFAULT_MODEL)
                ): TextSelector(),
                vol.Optional(
                    CONF_FALLBACK_AGENT,
                    description={"suggested_value": options.get(CONF_FALLBACK_AGENT)},
                ): ConversationAgentSelector(ConversationAgentSelectorConfig()),
                vol.Optional(
                    CONF_INTENT_CONFIDENCE,
                    default=options.get(
                        CONF_INTENT_CONFIDENCE, DEFAULT_INTENT_CONFIDENCE
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=1, step=0.05, mode=NumberSelectorMode.SLIDER
                    )
                ),
                vol.Optional(
                    CONF_TARGET_CONFIDENCE,
                    default=options.get(
                        CONF_TARGET_CONFIDENCE, DEFAULT_TARGET_CONFIDENCE
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=1, step=0.05, mode=NumberSelectorMode.SLIDER
                    )
                ),
                vol.Optional(
                    CONF_INCLUDE_STATE,
                    default=options.get(CONF_INCLUDE_STATE, DEFAULT_INCLUDE_STATE),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_TIMEOUT, default=options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=60,
                        step=0.5,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


def _key_fingerprint(api_key: str) -> str:
    """Stable, non-reversible id for an API key."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]
