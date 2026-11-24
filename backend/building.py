"""Building model: loads the JSON files and expands rooms into device instances and groups."""
import json
from pathlib import Path

from devices import Device

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

LIGHT_KINDS = ("light_switched", "light_dimmable", "light_blinding")
ROOM_LIGHT_KINDS = ("light_switched", "light_dimmable")
# Which light kind carries the room's EOA64 master, by the model named in the room's "eoa64" field
EOA64_KINDS = {"ESR64PF": ("light_switched",), "EUD64": ("light_dimmable",), "ESB64": ("light_blinding",)}


def load_json(name):
    with open(DATA_DIR / name, encoding="utf-8") as f:
        return json.load(f)


class Group:
    """A group of lights. It only maps to real devices, it is not an actuator.

    kind "atios"          : building-wide ATIOS group (number 0-15), defined in devices.json
    kind "room_lights"    : switching + dimmable lights of one room
    kind "room_blinding"  : blinding lights of one room
    """

    def __init__(self, id, name, kind, members, number=None, room=None):
        self.id, self.name, self.kind, self.members = id, name, kind, members
        self.number, self.room = number, room

    def to_dict(self):
        return {"id": self.id, "name": self.name, "kind": self.kind,
                "number": self.number, "members": self.members}


class Room:
    def __init__(self, data):
        self.data = data
        self.id, self.name, self.floor, self.type = data["id"], data["name"], data["floor"], data["type"]
        self.sens = data.get("sens", {})
        self.device_ids, self.group_ids = [], []
        self.master = None  # id of the device carrying the room's EOA64


class Building:
    def __init__(self, bdef, catalog):
        self.id, self.name, self.floors = bdef["id"], bdef["name"], bdef["floors"]
        self.expected = bdef.get("expected_totals", {})
        self.fallback_room = bdef["daylight_fallback_room"]
        self.catalog = catalog
        names = {**catalog["models"], **catalog["controllers"]}
        infra = list(catalog["infrastructure_sets"][bdef["infrastructure"]]) + list(bdef.get("extra_infrastructure", []))
        self.infrastructure = [
            names.get(k, {}).get("name") or names.get(k, {}).get("full_name") or k for k in infra
        ]
        self.rooms, self.devices, self.groups = {}, {}, {}
        for rd in bdef["rooms"]:
            self._expand(rd)
        self._build_atios()

    def _expand(self, rd):
        """Create device instances: <room_id>_<model>_<index>."""
        M, rid = self.catalog["models"], rd["id"]
        room = Room(rd)
        self.rooms[rid] = room

        def add(model, idx, kind, spec, sensor=None):
            dev = Device(f"{rid}_{model}_{idx}", model, kind, rid, spec, sensor)
            self.devices[dev.id] = dev
            room.device_ids.append(dev.id)

        # ESR64PF channels: switched lights first, then ventilation fans
        for i in range(rd["sw"]):
            add("ESR64PF", i, "light_switched", M["ESR64PF"])
        fan_spec = {**M["ESR64PF"], "commands": M["FAN"]["commands"]}
        for i in range(rd["fans"]):
            add("ESR64PF", rd["sw"] + i, "fan", fan_spec)
        for i in range(rd["dim"]):
            add("EUD64", i, "light_dimmable", M["EUD64"])
        for i in range(rd["blinding"]):  # ESB64 = blinding / accent light, NOT a window blind
            add("ESB64", i, "light_blinding", M["ESB64"])
        ac_spec = {**M["AC_UNIT"], "commands": M["AC_CONTROLLER"]["commands"]}
        for i in range(rd["ac"]):
            add("AC_CONTROLLER", i, "ac", ac_spec)
        for i in range(rd["pb"]):
            add("F2T55E", i, "pushbutton", M["F2T55E"])
        for i in range(rd["ftke"]):
            add("FTKE", i, "window", M["FTKE"])
        for stype, n in room.sens.items():
            model = self.catalog["sensor_to_model"][stype]
            for i in range(n):
                add(model, i, "sensor", M[model], stype)
        for other in rd.get("other", []):  # e.g. "garage_door_controller"
            add(other.upper(), 0, "garage", M[other.upper()])

        self._mark_master(room, rd.get("eoa64"))

        # Room groups: <room>_lights (switching + dimmable) and <room>_blinding when the room has blinding lights
        self._room_group(room, "lights", "room_lights", ROOM_LIGHT_KINDS)
        if rd["blinding"]:
            self._room_group(room, "blinding", "room_blinding", ("light_blinding",))

    def _mark_master(self, room, model):
        """The room's EOA64 sits on one master actuator: the first device of the named model."""
        if not model:
            return
        kinds = EOA64_KINDS.get(model, ())
        devs = [self.devices[i] for i in room.device_ids]
        master = next((d for d in devs if d.kind in kinds), None) or next((d for d in devs if d.model == model), None)
        if master:
            master.eoa64 = True
            room.master = master.id

    def _room_group(self, room, suffix, kind, kinds):
        members = [i for i in room.device_ids if self.devices[i].kind in kinds]
        if not members:
            return
        g = Group(f"{room.id}_{suffix}", f"{room.name} {suffix}", kind, members, room=room.id)
        self.groups[g.id] = g
        room.group_ids.append(g.id)

    # ---- ATIOS groups (devices.json -> atios_groups) ----
    @staticmethod
    def _selected(room, sel):
        if sel == "all":
            return True
        if not isinstance(sel, dict):
            return False
        if "types" in sel and room.type not in sel["types"]:
            return False
        if "floor" in sel and room.floor != sel["floor"]:
            return False
        return True

    def _build_atios(self):
        """A group holds every light of the rooms it selects and exists only when it has a member."""
        for g in self.catalog["atios_groups"]:
            above = g.get("only_if_floors_above")
            if above is not None and self.floors <= above:
                continue
            if g["select"] == "reserved":
                continue
            members = [i for r in self.rooms.values() if self._selected(r, g["select"])
                       for i in r.device_ids if self.devices[i].kind in LIGHT_KINDS]
            if members:
                gid = f"{self.id}_g{g['group']}"
                self.groups[gid] = Group(gid, g["name"], "atios", members, number=g["group"])

    # ---- queries ----
    def in_room(self, room_id, *kinds):
        return [d for d in (self.devices[i] for i in self.rooms[room_id].device_ids) if d.kind in kinds]

    def sensor(self, room_id, stype):
        return next((d for d in self.in_room(room_id, "sensor") if d.sensor == stype), None)

    def sensor_value(self, room_id, stype, fallback=False):
        """Sensor value in a room; lux can fall back to the building's daylight room."""
        dev = self.sensor(room_id, stype)
        if dev is None and fallback and stype == "lux":
            dev = self.sensor(self.fallback_room, "lux")
        return None if dev is None else dev.state["value"]

    # ---- validation ----
    def totals(self):
        devs = list(self.devices.values())
        count = lambda kind: sum(d.kind == kind for d in devs)
        t = {"lights": sum(r.data["lights"] for r in self.rooms.values()),
             "sw": count("light_switched"), "dim": count("light_dimmable"), "blinding": count("light_blinding"),
             "ac": count("ac"), "fans": count("fan"), "pb": count("pushbutton"),
             "ftke": count("window"), "eoa64": sum(d.eoa64 for d in devs),
             "atios_groups": sum(g.kind == "atios" for g in self.groups.values())}
        for s in ("presence", "motion", "lux", "temperature", "humidity", "smoke"):
            t[s] = sum(d.sensor == s for d in devs)
        return t

    def mismatches(self):
        """Return {key: (expected, actual)} for totals that differ from expected_totals."""
        t = self.totals()
        return {k: (v, t.get(k)) for k, v in self.expected.items() if t.get(k) != v}

    def atios_load(self):
        """Address usage on the ATIOS SmartCore compared with its limits."""
        core = self.catalog["controllers"]["atios_smartcore"]
        devs = list(self.devices.values())
        return {"control_gear": sum(d.model in core["counts_as_control_gear"] for d in devs),
                "control_devices": sum(d.model in core["counts_as_control_device"] for d in devs),
                "groups": sum(g.kind == "atios" for g in self.groups.values()),
                "limits": core["limits"]}

    def limit_warnings(self):
        load, lim = self.atios_load(), self.catalog["controllers"]["atios_smartcore"]["limits"]
        pairs = (("control_gear", "control_gear_max"), ("control_devices", "control_devices_max"), ("groups", "groups_max"))
        return {k: (load[k], lim[m]) for k, m in pairs if load[k] > lim[m]}

    def snapshot(self):
        rooms = [{"id": r.id, "name": r.name, "type": r.type, "floor": r.floor, "lights": r.data["lights"],
                  "master": r.master,
                  "devices": [self.devices[i].to_dict() for i in r.device_ids],
                  "groups": [self.groups[g].to_dict() for g in r.group_ids]}
                 for r in self.rooms.values()]
        return {"id": self.id, "name": self.name, "floors": self.floors,
                "infrastructure": self.infrastructure, "rooms": rooms,
                "atios_groups": [g.to_dict() for g in self.groups.values() if g.kind == "atios"],
                "atios": self.atios_load()}


def load_buildings():
    catalog = load_json("devices.json")
    return {b["id"]: Building(b, catalog) for b in load_json("buildings.json")["buildings"]}


if __name__ == "__main__":  # quick check: python backend/building.py
    for b in load_buildings().values():
        status = "OK" if not b.mismatches() else b.mismatches()
        print(f"{b.id}: {len(b.devices)} devices, {len(b.groups)} groups -> {status}")
        if b.limit_warnings():
            print(f"  ATIOS limits exceeded: {b.limit_warnings()}")