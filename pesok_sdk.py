"""Учебный ESP32Client. Чистый Python; работает в браузере и обычном Python."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Optional

STX = b"\x02"
ETX = b"\x03"
MAX_FRAME = 512
SENSOR_NAMES = ("fl", "fr", "rl", "rr", "laser")


class RobotError(RuntimeError):
    """Команда отклонена или подтверждение не получено."""


def encode_frame(message: dict) -> bytes:
    data = json.dumps(message, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) + 2 > MAX_FRAME:
        raise ValueError("Размер кадра превышает 512 байт")
    return STX + data + ETX


class FrameDecoder:
    """Потоковый STX/ETX-декодер: частичные кадры, шум и ресинхронизация."""

    def __init__(self):
        self.buffer = bytearray()
        self.started = False

    def feed(self, chunk: bytes) -> list[dict]:
        messages = []
        for value in chunk:
            if value == 2:
                self.buffer.clear()
                self.started = True
            elif value == 3 and self.started:
                try:
                    item = json.loads(self.buffer.decode("utf-8"))
                    if isinstance(item, dict):
                        messages.append(item)
                except (ValueError, UnicodeDecodeError):
                    pass
                self.started = False
                self.buffer.clear()
            elif self.started:
                self.buffer.append(value)
                if len(self.buffer) > MAX_FRAME - 2:
                    self.started = False
                    self.buffer.clear()
        return messages


@dataclass(frozen=True)
class RangeReading:
    """Расстояние в сантиметрах; None означает отсутствие достоверного измерения."""

    cm: Optional[float]
    age_ms: float
    valid: bool


class ESP32Client:
    """Совместимое подмножество API клиента rpi_controller плюс проверка свежести.

    В практикуме экземпляр передаётся в Controller.step(robot, dt).
    transport предоставляет exchange(bytes), receive() и now() в секундах.
    """

    def __init__(self, transport):
        self._transport = transport
        self._decoder = FrameDecoder()
        self._telemetry = {}
        self._received_at = None
        self.last_error = ""

    def _receive(self):
        for packet in self._decoder.feed(self._transport.receive()):
            if packet.get("type") == "telemetry":
                self._telemetry = packet
                self._received_at = self._transport.now()

    def send_command(self, command: str, **payload) -> bool:
        """Отправить JSON-команду; вернуть ACK.ok. В симуляторе обмен синхронный."""
        replies = self._decoder.feed(
            self._transport.exchange(encode_frame({"cmd": command, **payload}))
        )
        for reply in replies:
            if reply.get("type") == "ack" and reply.get("cmd") == command:
                ok = bool(reply.get("ok", False))
                self.last_error = "" if ok else reply.get("message", "Команда отклонена")
                return ok
        self.last_error = "Нет подтверждения команды"
        return False

    @staticmethod
    def _integer(value, name, low, high):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name}: требуется целое число")
        if not low <= value <= high:
            raise ValueError(f"{name}: допустимый диапазон {low}…{high}")
        return value

    def raw_motors(self, left: int, right: int) -> bool:
        """Задать PWM −255…255. Положительные значения — движение вперёд."""
        return self.send_command(
            "raw", left=self._integer(left, "left", -255, 255),
            right=self._integer(right, "right", -255, 255),
        )

    def stop(self) -> bool:
        """Задать обоим моторам нулевой PWM. Физическая скорость падает постепенно."""
        return self.send_command("stop")

    def _motion(self, command, speed):
        return self.send_command(command, speed=self._integer(speed, "speed", 1, 255))

    def forward(self, speed: int = 140) -> bool:
        return self._motion("forward", speed)

    def backward(self, speed: int = 140) -> bool:
        return self._motion("backward", speed)

    def turn_left(self, speed: int = 130) -> bool:
        return self._motion("turn_left", speed)

    def turn_right(self, speed: int = 130) -> bool:
        return self._motion("turn_right", speed)

    def set_servo_pulse_us(self, channel: int, pulse_us: int) -> bool:
        """Задать целевой импульс 1000…2100 мкс для канала 0 или 1."""
        return self.send_command(
            "servo_us", channel=self._integer(channel, "channel", 0, 1),
            pulse_us=self._integer(pulse_us, "pulse_us", 1000, 2100),
        )

    def ping(self) -> bool:
        return self.send_command("ping")

    def get_telemetry(self) -> dict:
        """Копия последнего пакета; значения и единицы совпадают с ESP32."""
        self._receive()
        return dict(self._telemetry)

    def get_distances(self) -> dict[str, float]:
        """Сырые сантиметры; 999 — ошибка. Для управления удобнее distance()."""
        tel = self.get_telemetry()
        return {name: float(tel.get(name, 999)) for name in SENSOR_NAMES}

    def get_motor_speeds(self) -> tuple[int, int]:
        """Заданные PWM, несмотря на историческое имя метода; не скорость колёс."""
        tel = self.get_telemetry()
        return int(tel.get("motor_left", 0)), int(tel.get("motor_right", 0))

    def get_servo_pulses_us(self) -> tuple[int, int]:
        """Формируемые импульсы; не измеренные положения сервоприводов."""
        tel = self.get_telemetry()
        return int(tel.get("servo_0_us", -1)), int(tel.get("servo_1_us", -1))

    def is_link_lost(self) -> bool:
        return bool(self.get_telemetry().get("link_lost", True))

    def reading(self, name: str = "laser", max_age_ms: float = 250) -> RangeReading:
        """Возраст учитывает и время на ESP32, и время после приёма пакета."""
        if name not in SENSOR_NAMES:
            raise ValueError(f"Неизвестный датчик: {name}")
        tel = self.get_telemetry()
        age = math.inf
        if self._received_at is not None and tel.get("sensor_timestamp", 0) > 0:
            age = float(tel.get("sensor_age_ms", 0)) + max(
                0, (self._transport.now() - self._received_at) * 1000
            )
        value = tel.get(name, 999)
        valid = (
            isinstance(value, (int, float)) and math.isfinite(value)
            and 0 < value < 999 and age <= max_age_ms
        )
        return RangeReading(float(value) if valid else None, age, valid)

    def distance(self, name: str = "laser", max_age_ms: float = 250) -> Optional[float]:
        """Достоверное расстояние в см или None. None требует отдельной реакции."""
        return self.reading(name, max_age_ms).cm
