"""Config flow for Emporia Vue integration."""

import asyncio
from collections.abc import Mapping
from functools import partial
import logging
from typing import Any

from pyemvue import PyEmVue
import voluptuous as vol

from homeassistant import config_entries, exceptions
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
import homeassistant.helpers.config_validation as cv

from .const import (
    AUTH_METHOD,
    AUTH_METHOD_EMAIL_PASSWORD,
    AUTH_METHOD_SCHEMA,
    AUTH_METHOD_TOKENS,
    CONF_ACCESS_TOKEN,
    CONF_ID_TOKEN,
    CONF_MONITOR_GIDS,
    CONF_REFRESH_TOKEN,
    CONFIG_FLOW_SCHEMA,
    CONFIG_TITLE,
    CUSTOMER_GID,
    DOMAIN,
    ENABLE_1D,
    ENABLE_1M,
    ENABLE_1MON,
    SOLAR_INVERT,
    TOKEN_CONFIG_FLOW_SCHEMA,
)
from .hierarchy import merge_devices, monitor_options

_LOGGER: logging.Logger = logging.getLogger(__name__)
SENSITIVE_CONFIG_KEYS = {
    CONF_PASSWORD,
    CONF_ID_TOKEN,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
}


def redact_config_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return config data with sensitive auth values hidden for logging."""
    return {
        key: "***" if key in SENSITIVE_CONFIG_KEYS else value
        for key, value in data.items()
    }


class VueHub:
    """Hub for the Emporia Vue Integration."""

    def __init__(self) -> None:
        """Initialize."""
        self.vue = PyEmVue()

    async def authenticate(self, data: dict | Mapping[str, Any]) -> bool:
        """Test if we can authenticate with the host."""
        loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
        auth_method = data.get(AUTH_METHOD, AUTH_METHOD_EMAIL_PASSWORD)
        if auth_method == AUTH_METHOD_TOKENS:
            return await loop.run_in_executor(
                None,
                partial(
                    self.vue.login,
                    id_token=data[CONF_ID_TOKEN],
                    access_token=data[CONF_ACCESS_TOKEN],
                    refresh_token=data[CONF_REFRESH_TOKEN],
                ),
            )

        username = data[CONF_EMAIL]
        password = data[CONF_PASSWORD]
        # support using the simulator by looking at the username
        # if formatted like vue_simulator@localhost:8000 then use the simulator
        if username.startswith("vue_simulator@"):
            host = username.split("@")[1]
            return await loop.run_in_executor(None, self.vue.login_simulator, host)
        return await loop.run_in_executor(
            None,
            partial(self.vue.login, username=username, password=password),
        )


async def validate_input(
    data: dict | Mapping[str, Any], hub: VueHub | None = None
) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from DATA_SCHEMA with values provided by the user.
    """
    hub = hub or VueHub()
    if not await hub.authenticate(data):
        raise InvalidAuth

    # If you cannot connect:
    # throw CannotConnect
    # If the authentication is wrong:
    # InvalidAuth

    if not hub.vue.customer:
        raise InvalidAuth

    new_data = dict(data)

    if SOLAR_INVERT not in new_data:
        new_data[SOLAR_INVERT] = True
    if AUTH_METHOD not in new_data:
        new_data[AUTH_METHOD] = AUTH_METHOD_EMAIL_PASSWORD

    # Return info that you want to store in the config entry.
    entry_data = {
        CONFIG_TITLE: f"{hub.vue.customer.email} ({hub.vue.customer.customer_gid})",
        CUSTOMER_GID: f"{hub.vue.customer.customer_gid}",
        ENABLE_1M: new_data[ENABLE_1M],
        ENABLE_1D: new_data[ENABLE_1D],
        ENABLE_1MON: new_data[ENABLE_1MON],
        SOLAR_INVERT: new_data[SOLAR_INVERT],
        AUTH_METHOD: new_data[AUTH_METHOD],
    }
    if new_data[AUTH_METHOD] == AUTH_METHOD_TOKENS:
        entry_data.update(
            {
                CONF_ID_TOKEN: new_data[CONF_ID_TOKEN],
                CONF_ACCESS_TOKEN: new_data[CONF_ACCESS_TOKEN],
                CONF_REFRESH_TOKEN: new_data[CONF_REFRESH_TOKEN],
            }
        )
    else:
        entry_data.update(
            {
                CONF_EMAIL: new_data[CONF_EMAIL],
                CONF_PASSWORD: new_data[CONF_PASSWORD],
            }
        )

    return entry_data


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Emporia Vue."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self) -> None:
        """Initialize pending account details."""
        super().__init__()
        self._pending_entry_data: dict[str, Any] | None = None
        self._pending_monitor_options: dict[str, str] = {}

    async def _authenticate_and_get_monitors(
        self, user_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Authenticate once and retain monitor choices for the next step."""
        hub = VueHub()
        info = await validate_input(user_input, hub)
        devices = await asyncio.get_running_loop().run_in_executor(
            None, hub.vue.get_devices
        )
        self._pending_entry_data = info
        self._pending_monitor_options = monitor_options(merge_devices(devices))
        return info

    async def async_step_select_monitors(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Select the top-level monitor trees to import."""
        if self._pending_entry_data is None:
            return self.async_abort(reason="unknown")
        if user_input is not None:
            data = dict(self._pending_entry_data)
            data[CONF_MONITOR_GIDS] = list(user_input[CONF_MONITOR_GIDS])
            return self.async_create_entry(title=data[CONFIG_TITLE], data=data)
        return self.async_show_form(
            step_id="select_monitors",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MONITOR_GIDS,
                        default=list(self._pending_monitor_options),
                    ): vol.All(
                        cv.multi_select(self._pending_monitor_options),
                        vol.Length(min=1),
                    )
                }
            ),
        )

    async def async_step_user(self, user_input=None) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        if user_input is not None:
            if CONF_EMAIL in user_input and CONF_PASSWORD in user_input:
                return await self.async_step_email_password(user_input)
            if user_input[AUTH_METHOD] == AUTH_METHOD_TOKENS:
                return await self.async_step_tokens()
            return await self.async_step_email_password()

        return self.async_show_form(
            step_id="user", data_schema=AUTH_METHOD_SCHEMA, errors={}
        )

    async def async_step_email_password(
        self, user_input=None
    ) -> config_entries.ConfigFlowResult:
        """Handle email and password authentication."""
        errors = {}
        if user_input is not None:
            try:
                user_input[AUTH_METHOD] = AUTH_METHOD_EMAIL_PASSWORD
                info = await self._authenticate_and_get_monitors(user_input)
                # prevent setting up the same account twice
                await self.async_set_unique_id(info[CUSTOMER_GID])
                self._abort_if_unique_id_configured()

                if self.context.get("source") == config_entries.SOURCE_IMPORT:
                    return self.async_create_entry(
                        title=info[CONFIG_TITLE], data=info
                    )
                return await self.async_step_select_monitors()
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="email_password", data_schema=CONFIG_FLOW_SCHEMA, errors=errors
        )

    async def async_step_tokens(
        self, user_input=None
    ) -> config_entries.ConfigFlowResult:
        """Handle token authentication for Google/SSO accounts."""
        errors = {}
        if user_input is not None:
            try:
                user_input[AUTH_METHOD] = AUTH_METHOD_TOKENS
                info = await self._authenticate_and_get_monitors(user_input)
                # prevent setting up the same account twice
                await self.async_set_unique_id(info[CUSTOMER_GID])
                self._abort_if_unique_id_configured()

                return await self.async_step_select_monitors()
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="tokens", data_schema=TOKEN_CONFIG_FLOW_SCHEMA, errors=errors
        )

    async def async_step_import(
        self, import_data: Mapping[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Import YAML configuration."""
        return await self.async_step_email_password(dict(import_data))

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the reconfiguration step."""
        current_config = self._get_reconfigure_entry()
        if not self._pending_monitor_options:
            try:
                hub = VueHub()
                if not await hub.authenticate(current_config.data):
                    raise InvalidAuth
                devices = await asyncio.get_running_loop().run_in_executor(
                    None, hub.vue.get_devices
                )
                self._pending_monitor_options = monitor_options(merge_devices(devices))
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unable to retrieve monitors during reconfiguration")
                return self.async_abort(reason="cannot_connect")
        if user_input is not None:
            _LOGGER.debug("User input on reconfigure was the following: %s", user_input)
            _LOGGER.debug(
                "Current config is: %s",
                redact_config_data(current_config.data),
            )
            info = current_config.data
            # if gid is not in current config, reauth and get gid again
            if (
                CUSTOMER_GID not in current_config.data
                or not current_config.data[CUSTOMER_GID]
            ):
                info = await validate_input(current_config.data)

            await self.async_set_unique_id(info[CUSTOMER_GID])
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            data = {
                ENABLE_1M: user_input[ENABLE_1M],
                ENABLE_1D: user_input[ENABLE_1D],
                ENABLE_1MON: user_input[ENABLE_1MON],
                SOLAR_INVERT: user_input[SOLAR_INVERT],
                CONF_MONITOR_GIDS: list(user_input[CONF_MONITOR_GIDS]),
                CUSTOMER_GID: info[CUSTOMER_GID],
                CONFIG_TITLE: info[CONFIG_TITLE],
            }
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(),
                data_updates=data,
            )

        data_schema: dict[vol.Optional | vol.Required, Any] = {
            vol.Optional(
                ENABLE_1M,
                default=current_config.data.get(ENABLE_1M, True),
            ): cv.boolean,
            vol.Optional(
                ENABLE_1D,
                default=current_config.data.get(ENABLE_1D, True),
            ): cv.boolean,
            vol.Optional(
                ENABLE_1MON,
                default=current_config.data.get(ENABLE_1MON, True),
            ): cv.boolean,
            vol.Optional(
                SOLAR_INVERT,
                default=current_config.data.get(SOLAR_INVERT, True),
            ): cv.boolean,
            vol.Required(
                CONF_MONITOR_GIDS,
                default=current_config.data.get(
                    CONF_MONITOR_GIDS, list(self._pending_monitor_options)
                ),
            ): vol.All(
                cv.multi_select(self._pending_monitor_options),
                vol.Length(min=1),
            ),
        }

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(data_schema),
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Perform reauthentication upon an API authentication error."""
        return await self.async_step_reauth_confirm(entry_data)

    async def async_step_reauth_confirm(
        self, user_input: Mapping[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Confirm reauthentication dialog."""
        errors: dict[str, str] = {}
        existing_entry = self._get_reauth_entry()
        auth_method = existing_entry.data.get(AUTH_METHOD, AUTH_METHOD_EMAIL_PASSWORD)
        if user_input:
            try:
                reauth_data = dict(existing_entry.data)
                reauth_data.update(user_input)
                reauth_data[AUTH_METHOD] = auth_method
                info = await validate_input(reauth_data)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            else:
                await self.async_set_unique_id(info[CUSTOMER_GID])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                data_updates: dict[str, Any]
                if auth_method == AUTH_METHOD_TOKENS:
                    data_updates = {
                        AUTH_METHOD: AUTH_METHOD_TOKENS,
                        CONF_ID_TOKEN: user_input[CONF_ID_TOKEN],
                        CONF_ACCESS_TOKEN: user_input[CONF_ACCESS_TOKEN],
                        CONF_REFRESH_TOKEN: user_input[CONF_REFRESH_TOKEN],
                    }
                else:
                    data_updates = {
                        AUTH_METHOD: AUTH_METHOD_EMAIL_PASSWORD,
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    }
                return self.async_update_reload_and_abort(
                    existing_entry,
                    data_updates=data_updates,
                )
        if auth_method == AUTH_METHOD_TOKENS:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_ID_TOKEN): cv.string,
                        vol.Required(CONF_ACCESS_TOKEN): cv.string,
                        vol.Required(CONF_REFRESH_TOKEN): cv.string,
                    }
                ),
                errors=errors,
            )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EMAIL, default=existing_entry.data[CONF_EMAIL]
                    ): cv.string,
                    vol.Required(CONF_PASSWORD): cv.string,
                }
            ),
            errors=errors,
        )


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(exceptions.HomeAssistantError):
    """Error to indicate there is invalid auth."""
