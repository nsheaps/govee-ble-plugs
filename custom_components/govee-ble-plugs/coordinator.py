from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any


from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothDataUpdateCoordinator,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .plugs import GoveePlugApi

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_LOGGER: logging.Logger = logging.getLogger(__package__)
PLATFORMS: list[str] = [Platform.SWITCH]

# Interval for polling power data (BLE connections are expensive, don't poll too often)
POWER_UPDATE_INTERVAL = timedelta(seconds=60)


class GoveePlugDataUpdateCoordinator(PassiveBluetoothDataUpdateCoordinator):
    """Class to manage fetching data from the plug."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: GoveePlugApi,
        ble_device: BLEDevice,
    ) -> None:
        """Initialize."""
        self.api: GoveePlugApi = api
        self.ble_device = ble_device
        self._power_update_unsub = None
        super().__init__(
            hass,
            _LOGGER,
            ble_device.address,
            bluetooth.BluetoothScanningMode.PASSIVE,
        )

    def async_start(self):
        """Start the coordinator and set up power polling if supported."""
        unsub = super().async_start()

        # Set up periodic power data polling if device supports it
        if self.api.supports_power_monitoring():
            self._power_update_unsub = async_track_time_interval(
                self.hass,
                self._async_update_power_data,
                POWER_UPDATE_INTERVAL,
            )
            # Also request initial power data
            self.hass.async_create_task(self._async_update_power_data())

        def _unsub():
            unsub()
            if self._power_update_unsub:
                self._power_update_unsub()

        return _unsub

    async def _async_update_power_data(self, *args) -> None:
        """Update power monitoring data."""
        if self.api.supports_power_monitoring():
            _LOGGER.debug("Requesting power data update")
            if await self.api.async_request_power_data():
                self.async_update_listeners()

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle a Bluetooth event."""
        self.api.handle_bluetooth_event(service_info.device, service_info.advertisement)
        super()._async_handle_bluetooth_event(service_info, change)
