"""Device models: specs, physical constraints and runtime state (control logic v4.1)."""
from datetime import datetime


class CommandError(ValueError):
    """A command violates the device's physical constraints."""


class SimClock:
    """Simulation clock. Speed scales every timer; the hour of day can be pinned for demos."""

    def __init__(self):
        self.speed = 1.0
        self.hour_override = None
        self._t = 0.0

    def advance(self, real_dt):
        self._t += real_dt * self.speed
        return real_dt * self.speed

    def now(self):
        return self._t

    def hour(self):
        if self.hour_override is not None:
            return self.hour_override
        n = datetime.now()
        return n.hour + n.minute / 60


CLOCK = SimClock()

SENSOR_DEFAULTS = {"presence": False, "motion": False, "lux": 500, "temperature": 24.0,
                   "humidity": 50.0, "smoke": "normal"}


def _num(v, name):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise CommandError(f"'{name}' must be a number")
    return v


class Device:
    """One device instance. Limits come from the spec taken from devices.json."""

    def __init__(self, id, model, kind, room, spec, sensor=None):
        self.id, self.model, self.kind, self.room = id, model, kind, room
        self.spec, self.sensor = spec, sensor
        self.eoa64 = False        # True when this device is the room's EOA64 master actuator
        self.state = self._initial()
        self._last_level = None   # dimmer / blinding light: level restored by "on"
        self._off_since = None    # AC: compressor protection timer

    def _initial(self):
        k = self.kind
        if k in ("light_switched", "fan"):
            return {"on": False}
        if k == "light_dimmable":
            return {"on": False, "level": 0}
        if k == "light_blinding":
            return {"on": False, "level": 0, "mode": "normal"}
        if k == "ac":
            return {"on": False, "mode": "cool", "setpoint": 24, "fan_speed": "auto"}
        if k == "window":
            return {"open": False}
        if k == "pushbutton":
            return {"last_event": None}
        if k == "garage":
            return {"state": "closed", "position": 0.0, "target": 0.0}
        return {"value": SENSOR_DEFAULTS[self.sensor]}

    def commands(self):
        return list(self.spec.get("commands", []))

    def apply(self, command, **a):
        """Run a command after checking it is supported by this model."""
        if command not in self.commands():
            raise CommandError(f"{self.id}: '{command}' not supported (allowed: {self.commands()})")
        getattr(self, "_" + self.kind)(command, a)

    # ---- command handlers (one per device kind) ----
    def _switch(self, c, a):
        s = self.state
        s["on"] = (not s["on"]) if c == "toggle" else c == "on"

    _light_switched = _switch
    _fan = _switch

    def _level_light(self, c, a):
        """Shared by dimmable and blinding lights: on / off / toggle / set_level (0-100)."""
        lo, hi = self.spec.get("level_range_pct", [0, 100])
        min_lvl = self.spec.get("estimated", {}).get("min_level_pct", 0)
        s = self.state
        if c == "toggle":
            c = "off" if s["on"] else "on"
        if c == "on":
            level = self._last_level or hi
        elif c == "off":
            level = 0
        else:
            level = round(_num(a.get("level"), "level"))
            if not lo <= level <= hi:
                raise CommandError(f"level must be between {lo} and {hi}")
            if 0 < level < min_lvl:
                level = min_lvl  # the driver cannot go below its minimum level
        if level == 0 and s["level"]:
            self._last_level = s["level"]
        s["level"], s["on"] = level, level > 0

    def _light_dimmable(self, c, a):
        self._level_light(c, a)

    def _light_blinding(self, c, a):
        if c == "set_mode":
            mode = a.get("mode")
            if mode not in self.spec["modes"]:
                raise CommandError(f"mode must be one of {self.spec['modes']}")
            self.state["mode"] = mode
        else:
            self._level_light(c, a)

    def _ac(self, c, a):
        s, sp = self.state, self.spec
        est = sp.get("estimated", {})
        if c == "power":
            on = a.get("on")
            if not isinstance(on, bool):
                raise CommandError("'on' must be true or false")
            if on and not s["on"] and self._off_since is not None:
                wait = est.get("min_off_time_s", 0) - (CLOCK.now() - self._off_since)
                if wait > 0:
                    raise CommandError(f"compressor protection: wait {wait:.0f}s before restart")
            if s["on"] and not on:
                self._off_since = CLOCK.now()
            s["on"] = on
        elif c == "mode":
            if a.get("mode") not in sp["modes"]:
                raise CommandError(f"mode must be one of {sp['modes']}")
            s["mode"] = a["mode"]
        elif c == "set_temperature":
            lo, hi = est.get("setpoint_c_range", [16, 30])
            step = est.get("setpoint_step_c", 1)
            t = _num(a.get("temperature"), "temperature")
            if not lo <= t <= hi or (t - lo) % step:
                raise CommandError(f"temperature must be {lo}-{hi} in steps of {step}")
            s["setpoint"] = t
        elif c == "fan_speed":
            if a.get("fan_speed") not in sp["fan_speeds"]:
                raise CommandError(f"fan_speed must be one of {sp['fan_speeds']}")
            s["fan_speed"] = a["fan_speed"]

    def _garage(self, c, a):
        s = self.state
        s["target"] = 100.0 if c == "open" else 0.0 if c == "close" else s["position"]
        self._update_motion()

    # ---- simulated inputs ----
    def set_input(self, value):
        """Set a sensor value or a window/door contact state (simulation input)."""
        if self.kind == "window":
            if not isinstance(value, bool):
                raise CommandError("window state must be true (open) or false (closed)")
            self.state["open"] = value
        elif self.kind == "sensor":
            self.state["value"] = self._check_sensor(value)
        else:
            raise CommandError(f"{self.id} has no simulated input")

    def _check_sensor(self, value):
        t, est = self.sensor, self.spec.get("estimated", {})
        if t in ("presence", "motion"):
            if not isinstance(value, bool):
                raise CommandError(f"{t} must be true or false")
        elif t == "smoke":
            if value not in self.spec["states"]:
                raise CommandError(f"smoke state must be one of {self.spec['states']}")
        else:
            rng = {"lux": est.get("range_lux"), "temperature": est.get("range_c"),
                   "humidity": est.get("range_pct")}[t]
            v = _num(value, t)
            if rng and not rng[0] <= v <= rng[1]:
                raise CommandError(f"{t} must be between {rng[0]} and {rng[1]}")
        return value

    def press(self, event="press"):
        """Simulate a physical pushbutton event (press, double_press, long_press, release)."""
        if self.kind != "pushbutton":
            raise CommandError(f"{self.id} is not a pushbutton")
        if event not in self.spec["events"]:
            raise CommandError(f"event must be one of {self.spec['events']}")
        self.state["last_event"] = event

    # ---- time ----
    def tick(self, dt):
        """Move the garage door at its travel speed."""
        if self.kind != "garage":
            return
        s, est = self.state, self.spec.get("estimated", {})
        step = 100.0 / est.get("travel_time_s", 15) * dt
        s["position"] += max(-step, min(step, s["target"] - s["position"]))
        self._update_motion()

    def _update_motion(self):
        s = self.state
        moving = abs(s["target"] - s["position"]) > 0.01
        if moving:
            s["state"] = "opening" if s["target"] > s["position"] else "closing"
        else:
            s["state"] = "open" if s["position"] >= 100 else "closed" if s["position"] <= 0 else "stopped"

    def to_dict(self):
        st = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in self.state.items()}
        return {"id": self.id, "model": self.model, "kind": self.kind, "sensor": self.sensor,
                "room": self.room, "eoa64": self.eoa64, "state": st, "commands": self.commands()}