from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    PERCENTAGE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import GoveePlugDataUpdateCoordinator
from .entity import GoveePlugEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Govee power monitoring sensors based on a config entry."""
    coordinator: GoveePlugDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Only add sensors if the device supports power monitoring
    if not coordinator.api.supports_power_monitoring():
        return

    entities = [
        GoveePlugVoltageSensor(coordinator, entry),
        GoveePlugCurrentSensor(coordinator, entry),
        GoveePlugPowerSensor(coordinator, entry),
        GoveePlugEnergySensor(coordinator, entry),
        GoveePlugPowerFactorSensor(coordinator, entry),
    ]
    async_add_entities(entities)


class GoveePlugSensorBase(GoveePlugEntity, SensorEntity):
    """Base class for Govee power monitoring sensors."""

    def __init__(
        self,
        coordinator: GoveePlugDataUpdateCoordinator,
        config_entry: ConfigEntry,
        sensor_type: str,
    ):
        """Initialize the sensor."""
        # Pass None for port and port_name since these are device-level sensors
        super().__init__(coordinator, config_entry, None, None)
        # Override unique_id to include sensor type
        self._attr_unique_id = f"{self._address}-{sensor_type}"
        self._sensor_type = sensor_type


class GoveePlugVoltageSensor(GoveePlugSensorBase):
    """Voltage sensor for Govee plug."""

    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_name = "Voltage"

    def __init__(
        self,
        coordinator: GoveePlugDataUpdateCoordinator,
        config_entry: ConfigEntry,
    ):
        """Initialize voltage sensor."""
        super().__init__(coordinator, config_entry, "voltage")

    @property
    def native_value(self) -> float | None:
        """Return the voltage."""
        power_data = self.coordinator.api.get_power_data()
        if power_data is None:
            return None
        return power_data.voltage


class GoveePlugCurrentSensor(GoveePlugSensorBase):
    """Current sensor for Govee plug."""

    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_name = "Current"

    def __init__(
        self,
        coordinator: GoveePlugDataUpdateCoordinator,
        config_entry: ConfigEntry,
    ):
        """Initialize current sensor."""
        super().__init__(coordinator, config_entry, "current")

    @property
    def native_value(self) -> float | None:
        """Return the current."""
        power_data = self.coordinator.api.get_power_data()
        if power_data is None:
            return None
        return power_data.current


class GoveePlugPowerSensor(GoveePlugSensorBase):
    """Power sensor for Govee plug."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_name = "Power"

    def __init__(
        self,
        coordinator: GoveePlugDataUpdateCoordinator,
        config_entry: ConfigEntry,
    ):
        """Initialize power sensor."""
        super().__init__(coordinator, config_entry, "power")

    @property
    def native_value(self) -> float | None:
        """Return the power consumption."""
        power_data = self.coordinator.api.get_power_data()
        if power_data is None:
            return None
        return power_data.power


class GoveePlugEnergySensor(GoveePlugSensorBase):
    """Energy sensor for Govee plug."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
    _attr_name = "Energy"

    def __init__(
        self,
        coordinator: GoveePlugDataUpdateCoordinator,
        config_entry: ConfigEntry,
    ):
        """Initialize energy sensor."""
        super().__init__(coordinator, config_entry, "energy")

    @property
    def native_value(self) -> float | None:
        """Return the total energy consumed."""
        power_data = self.coordinator.api.get_power_data()
        if power_data is None:
            return None
        return power_data.energy


class GoveePlugPowerFactorSensor(GoveePlugSensorBase):
    """Power factor sensor for Govee plug."""

    _attr_device_class = SensorDeviceClass.POWER_FACTOR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_name = "Power Factor"

    def __init__(
        self,
        coordinator: GoveePlugDataUpdateCoordinator,
        config_entry: ConfigEntry,
    ):
        """Initialize power factor sensor."""
        super().__init__(coordinator, config_entry, "power_factor")

    @property
    def native_value(self) -> int | None:
        """Return the power factor."""
        power_data = self.coordinator.api.get_power_data()
        if power_data is None:
            return None
        return power_data.power_factor
