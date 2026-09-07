"""Детерминированный учебный стенд. Единицы геометрии: сантиметры, радианы.

Это объявленная модель, не идентифицированный цифровой двойник физического робота.
Один и тот же модуль выполняется в браузере (Pyodide) и в обычном Python.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import random
import traceback

from pesok_sdk import ESP32Client, FrameDecoder, encode_frame

MODEL_VERSION = "1.0.0"
DT = 0.05
RADIUS = 12.0
WHEELBASE = 22.0
SENSORS = {"laser": (10, 0, 0), "fl": (8, 6, math.pi / 4),
           "fr": (8, -6, -math.pi / 4), "rl": (-8, 6, 3 * math.pi / 4),
           "rr": (-8, -6, -3 * math.pi / 4)}
TASKS = {
    "distance": {"title": "Остановка у стены", "duration": 22, "target": 30},
    "corridor": {"title": "Движение по коридору", "duration": 28},
    "obstacles": {"title": "Обход препятствий", "duration": 40},
    "servos": {"title": "Автомат манипулятора", "duration": 16},
    "delivery": {"title": "Доставка груза", "duration": 55},
}


def clamp(value, low, high):
    return max(low, min(high, value))


class World:
    def __init__(self, task: str, seed: int = 1, disturbances: bool = False):
        if task not in TASKS:
            raise ValueError("Неизвестное задание")
        self.task, self.seed, self.disturbances = task, seed, disturbances
        self.rng = random.Random(seed)
        self.width = 320.0 if task in ("obstacles", "delivery") else 260.0
        self.height = 200.0 if task == "obstacles" else 120.0
        self.x = (65.0 if task == "distance" else 40.0) + self.rng.uniform(-6, 6)
        self.y = self.height / 2 + self.rng.uniform(-8, 8)
        self.theta = self.rng.uniform(-0.06, 0.06) if task in ("corridor", "obstacles") else 0.0
        self.left = self.right = self.vl = self.vr = 0.0
        self.time = 0.05
        self.last_host = self.time
        self.link_lost = False
        self.collision = False
        self.distance_travelled = 0.0
        self.servo = [1700, 1700]
        self.servo_target = [1700, 1700]
        self._servo_clock = 0.0
        self._sensor_due = self.time
        self._telemetry_due = self.time
        self._sensor_time = 0.0
        self._samples = {name: 999 for name in SENSORS}
        self._wire = b""
        self._decoder = FrameDecoder()
        self.protocol_log = []
        self.obstacles = []
        if task == "obstacles":
            self.obstacles = [
                [130 + self.rng.uniform(-12, 12), 70, 24],
                [215 + self.rng.uniform(-10, 10), 143, 23],
            ]
        self.left_gain = self.rng.uniform(0.91, 1.03) if disturbances else 1.0
        self.right_gain = self.rng.uniform(0.91, 1.03) if disturbances else 1.0
        self.deadband = self.rng.uniform(68, 82) if disturbances else 75.0
        self.noise = 1.2 if disturbances else 0.0
        self.dropout = 0.05 if disturbances else 0.0
        self.phase = 0
        self.phase_hold = 0.0
        self.stable_for = 0.0
        self.success = False
        self.reason = "Время задания истекло"
        self.max_lateral_error = 0.0
        self.sensor_rays = {}
        self._sample_and_publish()

    def now(self):
        return self.time

    def receive(self):
        wire, self._wire = self._wire, b""
        return wire

    def exchange(self, frame):
        replies = b""
        for command in self._decoder.feed(frame):
            name = command.get("cmd", "")
            if not isinstance(name, str) or not name:
                continue
            self.last_host = self.time
            self.link_lost = False
            ok, message = True, ""
            speed = command.get("speed", 0)
            if name == "raw":
                self.left = int(clamp(command.get("left", 0), -255, 255))
                self.right = int(clamp(command.get("right", 0), -255, 255))
            elif name in ("forward", "backward", "turn_left", "turn_right"):
                speed = int(clamp(speed if speed > 0 else (220 if name in ("forward", "backward") else 150), 0, 255))
                self.left, self.right = {
                    "forward": (speed, speed), "backward": (-speed, -speed),
                    "turn_left": (-speed, speed), "turn_right": (speed, -speed),
                }[name]
            elif name == "stop":
                self.left = self.right = 0
            elif name == "servo_us":
                channel = command.get("channel", 0)
                if channel not in (0, 1):
                    ok, message = False, "invalid_servo"
                else:
                    self.servo_target[channel] = int(clamp(command.get("pulse_us", 1700), 1000, 2100))
            elif name in ("safety_auto", "safety_manual", "reset_emergency"):
                message = "ignored_host_authority"
            elif name != "ping":
                ok, message = False, "unknown_command"
            ack = {"type": "ack", "cmd": name, "ok": ok, "timestamp": round(self.time * 1000)}
            if message:
                ack["message"] = message
            replies += encode_frame(ack)
            if len(self.protocol_log) < 100:
                self.protocol_log.append({"t": round(self.time, 2), "tx": command, "rx": ack})
        return replies

    def _ray(self, name):
        sx, sy, heading = SENSORS[name]
        c, s = math.cos(self.theta), math.sin(self.theta)
        ox, oy = self.x + c * sx - s * sy, self.y + s * sx + c * sy
        angle = self.theta + heading
        dx, dy = math.cos(angle), math.sin(angle)
        hits = []
        if abs(dx) > 1e-9:
            for border in (0, self.width):
                t = (border - ox) / dx
                if t >= 0 and 0 <= oy + t * dy <= self.height:
                    hits.append(t)
        if abs(dy) > 1e-9:
            for border in (0, self.height):
                t = (border - oy) / dy
                if t >= 0 and 0 <= ox + t * dx <= self.width:
                    hits.append(t)
        for cx, cy, radius in self.obstacles:
            bx, by = ox - cx, oy - cy
            projection = bx * dx + by * dy
            discriminant = projection * projection - (bx * bx + by * by - radius * radius)
            if discriminant >= 0:
                t = -projection - math.sqrt(discriminant)
                if t >= 0:
                    hits.append(t)
        distance = min(hits) if hits else 999.0
        self.sensor_rays[name] = [ox, oy, ox + dx * min(distance, 205), oy + dy * min(distance, 205)]
        return distance

    def _sample_and_publish(self):
        if self.time + 1e-8 >= self._sensor_due:
            for name in SENSORS:
                value = self._ray(name)
                limit = 200 if name == "laser" else 204
                if value > limit or self.rng.random() < self.dropout:
                    self._samples[name] = 999
                else:
                    value = max(1, value + self.rng.gauss(0, self.noise))
                    self._samples[name] = int(value) if name == "laser" else round(value, 2)
            self._sensor_time = self.time
            self._sensor_due += 0.1
        if self.time + 1e-8 >= self._telemetry_due:
            packet = {
                "type": "telemetry", "control_authority": "host", **self._samples,
                "motor_left": int(self.left), "motor_right": int(self.right),
                "servo_0_us": self.servo[0], "servo_1_us": self.servo[1],
                "emergency": self.link_lost, "link_lost": self.link_lost,
                "sensor_timestamp": round(self._sensor_time * 1000),
                "sensor_age_ms": round((self.time - self._sensor_time) * 1000),
                "timestamp": round(self.time * 1000),
            }
            # Последний пакет, как в клиентском кэше. При сбое пакеты не приходят.
            if not (self.disturbances and 5 <= self.time < 5.65):
                self._wire = encode_frame(packet)
            self._telemetry_due += 0.05

    def _wheel_target(self, pwm, gain):
        if abs(pwm) <= self.deadband:
            return 0.0
        return math.copysign((abs(pwm) - self.deadband) / (255 - self.deadband) * 65 * gain, pwm)

    def advance(self):
        # Мелкие подшаги удерживают контроль столкновений и watchdog при высоком PWM.
        for _ in range(5):
            dt = 0.01
            self.time += dt
            if self.time - self.last_host > 0.5 + 1e-8:
                self.left = self.right = 0
                self.link_lost = True
            alpha = 1 - math.exp(-dt / 0.16)
            self.vl += alpha * (self._wheel_target(self.left, self.left_gain) - self.vl)
            self.vr += alpha * (self._wheel_target(self.right, self.right_gain) - self.vr)
            velocity = (self.vl + self.vr) / 2
            angular = (self.vr - self.vl) / WHEELBASE
            heading = self.theta + angular * dt / 2
            self.x += velocity * math.cos(heading) * dt
            self.y += velocity * math.sin(heading) * dt
            self.theta = (self.theta + angular * dt + math.pi) % (2 * math.pi) - math.pi
            self.distance_travelled += abs(velocity) * dt
            self._servo_clock += dt
            while self._servo_clock >= 0.015 - 1e-9:
                self._servo_clock -= 0.015
                for i in (0, 1):
                    self.servo[i] += int(clamp(self.servo_target[i] - self.servo[i], -10, 10))
            self.collision = self.collision or (
                self.x < RADIUS or self.x > self.width - RADIUS
                or self.y < RADIUS or self.y > self.height - RADIUS
                or any(math.hypot(self.x - x, self.y - y) < RADIUS + r for x, y, r in self.obstacles)
            )
            if self.collision:
                self.left = self.right = self.vl = self.vr = 0
                break
        self._sample_and_publish()
        self._evaluate()

    def _evaluate(self):
        stationary = max(abs(self.vl), abs(self.vr)) < 1.5
        if self.collision:
            self.reason = "Столкновение"
            return
        if self.task == "distance":
            # Независимая геометрическая проверка, не самоотчёт студента и не шум датчика.
            good = abs((self.width - self.x - 10) - 30) <= 3 and abs(self.theta) < 0.15 and stationary
            self.stable_for = self.stable_for + DT if good else 0
            self.success = self.stable_for >= 2.0
        elif self.task == "corridor":
            if self.x > 75:
                self.max_lateral_error = max(self.max_lateral_error, abs(self.y - self.height / 2))
            self.success = self.x >= self.width - 48 and self.max_lateral_error <= 14
            if self.x >= self.width - 48 and not self.success:
                self.reason = "Превышено боковое отклонение 14 см"
        elif self.task == "obstacles":
            self.success = self.x >= self.width - 45
        elif self.task == "servos":
            poses = [(1450, 1400), (1300, 1400), (1300, 1750), (1550, 1750), (1350, 1750), (1350, 1400)]
            pose = poses[min(self.phase, len(poses) - 1)]
            good = all(abs(a - b) <= 10 for a, b in zip(self.servo, pose)) and stationary
            self.phase_hold = self.phase_hold + DT if good else 0
            if self.phase_hold >= 0.3:
                self.phase += 1
                self.phase_hold = 0
            self.success = self.phase >= len(poses)
        elif self.task == "delivery":
            # Учебная модель загрузки на станции: манипуляция физически не моделируется.
            if self.phase == 0:
                good = self.x >= self.width - 48 and self.servo[1] >= 1740 and stationary
            else:
                good = self.x <= 55 and self.servo[1] <= 1410 and stationary
            self.phase_hold = self.phase_hold + DT if good else 0
            if self.phase_hold >= 0.5:
                self.phase += 1
                self.phase_hold = 0
            self.success = self.phase >= 2
        if self.success:
            self.reason = "Задание выполнено"

    def frame(self):
        return {
            "t": round(self.time, 3), "x": round(self.x, 2), "y": round(self.y, 2),
            "theta": self.theta, "left": int(self.left), "right": int(self.right),
            "distances": dict(self._samples), "servos": list(self.servo),
            "rays": dict(self.sensor_rays), "phase": self.phase,
        }

    def scene(self):
        return {"width": self.width, "height": self.height, "radius": RADIUS,
                "obstacles": self.obstacles, "task": self.task}


class LimitedOutput(io.StringIO):
    def write(self, text):
        remaining = 6000 - self.tell()
        if remaining > 0:
            super().write(text[:remaining])
        return len(text)


def run_case(source: str, task="distance", seed=1, disturbances=False, trace=True):
    world = World(task, seed, disturbances)
    robot = ESP32Client(world)
    frames = [world.frame()] if trace else []
    output = LimitedOutput()
    error = None
    namespace = {"__name__": "student_solution", "ESP32Client": ESP32Client}
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            exec(compile(source, "solution.py", "exec"), namespace)
            if not callable(namespace.get("Controller")):
                raise ValueError("Определите класс Controller с методом step(self, robot, dt)")
            controller = namespace["Controller"]()
            for _ in range(round(TASKS[task]["duration"] / DT)):
                controller.step(robot, DT)
                world.advance()
                if trace:
                    frames.append(world.frame())
                if world.success or world.collision:
                    break
        except BaseException:
            error = traceback.format_exc(limit=6)
            world.success = False
            world.reason = "Ошибка программы"
        finally:
            # Остановка проходит и при исключении в решении.
            world.exchange(encode_frame({"cmd": "stop"}))
    return {
        "task": task, "seed": seed, "disturbances": disturbances,
        "model_version": MODEL_VERSION, "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "passed": world.success and error is None, "reason": world.reason,
        "elapsed_s": round(world.time - 0.05, 2), "path_cm": round(world.distance_travelled, 1),
        "collision": world.collision, "final_distance_cm": round(world.width - world.x - 10, 1),
        "max_lateral_error_cm": round(world.max_lateral_error, 1),
        "completed_phases": world.phase, "stdout": output.getvalue(), "error": error,
        "scene": world.scene(), "frames": frames,
        "protocol": world.protocol_log if trace else [],
    }


def evaluate_json(source: str, task: str, seed=1, disturbances=False, suite=False):
    seeds = [seed + i for i in range(5)] if suite else [seed]
    # Условия объявлены; это самопроверка. Браузер не скрывает тесты от студента.
    cases = [run_case(source, task, s, disturbances, trace=not suite) for s in seeds]
    return json.dumps({"cases": cases, "passed": sum(c["passed"] for c in cases),
                       "total": len(cases), "model_version": MODEL_VERSION}, ensure_ascii=False)
