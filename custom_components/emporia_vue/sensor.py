"""Platform for sensor integration."""

from datetime import datetime
import logging

from pyemvue.device import VueDevice, VueDeviceChannel, ChargerDevice
from pyemvue.enums import Scale

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_HOME_GIDS,
    CONF_MONITOR_GIDS,
    CONF_VIRTUAL_HOME,
    CONF_VIRTUAL_HOME_GIDS,
    CUSTOMER_GID,
    DOMAIN,
)
from .hierarchy import aggregate_root_gids, channel_name, device_identifier
from .resilience import TolerantUpdateMethod

_LOGGER: logging.Logger = logging.getLogger(__name__)


# def setup_platform(hass, config, add_entities, discovery_info=None):
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinator_1min = hass.data[DOMAIN][config_entry.entry_id]["coordinator_1min"]
    coordinator_1mon = hass.data[DOMAIN][config_entry.entry_id]["coordinator_1mon"]
    coordinator_day_sensor = hass.data[DOMAIN][config_entry.entry_id][
        "coordinator_day_sensor"
    ]

    device_information: dict[int, VueDevice] = hass.data[DOMAIN][
        config_entry.entry_id
    ]["device_information"]
    configured_virtual_gids = config_entry.data.get(CONF_VIRTUAL_HOME_GIDS)
    if config_entry.data.get(CONF_HOME_GIDS):
        configured_virtual_gids = []
    if configured_virtual_gids is None:
        # Preserve the behavior of entries created before independent selection.
        configured_virtual_gids = (
            config_entry.data.get(CONF_MONITOR_GIDS, [])
            if config_entry.data.get(CONF_VIRTUAL_HOME, False)
            else []
        )
    virtual_source_gids = aggregate_root_gids(
        device_information,
        configured_virtual_gids,
    )
    native_homes = [
        {
            **home,
            "device_gids": aggregate_root_gids(
                device_information,
                [str(gid) for gid in home["device_gids"]],
            ),
        }
        for home in hass.data[DOMAIN][config_entry.entry_id].get("native_homes", [])
    ]

    def entities_for(coordinator) -> list[SensorEntity]:
        """Build channel sensors and the optional virtual aggregate."""
        entities: list[SensorEntity] = [
            CurrentVuePowerSensor(coordinator, identifier)
            for identifier in coordinator.data
        ]
        if virtual_source_gids:
            entities.append(
                VirtualHomeSensor(
                    coordinator,
                    config_entry.data[CUSTOMER_GID],
                    virtual_source_gids,
                )
            )
        entities.extend(
            EmporiaHomeSensor(
                coordinator,
                home["site_gid"],
                home["name"],
                home["device_gids"],
            )
            for home in native_homes
            if home["device_gids"]
        )
        return entities

    if coordinator_1min:
        async_add_entities(entities_for(coordinator_1min))

    if coordinator_1mon:
        async_add_entities(entities_for(coordinator_1mon))

    if coordinator_day_sensor:
        async_add_entities(entities_for(coordinator_day_sensor))

    retry_update_methods: dict[str, TolerantUpdateMethod] = hass.data[DOMAIN][
        config_entry.entry_id
    ]["retry_update_methods"]
    if retry_update_methods:
        async_add_entities(
            [EmporiaApiRetrySensor(retry_update_methods, config_entry.entry_id)]
        )
        if "minute" in retry_update_methods:
            async_add_entities(
                [EmporiaApiLatencySensor(retry_update_methods, config_entry.entry_id)]
            )

    # Add charger status sensors
    coordinator_device_status = hass.data[DOMAIN][config_entry.entry_id][
        "coordinator_device_status"
    ]
    if coordinator_device_status and coordinator_device_status.data:
        async_add_entities(
            EmporiaChargerStatusSensor(coordinator_device_status, device_information[int(gid)])
            for gid in coordinator_device_status.data
            if int(gid) in device_information and device_information[int(gid)].ev_charger
        )


class VirtualHomeSensor(CoordinatorEntity, SensorEntity):  # type: ignore
    """Aggregate the main channels of selected top-level monitors."""

    def __init__(self, coordinator, customer_gid: str, source_gids: list[int]) -> None:
        """Initialize a stable virtual home aggregate."""
        super().__init__(coordinator)
        self._source_gids = source_gids
        self._scale: str = next(iter(coordinator.data.values()))["scale"]
        self._attr_has_entity_name = False
        self._attr_suggested_display_precision = 3
        if self._scale == Scale.MINUTE.value:
            self._attr_name = "Emporia Vue Virtual Home Power"
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 1
        else:
            period = "Today" if self._scale == Scale.DAY.value else "This Month"
            self._attr_name = f"Emporia Vue Virtual Home Energy {period}"
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL
        self._attr_unique_id = (
            f"sensor.emporia_vue.virtual_home.{self._scale}.{customer_gid}"
        )

    def _source_data(self) -> list[dict]:
        """Return available main-channel coordinator records."""
        return [
            self.coordinator.data[identifier]
            for gid in self._source_gids
            if (identifier := f"{gid}-1,2,3-{self._scale}")
            in self.coordinator.data
        ]

    @property
    def available(self) -> bool:
        """Return whether at least one aggregate source is available."""
        return super().available and bool(self._source_data())

    @property
    def native_value(self) -> float:
        """Return the sum of the selected monitor mains."""
        usage = sum(
            item["usage"] for item in self._source_data() if item["usage"] is not None
        )
        if self._scale == Scale.MINUTE.value:
            return 60 * 1000 * usage
        return usage

    @property
    def last_reset(self) -> datetime | None:
        """Use the common source reset for total-state energy statistics."""
        resets = {
            item["reset"] for item in self._source_data() if item["reset"] is not None
        }
        return resets.pop() if len(resets) == 1 else None


class EmporiaHomeSensor(VirtualHomeSensor):
    """Aggregate sensor for a home configured in the Emporia app."""

    def __init__(
        self,
        coordinator,
        site_gid: str,
        name: str,
        source_gids: list[int],
    ) -> None:
        """Initialize a native Emporia home aggregate."""
        super().__init__(coordinator, site_gid, source_gids)
        if self._scale == Scale.MINUTE.value:
            self._attr_name = f"{name} Power"
        else:
            period = "Today" if self._scale == Scale.DAY.value else "This Month"
            self._attr_name = f"{name} Energy {period}"
        self._attr_unique_id = f"sensor.emporia_vue.home.{site_gid}.{self._scale}"


class CurrentVuePowerSensor(CoordinatorEntity, SensorEntity):  # type: ignore
    """Representation of a Vue Sensor's current power."""

    def __init__(self, coordinator, identifier) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        self._id = identifier
        self._scale: str = coordinator.data[identifier]["scale"]
        device_gid: int = coordinator.data[identifier]["device_gid"]
        channel_num: str = coordinator.data[identifier]["channel_num"]
        self._device: VueDevice = coordinator.data[identifier]["info"]
        final_channel: VueDeviceChannel | None = None
        if self._device is not None:
            for channel in self._device.channels:
                if channel.channel_num == channel_num:
                    final_channel = channel
                    break
        if final_channel is None:
            _LOGGER.warning(
                "No channel found for device_gid %s and channel_num %s",
                device_gid,
                channel_num,
            )
            raise RuntimeError(
                f"No channel found for device_gid {device_gid} and channel_num {channel_num}"
            )
        self._channel: VueDeviceChannel = final_channel
        self._iskwh = self.scale_is_energy()
        prefix = channel_name(self._device, self._channel)

        self._attr_has_entity_name = True
        if self._iskwh:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL
            self._attr_suggested_display_precision = 3
            measurement = f"Energy {self.scale_readable()}"
        else:
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 1
            measurement = "Power"
        self._attr_name = f"{prefix} {measurement}" if prefix else measurement

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, device_identifier(self._device))},
            name=self._device.display_name or self._device.device_name,
            model=self._device.model,
            sw_version=self._device.firmware,
            manufacturer="Emporia",
            via_device=(DOMAIN, str(self._device.parent_device_gid))
            if self._device.parent_device_gid
            else None,
        )

    @property
    def last_reset(self) -> datetime | None:
        """Reset time of the daily/monthly sensor. Midnight local time."""
        if self._id in self.coordinator.data:
            return self.coordinator.data[self._id]["reset"]
        return None

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if self._id in self.coordinator.data:
            usage = self.coordinator.data[self._id]["usage"]
            return self.scale_usage(usage) if usage is not None else None
        return None

    @property
    def unique_id(self) -> str:
        """Return the Unique ID for the sensor."""
        if self._scale == Scale.MINUTE.value:
            return (
                "sensor.emporia_vue.instant."
                f"{self._channel.device_gid}-{self._channel.channel_num}"
            )
        return (
            f"sensor.emporia_vue.{self._scale}."
            f"{self._channel.device_gid}-{self._channel.channel_num}"
        )

    def scale_usage(self, usage):
        """Scales the usage to the correct timescale and magnitude."""
        if self._scale == Scale.MINUTE.value:
            usage = 60 * 1000 * usage  # convert from kwh to w rate
        elif self._scale == Scale.SECOND.value:
            usage = 3600 * 1000 * usage  # convert to rate
        elif self._scale == Scale.MINUTES_15.value:
            usage = (
                4 * 1000 * usage
            )  # this might never be used but for safety, convert to rate
        return usage

    def scale_is_energy(self):
        """Return True if the scale is an energy unit instead of power."""
        return self._scale not in (
            Scale.MINUTE.value,
            Scale.SECOND.value,
            Scale.MINUTES_15.value,
        )

    def scale_readable(self):
        """Return a human readable scale."""
        if self._scale == Scale.MINUTE.value:
            return "Minute Average"
        if self._scale == Scale.DAY.value:
            return "Today"
        if self._scale == Scale.MONTH.value:
            return "This Month"
        return self._scale


class EmporiaUpdateTelemetrySensor(SensorEntity):
    """Base entity for Emporia cloud update telemetry."""

    _attr_should_poll = False

    def __init__(
        self,
        update_methods: dict[str, TolerantUpdateMethod],
        listener_names: set[str] | None = None,
    ) -> None:
        """Initialize an update telemetry sensor."""
        self._update_methods = update_methods
        self._listener_names = listener_names

    async def async_added_to_hass(self) -> None:
        """Subscribe to update telemetry changes."""
        await super().async_added_to_hass()
        for name, update_method in self._update_methods.items():
            if self._listener_names is not None and name not in self._listener_names:
                continue
            self.async_on_remove(update_method.add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        """Write update telemetry changes to Home Assistant."""
        self.async_write_ha_state()


class EmporiaApiRetrySensor(EmporiaUpdateTelemetrySensor):
    """Expose cloud retry telemetry in Home Assistant."""

    _attr_icon = "mdi:cloud-refresh"
    _attr_name = "Emporia API Retries"
    _attr_native_unit_of_measurement = "retries"

    def __init__(
        self,
        update_methods: dict[str, TolerantUpdateMethod],
        entry_id: str,
    ) -> None:
        """Initialize the API retry sensor."""
        super().__init__(update_methods)
        self._attr_unique_id = f"sensor.emporia_vue.api_retries.{entry_id}"

    @property
    def native_value(self) -> int:
        """Return total retries across enabled telemetry coordinators."""
        return sum(
            update_method.total_failures
            for update_method in self._update_methods.values()
        )

    @property
    def extra_state_attributes(self) -> dict:
        """Return per-coordinator retry details."""
        last_failures = [
            update_method.last_failure
            for update_method in self._update_methods.values()
            if update_method.last_failure is not None
        ]
        return {
            "retry_totals": {
                name: update_method.total_failures
                for name, update_method in self._update_methods.items()
            },
            "consecutive_retries": {
                name: update_method.consecutive_failures
                for name, update_method in self._update_methods.items()
            },
            "last_retry": max(last_failures).isoformat() if last_failures else None,
        }


class EmporiaApiLatencySensor(EmporiaUpdateTelemetrySensor):
    """Expose end-to-end Emporia API update latency."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_icon = "mdi:timer-outline"
    _attr_name = "Emporia API Latency"
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        update_methods: dict[str, TolerantUpdateMethod],
        entry_id: str,
    ) -> None:
        """Initialize the API latency sensor."""
        super().__init__(update_methods, listener_names={"minute"})
        self._attr_unique_id = f"sensor.emporia_vue.api_latency.{entry_id}"

    @property
    def native_value(self) -> float | None:
        """Return the latest minute telemetry update duration in milliseconds."""
        minute_update = self._update_methods.get("minute")
        if not minute_update or minute_update.last_duration_ms is None:
            return None
        return round(minute_update.last_duration_ms, 1)

    @property
    def extra_state_attributes(self) -> dict:
        """Return per-coordinator update durations."""
        minute_update = self._update_methods["minute"]
        return {
            "update_latency_ms": {
                name: round(update_method.last_duration_ms, 1)
                for name, update_method in self._update_methods.items()
                if update_method.last_duration_ms is not None
            },
            "last_update_attempt": (
                minute_update.last_attempt.isoformat()
                if minute_update.last_attempt
                else None
            ),
            "measurement": "end_to_end_update_duration",
        }


# Known Emporia charger API responses (from historical data):
#   Status: "Charging", "Standby", "DeviceNotConnected", ""
#   Messages: "Charging", "Ready", "Off", "Self Test", "Offline",
#             "EV is not accepting charge", "Connected to EV",
#             "Please Wait", "Charging Halted", ""

def _map_charger_state(status: str | None, message: str | None, fault_text: str | None) -> tuple[str, str]:
    """Map Emporia charger status/message to a human-friendly state and IEC 61851 code."""
    status_lower = (status or "").lower()
    message_lower = (message or "").lower()
    fault = (fault_text or "").strip()

    # F: Fault condition
    if fault or "error" in status_lower or "fault" in status_lower or "error" in message_lower or "fault" in message_lower:
        return "Error", "F"
    # C: Actively charging
    if status_lower == "charging":
        return "Charging", "C"
    # A: Disconnected -  no EV present or device offline
    if not status_lower:
        return "Disconnected", "A"
    if status_lower == "devicenotconnected":
        return "Disconnected", "A"
    if status_lower == "standby" and message_lower in ("ready", "off", "self test", "please wait"):
        return "Disconnected", "A"
    # B: Connected but not charging (default for unknown/unmapped states)
    if status_lower != "standby":
        _LOGGER.debug(
            "Unmapped charger state: status=%s, message=%s", status, message
        )
    return "Connected", "B"


CHARGER_STATUS_OPTIONS = ["Disconnected", "Connected", "Charging", "Error"]

class EmporiaChargerStatusSensor(CoordinatorEntity, SensorEntity):  # type: ignore
    """Representation of an Emporia Charger status sensor."""

    def __init__(self, coordinator, device: VueDevice) -> None:
        """Initialize the charger status sensor."""
        super().__init__(coordinator)
        self._device = device
        self._device_gid = str(device.device_gid)
        self._attr_has_entity_name = True
        self._attr_name = "Status"
        self._attr_translation_key = "charger_status"
        self._attr_device_class = SensorDeviceClass.ENUM
        self._attr_options = CHARGER_STATUS_OPTIONS
        self._attr_icon = "mdi:ev-station"

    @property
    def native_value(self) -> str:
        """Return the human-friendly charger status."""
        data: ChargerDevice | None = self.coordinator.data.get(self._device_gid)
        if data:
            state, _ = _map_charger_state(data.status, data.message, data.fault_text)
            return state
        return "Unknown"

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Return IEC code and raw Emporia values as attributes."""
        data: ChargerDevice | None = self.coordinator.data.get(self._device_gid)
        if data:
            _, iec_code = _map_charger_state(data.status, data.message, data.fault_text)
            return {
                "iec_status": iec_code,
                "raw_status": data.status,
                "raw_message": data.message,
                "fault_text": data.fault_text,
            }
        return {}

    @property
    def unique_id(self) -> str:
        """Unique ID for the charger status sensor."""
        return f"emporia_vue.charger_status_{self._device_gid}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, device_identifier(self._device))},
            name=self._device.display_name or self._device.device_name,
            model=self._device.model,
            sw_version=self._device.firmware,
            manufacturer="Emporia",
            via_device=(DOMAIN, str(self._device.parent_device_gid))
            if self._device.parent_device_gid
            else None,
        )
