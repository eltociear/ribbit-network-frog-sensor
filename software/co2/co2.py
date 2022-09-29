# The MIT License (MIT)
#
# Copyright (c) 2021 Keenan Johnson
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Mapping, Optional

import adafruit_gps  # type: ignore
import adafruit_scd30  # type: ignore
import gpsd  # type: ignore
from adafruit_dps310.advanced import DPS310  # type: ignore
from adafruit_extended_bus import ExtendedI2C as I2C  # type: ignore
from influxdb_client import InfluxDBClient, Point, WritePrecision  # type: ignore
from influxdb_client.client.write_api import SYNCHRONOUS  # type: ignore

BUCKET = "co2"
ORG = "keenan.johnson@gmail.com"
DEVICE_UUID = os.getenv("BALENA_DEVICE_UUID")
RESIN_DEVICE_TYPE = os.getenv("RESIN_DEVICE_TYPE")
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "0.5"))

LOGGER = logging.getLogger(__name__)

ENABLE_INFLUXDB = os.getenv("ENABLE_INFLUXDB", "true") in [
    "true",
    "True",
    "1",
    "yes",
    "Yes",
]

DEFAULT_I2C_BUS_ID = 11
I2C_BUS_ID_MAP: Mapping[Optional[str], int] = {"beaglebone-green-gateway": 2}


class GpsSourceType(Enum):
    DUMMY = "dummy"
    GPSD = "gpsd"
    I2C = "i2c"


DEFAULT_GPS_SOURCE = GpsSourceType.GPSD
GPS_SOURCE_MAP: Mapping[Optional[str], GpsSourceType] = {
    "beaglebone-green-gateway": GpsSourceType.I2C,
    "raspberrypicm4-ioboard": GpsSourceType.I2C
}
GPS_DIGITS_PRECISION = int(os.getenv("GPS_DIGITS_PRECISION", "2"))
GPS_FIX_MAX_AGE = timedelta(seconds=int(os.getenv("GPS_FIX_MAX_AGE_SECONDS", "600")))


@dataclass
class GpsData:
    """Uniform data type to be returned by GPS implementations"""

    latitude: float
    longitude: float
    altitude: Optional[float]
    acquired_at: datetime = field(default_factory=datetime.now)


class BaseGps(ABC):  # pylint: disable=too-few-public-methods
    """Base class for GPS implementations"""

    def __init__(self) -> None:
        self._last_fix: Optional[GpsData] = None

    @abstractmethod
    def _get_data(self) -> GpsData:
        raise NotImplementedError()

    def get_data(self) -> GpsData:
        """Get the current GPS data. Raises if no data is available for any reason."""
        try:
            self._last_fix = self._get_data()
        except Exception as ex:  # pylint: disable=broad-except; gpsd actually throws an Exception
            if self._last_fix is not None:
                age = datetime.now() - self._last_fix.acquired_at
                if age < GPS_FIX_MAX_AGE:
                    LOGGER.warning(
                        "Failed to fetch GPS data, using last known fix (age: %s)", age
                    )
                else:
                    raise Exception(
                        f"Last fix is too old ({age} > {GPS_FIX_MAX_AGE})"
                    ) from ex
        if self._last_fix is None:
            raise Exception("Waiting for first fix")
        return self._last_fix


class GpsdGps(BaseGps):  # pylint: disable=too-few-public-methods
    """Read GPS data from the GPSD daemon"""

    def __init__(self):
        super().__init__()
        self._is_valid = False

    def _connect(self) -> None:
        try:
            gpsd.connect()
        except Exception:
            self._is_valid = False
            raise
        finally:
            self._is_valid = True

    def _get_data(self) -> GpsData:
        if not self._is_valid:
            self._connect()
        try:
            packet = gpsd.get_current()
            # Read GPS Data
            # See https://github.com/Ribbit-Network/ribbit-network-frog-sensor/issues/41
            # for more information about the rounding of the coordinates
            return GpsData(
                latitude=round(packet.position()[0], GPS_DIGITS_PRECISION),
                longitude=round(packet.position()[1], GPS_DIGITS_PRECISION),
                altitude=packet.altitude(),
            )
        except Exception:
            self._is_valid = False
            raise


class I2CGps(BaseGps):  # pylint: disable=too-few-public-methods
    """Read GPS data from from I2C via the Adafruit GPS library"""

    def __init__(self, i2c: I2C) -> None:
        super().__init__()
        self._i2c = i2c
        self._gps = adafruit_gps.GPS_GtopI2C(i2c)

        # Initialize what data to receive. As seen on
        # https://circuitpython.readthedocs.io/projects/gps/en/latest/examples.html
        self._gps.send_command(b"PMTK314,0,1,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0")

        # Set the update rate to twice our polling interval
        gps_poll_interval = round(POLL_INTERVAL_SECONDS * 2 * 1000)
        self._gps.send_command(f"PMTK220,{gps_poll_interval}".encode("ascii"))

    def _get_data(self) -> GpsData:
        self._gps.update()
        if not self._gps.has_fix:
            raise Exception("No GPS Fix")
        return GpsData(
            latitude=round(self._gps.latitude, GPS_DIGITS_PRECISION),
            longitude=round(self._gps.longitude, GPS_DIGITS_PRECISION),
            altitude=self._gps.altitude_m,
        )


class DummyGps(BaseGps):  # pylint: disable=too-few-public-methods
    """Always return a static fix. Useful for testing without a GPS."""

    def __init__(self) -> None:
        super().__init__()
        self._latitude = float(os.getenv("DUMMY_GPS_LATITUDE", "0"))
        self._longitude = float(os.getenv("DUMMY_GPS_LONGITUDE", "0"))
        self._altitude = float(os.getenv("DUMMY_GPS_ALTITUDE", "0"))

    def _get_data(self) -> GpsData:
        return GpsData(
            latitude=self._latitude, longitude=self._longitude, altitude=self._altitude
        )


def get_i2c_bus_id() -> int:
    override = os.getenv("I2C_BUS_ID")
    if override is not None:
        logging.info("Using I2C bus override: %s", override)
        return int(override)

    retval = I2C_BUS_ID_MAP.get(RESIN_DEVICE_TYPE, DEFAULT_I2C_BUS_ID)
    logging.info("I2C bus for device type %s: %s", RESIN_DEVICE_TYPE, retval)
    return retval


def get_gps_source() -> GpsSourceType:
    override = os.getenv("GPS_SOURCE")
    if override is not None:
        logging.info("Using GPS source override: %s", override)
        return GpsSourceType(override)

    retval = GPS_SOURCE_MAP.get(RESIN_DEVICE_TYPE, DEFAULT_GPS_SOURCE)
    logging.info("GPS source for device type %s: %s", RESIN_DEVICE_TYPE, retval)
    return retval


def main() -> None:  # pylint: disable=missing-function-docstring
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO
    )

    if ENABLE_INFLUXDB:
        #
        # config ini file
        #
        # [influx2]
        # url=http://localhost:8086
        # org=my-org
        # token=my-token
        # timeout=6000
        # verify_ssl=False
        #
        client = InfluxDBClient.from_config_file("influx_config.ini")
        write_api = client.write_api(write_options=SYNCHRONOUS)

    i2c_bus = I2C(get_i2c_bus_id())
    scd = adafruit_scd30.SCD30(i2c_bus)
    time.sleep(1)
    scd.ambient_pressure = 1007
    dps310 = DPS310(i2c_bus)

    gps: BaseGps
    gps_source = get_gps_source()
    if gps_source == GpsSourceType.GPSD:
        gps = GpsdGps()
    elif gps_source == GpsSourceType.I2C:
        gps = I2CGps(i2c_bus)
    elif gps_source == GpsSourceType.DUMMY:
        gps = DummyGps()
    else:
        raise ValueError(f"Unknown GPS Source: {gps_source}")

    # Enable self calibration mode
    scd.temperature_offset = 4.0
    scd.altitude = 0
    scd.self_calibration_enabled = True

    while True:
        # Sleep at the top of the loop allows calling `continue` anywhere to skip this
        # cycle, without entering a busy loop
        time.sleep(POLL_INTERVAL_SECONDS)

        # since the measurement interval is long (2+ seconds) we check for new data
        # before reading the values, to ensure current readings.
        if not scd.data_available:
            continue

        try:
            gps_data = gps.get_data()
        except Exception:  # pylint: disable=broad-except; gpsd actually throws an Exception
            # Unable to get current GPS position
            # log error and attempt to reconnect GPS
            # TODO: log GPS failures to database?
            logging.exception("Error getting current GPS position")
            continue

        # Set SCD Pressure from Barometer
        #
        # See Section 1.4.1 in SCD30 Interface Guide
        # https://www.sensirion.com/fileadmin/user_upload/customers/sensirion/Dokumente/9.5_CO2/Sensirion_CO2_Sensors_SCD30_Interface_Description.pdf
        #
        # The Reference pressure must be greater than 0.
        if dps310.pressure > 0:
            scd.ambient_pressure = dps310.pressure

        # Publish to Influx DB Cloud
        if ENABLE_INFLUXDB:
            point = (
                Point("ghg_point")
                .tag("host", DEVICE_UUID)
                .field("co2", scd.CO2)
                .time(datetime.utcnow(), WritePrecision.NS)
                .field("temperature", scd.temperature)
                .field("humidity", scd.relative_humidity)
                .field("lat", gps_data.latitude)
                .field("lon", gps_data.longitude)
                .field("alt", gps_data.altitude)
                .field("baro_pressure", dps310.pressure)
                .field("baro_temperature", dps310.temperature)
                .field("scd30_pressure_mbar", scd.ambient_pressure)
                .field("scd30_altitude_m", scd.altitude)
            )

            write_api.write(BUCKET, ORG, point)

        # Publish to Local MQTT Broker
        data = {}
        data["CO2"] = scd.CO2
        data["Temperature"] = scd.temperature
        data["Relative_Humidity"] = scd.relative_humidity
        data["Latitude"] = gps_data.latitude
        data["Longitude"] = gps_data.longitude
        data["Altitude"] = gps_data.altitude
        data["scd_temp_offset"] = scd.temperature_offset
        data["baro_temp"] = dps310.temperature
        data["baro_pressure_hpa"] = dps310.pressure
        data["scd30_pressure_mbar"] = scd.ambient_pressure
        data["scd30_alt_m"] = scd.altitude

        logging.info(json.dumps(data))


if __name__ == "__main__":
    main()
