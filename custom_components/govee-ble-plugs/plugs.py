import asyncio
import dataclasses
import logging
import queue
import typing as T

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak_retry_connector import establish_connection

from homeassistant.exceptions import ConfigEntryError

_LOGGER: logging.Logger = logging.getLogger(__package__)


@dataclasses.dataclass
class GoveePowerData:
    """Power monitoring data from Govee device."""

    time_on: int | None = None  # seconds device has been on
    energy: float | None = None  # Wh accumulated
    voltage: float | None = None  # V
    current: float | None = None  # A
    power: float | None = None  # W
    power_factor: int | None = None  # percentage


def _b(s: str):
    return bytes(bytearray.fromhex(s))


def _sign_payload(data):
    checksum = 0
    for b in data:
        checksum ^= b
    return checksum & 0xFF


class GoveePlugApi(T.Protocol):
    MODEL: T.Final[str]

    def __init__(self, device: BLEDevice, token: str) -> None: ...

    def port_names(self) -> T.List[T.Tuple[T.Optional[int], T.Optional[str]]]: ...

    def is_on(self, port: int) -> bool | None: ...

    def handle_bluetooth_event(self, device: BLEDevice, adv: AdvertisementData): ...

    async def async_turn_on(self, port: int): ...

    async def async_turn_off(self, port: int): ...

    def supports_power_monitoring(self) -> bool: ...

    def get_power_data(self) -> GoveePowerData | None: ...

    async def async_request_power_data(self) -> bool: ...


class GoveePairApi(T.Protocol):

    async def begin(self): ...

    async def finish(self) -> str | None: ...


def get_api_by_model(model: str, device: BLEDevice, token: str) -> GoveePlugApi:
    if model == "H5080":
        return GoveePlugH5080(device, token)

    if model == "H5082":
        return GoveePlugH5082(device, token)

    if model == "H5086":
        return GoveePlugH5086(device, token)

    raise ConfigEntryError(f"Unsupported model {model}")


def get_pair_by_model(model: str, device: BLEDevice) -> GoveePairApi:
    if model == "H5080":
        return GoveePlugPairer(
            device,
            GoveePlugH5080.RECV_CHARACTERISTIC_UUID,
            GoveePlugH5080.SEND_CHARACTERISTIC_UUID,
            GoveePlugH5080.MSG_GET_AUTH_KEY,
        )

    if model == "H5082":
        return GoveePlugPairer(
            device,
            GoveePlugH5082.RECV_CHARACTERISTIC_UUID,
            GoveePlugH5082.SEND_CHARACTERISTIC_UUID,
            GoveePlugH5082.MSG_GET_AUTH_KEY,
        )

    if model == "H5086":
        return GoveePlugPairer(
            device,
            GoveePlugH5086.RECV_CHARACTERISTIC_UUID,
            GoveePlugH5086.SEND_CHARACTERISTIC_UUID,
            GoveePlugH5086.MSG_GET_AUTH_KEY,
        )

    raise ConfigEntryError(f"Unsupported model {model}")


@dataclasses.dataclass
class GoveeAdvertisementData:
    name: str
    address: str
    device: BLEDevice
    model: str


def parse_advertisement_data(
    device: BLEDevice, adv: AdvertisementData
) -> GoveeAdvertisementData | None:
    local_name = adv.local_name
    if not local_name:
        return

    if local_name.startswith("ihoment_H5080_"):
        return GoveeAdvertisementData(
            local_name, device.address, device, GoveePlugH5080.MODEL
        )

    if local_name.startswith("ihoment_H5082_"):
        return GoveeAdvertisementData(
            local_name, device.address, device, GoveePlugH5082.MODEL
        )

    if local_name.startswith("GVH5086"):
        return GoveeAdvertisementData(
            local_name, device.address, device, GoveePlugH5086.MODEL
        )


class GoveePlugH508x:

    def __init__(
        self,
        device: BLEDevice,
        token: str,
        RECV_CHARACTERISTIC_UUID: str,
        SEND_CHARACTERISTIC_UUID: str,
    ) -> None:
        self._device = device
        self._token = token
        self._RECV_CHARACTERISTIC_UUID = RECV_CHARACTERISTIC_UUID
        self._SEND_CHARACTERISTIC_UUID = SEND_CHARACTERISTIC_UUID

        self._connection_task: T.Optional[asyncio.Task] = None
        self._msgqueue = asyncio.Queue[T.Tuple[bytes, asyncio.Future[bool]]]()

    def supports_power_monitoring(self) -> bool:
        """Return True if this device supports power monitoring."""
        return False

    def get_power_data(self) -> GoveePowerData | None:
        """Return current power data, or None if not supported."""
        return None

    async def async_request_power_data(self) -> bool:
        """Request updated power data from device. Returns True on success."""
        return False

    async def _send_message(self, msg: bytes) -> bool:
        f = asyncio.Future[bool]()
        self._msgqueue.put_nowait((msg, f))
        self._ensure_message_task()
        return await f

    def _ensure_message_task(self):
        if not self._connection_task:
            self._connection_task = asyncio.create_task(self._message_task_fn())
            self._connection_task.add_done_callback(self._message_task_done)

    def _message_task_done(self, task: asyncio.Task):
        try:
            task.result()
        except Exception:
            # if this failed, it was logged or failed while disconnecting
            pass

        if self._connection_task is task:
            self._connection_task = None

        if self._connection_task is None and not self._msgqueue.empty():
            self._ensure_message_task()

    async def _message_task_fn(self):
        client = None
        must_process = queue.Queue[T.Tuple[bytes, asyncio.Future]]()

        try:
            # Pull anything on the message queue directly off, these must
            # be processed one way or another
            while not self._msgqueue.empty():
                must_process.put(self._msgqueue.get_nowait())

            client = await establish_connection(
                BleakClient,
                self._device,
                f"{self._device.name} ({self._device.address})",
            )

            # events to control execution flow
            on_auth_ready = asyncio.Event()
            on_set_state_ready = asyncio.Event()

            async def recv_handler(c, data):
                if data[0] == 0x33 and data[1] == 0xB2:
                    on_auth_ready.set()
                elif data[0] == 0x33 and data[1] == 0x01:
                    on_set_state_ready.set()

            await client.start_notify(self._RECV_CHARACTERISTIC_UUID, recv_handler)

            ba = bytearray([0x33, 0xB2]) + bytearray.fromhex(self._token).ljust(
                17, b"\0"
            )
            ba.append(_sign_payload(ba))
            await client.write_gatt_char(self._SEND_CHARACTERISTIC_UUID, ba)
            await on_auth_ready.wait()

            #
            # Send messages after authentication occurs
            #

            async def _send_msg(msg: bytes, f: asyncio.Future):
                try:
                    await client.write_gatt_char(self._SEND_CHARACTERISTIC_UUID, msg)
                    await on_set_state_ready.wait()
                except Exception:
                    f.set_result(False)
                    raise
                else:
                    f.set_result(True)

            # Process must process entries first
            while not must_process.empty():
                msg, f = must_process.get_nowait()
                await _send_msg(msg, f)

            # Then process anything else that might be in the queue
            while True:
                try:
                    msg, f = await asyncio.wait_for(self._msgqueue.get(), timeout=1)
                except TimeoutError:
                    break
                else:
                    await _send_msg(msg, f)

            await client.stop_notify(self._RECV_CHARACTERISTIC_UUID)

        except Exception as e:
            _LOGGER.error("failed to set state: %s", e)
        finally:
            # We only force clearing the must process queue. Anything that
            # was queued while the connection was failing deserves another try
            # and will be requeued when this task's done callback is called
            while not must_process.empty():
                _, f = must_process.get_nowait()
                f.set_result(False)

            if client is not None:
                await client.disconnect()


class GoveePlugH5080(GoveePlugH508x):
    MODEL = "H5080"

    MSG_GET_AUTH_KEY = _b("aab100000000000000000000000000000000001b")
    MSG_TURN_ON = _b("3301ff00000000000000000000000000000000cd")
    MSG_TURN_OFF = _b("3301f000000000000000000000000000000000c2")

    SEND_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
    RECV_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"

    def __init__(self, device: BLEDevice, token: str) -> None:
        super().__init__(
            device, token, self.RECV_CHARACTERISTIC_UUID, self.SEND_CHARACTERISTIC_UUID
        )
        self._is_on = None

    def port_names(self) -> T.List[T.Tuple[T.Optional[int], T.Optional[str]]]:
        return [(None, None)]

    def is_on(self, port: int):
        return self._is_on

    def handle_bluetooth_event(self, device: BLEDevice, adv: AdvertisementData):
        for _, mfr_data in adv.manufacturer_data.items():
            self._device = device
            self._is_on = mfr_data[-1] == 0x01

    async def async_turn_on(self, port: int):
        assert port == 0
        if await self._send_message(self.MSG_TURN_ON):
            self._is_on = True

    async def async_turn_off(self, port: int):
        assert port == 0
        if await self._send_message(self.MSG_TURN_OFF):
            self._is_on = False


class GoveePlugH5082(GoveePlugH508x):
    MODEL = "H5082"

    MSG_GET_AUTH_KEY = _b("aab100000000000000000000000000000000001b")

    MSG_LEFT_ON = _b("3301220000000000000000000000000000000010")
    MSG_LEFT_OFF = _b("3301200000000000000000000000000000000012")
    MSG_RIGHT_ON = _b("3301110000000000000000000000000000000023")
    MSG_RIGHT_OFF = _b("3301100000000000000000000000000000000022")

    SEND_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
    RECV_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"

    def __init__(self, device: BLEDevice, token: str) -> None:
        super().__init__(
            device, token, self.RECV_CHARACTERISTIC_UUID, self.SEND_CHARACTERISTIC_UUID
        )
        self._is_on: T.List[T.Optional[bool]] = [None, None]

    def port_names(self) -> T.List[T.Tuple[T.Optional[int], T.Optional[str]]]:
        return [(0, "Left Power"), (1, "Right Power")]

    def is_on(self, port: int):
        return self._is_on[port]

    def handle_bluetooth_event(self, device: BLEDevice, adv: AdvertisementData):
        for _, mfr_data in adv.manufacturer_data.items():
            self._device = device
            self._is_on[0] = (mfr_data[-1] & 0x2) == 0x2
            self._is_on[1] = (mfr_data[-1] & 0x1) == 0x1

    async def async_turn_on(self, port: int):
        if port == 0:
            msg = self.MSG_LEFT_ON
        elif port == 1:
            msg = self.MSG_RIGHT_ON
        else:
            assert False

        if await self._send_message(msg):
            self._is_on[port] = True

    async def async_turn_off(self, port: int):
        if port == 0:
            msg = self.MSG_LEFT_OFF
        elif port == 1:
            msg = self.MSG_RIGHT_OFF
        else:
            assert False

        if await self._send_message(msg):
            self._is_on[port] = False


class GoveePlugH5086(GoveePlugH508x):
    MODEL = "H5086"

    MSG_GET_AUTH_KEY = _b("aab100000000000000000000000000000000001b")
    MSG_TURN_ON = _b("3301010000000000000000000000000000000033")
    MSG_TURN_OFF = _b("3301000000000000000000000000000000000032")
    # Power monitoring request command: aa00 + 17 zero bytes + checksum (aa)
    MSG_GET_POWER = _b("aa000000000000000000000000000000000000aa")

    SEND_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
    RECV_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"

    def __init__(self, device: BLEDevice, token: str) -> None:
        super().__init__(
            device, token, self.RECV_CHARACTERISTIC_UUID, self.SEND_CHARACTERISTIC_UUID
        )
        self._is_on = None
        self._power_data = GoveePowerData()

    def port_names(self) -> T.List[T.Tuple[T.Optional[int], T.Optional[str]]]:
        return [(None, None)]

    def is_on(self, port: int):
        return self._is_on

    def supports_power_monitoring(self) -> bool:
        """H5086 supports power monitoring."""
        return True

    def get_power_data(self) -> GoveePowerData | None:
        """Return current power data."""
        return self._power_data

    def handle_bluetooth_event(self, device: BLEDevice, adv: AdvertisementData):
        for _, mfr_data in adv.manufacturer_data.items():
            self._device = device
            self._is_on = mfr_data[-1] == 0x01

    def _parse_power_response(self, data: bytes) -> bool:
        """Parse ee19 power data response.

        Format: ee19 [time:3] [energy:3] [voltage:2] [current:2] [power:3] [factor:1]
        - time: 3 bytes, seconds device has been on
        - energy: 3 bytes, in 1/10 Wh units
        - voltage: 2 bytes, in 1/100 V units
        - current: 2 bytes, in 1/100 A units
        - power: 3 bytes, in 1/100 W units
        - power_factor: 1 byte, percentage
        """
        if len(data) < 16 or data[0] != 0xEE or data[1] != 0x19:
            return False

        # Parse time (3 bytes, big-endian)
        time_on = (data[2] << 16) | (data[3] << 8) | data[4]

        # Parse energy (3 bytes, big-endian, 1/10 Wh -> Wh)
        energy_raw = (data[5] << 16) | (data[6] << 8) | data[7]
        energy = energy_raw / 10.0

        # Parse voltage (2 bytes, big-endian, 1/100 V -> V)
        voltage_raw = (data[8] << 8) | data[9]
        voltage = voltage_raw / 100.0

        # Parse current (2 bytes, big-endian, 1/100 A -> A)
        current_raw = (data[10] << 8) | data[11]
        current = current_raw / 100.0

        # Parse power (3 bytes, big-endian, 1/100 W -> W)
        power_raw = (data[12] << 16) | (data[13] << 8) | data[14]
        power = power_raw / 100.0

        # Parse power factor (1 byte, percentage)
        power_factor = data[15]

        self._power_data = GoveePowerData(
            time_on=time_on,
            energy=energy,
            voltage=voltage,
            current=current,
            power=power,
            power_factor=power_factor,
        )

        _LOGGER.debug(
            "H5086 power data: voltage=%.2fV, current=%.2fA, power=%.2fW, "
            "energy=%.1fWh, power_factor=%d%%, time_on=%ds",
            voltage,
            current,
            power,
            energy,
            power_factor,
            time_on,
        )
        return True

    async def async_request_power_data(self) -> bool:
        """Request power monitoring data from device."""
        client = None
        error = False
        try:
            client = await establish_connection(
                BleakClient,
                self._device,
                f"{self._device.name} ({self._device.address})",
            )

            # Event to track when we receive power data
            on_auth_ready = asyncio.Event()
            on_power_data_ready = asyncio.Event()

            async def recv_handler(c, data):
                if data[0] == 0x33 and data[1] == 0xB2:
                    on_auth_ready.set()
                elif data[0] == 0xEE and data[1] == 0x19:
                    if self._parse_power_response(data):
                        on_power_data_ready.set()

            await client.start_notify(self._RECV_CHARACTERISTIC_UUID, recv_handler)

            # Authenticate first
            ba = bytearray([0x33, 0xB2]) + bytearray.fromhex(self._token).ljust(
                17, b"\0"
            )
            ba.append(_sign_payload(ba))
            await client.write_gatt_char(self._SEND_CHARACTERISTIC_UUID, ba)
            await asyncio.wait_for(on_auth_ready.wait(), timeout=10.0)
        
        except asyncio.TimeoutError:
            _LOGGER.warning("Timeout waiting to authenticate with H5086")
            return False
        except Exception as e:
            _LOGGER.exception("Failed to authenticate with H5086: %s", e)
            return False
        finally:
            if client is not None:
                await client.disconnect()

        try:
            # Send power data request
            await client.write_gatt_char(
                self._SEND_CHARACTERISTIC_UUID, self.MSG_GET_POWER
            )
            await asyncio.wait_for(on_power_data_ready.wait(), timeout=10.0)

            await client.stop_notify(self._RECV_CHARACTERISTIC_UUID)
            return True

        except asyncio.TimeoutError:
            _LOGGER.warning("Timeout waiting for power data from H5086")
            return False
        except Exception as e:
            _LOGGER.exception("Failed to get power data from H5086: %s", e)
            return False
        finally:
            if client is not None:
                await client.disconnect()

    async def async_turn_on(self, port: int):
        assert port == 0
        if await self._send_message(self.MSG_TURN_ON):
            self._is_on = True

    async def async_turn_off(self, port: int):
        assert port == 0
        if await self._send_message(self.MSG_TURN_OFF):
            self._is_on = False


class GoveePlugPairer:
    # At least H5080, H5082, and H5086 all have the same pairing procedure
    # as implemented here

    def __init__(
        self, device: BLEDevice, recv_uuid: str, send_uuid: str, auth_msg: bytes
    ) -> None:
        self._device = device
        self._recv_uuid = recv_uuid
        self._send_uuid = send_uuid
        self._auth_msg = auth_msg
        self._result = asyncio.Future()

    async def begin(self):
        _LOGGER.info(f"%s: connecting to begin pairing", self._device.name)
        self._client = await establish_connection(
            BleakClient,
            self._device,
            f"{self._device.name} ({self._device.address})",
        )

        await self._client.start_notify(self._recv_uuid, self._recv_handler)
        await self._send_get_auth_key()

    async def finish(self) -> str | None:
        token = await self._result
        _LOGGER.info(f"%s: finishing pairing", self._device.name)
        await self._client.stop_notify(self._recv_uuid)
        await self._client.disconnect()
        return token

    async def _send_get_auth_key(self):
        _LOGGER.info(f"%s: asking for auth key", self._device.name)
        await self._client.write_gatt_char(self._send_uuid, self._auth_msg)

    async def _recv_handler(self, _, msg: bytearray):
        if len(msg) != 20:
            return

        # Check for the response type and subtype
        if msg[0] == 0xAA and msg[1] == 0xB1:
            if msg[2] == 0x01:
                auth_key = msg[3:-1]
                _LOGGER.info(f"%s: received authentication key", self._device.name)
                if not self._result.done():
                    self._result.set_result(auth_key.hex())
            else:
                await self._send_get_auth_key()
