"""Platform for sensor integration."""

# from __future__ import annotations
import logging
from datetime import datetime
from datetime import timedelta

import homeassistant.components.recorder.util as recorder_util

try:
    from homeassistant.components.recorder.const import (
        DATA_INSTANCE as RECORDER_DATA_INSTANCE,
    )
except ImportError:  # NEW Home Assistant 2024.08
    from homeassistant.helpers.recorder import (
        DATA_INSTANCE as RECORDER_DATA_INSTANCE,
    )
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.components.recorder.statistics import clear_statistics
from homeassistant.components.recorder.statistics import list_statistic_ids
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import CONF_PASSWORD
from homeassistant.const import CONF_STATE
from homeassistant.const import CONF_TOKEN
from homeassistant.const import CONF_USERNAME
from homeassistant.const import EVENT_HOMEASSISTANT_START
from homeassistant.const import UnitOfVolume
from homeassistant.core import callback
from homeassistant.core import CoreState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.update_coordinator import TimestampDataUpdateCoordinator

from .api import AiguesApiClient
from .const import API_ERROR_TOKEN_REVOKED
from .const import ATTR_LAST_MEASURE
from .const import CONF_CONTRACT
from .const import CONF_VALUE
from .const import DEFAULT_SCAN_PERIOD
from .const import DOMAIN
from .const import CONF_COMPANY_IDENTIFICATOR  # Add this

from typing import Optional

_LOGGER = logging.getLogger(__name__)


def get_db_instance(hass):
    """Workaround for older HA versions."""
    try:
        return recorder_util.get_instance(hass)
    except AttributeError:
        return hass


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities):
    """Set up entry."""
    hass.data.setdefault(DOMAIN, {})

    _LOGGER.info("calling async_setup_entry")

    username = config_entry.data[CONF_USERNAME]
    password = config_entry.data[CONF_PASSWORD]
    contracts = config_entry.data[CONF_CONTRACT]
    token = config_entry.data.get(CONF_TOKEN)
    company_identification = config_entry.data.get(CONF_COMPANY_IDENTIFICATOR)

    contadores = list()

    for contract in contracts:
        coordinator = ContratoAgua(
            hass,
            username,
            password,
            contract,
            token=token,
            company_identification=company_identification,
        )
        contadores.append(ContadorAgua(coordinator))

    # postpone first refresh to speed up startup
    @callback
    async def async_first_refresh(*args):
        for sensor in contadores:
            await sensor.coordinator.async_refresh()

    # ------

    if hass.state == CoreState.running:
        await async_first_refresh()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, async_first_refresh)

    _LOGGER.info("about to add entities")
    async_add_entities(contadores)

    return True


class ContratoAgua(TimestampDataUpdateCoordinator):
    def __init__(
        self,
        hass: HomeAssistant,
        username: str,
        password: str,
        contract: str,
        token: str = None,
        prev_data=None,
        company_identification=None,
    ) -> None:
        """Initialize the data handler."""
        self.reset = prev_data is None

        self.contract = contract.upper()
        self.id = contract.lower()
        self.internal_sensor_id = f"sensor.contador_{self.id}"

        if not hass.data[DOMAIN].get(self.contract):
            # init data shared store
            hass.data[DOMAIN][self.contract] = {}

        # create alias
        self._data = hass.data[DOMAIN][self.contract]

        # WARN define a pointer to this object
        hass.data[DOMAIN][self.contract]["coordinator"] = self

        # the api object
        self._api = AiguesApiClient(
            username, password, contract, company_identification=company_identification
        )
        if token:
            self._api.set_token(token)

        super().__init__(
            hass,
            _LOGGER,
            name=self.id,
            update_interval=timedelta(seconds=DEFAULT_SCAN_PERIOD),
        )

    def __repr__(self):
        return f"<{self.__class__.__name__} {self.contract}>"

    async def _async_update_data(self):
        _LOGGER.info(f"Updating coordinator data for {self.contract}")
        TODAY = datetime.now()
        LAST_WEEK = TODAY - timedelta(days=7)
        LAST_TIME_DAYS = None

        # last_measurement = await self.get_last_measurement_stored()
        # _LOGGER.info("Last stored measurement: %s", last_measurement)

        try:
            previous = datetime.fromisoformat(self._data.get(CONF_STATE, ""))
            # FIX: TypeError: can't subtract offset-naive and offset-aware datetimes
            previous = previous.replace(tzinfo=None)
            if previous:
                LAST_TIME_DAYS = (TODAY - previous).days
        except ValueError:
            previous = None

        if previous and (TODAY - previous) <= timedelta(minutes=60):
            _LOGGER.warning("Skipping request update data - too early")
            return

        consumptions = None
        try:
            if self._api.is_token_expired():
                raise ConfigEntryAuthFailed
            # TODO: change once recaptcha is fiexd
            # await self.hass.async_add_executor_job(self._api.login)
            consumptions = await self.hass.async_add_executor_job(
                self._api.consumptions, LAST_WEEK, TODAY, self.contract
            )
        except ConfigEntryAuthFailed as exp:
            _LOGGER.error("Token has expired, cannot check consumptions.")
            raise ConfigEntryAuthFailed from exp
        except Exception as exp:
            self.async_set_update_error(exp)
            if API_ERROR_TOKEN_REVOKED in str(exp):
                raise ConfigEntryAuthFailed from exp

        if not consumptions:
            _LOGGER.error("No consumptions available")
            return False

        self._data["consumptions"] = consumptions

        # get last entry - most updated
        metric = consumptions[-1]
        self._data[CONF_VALUE] = metric["accumulatedConsumption"]
        self._data[CONF_STATE] = metric["datetime"]

        # await self._clear_statistics()
        try:
            await self._async_import_statistics(consumptions)
        except:
            pass

        if LAST_TIME_DAYS and LAST_TIME_DAYS >= 7:
            await self.import_old_consumptions(days=LAST_TIME_DAYS)

        return True

    async def _clear_statistics(self) -> None:
        all_ids = await get_db_instance(self.hass).async_add_executor_job(
            list_statistic_ids, self.hass
        )
        to_clear = [
            x["statistic_id"]
            for x in all_ids
            if x["statistic_id"].startswith(self.internal_sensor_id)
        ]

        if to_clear:
            _LOGGER.warn(
                f"About to delete {len(to_clear)} entries from {self.contract}"
            )
            # NOTE: This does not seem to work?
            await get_db_instance(self.hass).async_add_executor_job(
                clear_statistics, self.hass.data[RECORDER_DATA_INSTANCE], to_clear
            )

    async def get_last_measurement_stored(self) -> Optional[datetime]:
        last_stored = None

        all_ids = await get_db_instance(self.hass).async_add_executor_job(
            list_statistic_ids, self.hass
        )

        for stat_id in all_ids:
            if stat_id["statistic_id"] == self.internal_sensor_id:
                if stat_id.get("sum") and stat_id["sum"] > last_stored["sum"]:
                    last_stored = stat_id

        if last_stored:
            _LOGGER.debug(f"Found last stored value: {last_stored}")
            return datetime.fromtimestamp(last_stored.get("start_ts"))

        return None

    async def _async_import_statistics(self, consumptions) -> None:
        # force sort by datetime
        consumptions = sorted(
            consumptions, key=lambda x: datetime.fromisoformat(x["datetime"])
        )

        stats = list()
        for metric in consumptions:
            start_ts = datetime.fromisoformat(metric["datetime"])
            start_ts = start_ts.replace(minute=0, second=0, microsecond=0)  # required

            # round: fixes decimal with 20 digits precision
            state = round(metric["accumulatedConsumption"], 4)
            stats.append(
                {
                    "start": start_ts,
                    "state": state,
                    # -- required to show in historic/recorder
                    # -- incremental sum = current total value, so we don't show negative values in HA
                    "sum": state,
                    # "last_reset": start_ts,
                }
            )
        metadata = {
            "has_mean": False,
            "has_sum": True,
            "name": None,
            "source": "recorder",  # required
            "statistic_id": self.internal_sensor_id,
            "unit_of_measurement": UnitOfVolume.CUBIC_METERS,
        }
        # _LOGGER.debug(f"Adding metric: {metadata} {stats}")
        async_import_statistics(self.hass, metadata, stats)

    async def clear_all_stored_data(self) -> None:
        await self._clear_statistics()

    async def import_old_consumptions(self, days: int = 365) -> None:
        today = datetime.now()
        one_year_ago = today - timedelta(days=days)

        if self._api.is_token_expired():
            raise ConfigEntryAuthFailed

        current_date = one_year_ago
        while current_date < today:
            consumptions = await self.hass.async_add_executor_job(
                self._api.consumptions_week, current_date, self.contract
            )

            if consumptions:
                await self._async_import_statistics(consumptions)
            else:
                _LOGGER.warning(f"No data available for {current_date}")

            current_date += timedelta(weeks=1)


class ContadorAgua(CoordinatorEntity, SensorEntity):
    """Representation of a sensor."""

    def __init__(self, coordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_name = f"Contador {coordinator.id}"
        self._attr_unique_id = coordinator.id
        self._attr_icon = "mdi:water-pump"
        self._attr_has_entity_name = True
        self._attr_should_poll = False
        self._attr_device_class = SensorDeviceClass.WATER
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS

    @property
    def native_value(self):
        return self.coordinator._data.get(CONF_VALUE, None)

    @property
    def last_measurement(self):
        try:
            last_measure = datetime.fromisoformat(
                self.coordinator._data.get(CONF_STATE, "")
            )
        except ValueError:
            last_measure = None
        return last_measure

    @property
    def extra_state_attributes(self):
        attrs = {ATTR_LAST_MEASURE: self.last_measurement}
        return attrs
