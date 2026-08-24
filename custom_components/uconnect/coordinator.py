"""Coordinator for Uconnect integration"""

from __future__ import annotations

from datetime import timedelta
import logging

from py_uconnect import Client
from py_uconnect.command import Command
from py_uconnect.api import CHARGING_LEVELS_BY_NAME
from py_uconnect.brands import BRANDS as BRANDS_BY_NAME

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_PIN,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_BRAND_REGION,
    CONF_CHARGING_REFRESH_INTERVAL,
    CONF_DISABLE_TLS_VERIFICATION,
    BRANDS,
    DEFAULT_CHARGING_REFRESH_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    MIN_CHARGING_REFRESH_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(DOMAIN)


class UconnectDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self.platforms: set[str] = set()
        self.extrapolated_soc_sensors: dict = {}
        self.vhr_data: dict[str, dict] = {}
        self.maintenance_data: dict[str, dict] = {}
        self.charge_schedule_data: dict[str, dict] = {}
        self.svla_data: dict[str, dict] = {}

        self.client = Client(
            email=config_entry.data.get(CONF_USERNAME),
            password=config_entry.data.get(CONF_PASSWORD),
            pin=self._read_pin(config_entry),
            brand=BRANDS_BY_NAME[BRANDS[config_entry.data[CONF_BRAND_REGION]]],
            disable_tls_verification=config_entry.data.get(
                CONF_DISABLE_TLS_VERIFICATION
            ),
        )

        self.refresh_interval: int = (
            config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL) * 60
        )

        self.charging_refresh_interval: int = self._read_charging_refresh_interval(
            config_entry
        )

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=self.refresh_interval),
            always_update=True,
        )

    @staticmethod
    def _read_pin(config_entry: ConfigEntry) -> str:
        """Return the PIN from the options, falling back to the setup value.

        An empty option means the field was left blank rather than cleared, so
        the PIN given during setup still applies. The API encodes the PIN
        without checking it, so an absent one is returned as an empty string.
        """

        return config_entry.options.get(CONF_PIN) or config_entry.data.get(CONF_PIN, "")

    @staticmethod
    def _read_charging_refresh_interval(config_entry: ConfigEntry) -> int:
        """Return the deep refresh interval while charging, in minutes.

        Zero disables the recurring refresh. Shorter intervals are raised to
        the floor because each deep refresh wakes the vehicle and holds an
        executor thread for as long as the command status poll takes.
        """

        minutes = config_entry.options.get(
            CONF_CHARGING_REFRESH_INTERVAL, DEFAULT_CHARGING_REFRESH_INTERVAL
        )

        if not isinstance(minutes, int) or minutes <= 0:
            return 0

        return max(minutes, MIN_CHARGING_REFRESH_INTERVAL)

    async def _async_update_data(self):
        """Update data via library. Called by update_coordinator periodically."""

        try:
            await self.hass.async_add_executor_job(self.client.refresh)
        except Exception as err:
            # On first run (no cached data), re-raise to signal setup failure
            if self.data is None and not self.client.vehicles:
                _LOGGER.error("Initial data fetch failed: %s", err)
                raise
            # On subsequent runs, log and fall back to cached data
            _LOGGER.warning("Update failed, falling back to cached data: %s", err)

        await self._async_update_extra_data()

        return True

    async def _async_update_extra_data(self) -> None:
        """Fetch extra data (VHR, maintenance) for all vehicles."""

        for vin in self.client.vehicles:
            try:
                self.vhr_data[vin] = await self.hass.async_add_executor_job(
                    self.client.get_vehicle_health_report, vin
                )
            except Exception as err:
                _LOGGER.debug("VHR not available for %s: %s", vin, err)

            try:
                self.maintenance_data[vin] = await self.hass.async_add_executor_job(
                    self.client.get_maintenance_history, vin
                )
            except Exception as err:
                _LOGGER.debug("Maintenance history not available for %s: %s", vin, err)

            try:
                self.charge_schedule_data[vin] = await self.hass.async_add_executor_job(
                    self.client.get_charge_schedules, vin
                )
            except Exception as err:
                _LOGGER.debug("Charge schedules not available for %s: %s", vin, err)

            try:
                self.svla_data[vin] = await self.hass.async_add_executor_job(
                    self.client.get_stolen_vehicle_status, vin
                )
            except Exception as err:
                _LOGGER.debug(
                    "Stolen vehicle status not available for %s: %s", vin, err
                )

    async def async_set_charge_schedule(self, vin: str, schedule: dict) -> None:
        """Set a charge schedule"""

        r = await self.hass.async_add_executor_job(
            self.client.set_charge_schedule_verify, vin, schedule
        )
        await self.async_refresh()

        if not r:
            raise HomeAssistantError("Set charge schedule failed")

    async def async_command(self, vin: str, cmd: Command) -> None:
        """Execute the given command"""

        r = await self.hass.async_add_executor_job(self.client.command_verify, vin, cmd)
        await self.async_refresh()

        if not r:
            raise HomeAssistantError("Command execution failed")

    async def async_set_charging_level(self, vin: str, level: str) -> None:
        """Set the charging level"""

        if level not in CHARGING_LEVELS_BY_NAME:
            raise ValueError(f"Invalid charging level: {level}")
        level = CHARGING_LEVELS_BY_NAME[level]

        r = await self.hass.async_add_executor_job(
            self.client.set_charging_level_verify, vin, level
        )
        await self.async_refresh()

        if not r:
            raise HomeAssistantError("Set charging level failed")

    async def update_options(self, hass: HomeAssistant, config_entry: ConfigEntry):
        self.update_interval = timedelta(
            seconds=config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            * 60
        )
        self.charging_refresh_interval = self._read_charging_refresh_interval(
            config_entry
        )
        self.client.set_pin(self._read_pin(config_entry))
