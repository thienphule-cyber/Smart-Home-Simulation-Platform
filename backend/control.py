"""Control logic (v4.1): priority and overrides, groups, pushbuttons, scenes, safety and automation rules."""
from devices import CLOCK, CommandError

LIGHTS = ("light_switched", "light_dimmable", "light_blinding")
ROOM_LIGHTS = ("light_switched", "light_dimmable")  # blinding lights are changed by scenes only

TARGETS = {
    "all_lights": LIGHTS, "triggering_room_lights": LIGHTS,
    "lights": ROOM_LIGHTS, "room_lights": ROOM_LIGHTS,
    "switching_lights": ("light_switched",), "dimmable_lights": ("light_dimmable",),
    "blinding_lights": ("light_blinding",),
    "all_fans": ("fan",), "fans": ("fan",),
    "all_ac": ("ac",), "ac": ("ac",),
}

DIM_STEPS = [100, 75, 50, 25, 10]  # pushbutton long press "dim_cycle"
DIM_DOWN_STEP = 20                 # pushbutton long press "dim_down"
COOKING_SCENE_VALID_S = 7200       # how long the Cooking scene counts as a cooking context for R01b


class ControlError(Exception):
    """A command was blocked by the safety lock, a priority override or a bad request."""


def _hours(s):
    h, m = s.split(":")
    return int(h) + int(m) / 60


def in_window(frm, to, h=None):
    """True when the hour is inside [frm, to), also across midnight."""
    h = CLOCK.hour() if h is None else h
    f, t = _hours(frm), _hours(to)
    return (f <= h < t) if f < t else (h >= f or h < t)


class ControlSystem:
    def __init__(self, building, rules):
        self.b, self.rules, self.th = building, rules, rules["thresholds"]
        self.rule = {r["id"]: r for r in rules["rules"]}
        self.level = {p["source"]: p["level"] for p in rules["priority"]}  # 1 = highest priority
        self.enabled = {r["id"]: r.get("enabled", True) for r in rules["rules"]}
        self.sun = (_hours(rules["sun"]["sunrise"]), _hours(rules["sun"]["sunset"]))
        self.mode = rules["modes"]["default"]
        self.locks = {}          # device_id -> (priority level, expiry on the sim clock)
        self.safety = False
        self.precheck = {}       # smoke device id -> start time of the R01b kitchen pre-check
        self.alert_rooms = set() # rooms with an active intrusion alert (R18)
        self.arrived_at = None   # sim time of the last geofence "arrive"
        self.last_scene = None
        self.notifications = []
        self.flag = {}           # edge / hysteresis state used by the rules
        self.until = {}          # timed actions (R14 fan run)
        self.hist = {}           # room id -> [(time, humidity)] for humidity rise detection
        self._since = {}

    # ---------- helpers ----------
    def on(self, rule_id):
        return self.enabled.get(rule_id, True)

    def _msg(self, rule_id):
        return next((a["message"] for a in self.rule[rule_id]["actions"] if a.get("command") == "notify"), rule_id)

    def notify(self, message):
        self.notifications = (self.notifications + [{"t": round(CLOCK.now()), "message": message}])[-50:]

    def profile(self):
        """Current time profile name (morning, day, evening, night)."""
        for name, p in self.rules["time_profiles"].items():
            if in_window(p["from"], p["to"]):
                return name
        return "day"

    def prof(self, room):
        return self.rules["room_profiles"].get(room.type, {})

    def pv(self, room, key, default=None):
        """Room profile value, falling back to the global thresholds."""
        return self.prof(room).get(key, self.th.get(key, default))

    def on_level(self, room, profile):
        v = self.pv(room, "on_level_pct", 100)
        return v["night" if profile == "night" else "day"] if isinstance(v, dict) else v

    def rise(self, key, value):
        """True only when value goes from False to True."""
        prev, self.flag[key] = self.flag.get(key, False), bool(value)
        return bool(value) and not prev

    def age(self, key, value):
        """Seconds the given value has been current for this key."""
        if key not in self._since or self._since[key][0] != value:
            self._since[key] = (value, CLOCK.now())
        return CLOCK.now() - self._since[key][1]

    def due(self, key, every):
        if CLOCK.now() - self.flag.get(key, -1e9) >= every:
            self.flag[key] = CLOCK.now()
            return True
        return False

    def hum_rise(self, room_id, pct, window_s):
        """True when humidity rose by at least pct inside the last window_s seconds."""
        now = CLOCK.now()
        vals = [v for t, v in self.hist.get(room_id, []) if now - t <= window_s]
        return bool(vals) and vals[-1] - min(vals) >= pct

    def _record(self):
        now = CLOCK.now()
        for room in self.b.rooms.values():
            hum = self.b.sensor_value(room.id, "humidity")
            if hum is not None:
                h = self.hist.setdefault(room.id, [])
                h.append((now, hum))
                h[:] = [(t, v) for t, v in h if now - t <= 900]

    # ---------- commands ----------
    def command(self, device_id, command, source="online", **args):
        """Single entry point for every command. Enforces safety lock and control priority."""
        dev = self.b.devices.get(device_id)
        if dev is None:
            raise ControlError(f"unknown device '{device_id}'")
        if source not in self.level:
            raise ControlError(f"unknown source '{source}'")
        lvl = self.level[source]
        if self.safety and source != "safety":
            raise ControlError("safety lock active: reset the alarm first")
        if dev.kind == "garage" and source == "automation":
            raise ControlError("garage door is user controlled only (R12)")
        lock = self.locks.get(device_id)
        if lock and CLOCK.now() < lock[1] and lvl > lock[0]:
            raise ControlError("blocked: a higher priority manual override is active (resume auto to clear)")
        dev.apply(command, **args)
        if source in ("pushbutton", "online"):  # manual control pauses automation for a while
            minutes = self.rules["override"][f"{source}_timeout_min"]
            self.locks[device_id] = (lvl, CLOCK.now() + minutes * 60)
        return dev.to_dict()

    def send(self, dev, command, source="online", **a):
        """Translate a generic command to what this device supports, then run it."""
        if dev.kind == "light_switched" and command == "set_level":
            command = "on" if (a.get("level") or 0) > 0 else "off"  # switching lights never take a level
        elif dev.kind in ("light_dimmable", "light_blinding") and command == "on" and "level" in a:
            command = "set_level"
        try:
            self.command(dev.id, command, source, **a)
            return True, None
        except (ControlError, CommandError) as e:
            return False, str(e)

    def group_command(self, group_id, command, source="online", **a):
        g = self.b.groups.get(group_id)
        if g is None:
            raise ControlError(f"unknown group '{group_id}'")
        out = {}
        for did in g.members:
            ok, err = self.send(self.b.devices[did], command, source, **a)
            out[did] = "ok" if ok else err
        return out

    def set_input(self, device_id, value):
        dev = self.b.devices.get(device_id)
        if dev is None:
            raise ControlError(f"unknown device '{device_id}'")
        dev.set_input(value)
        return dev.to_dict()

    def resume_auto(self, room_id):
        """Clear manual overrides in a room so automation takes over again."""
        if room_id not in self.b.rooms:
            raise ControlError(f"unknown room '{room_id}'")
        for i in self.b.rooms[room_id].device_ids:
            self.locks.pop(i, None)

    # ---------- mode, geofence, optional rules ----------
    def set_mode(self, mode):
        if mode not in self.rules["modes"]["available"]:
           raise ControlError(f"mode must be one of {self.rules['modes']['available']}")
        if mode == self.mode:
           return {"mode": mode}
        self.locks.clear()  # manual overrides must not survive a mode change
        if mode == "away":
           self.mode = "away"
        else:
           self.alert_rooms.clear()
           self.geofence("arrive")  # R19-R22: clears away mode and runs Welcome Home
           self.mode = "home"
        return {"mode": self.mode}

    def set_rule_enabled(self, rule_id, enabled):
        if rule_id not in self.rule:
            raise ControlError(f"unknown rule '{rule_id}'")
        self.enabled[rule_id] = bool(enabled)
        return {"id": rule_id, "enabled": self.enabled[rule_id]}

    def geofence(self, event):
        """Geofence event from the hub. Only 'arrive' triggers rules (R19-R22)."""
        if event not in self.rules["geofence"]["events"]:
            raise ControlError(f"event must be one of {self.rules['geofence']['events']}")
        if event == "leave":
            self.set_mode("away")
            self.notify("Left home: away mode on")
            return {"event": event}
        self.arrived_at = CLOCK.now()
        if self.on("R19_geofence_arrive_home"):  # R19: arriving clears away mode first
            self.mode = "home"
            self.alert_rooms.clear()
            self.notify(self._msg("R19_geofence_arrive_home"))
        if self.on("R21_welcome_outdoor_lights") and not self.safety:  # R21: porch lights when dark
            r21 = self.rule["R21_welcome_outdoor_lights"]
            for room in self.b.rooms.values():
                if room.type in r21["applies_to_types"] and room.id in r21["only_room_ids"]:
                    lux = self.b.sensor_value(room.id, "lux", True)
                    h = CLOCK.hour()
                    dark = (lux is not None and lux < self.pv(room, "lux_low")) or h >= self.sun[1] or h < self.sun[0]
                    if dark:
                        self.act("switching_lights", "on", room.id)
        if self.on("R22_geofence_garage_door") and not self.safety:  # R22: opt-in, user configured
            for g in [d for d in self.b.devices.values() if d.kind == "garage"]:
                self.send(g, "open", "online")
                for light in self.b.in_room(g.room, "light_switched"):
                    self.send(light, "on", "online")
        return {"event": event}

    # ---------- pushbuttons ----------
    def press(self, device_id, event="press"):
        """Pushbutton event. Same-room lights go through the room's EOA64 master (direct path),
        scenes go through the hub."""
        dev = self.b.devices.get(device_id)
        if dev is None:
            raise ControlError(f"unknown device '{device_id}'")
        if self.safety:
            raise ControlError("safety lock active: reset the alarm first")
        dev.press(event)  # validates the device and the event
        room = self.b.rooms[dev.room]
        pb = self.rules["pushbuttons"]
        profiles = pb["by_type"].get(room.type)
        idx = int(dev.id.rsplit("_", 1)[1])
        prof = profiles[idx] if profiles and idx < len(profiles) else pb["default"]
        action = prof.get(event)
        path = None
        if action:
            kind, _, name = action.partition(":")
            if kind == "scene":
                self.scene(name, "pushbutton")
                path = "hub"
            else:
                self._room_lights_action(name, room)
                path = "direct"
        return {"pushbutton": device_id, "event": event, "action": action, "path": path}

    def _room_lights_action(self, name, room):
        lights = self.b.in_room(room.id, *ROOM_LIGHTS)
        dims = self.b.in_room(room.id, "light_dimmable")
        any_on = any(d.state["on"] for d in lights)
        if name == "toggle":
            for d in lights:
                self.send(d, "off" if any_on else "on", "pushbutton")
        elif name == "toggle_at_20":  # bedside button: switch on dimmed to 20%
            for d in lights:
                if any_on:
                    self.send(d, "off", "pushbutton")
                elif d.kind == "light_dimmable":
                    self.send(d, "set_level", "pushbutton", level=20)
                else:
                    self.send(d, "on", "pushbutton")
        elif name == "dim_cycle" and dims:  # step down through DIM_STEPS, wrap back to 100
            cur = max(d.state["level"] for d in dims)
            nxt = next((s for s in DIM_STEPS if s < cur), DIM_STEPS[0])
            for d in dims:
                self.send(d, "set_level", "pushbutton", level=nxt)
        elif name == "dim_down" and dims:
            cur = max(d.state["level"] for d in dims)
            for d in dims:
                self.send(d, "set_level", "pushbutton", level=max(0, cur - DIM_DOWN_STEP))

    # ---------- actions and scenes ----------
    def _targets(self, token, room_id):
        kinds = TARGETS.get(token, ())
        if room_id is None:
            return [d for d in self.b.devices.values() if d.kind in kinds]
        return self.b.in_room(room_id, *kinds)

    def _action(self, act, room_id, source):
        if "scene" in act:
            self.scene(act["scene"], source)
            return
        tgt, cmd = act["target"], act["command"]
        if tgt == "system":
            if cmd == "notify":
                self.notify(act["message"])
            return
        if tgt == "automation":
            return  # the automation lock is the safety flag
        level = act.get("level_pct")
        for d in self._targets(tgt, room_id):
            if d.kind == "ac":
                if cmd in ("on", "off"):
                    self.send(d, "power", source, on=(cmd == "on"))
            elif cmd == "set_level":
                self.send(d, "set_level", source, level=level)
            elif cmd == "on" and level is not None:
                self.send(d, "on", source, level=level)
            else:
                self.send(d, cmd, source)

    def act(self, target, command, room_id, **kw):
        self._action({"target": target, "command": command, **kw}, room_id, "automation")

    def _select(self, sel):
        rooms = list(self.b.rooms.values())
        if sel == "all":
            return rooms
        return [r for r in rooms if r.type in sel.get("types", [])]

    def _night_light(self, room, source):
        """Switching lights cannot dim, so keep a share of the fixtures on (at least one)."""
        sws = self.b.in_room(room.id, "light_switched")
        if not sws:
            return
        ratio = self.prof(room).get("night_light", {}).get("keep_on_ratio", 0.5)
        keep = max(1, int(len(sws) * ratio))
        for i, d in enumerate(sws):
            self.send(d, "on" if i < keep else "off", source)

    def _scene_room(self, room, tgt, source):
        sws = self.b.in_room(room.id, "light_switched")
        dims = self.b.in_room(room.id, "light_dimmable")
        blinds = self.b.in_room(room.id, "light_blinding")
        if tgt.get("night_light"):
            self._night_light(room, source)
        lvl, sw_mode = tgt.get("level_pct"), tgt.get("switching")
        if lvl is not None:
            for d in dims:
                self.send(d, "set_level", source, level=lvl)
            if sw_mode is None:  # no explicit switching state: follow the level
                for d in sws:
                    self.send(d, "on" if lvl > 0 else "off", source)
        if sw_mode:
            for d in sws:
                self.send(d, sw_mode, source)
        if tgt.get("blinding_pct") is not None:
            for d in blinds:
                self.send(d, "set_level", source, level=tgt["blinding_pct"])

    def scene(self, scene_id, source="online"):
        sc = next((s for s in self.rules["scenes"] if s["id"] == scene_id), None)
        if sc is None:
            raise ControlError(f"unknown scene '{scene_id}'")
        if self.safety and source != "safety":
            raise ControlError("safety lock active: reset the alarm first")
        for tgt in sc["targets"]:
            only = tgt.get("only_buildings")
            if only and self.b.id not in only:
                continue
            for room in self._select(tgt["select"]):
                self._scene_room(room, tgt, source)
        if sc.get("keep_night_light"):
            for room in self.b.rooms.values():
                if self.prof(room).get("night_light"):
                    self._night_light(room, source)
        self.last_scene = {"id": scene_id, "t": CLOCK.now()}

    # ---------- safety (R01 / R01b) ----------
    def _alarms(self):
        return [d for d in self.b.devices.values() if d.sensor == "smoke" and d.state["value"] == "alarm"]

    def _cooking_context(self, room):
        sc = self.last_scene
        scene = bool(sc and sc["id"] == "cooking" and CLOCK.now() - sc["t"] <= COOKING_SCENE_VALID_S)
        rise = self.prof(room).get("humidity_rise")
        humid = bool(rise) and self.hum_rise(room.id, rise["pct"], rise["window_s"])
        fans = any(f.state["on"] for f in self.b.in_room(room.id, "fan"))
        return scene or humid or fans

    def _precheck_applies(self, room):
        return (self.on("R01b_kitchen_smoke_precheck") and room.type == "kitchen"
                and self.b.sensor_value(room.id, "presence") is True and self._cooking_context(room))

    def _escalate(self):
        self.safety = True
        self.precheck.clear()
        for a in self.rule["R01_smoke_safety"]["actions"]:
            self._action(a, None, "safety")

    def check_safety(self):
        if self.safety or not self.on("R01_smoke_safety"):
            return
        alarms = self._alarms()
        ids = {d.id for d in alarms}
        for did in list(self.precheck):  # smoke cleared inside the window: it was cooking smoke
            if did not in ids:
                self.precheck.pop(did)
                self.notify("Kitchen smoke cleared: treated as cooking smoke")
        pre = self.rule["R01b_kitchen_smoke_precheck"]
        for d in alarms:
            room = self.b.rooms[d.room]
            started = self.precheck.get(d.id)
            if started is None:
                if self._precheck_applies(room):
                    self.precheck[d.id] = CLOCK.now()
                    self.flag[f"{room.id}:fan_on_at"] = CLOCK.now()  # R10 takes over the fan afterwards
                    for a in pre["actions"]:
                        self._action(a, room.id, "safety")
                    continue
                self._escalate()  # not a cooking context: full smoke response
                return
            other_room = any(o.room != d.room for o in alarms)
            vacant = self.b.sensor_value(room.id, "presence") is not True
            if other_room or vacant or CLOCK.now() - started >= pre["confirm_window_s"]:
                self._escalate()
                return

    def reset_safety(self):
        """The safety lock needs a manual reset, and only once every smoke sensor is normal."""
        if any(d.sensor == "smoke" and d.state["value"] != "normal" for d in self.b.devices.values()):
            raise ControlError("a smoke sensor is still not normal")
        self.safety = False
        self.precheck.clear()
        self.notify("Safety lock reset")

    # ---------- automation ----------
    def _ac_cool(self, ac, setpoint):
        ok, _ = self.send(ac, "power", "automation", on=True)
        if ok:
            self.send(ac, "mode", "automation", mode="cool")
            self.send(ac, "set_temperature", "automation", temperature=setpoint)
            self.send(ac, "fan_speed", "automation", fan_speed="auto")

    def automation(self):
        if self.safety:
            return  # R01: all automation is locked during a safety alarm
        T, R = self.th, self.rule
        now, hour, profile = CLOCK.now(), CLOCK.hour(), self.profile()
        sunrise, sunset = self.sun
        dark_time = hour >= sunset or hour < sunrise
        home, away = self.mode == "home", self.mode == "away"

        # R17: away mode turns every light off (once, when the mode changes)
        if self.rise("r17", away) and self.on("R17_away_mode"):
            for a in R["R17_away_mode"]["actions"]:
                self._action(a, None, "automation")
        # R18: intrusion alert while away (door contact opens or garage motion)
        for d in self.b.devices.values():
            if d.kind == "window":
                active = d.state["open"]
            elif d.kind == "sensor" and d.sensor == "motion":
                active = d.state["value"]
            else:
                continue
            if self.rise(f"{d.id}:r18", away and active) and self.on("R18_intrusion_alert"):
                self.alert_rooms.add(d.room)
                for a in R["R18_intrusion_alert"]["actions"]:
                    self._action(a, d.room, "automation")

        for room in self.b.rooms.values():
            rid, d, rtype = room.id, room.data, room.type
            p = self.prof(room)
            sw = self.b.in_room(rid, "light_switched")
            sensor = lambda s, fb=False, rid=rid: self.b.sensor_value(rid, s, fb)
            presence, motion = sensor("presence"), sensor("motion")
            temp, hum = sensor("temperature"), sensor("humidity")
            lux = sensor("lux", True)  # room sensor, else the building's daylight fallback room
            pres_age = self.age(f"{rid}:presence", presence)
            mot_age = self.age(f"{rid}:motion", motion)
            wins = self.b.in_room(rid, "window")
            n_open = sum(w.state["open"] for w in wins)
            win_open = n_open > 0
            win_age = self.age(f"{rid}:window", win_open)
            has_lights = d["lights"] > 0
            vacancy = self.pv(room, "vacancy_s", T["vacancy_s"])
            nl = p.get("night_light")
            night_hold = bool(nl and sw and home and self.on("R04_night_light") and in_window(nl["from"], nl["to"]))

            # R02: presence turns lights on (lux gate: dark room or evening/night)
            if presence is not None and has_lights:
                ok = True
                if p.get("lux_gate"):
                    ok = (lux is not None and lux < self.pv(room, "lux_low")) or profile in ("evening", "night")
                if self.rise(f"{rid}:r02", home and presence and ok) and self.on("R02_presence_light_on"):
                    self.act("switching_lights", "on", rid)
                    self.act("dimmable_lights", "set_level", rid, level_pct=self.on_level(room, profile))

            # R03: vacancy turns lights off (R04 keeps the entry night light, R18 keeps alert lights)
            if (presence is False and has_lights and pres_age >= vacancy and self.on("R03_vacancy_light_off")
                    and not night_hold and rid not in self.alert_rooms):
                self.act("switching_lights", "off", rid)
                self.act("dimmable_lights", "set_level", rid, level_pct=0)
                self.act("blinding_lights", "off", rid)

            # R04: entry night light while the room is vacant
            if night_hold:
                vacant = presence is None or (presence is False and pres_age >= vacancy)
                if vacant:
                    self._night_light(room, "automation")

            # R05: outdoor lights at dusk. Rooms with a presence sensor follow R02/R03 instead,
            # otherwise R05 and R03 would fight each other all night.
            if rtype == "outdoor" and sw and presence is None:
                on_cond = home and ((lux is not None and lux < self.pv(room, "lux_low")) or dark_time)
                off_cond = ((lux is not None and lux > self.pv(room, "lux_off")) or not dark_time) and not on_cond
                if self.rise(f"{rid}:r05_on", on_cond) and self.on("R05_outdoor_dusk_lights"):
                    self.act("switching_lights", "on", rid)
                if self.rise(f"{rid}:r05_off", off_cond) and self.on("R05_outdoor_dusk_lights"):
                    self.act("switching_lights", "off", rid)

            # R06: lights off when enough windows are open and daylight is bright (only rooms with params)
            params = R["R06_natural_light_off"]["params_by_room"].get(rid)
            if params and wins and has_lights:
                cond = (home and n_open >= params["min_open_windows"]
                        and lux is not None and lux > params["lux_above"])
                if self.rise(f"{rid}:r06", cond) and self.on("R06_natural_light_off"):
                    self.act("switching_lights", "off", rid)
                    self.act("dimmable_lights", "set_level", rid, level_pct=0)

            # R07: dining lights follow presence in the living room
            if rtype == "dining" and has_lights and p.get("presence_from"):
                srcs = [self.b.sensor(r.id, "presence") for r in self.b.rooms.values() if r.type == p["presence_from"]]
                srcs = [s for s in srcs if s is not None]
                if srcs:
                    src = any(s.state["value"] for s in srcs)
                    src_age = self.age(f"{rid}:r07_src", src)
                    on_c = home and src and lux is not None and lux < self.pv(room, "lux_low")
                    if self.rise(f"{rid}:r07_on", on_c) and self.on("R07_dining_follows_living"):
                        self.act("dimmable_lights", "set_level", rid, level_pct=self.on_level(room, profile))
                    if self.rise(f"{rid}:r07_off", home and not src and src_age >= vacancy) \
                            and self.on("R07_dining_follows_living"):
                        self.act("dimmable_lights", "set_level", rid, level_pct=0)

            # R08 / R09: AC by temperature and presence, paused while a window is open
            acs = self.b.in_room(rid, "ac")
            if acs:
                if wins and win_open and win_age >= T["window_open_ac_off_s"]:
                    if self.on("R09_ac_window_open"):
                        for ac in acs:
                            if ac.state["on"]:
                                self.send(ac, "power", "automation", on=False)
                                self.notify(f"{self._msg('R09_ac_window_open')} ({room.name})")
                elif not win_open and temp is not None and presence is not None and self.on("R08_ac_temp_presence"):
                    acp = p.get("ac", {})
                    on_t = acp.get("on_temp_c", T["ac_on_temp_c"])
                    setpoint = acp.get("setpoint_c", T["ac_setpoint_c"])
                    for ac in acs:
                        if temp > on_t and presence:
                            if not ac.state["on"]:
                                self._ac_cool(ac, setpoint)
                        elif ac.state["on"] and (temp < T["ac_off_temp_c"] or (not presence and pres_age >= vacancy)):
                            self.send(ac, "power", "automation", on=False)

            # R10: ventilation fans follow humidity
            fans = self.b.in_room(rid, "fan")
            if fans and hum is not None and self.on("R10_fan_humidity"):
                on_pct, off_pct = self.pv(room, "fan_on_humidity_pct"), self.pv(room, "fan_off_humidity_pct")
                rcfg = p.get("humidity_rise")
                rising = bool(rcfg) and self.hum_rise(rid, rcfg["pct"], rcfg["window_s"])
                key = f"{rid}:fan_on_at"
                if self.rise(f"{rid}:r10", hum > on_pct or rising):
                    self.flag[key] = now
                    self.act("fans", "on", rid)
                if key in self.flag:  # the fan was started by this rule
                    ran = now - self.flag[key] >= T["fan_min_run_s"]
                    low_age = self.age(f"{rid}:r10_low", hum < off_pct)
                    low = hum < off_pct and low_age >= p.get("fan_off_delay_s", 0)
                    ro = p.get("fan_run_on_after_vacancy")
                    run_on_done = bool(ro) and presence is False and pres_age >= ro["s"]
                    if ran and (low or run_on_done):
                        self.act("fans", "off", rid)
                        self.flag.pop(key, None)

            # R11: garage lighting follows motion (stays on while the door is not closed, see R16)
            if motion is not None and sw and self.on("R11_garage_light_motion"):
                door_open = any(g.state["state"] != "closed" for g in self.b.in_room(rid, "garage"))
                if motion:
                    self.act("switching_lights", "on", rid)
                elif mot_age >= self.pv(room, "motion_on_s", 300) and not door_open:
                    self.act("switching_lights", "off", rid)

            # R13: window or door left open
            if wins and self.on("R13_window_open_alert"):
                if self.rise(f"{rid}:r13", win_open and win_age >= T["window_open_alert_s"]):
                    self.notify(f"{self._msg('R13_window_open_alert')} ({room.name})")

            # R14: daily bathroom fan run (anti mold)
            if rtype == "bathroom" and fans:
                am, key = p["anti_mold"], f"{rid}:r14"
                at = _hours(am["at"])
                if self.rise(key, home and at <= hour < at + 0.25) and self.on("R14_bathroom_anti_mold"):
                    self.act("fans", "on", rid)
                    self.until[key] = now + am["run_s"]
                if key in self.until and now >= self.until[key]:
                    self.until.pop(key)
                    if f"{rid}:fan_on_at" not in self.flag:  # do not cut a fan R10 is running
                        self.act("fans", "off", rid)

            # R15: toilet light turns off by itself
            if rtype == "toilet" and sw and self.on("R15_toilet_light_auto_off"):
                lights_on = any(s.state["on"] for s in sw)
                if lights_on and self.age(f"{rid}:lights_on", lights_on) >= self.pv(room, "light_auto_off_s", 600):
                    self.act("switching_lights", "off", rid)

            # R16: garage door alerts
            for g in self.b.in_room(rid, "garage"):
                opened = g.state["state"] != "closed"
                if self.rise(f"{rid}:r16", opened) and self.on("R16_garage_door_alerts"):
                    self.act("switching_lights", "on", rid)
                    self.notify(self._msg("R16_garage_door_alerts"))
                if opened:
                    every = self.pv(room, "door_open_notify_s", 600)
                    if self.age(f"{rid}:r16_age", True) >= every and self.due(f"{rid}:r16_rep", every):
                        self.notify(R["R16_garage_door_alerts"]["repeat_notify"]["message"])
                else:
                    self.age(f"{rid}:r16_age", False)
                    self.flag.pop(f"{rid}:r16_rep", None)

            # R20: Welcome Home, entry lights when the door opens shortly after arriving
            if rtype == "entry" and wins and sw and self.on("R20_welcome_entry_lights"):
                within = R["R20_welcome_entry_lights"]["trigger"]["then"]["within_s"]
                recent = self.arrived_at is not None and now - self.arrived_at <= within
                if self.rise(f"{rid}:r20", home and recent and win_open):
                    self.act("switching_lights", "on", rid)

    def tick(self, dt):
        for dev in self.b.devices.values():
            dev.tick(dt)
        self._record()
        self.check_safety()
        self.automation()

    def snapshot(self):
        now = CLOCK.now()
        snap = self.b.snapshot()
        active = {i for i, (_, exp) in self.locks.items() if now < exp}
        for r in snap["rooms"]:
            r["override"] = any(dev["id"] in active for dev in r["devices"])
        snap.update(
            time={"hour": round(CLOCK.hour(), 2), "profile": self.profile(), "speed": CLOCK.speed},
            mode=self.mode, safety=self.safety, precheck=bool(self.precheck),
            notifications=self.notifications[-10:],
            scenes=[{"id": s["id"], "name": s["name"]} for s in self.rules["scenes"]],
            optional_rules=[{"id": r["id"], "name": r["name"], "enabled": self.enabled[r["id"]]}
                            for r in self.rules["rules"] if r.get("requires_user_opt_in")],
        )
        return snap