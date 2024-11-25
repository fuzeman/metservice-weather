"""The MetService NZ data coordinator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import urllib.parse
from typing import Any

import aiohttp
import async_timeout
from homeassistant.util import dt as dt_util

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.const import (
    PERCENTAGE,
    UnitOfPressure,
    UnitOfTemperature,
    UnitOfLength,
    UnitOfSpeed,
    UnitOfVolumetricFlux,
)

from .const import (
    LOCATION_REGIONS,
    SENSOR_MAP_MOBILE,
    SENSOR_MAP_PUBLIC,
    RESULTS_CURRENT,
    RESULTS_FIRE,
    RESULTS_FORECAST_DAILY,
    RESULTS_FORECAST_HOURLY,
    RESULTS_INTEGRATIONS,
)

_LOGGER = logging.getLogger(__name__)

MIN_TIME_BETWEEN_UPDATES = timedelta(minutes=20)


@dataclass
class WeatherUpdateCoordinatorConfig:
    """Class representing coordinator configuration."""

    api_url: str
    warnings_url: str
    api_key: str
    api_type: str
    unit_system_api: str
    unit_system: str
    location: str
    location_name: str
    latitude: str
    longitude: str
    enable_tides: bool
    tide_url: str
    update_interval = MIN_TIME_BETWEEN_UPDATES


class WeatherUpdateCoordinator(DataUpdateCoordinator):
    """The MetService update coordinator."""

    def __init__(
        self, hass: HomeAssistant, config: WeatherUpdateCoordinatorConfig
    ) -> None:
        """Initialize."""
        self._hass = hass
        self._api_url = config.api_url
        self._warnings_url = config.warnings_url
        self._api_key = config.api_key
        self._api_type = config.api_type
        self._location = WeatherUpdateCoordinator._format_location(config.location)
        self._location_name = config.location_name
        self._latitude = config.latitude
        self._longitude = config.longitude
        self._enable_tides = config.enable_tides
        self._tide_url = config.tide_url
        self._unit_system_api = config.unit_system_api
        self.unit_system = config.unit_system
        self.data = None
        self._session = async_get_clientsession(self._hass)
        self.units_of_measurement = (
            UnitOfTemperature.CELSIUS,
            UnitOfLength.MILLIMETERS,
            UnitOfLength.METERS,
            UnitOfSpeed.KILOMETERS_PER_HOUR,
            UnitOfPressure.MBAR,
            UnitOfVolumetricFlux.MILLIMETERS_PER_HOUR,
            PERCENTAGE,
        )

        super().__init__(
            hass,
            _LOGGER,
            name="WeatherUpdateCoordinator",
            update_interval=config.update_interval,
        )

    @property
    def location(self):
        """Return the location used for data."""
        return self._location

    @property
    def location_name(self):
        """Return the entity name prefix."""
        return self._location_name

    @property
    def api_type(self):
        """Return the API type."""
        return self._api_type

    @property
    def enable_tides(self):
        """Return if tides data is enabled."""
        return self._enable_tides

    @property
    def tide_url(self):
        """Return the tide URL."""
        return self._tide_url

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from API."""
        if self._api_type == "public":
            return await self.get_public_weather()
        else:
            return await self.get_mobile_weather()

    async def get_mobile_weather(self):
        """Get weather data from mobile API."""
        headers = {
            "Accept": "*/*",
            "User-Agent": "MetServiceNZ/2.19.3 (com.metservice.iphoneapp; build:332; iOS 17.1.1) Alamofire/5.4.3",
            "Accept-Language": "en-CA;q=1.0",
            "Accept-Encoding": "br;q=1.0, gzip;q=0.9, deflate;q=0.8",
            "Connection": "keep-alive",
            "apiKey": self._api_key
        }
        try:
            async with async_timeout.timeout(10):
                url = f"{self._api_url}/{self._latitude}/{self._longitude}"
                response = await self._session.get(url, headers=headers)
                result_current = await response.json(content_type=None)
                if result_current is None:
                    raise ValueError("No current weather data received.")
                self._check_errors(url, result_current)
            warnings_text = '\n'.join([
                f"{warning['name']}, {warning['markdown']}"
                for warning in result_current['result']['warnings'].get('previews', [])
            ]).replace('**', '').replace('#', '').replace('\n', ' ')
            async with async_timeout.timeout(10):
                url = f"{self._api_url}/locations/{self.location}/7-days"
                response = await self._session.get(url, headers=headers)
                result_daily = await response.json(content_type=None)
                if result_daily is None:
                    raise ValueError("No daily forecast data received.")
                self._check_errors(url, result_daily)
            result_current['weather_warnings'] = warnings_text
            result = {}
            if self._enable_tides:
                result_tides = await self.get_tides()
                result_current['tideImport'] = result_tides
                result = {
                    RESULTS_CURRENT: result_current,
                    RESULTS_FORECAST_DAILY: result_daily,
                }
            else:
                result = {
                    RESULTS_CURRENT: result_current,
                    RESULTS_FORECAST_DAILY: result_daily,
                }
            self.data = result
            return result

        except ValueError as err:
            _LOGGER.error("Data validation error: %s", err)
            raise UpdateFailed(f"Data validation error: {err}") from err
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("Error fetching MetService data: %s", repr(err))
            raise UpdateFailed(f"Error fetching MetService data: {err}") from err
        except Exception as err:
            _LOGGER.error("Unexpected error fetching MetService data: %s", repr(err))
            raise UpdateFailed(f"Unexpected error: {err}") from err
        # finally:
            # _LOGGER.info(f"MetService data updated: {self.data}")

    async def get_public_weather(self):
        """Get weather data from public API."""
        headers = {
            "Accept-Encoding": "gzip",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36",
        }
        try:
            async with async_timeout.timeout(10):
                url = f"{self._api_url}{self.location}"
                response = await self._session.get(url, headers=headers)
                page_location = await response.json(content_type=None)
                if page_location is None:
                    raise ValueError("No location data received.")
                self._check_errors(url, page_location)
            async with async_timeout.timeout(10):
                result_current = await self._fetch_module(page_location, 'current-conditions/1', headers=headers)
            async with async_timeout.timeout(10):
                result_forecast_hourly = await self._fetch_module(page_location, 'forty-eight-hour-forecast/1', headers=headers)
            async with async_timeout.timeout(10):
                url = f"{self._warnings_url}/{page_location['location']['type']}/{page_location['location']['key']}"
                response = await self._session.get(url, headers=headers)
                result_warnings = await response.json(content_type=None)
                if result_warnings is None:
                    raise ValueError("No warnings data received.")
                self._check_errors(url, result_warnings)
                warnings_text = '\n'.join([
                    f"{warning['name']}, {warning['text']}, {warning['threatPeriod']}"
                    for warning in result_warnings.get('warnings', [])
                ])
            async with async_timeout.timeout(10):
                url = f"{self._api_url}{self.location}/7-days"
                response = await self._session.get(url, headers=headers)
                page_daily_forecast = await response.json(content_type=None)
                if page_daily_forecast is None:
                    raise ValueError("No daily forecast data received.")
                self._check_errors(url, page_daily_forecast)
            async with async_timeout.timeout(10):
                result_forecast_daily = await self._fetch_module(page_daily_forecast, 'city-forecast/tabs/1', headers=headers)
            async with async_timeout.timeout(10):
                result_integrations = await self._fetch_module(page_location, 'integrations', headers=headers)
            async with async_timeout.timeout(10):
                result_fire = await self._fetch_module(page_location, 'city-forecast/tabs/1', headers=headers, field='fireWeatherDataUrl')
                if result_fire is not None and 'fireWeather' in result_fire:
                    result_fire = result_fire['fireWeather'][0]
                else:
                    result_fire = await self._fetch_module(page_location, 'city-forecast/tabs/1', headers=headers)
                    if result_fire is not None and 'days' in result_fire:
                        result_fire = result_fire['days'][0].get('fireWeatherData', {}).get('fireWeather')
            result_current['weather_warnings'] = warnings_text
            if self._enable_tides:
                result_tides = await self.get_tides()
                result_current['tideImport'] = result_tides
            result = {
                RESULTS_CURRENT: result_current,
                RESULTS_FIRE: result_fire,
                RESULTS_FORECAST_DAILY: result_forecast_daily,
                RESULTS_FORECAST_HOURLY: result_forecast_hourly,
                RESULTS_INTEGRATIONS: result_integrations,
            }
            self.data = result
            return result

        except ValueError as err:
            _LOGGER.error("Data validation error: %s", err)
            raise UpdateFailed(f"Data validation error: {err}") from err
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("Error fetching MetService data: %s", repr(err))
            raise UpdateFailed(f"Error fetching MetService data: {err}") from err
        except Exception as err:
            _LOGGER.error("Unexpected error fetching MetService data: %s", repr(err))
            raise UpdateFailed(f"Unexpected error: {err}") from err
        # finally:
        #     _LOGGER.info(f"MetService data updated: {self.data}")

    async def get_tides(self):
        """Get tides data."""
        headers = {
            "Accept-Encoding": "gzip",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36",
        }
        try:
            async with async_timeout.timeout(10):
                url = f"{self._tide_url}"
                response = await self._session.get(url, headers=headers)
                result_tides = await response.json(content_type=None)
                if result_tides is None:
                    raise ValueError("No tides data received.")
                self._check_errors(url, result_tides)

            tide_data = result_tides["layout"]["primary"]["slots"]["main"]["modules"][0]["tideData"]

            return tide_data

        except ValueError as err:
            _LOGGER.error("Data validation error in tides: %s", err)
            raise UpdateFailed(f"Data validation error in tides: {err}") from err
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("Error fetching tides data: %s", repr(err))
            raise UpdateFailed(f"Error fetching tides data: {err}") from err
        except Exception as err:
            _LOGGER.error("Unexpected error fetching tides data: %s", repr(err))
            raise UpdateFailed(f"Unexpected error in tides: {err}") from err
        # finally:
        #     _LOGGER.info(f"Tides data updated: {tide_data if 'tide_data' in locals() else 'No tides data'}")

    def _check_errors(self, url: str, response: dict):
        """Check for errors in the API response."""
        if "errors" not in response:
            return
        if errors := response["errors"]:
            error_messages = "; ".join([e["message"] for e in errors])
            raise ValueError(f"Error from {url}: {error_messages}")

    def get_from_dict(self, data_dict, map_list):
        """Recursively look for a given key path within a dictionary."""
        if not map_list:
            return data_dict
        if isinstance(data_dict, list):
            for idx, item in enumerate(data_dict):
                if map_list[0].isdigit() and idx == int(map_list[0]):
                    result = self.get_from_dict(item, map_list[1:])
                    if result is not None:
                        return result
                else:
                    result = self.get_from_dict(item, map_list)
                    if result is not None:
                        return result
        elif isinstance(data_dict, dict):
            for key, value in data_dict.items():
                if key == map_list[0]:
                    result = self.get_from_dict(value, map_list[1:])
                    if result is not None:
                        return result
                else:
                    result = self.get_from_dict(value, map_list)
                    if result is not None:
                        return result
        return None

    def get_current_public(self, field):
        """Get a specific key from the MetService returned data."""
        try:
            keys = SENSOR_MAP_PUBLIC[field].split(".")
            result = self.get_from_dict(self.data[RESULTS_CURRENT], keys)
            return result
        except Exception as e:
            _LOGGER.error(f"Error retrieving public sensor '{field}': {e}")
            return None  # Return a dummy value if an error occurs

    def get_current_mobile(self, field):
        """Get a specific key from the MetService returned data."""
        try:
            keys = SENSOR_MAP_MOBILE[field].split(".")
            result = self.get_from_dict(self.data[RESULTS_CURRENT], keys)
            return result
        except Exception as e:
            _LOGGER.error(f"Error retrieving mobile sensor '{field}': {e}")
            return None  # Return a dummy value if an error occurs

    def get_forecast_daily_public(self, field, day):
        """Get a specific key from the MetService returned data."""
        try:
            all_days = self.data[RESULTS_FORECAST_DAILY]["days"]
            if field == "":  # send a blank to get the number of days
                return len(all_days)
            this_day = all_days[day]
            keys = SENSOR_MAP_PUBLIC[field].split(".")
            result = self.get_from_dict(this_day, keys)
            return result
        except Exception as e:
            _LOGGER.error(f"Error retrieving public forecast daily sensor '{field}' for day {day}: {e}")
            return None

    def get_forecast_daily_mobile(self, field, day):
        """Get a specific key from the MetService returned data."""
        try:
            all_days = self.data["current"]["result"]["forecastData"]["days"]
            if field == "":  # send a blank to get the number of days
                return len(all_days)
            this_day = all_days[day]
            keys = SENSOR_MAP_MOBILE[field].split(".")
            result = self.get_from_dict(this_day, keys)
            return result
        except Exception as e:
            _LOGGER.error(f"Error retrieving mobile forecast daily sensor '{field}' for day {day}: {e}")
            return None

    def get_forecast_hourly_public(self, field):
        """Get a specific key from the MetService returned data."""
        try:
            keys = SENSOR_MAP_PUBLIC[field].split(".")
            result = self.get_from_dict(self.data[RESULTS_FORECAST_HOURLY], keys)
            return result
        except Exception as e:
            _LOGGER.error(f"Error retrieving public forecast hourly sensor '{field}' for day {day}: {e}")
            return None

    async def _fetch_module(self, obj, name, headers, field='dataUrl'):
        matches = list(self._find_object(
            obj,
            lambda x: x.get('type') == 'module' and x.get('name') == name
        ))
        if not matches or field not in matches[0]:
            return None

        url = urllib.parse.urljoin(self._api_url, matches[0][field])
        response = await self._session.get(url, headers=headers)
        result = await response.json(content_type=None)

        if result is None:
            return None

        self._check_errors(url, result)
        return result

    def _find_object(self, obj, where):
        if isinstance(obj, dict):
            if where(obj):
                yield obj

            for key, value in obj.items():
                for child in self._find_object(value, where):
                    yield child
        elif isinstance(obj, (list, set)) and not isinstance(obj, str):
            for index, value in enumerate(obj):
                for child in self._find_object(value, where):
                    yield child

    @classmethod
    def _format_location(cls, location):
        parts = location.split("/")
    
        if parts[2] == 'regions':
            return location

        return "/".join(parts[:2] + [
            'regions',
            LOCATION_REGIONS[parts[3]]
        ] + parts[2:])

    @classmethod
    def _format_timestamp(cls, timestamp_val):
        """Format timestamp to ISO format in UTC."""
        return datetime.fromisoformat(timestamp_val).astimezone(dt_util.get_time_zone("UTC")).isoformat()
