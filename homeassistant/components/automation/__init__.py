"""Allow to set up simple automation rules via the config file."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, cast

import voluptuous as vol
from voluptuous.humanize import humanize_error

from homeassistant.components import blueprint
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_MODE,
    ATTR_NAME,
    CONF_ALIAS,
    CONF_CONDITION,
    CONF_DEVICE_ID,
    CONF_ENTITY_ID,
    CONF_ID,
    CONF_MODE,
    CONF_PLATFORM,
    CONF_VARIABLES,
    CONF_ZONE,
    EVENT_HOMEASSISTANT_STARTED,
    SERVICE_RELOAD,
    SERVICE_TOGGLE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
)
from homeassistant.core import (
    Context,
    CoreState,
    HomeAssistant,
    callback,
    split_entity_id,
)
from homeassistant.exceptions import (
    ConditionError,
    ConditionErrorContainer,
    ConditionErrorIndex,
    HomeAssistantError,
)
from homeassistant.helpers import condition, extract_domain_configs, template
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import ToggleEntity
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.script import (
    ATTR_CUR,
    ATTR_MAX,
    CONF_MAX,
    CONF_MAX_EXCEEDED,
    Script,
)
from homeassistant.helpers.script_variables import ScriptVariables
from homeassistant.helpers.service import (
    ReloadServiceHelper,
    async_register_admin_service,
)
from homeassistant.helpers.trace import (
    TraceElement,
    script_execution_set,
    trace_append_element,
    trace_get,
    trace_path,
)
from homeassistant.helpers.trigger import async_initialize_triggers
from homeassistant.helpers.typing import TemplateVarsType
from homeassistant.loader import bind_hass
from homeassistant.util.dt import parse_datetime

from .config import AutomationConfig, async_validate_config_item

# Not used except by packages to check config structure
from .config import PLATFORM_SCHEMA  # noqa: F401
from .const import (
    CONF_ACTION,
    CONF_INITIAL_STATE,
    CONF_TRACE,
    CONF_TRIGGER,
    CONF_TRIGGER_VARIABLES,
    DEFAULT_INITIAL_STATE,
    DOMAIN,
    LOGGER,
)
from .helpers import async_get_blueprints
from .trace import trace_automation

# mypy: allow-untyped-calls, allow-untyped-defs
# mypy: no-check-untyped-defs, no-warn-return-any

ENTITY_ID_FORMAT = DOMAIN + ".{}"


CONF_SKIP_CONDITION = "skip_condition"
CONF_STOP_ACTIONS = "stop_actions"
DEFAULT_STOP_ACTIONS = True

EVENT_AUTOMATION_RELOADED = "automation_reloaded"
EVENT_AUTOMATION_TRIGGERED = "automation_triggered"

ATTR_LAST_TRIGGERED = "last_triggered"
ATTR_SOURCE = "source"
ATTR_VARIABLES = "variables"
SERVICE_TRIGGER = "trigger"

_LOGGER = logging.getLogger(__name__)
AutomationActionType = Callable[[HomeAssistant, TemplateVarsType], Awaitable[None]]


@bind_hass
def is_on(hass, entity_id):
    """
    Return true if specified automation entity_id is on.

    Async friendly.
    """
    return hass.states.is_state(entity_id, STATE_ON)


@callback
def automations_with_entity(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Return all automations that reference the entity."""
    if DOMAIN not in hass.data:
        return []

    component = hass.data[DOMAIN]

    return [
        automation_entity.entity_id
        for automation_entity in component.entities
        if entity_id in automation_entity.referenced_entities
    ]


@callback
def entities_in_automation(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Return all entities in a scene."""
    if DOMAIN not in hass.data:
        return []

    component = hass.data[DOMAIN]

    automation_entity = component.get_entity(entity_id)

    if automation_entity is None:
        return []

    return list(automation_entity.referenced_entities)


@callback
def automations_with_device(hass: HomeAssistant, device_id: str) -> list[str]:
    """Return all automations that reference the device."""
    if DOMAIN not in hass.data:
        return []

    component = hass.data[DOMAIN]

    return [
        automation_entity.entity_id
        for automation_entity in component.entities
        if device_id in automation_entity.referenced_devices
    ]


@callback
def devices_in_automation(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Return all devices in a scene."""
    if DOMAIN not in hass.data:
        return []

    component = hass.data[DOMAIN]

    automation_entity = component.get_entity(entity_id)

    if automation_entity is None:
        return []

    return list(automation_entity.referenced_devices)


@callback
def automations_with_area(hass: HomeAssistant, area_id: str) -> list[str]:
    """Return all automations that reference the area."""
    if DOMAIN not in hass.data:
        return []

    component = hass.data[DOMAIN]

    return [
        automation_entity.entity_id
        for automation_entity in component.entities
        if area_id in automation_entity.referenced_areas
    ]


@callback
def areas_in_automation(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Return all areas in an automation."""
    if DOMAIN not in hass.data:
        return []

    component = hass.data[DOMAIN]

    automation_entity = component.get_entity(entity_id)

    if automation_entity is None:
        return []

    return list(automation_entity.referenced_areas)


async def async_setup(hass, config):
    """Set up all automations."""
    # Local import to avoid circular import
    hass.data[DOMAIN] = component = EntityComponent(LOGGER, DOMAIN, hass)

    # To register the automation blueprints
    async_get_blueprints(hass)

    if not await _async_process_config(hass, config, component):
        await async_get_blueprints(hass).async_populate()

    async def trigger_service_handler(entity, service_call):
        """Handle forced automation trigger, e.g. from frontend."""
        await entity.async_trigger(
            {**service_call.data[ATTR_VARIABLES], "trigger": {"platform": None}},
            skip_condition=service_call.data[CONF_SKIP_CONDITION],
            context=service_call.context,
        )

    component.async_register_entity_service(
        SERVICE_TRIGGER,
        {
            vol.Optional(ATTR_VARIABLES, default={}): dict,
            vol.Optional(CONF_SKIP_CONDITION, default=True): bool,
        },
        trigger_service_handler,
    )
    component.async_register_entity_service(SERVICE_TOGGLE, {}, "async_toggle")
    component.async_register_entity_service(SERVICE_TURN_ON, {}, "async_turn_on")
    component.async_register_entity_service(
        SERVICE_TURN_OFF,
        {vol.Optional(CONF_STOP_ACTIONS, default=DEFAULT_STOP_ACTIONS): cv.boolean},
        "async_turn_off",
    )

    async def reload_service_handler(service_call):
        """Remove all automations and load new ones from config."""
        conf = await component.async_prepare_reload()
        if conf is None:
            return
        async_get_blueprints(hass).async_reset_cache()
        await _async_process_config(hass, conf, component)
        hass.bus.async_fire(EVENT_AUTOMATION_RELOADED, context=service_call.context)

    reload_helper = ReloadServiceHelper(reload_service_handler)

    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_RELOAD,
        reload_helper.execute_service,
        schema=vol.Schema({}),
    )

    return True


class AutomationEntity(ToggleEntity, RestoreEntity):
    """Entity to show status of entity."""

    _attr_should_poll = False

    def __init__(
        self,
        automation_id,
        name,
        trigger_config,
        cond_func,
        action_script,
        initial_state,
        variables,
        trigger_variables,
        raw_config,
        blueprint_inputs,
        trace_config,
    ):
        """Initialize an automation entity."""
        self._attr_name = name
        self._trigger_config = trigger_config
        self._async_detach_triggers = None
        self._cond_func = cond_func
        self.action_script = action_script
        self.action_script.change_listener = self.async_write_ha_state
        self._initial_state = initial_state
        self._is_enabled = False
        self._referenced_entities: set[str] | None = None
        self._referenced_devices: set[str] | None = None
        self._logger = LOGGER
        self._variables: ScriptVariables = variables
        self._trigger_variables: ScriptVariables = trigger_variables
        self._raw_config = raw_config
        self._blueprint_inputs = blueprint_inputs
        self._trace_config = trace_config
        self._attr_unique_id = automation_id

    @property
    def extra_state_attributes(self):
        """Return the entity state attributes."""
        attrs = {
            ATTR_LAST_TRIGGERED: self.action_script.last_triggered,
            ATTR_MODE: self.action_script.script_mode,
            ATTR_CUR: self.action_script.runs,
        }
        if self.action_script.supports_max:
            attrs[ATTR_MAX] = self.action_script.max_runs
        if self.unique_id is not None:
            attrs[CONF_ID] = self.unique_id
        return attrs

    @property
    def is_on(self) -> bool:
        """Return True if entity is on."""
        return self._async_detach_triggers is not None or self._is_enabled

    @property
    def referenced_areas(self):
        """Return a set of referenced areas."""
        return self.action_script.referenced_areas

    @property
    def referenced_devices(self):
        """Return a set of referenced devices."""
        if self._referenced_devices is not None:
            return self._referenced_devices

        referenced = self.action_script.referenced_devices

        if self._cond_func is not None:
            for conf in self._cond_func.config:
                referenced |= condition.async_extract_devices(conf)

        for conf in self._trigger_config:
            device = _trigger_extract_device(conf)
            if device is not None:
                referenced.add(device)

        self._referenced_devices = referenced
        return referenced

    @property
    def referenced_entities(self):
        """Return a set of referenced entities."""
        if self._referenced_entities is not None:
            return self._referenced_entities

        referenced = self.action_script.referenced_entities

        if self._cond_func is not None:
            for conf in self._cond_func.config:
                referenced |= condition.async_extract_entities(conf)

        for conf in self._trigger_config:
            for entity_id in _trigger_extract_entities(conf):
                referenced.add(entity_id)

        self._referenced_entities = referenced
        return referenced

    async def async_added_to_hass(self) -> None:
        """Startup with initial state or previous state."""
        await super().async_added_to_hass()

        self._logger = logging.getLogger(
            f"{__name__}.{split_entity_id(self.entity_id)[1]}"
        )
        self.action_script.update_logger(self._logger)

        state = await self.async_get_last_state()
        if state:
            enable_automation = state.state == STATE_ON
            last_triggered = state.attributes.get("last_triggered")
            if last_triggered is not None:
                self.action_script.last_triggered = parse_datetime(last_triggered)
            self._logger.debug(
                "Loaded automation %s with state %s from state "
                " storage last state %s",
                self.entity_id,
                enable_automation,
                state,
            )
        else:
            enable_automation = DEFAULT_INITIAL_STATE
            self._logger.debug(
                "Automation %s not in state storage, state %s from default is used",
                self.entity_id,
                enable_automation,
            )

        if self._initial_state is not None:
            enable_automation = self._initial_state
            self._logger.debug(
                "Automation %s initial state %s overridden from "
                "config initial_state",
                self.entity_id,
                enable_automation,
            )

        if enable_automation:
            await self.async_enable()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on and update the state."""
        await self.async_enable()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        if CONF_STOP_ACTIONS in kwargs:
            await self.async_disable(kwargs[CONF_STOP_ACTIONS])
        else:
            await self.async_disable()

    async def async_trigger(self, run_variables, context=None, skip_condition=False):
        """Trigger automation.

        This method is a coroutine.
        """
        reason = ""
        if "trigger" in run_variables and "description" in run_variables["trigger"]:
            reason = f' by {run_variables["trigger"]["description"]}'
        self._logger.debug("Automation triggered%s", reason)

        # Create a new context referring to the old context.
        parent_id = None if context is None else context.id
        trigger_context = Context(parent_id=parent_id)

        with trace_automation(
            self.hass,
            self.unique_id,
            self._raw_config,
            self._blueprint_inputs,
            trigger_context,
            self._trace_config,
        ) as automation_trace:
            this = None
            state = self.hass.states.get(self.entity_id)
            if state:
                this = state.as_dict()
            variables = {"this": this, **(run_variables or {})}
            if self._variables:
                try:
                    variables = self._variables.async_render(self.hass, variables)
                except template.TemplateError as err:
                    self._logger.error("Error rendering variables: %s", err)
                    automation_trace.set_error(err)
                    return

            # Prepare tracing the automation
            automation_trace.set_trace(trace_get())

            # Set trigger reason
            trigger_description = variables.get("trigger", {}).get("description")
            automation_trace.set_trigger_description(trigger_description)

            # Add initial variables as the trigger step
            if "trigger" in variables and "idx" in variables["trigger"]:
                trigger_path = f"trigger/{variables['trigger']['idx']}"
            else:
                trigger_path = "trigger"
            trace_element = TraceElement(variables, trigger_path)
            trace_append_element(trace_element)

            if (
                not skip_condition
                and self._cond_func is not None
                and not self._cond_func(variables)
            ):
                self._logger.debug(
                    "Conditions not met, aborting automation. Condition summary: %s",
                    trace_get(clear=False),
                )
                script_execution_set("failed_conditions")
                return

            self.async_set_context(trigger_context)
            event_data = {
                ATTR_NAME: self.name,
                ATTR_ENTITY_ID: self.entity_id,
            }
            if "trigger" in variables and "description" in variables["trigger"]:
                event_data[ATTR_SOURCE] = variables["trigger"]["description"]

            @callback
            def started_action():
                self.hass.bus.async_fire(
                    EVENT_AUTOMATION_TRIGGERED, event_data, context=trigger_context
                )

            try:
                with trace_path("action"):
                    await self.action_script.async_run(
                        variables, trigger_context, started_action
                    )
            except (vol.Invalid, HomeAssistantError) as err:
                self._logger.error(
                    "Error while executing automation %s: %s",
                    self.entity_id,
                    err,
                )
                automation_trace.set_error(err)
            except Exception as err:  # pylint: disable=broad-except
                self._logger.exception("While executing automation %s", self.entity_id)
                automation_trace.set_error(err)

    async def async_will_remove_from_hass(self):
        """Remove listeners when removing automation from Home Assistant."""
        await super().async_will_remove_from_hass()
        await self.async_disable()

    async def async_enable(self):
        """Enable this automation entity.

        This method is a coroutine.
        """
        if self._is_enabled:
            return

        self._is_enabled = True

        # HomeAssistant is starting up
        if self.hass.state != CoreState.not_running:
            self._async_detach_triggers = await self._async_attach_triggers(False)
            self.async_write_ha_state()
            return

        async def async_enable_automation(event):
            """Start automation on startup."""
            # Don't do anything if no longer enabled or already attached
            if not self._is_enabled or self._async_detach_triggers is not None:
                return

            self._async_detach_triggers = await self._async_attach_triggers(True)

        self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, async_enable_automation
        )
        self.async_write_ha_state()

    async def async_disable(self, stop_actions=DEFAULT_STOP_ACTIONS):
        """Disable the automation entity."""
        if not self._is_enabled and not self.action_script.runs:
            return

        self._is_enabled = False

        if self._async_detach_triggers is not None:
            self._async_detach_triggers()
            self._async_detach_triggers = None

        if stop_actions:
            await self.action_script.async_stop()

        self.async_write_ha_state()

    async def _async_attach_triggers(
        self, home_assistant_start: bool
    ) -> Callable[[], None] | None:
        """Set up the triggers."""

        def log_cb(level, msg, **kwargs):
            self._logger.log(level, "%s %s", msg, self.name, **kwargs)

        this = None
        self.async_write_ha_state()
        state = self.hass.states.get(self.entity_id)
        if state:
            this = state.as_dict()
        variables = {"this": this}
        if self._trigger_variables:
            try:
                variables = self._trigger_variables.async_render(
                    self.hass,
                    variables,
                    limited=True,
                )
            except template.TemplateError as err:
                self._logger.error("Error rendering trigger variables: %s", err)
                return None

        return await async_initialize_triggers(
            self.hass,
            self._trigger_config,
            self.async_trigger,
            DOMAIN,
            str(self.name),
            log_cb,
            home_assistant_start,
            variables,
        )


async def _async_process_config(
    hass: HomeAssistant,
    config: dict[str, Any],
    component: EntityComponent,
) -> bool:
    """Process config and add automations.

    Returns if blueprints were used.
    """
    entities = []
    blueprints_used = False

    for config_key in extract_domain_configs(config, DOMAIN):
        conf: list[dict[str, Any] | blueprint.BlueprintInputs] = config[config_key]

        for list_no, config_block in enumerate(conf):
            raw_blueprint_inputs = None
            raw_config = None
            if isinstance(config_block, blueprint.BlueprintInputs):
                blueprints_used = True
                blueprint_inputs = config_block
                raw_blueprint_inputs = blueprint_inputs.config_with_inputs

                try:
                    raw_config = blueprint_inputs.async_substitute()
                    config_block = cast(
                        Dict[str, Any],
                        await async_validate_config_item(hass, raw_config),
                    )
                except vol.Invalid as err:
                    LOGGER.error(
                        "Blueprint %s generated invalid automation with inputs %s: %s",
                        blueprint_inputs.blueprint.name,
                        blueprint_inputs.inputs,
                        humanize_error(config_block, err),
                    )
                    continue
            else:
                raw_config = cast(AutomationConfig, config_block).raw_config

            automation_id = config_block.get(CONF_ID)
            name = config_block.get(CONF_ALIAS) or f"{config_key} {list_no}"

            initial_state = config_block.get(CONF_INITIAL_STATE)

            action_script = Script(
                hass,
                config_block[CONF_ACTION],
                name,
                DOMAIN,
                running_description="automation actions",
                script_mode=config_block[CONF_MODE],
                max_runs=config_block[CONF_MAX],
                max_exceeded=config_block[CONF_MAX_EXCEEDED],
                logger=LOGGER,
                # We don't pass variables here
                # Automation will already render them to use them in the condition
                # and so will pass them on to the script.
            )

            if CONF_CONDITION in config_block:
                cond_func = await _async_process_if(hass, name, config, config_block)

                if cond_func is None:
                    continue
            else:
                cond_func = None

            # Add trigger variables to variables
            variables = None
            if CONF_TRIGGER_VARIABLES in config_block:
                variables = ScriptVariables(
                    dict(config_block[CONF_TRIGGER_VARIABLES].as_dict())
                )
            if CONF_VARIABLES in config_block:
                if variables:
                    variables.variables.update(config_block[CONF_VARIABLES].as_dict())
                else:
                    variables = config_block[CONF_VARIABLES]

            entity = AutomationEntity(
                automation_id,
                name,
                config_block[CONF_TRIGGER],
                cond_func,
                action_script,
                initial_state,
                variables,
                config_block.get(CONF_TRIGGER_VARIABLES),
                raw_config,
                raw_blueprint_inputs,
                config_block[CONF_TRACE],
            )

            entities.append(entity)

    if entities:
        await component.async_add_entities(entities)

    return blueprints_used


async def _async_process_if(hass, name, config, p_config):
    """Process if checks."""
    if_configs = p_config[CONF_CONDITION]

    checks = []
    for if_config in if_configs:
        try:
            checks.append(await condition.async_from_config(hass, if_config, False))
        except HomeAssistantError as ex:
            LOGGER.warning("Invalid condition: %s", ex)
            return None

    def if_action(variables=None):
        """AND all conditions."""
        errors = []
        for index, check in enumerate(checks):
            try:
                with trace_path(["condition", str(index)]):
                    if not check(hass, variables):
                        return False
            except ConditionError as ex:
                errors.append(
                    ConditionErrorIndex(
                        "condition", index=index, total=len(checks), error=ex
                    )
                )

        if errors:
            LOGGER.warning(
                "Error evaluating condition in '%s':\n%s",
                name,
                ConditionErrorContainer("condition", errors=errors),
            )
            return False

        return True

    if_action.config = if_configs

    return if_action


@callback
def _trigger_extract_device(trigger_conf: dict) -> str | None:
    """Extract devices from a trigger config."""
    if trigger_conf[CONF_PLATFORM] != "device":
        return None

    return trigger_conf[CONF_DEVICE_ID]


@callback
def _trigger_extract_entities(trigger_conf: dict) -> list[str]:
    """Extract entities from a trigger config."""
    if trigger_conf[CONF_PLATFORM] in ("state", "numeric_state"):
        return trigger_conf[CONF_ENTITY_ID]

    if trigger_conf[CONF_PLATFORM] == "zone":
        return trigger_conf[CONF_ENTITY_ID] + [trigger_conf[CONF_ZONE]]

    if trigger_conf[CONF_PLATFORM] == "geo_location":
        return [trigger_conf[CONF_ZONE]]

    if trigger_conf[CONF_PLATFORM] == "sun":
        return ["sun.sun"]

    return []
