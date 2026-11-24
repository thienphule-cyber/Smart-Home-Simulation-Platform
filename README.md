# Smart Building Simulator

A 2D digital twin of a smart home / building. It simulates lighting, ventilation, air conditioning, sensors, pushbuttons, scenes, safety and automation rules for six building types, following control logic v4.1.

The backend (Python, FastAPI) holds the simulation and exposes a REST API and a WebSocket. The frontend is a single-page dashboard that shows every room and lets you control and test it.

## Project structure

    SmartBuildingSimulator/
    ├── backend/
    │   ├── main.py        REST API, WebSocket, simulation loop
    │   ├── building.py    loads the JSON files, creates devices, groups and validation
    │   ├── devices.py     device models, physical limits, simulation clock
    │   └── control.py     priority, commands, pushbuttons, scenes, safety, automation rules
    ├── frontend/
    │   ├── index.html
    │   ├── style.css
    │   └── app.js
    └── data/
        ├── buildings.json rooms and device counts per building
        ├── devices.json   device models, ATIOS groups, infrastructure sets
        └── rules.json     thresholds, room profiles, rules, scenes, pushbuttons

All behaviour is data driven: changing the JSON files changes buildings, thresholds, rules, scenes and pushbutton actions without touching the code.

## Buildings

| Building | Floors | Hub set | Lights | AC | Fans | Pushbuttons | Window contacts |
|---|---|---|---|---|---|---|---|
| Studio | 1 | mini | 12 | 1 | 2 | 6 | 3 |
| 1 Bedroom | 1 | mini | 10 | 2 | 2 | 5 | 6 |
| 2 Bedroom | 1 | pro | 21 | 2 | 2 | 7 | 21 |
| 3 Bedroom | 1 | pro | 32 | 3 | 3 | 12 | 11 |
| H01 Single-Storey House | 1 | pro | 30 | 4 | 4 | 15 | 11 |
| H02 Two-Storey House | 2 | pro + upper-floor Wi-Fi AP | 38 | 4 | 4 | 16 | 13 |

Room types: entry, living, dining, kitchen, bedroom, bathroom, toilet, laundry, storage, outdoor, garage.

On startup each building is checked against its `expected_totals` (lights, switching, dimmable, blinding, AC, fans, pushbuttons, contacts, ATIOS groups, EOA64 masters and sensor counts). Differences and SmartCore limit violations are printed as warnings and available from the validation endpoint.

## Devices

Every device instance is named `<room_id>_<model>_<index>`.

| Device | Model | What it does |
|---|---|---|
| Switching light | Eltako ESR64PF | on, off, toggle. Never takes a level. |
| Ventilation fan | ESR64PF channel | on, off, toggle |
| Dimmable light | Eltako EUD64 | on, off, toggle, set_level 0-100 (minimum level 5%) |
| Blinding / accent light | Eltako ESB64 | on, off, toggle, set_level 0-100, set_mode (normal / blinding). **It is a light, not a window blind.** |
| EOA64 | Eltako EOA64 | EnOcean receiver on the room's master actuator |
| Pushbutton | Eltako F2T55E | press, double_press, long_press, release |
| Window / door contact | Eltako FTKE | open / closed |
| Presence | Aqara FP2 | true / false |
| Motion (garage) | Aqara Motion P1 | true / false |
| Lux | Aqara P1 | 0-10000 lux |
| Temperature, humidity | generic | temperature -10 to 50 C, humidity 0-100% |
| Smoke | Clipsal Firetek | normal / alarm / fault (simulation only, not a certified fire alarm) |
| Air conditioner | generic split unit | power, mode (off, cool, heat, dry, fan, auto), set_temperature 16-30 C, fan_speed (auto, low, medium, high), 180 s compressor protection |
| Garage door | controller | open, close, stop, about 15 s travel time with opening / closing / stopped states |

### EOA64 master

Each room with lights has one master actuator that carries the room's EOA64, named by the `eoa64` field of the room. It is the first device of that model in the room (dimmer first, then switching, then blinding). Same-room pushbutton commands use this path and work without Wi-Fi. Scenes, AC, cross-room and cross-floor commands go through the hub.

### Infrastructure sets

- **mini**: Homey Pro Mini, Homey Bridge, Apple Hub, EOA64, ATIOS SmartCore, Wi-Fi coverage
- **pro**: Homey Pro, Apple Hub, EOA64, ATIOS SmartCore, Wi-Fi coverage
- H02 adds an extra Wi-Fi access point for the upper floor.

## Groups

A group only maps to real devices; it is not an actuator.

**ATIOS groups** (building wide, numbers 0-15 of the SmartCore):

| Group | Name | Members |
|---|---|---|
| 0 | All Home Lights | every light |
| 1 | Common Areas | entry, living, dining, kitchen, laundry, storage |
| 2 | Bedrooms | bedrooms |
| 3 | Bathrooms | bathrooms and toilets |
| 4 | Outdoor | outdoor areas and garage |
| 5 | Emergency / Security | reserved, no members yet |
| 6 | Floor 0 Lights | only in buildings with more than one floor |
| 7 | Floor 1 Lights | only in buildings with more than one floor |

A group is created only when it has at least one member.

**Room groups**: `<room>_lights` (switching and dimmable lights) and `<room>_blinding` (blinding lights, only in rooms that have them).

**SmartCore limits** checked per building: 64 control gear, 64 control devices, 16 groups, 16 scenes.

## Control priority and overrides

Priority from highest to lowest: **safety, pushbutton, online, automation**.

- A manual command from a pushbutton (60 min) or from the dashboard / API (30 min) locks that device against lower priorities, so automation cannot undo it.
- "Resume auto" clears the locks of a room.
- While the safety lock is active, every command except safety actions is refused.
- The garage door can never be moved by automation (R12).
- Switching mode (home or away) and the geofence arrive event clear all manual locks.

## Pushbuttons

Actions come from `rules.json`. Each button supports press, double_press and long_press where defined.

| Room type | Button | Press | Double press | Long press |
|---|---|---|---|---|
| Bedroom | door | toggle room lights | Sleep scene | dim cycle |
| Bedroom | bedside | toggle, switching on at 20% | none | dim down |
| Living | wall | toggle room lights | Movie Night scene | dim cycle |
| Others | default | toggle room lights | none | dim cycle |

The result of a press reports the action and the path used: `direct` (EOA64 master, same room) or `hub` (scene).

## Scenes

| Scene | Effect |
|---|---|
| All Off | every light off |
| Welcome Home | entry, living, kitchen, laundry, storage 100%; dining 70%; bedrooms 50% |
| Sleep | every light off, entry night light kept |
| Movie Night | living dimmers 15%, switching off, blinding 0% |
| Emergency | every light 100%, blinding 100% |
| Night Light | entry night light; outdoor and garage on; living and dining 10%; bathrooms on (Studio only) |
| Reading | bedroom dimmers 50%, switching on, blinding 80% |
| Cooking | kitchen 100%; entry, living, dining, laundry, storage 60% |
| Party | living dimmers 60%, switching on, blinding 100% |
| Dinner | dining 80% |
| Relax | bedroom dimmers 30%, blinding 0% |

Switching lights follow the scene as on / off because they cannot dim.

## Automation rules

Rules run once per second, only for rooms that have the devices a rule needs. Thresholds and timings come from `rules.json` and room profiles by room type.

| Rule | Behaviour |
|---|---|
| R01 Smoke alarm | Emergency scene, all fans and AC off, notification, automation locked until the alarm is normal and the lock is reset by hand |
| R01b Kitchen pre-check | Smoke in a kitchen with presence and a cooking context (Cooking scene, humidity rise or fan on) turns fans on and waits 60 s. It escalates to R01 if smoke persists, another room alarms, or the kitchen becomes vacant. If smoke clears, it is treated as cooking smoke |
| R02 Presence light on | Lights on at the profile level; lux gate applies to entry, living, dining and outdoor rooms (or evening / night) |
| R03 Vacancy light off | Lights off after the room's vacancy time |
| R04 Entry night light | 23:00-06:00 keeps half of the entry's switching lights on while vacant |
| R05 Outdoor dusk | Outdoor lights on when dark, off in daylight (rooms with a presence sensor follow R02 / R03 instead) |
| R06 Natural light off | Lights off when enough windows are open and daylight is bright (2BR living: 3 windows and 400 lux; H02 living: 2 windows and 400 lux) |
| R07 Dining follows living | Dining dimmers follow presence in the living room |
| R08 AC control | Cool when temperature is above the room threshold, someone is present and windows are closed; off when cool enough or vacant |
| R09 AC window pause | AC off when a window stays open for 2 min, with a notification |
| R10 Fan by humidity | Fan on above the humidity threshold or on a quick rise; off after humidity drops, with minimum run time, delay and run-on after vacancy |
| R11 Garage motion light | Garage lights follow motion (5 min hold, stay on while the door is open) |
| R12 Garage door manual | Garage door is user controlled only |
| R13 Window open alert | Notification after 15 min open |
| R14 Bathroom anti-mold | Fan runs 10 min at 09:00 every day |
| R15 Toilet light auto off | Toilet light off after 10 min |
| R16 Garage door alerts | Garage lights on and notification when opened; repeat notice after 10 min |
| R17 Away mode | All lights off when away mode starts |
| R18 Intrusion alert | While away, an opened contact or garage motion turns on the lights of that room and sends a notification |
| R19 Arrive home | Geofence arrive switches to home mode |
| R20 Welcome entry lights | Entry lights on when the door opens within 10 min of arriving |
| R21 Welcome porch lights | Porch lights on at arrival when dark (H01 rear porch, H02 porch) |
| R22 Garage door on arrive | Opens the garage door and lights on arrival. Disabled by default, opt-in |

### Room profile defaults

| Room type | Main settings |
|---|---|
| entry | lux gate 80, 100%, vacancy 3 min |
| living | lux gate 150, 70%, vacancy 15 min, AC from 25 C to 24 C |
| dining | lux gate 120, 50%, presence from living, vacancy 15 min |
| kitchen | fan at 65% humidity, rise 10% in 5 min, vacancy 10 min |
| bedroom | 40% day / 20% night, vacancy 20 min, AC from 26 C to 25 C |
| bathroom | 100%, vacancy 10 s, fan at 70%, 8 min run-on above 60% |
| toilet | fan on at 65%, off at 55% after 5 min, light off after 10 min |
| laundry | vacancy 5 min |
| storage | vacancy 3 min |
| outdoor | on below 80 lux, off above 150 lux, vacancy 5 min |
| garage | motion hold 5 min, door-open notice after 10 min |

Global defaults: lux 150, vacancy 5 min, AC on above 26 C and off below 23 C with setpoint 24 C, fan on at 70% and off at 60%, minimum fan run 2 min.

## Modes and geofence

- **Modes**: `home` (default) and `away`.
- Switching to `away` clears manual locks and turns every light off (R17); opened contacts or garage motion raise intrusion alerts (R18).
- Switching to `home` clears alerts and runs the arrive logic (R19-R22).
- **Geofence events** (from the Apple Hub): `arrive` runs R19-R22; `leave` switches to away mode.

## Simulation clock and time profiles

- The simulation ticks once per second.
- The hour of day can follow the computer clock or be pinned for demos; timers can run from 0.1x to 600x.
- Time profiles: morning 06-09, day 09-18, evening 18-22, night 22-06. Sunrise 06:00, sunset 19:00 (estimated).
- Sensors and window contacts are simulated inputs set from the dashboard.

## API

Base path `/api`. Errors from the control system (safety lock, override, physical limits) are returned as HTTP 400 with a message.

| Method | Path | Purpose |
|---|---|---|
| GET | `/buildings` | list buildings |
| GET | `/buildings/{bid}` | full snapshot of a building |
| GET | `/buildings/{bid}/validation` | totals, expected totals, mismatches, SmartCore usage and limits |
| POST | `/buildings/{bid}/devices/{did}/command` | send a command to a device |
| POST | `/buildings/{bid}/devices/{did}/input` | set a sensor value or window contact |
| POST | `/buildings/{bid}/pushbuttons/{did}/press` | simulate a pushbutton event |
| POST | `/buildings/{bid}/groups/{gid}/command` | command every light of a group |
| POST | `/buildings/{bid}/scenes/{sid}` | activate a scene |
| POST | `/buildings/{bid}/rooms/{rid}/resume` | clear manual locks in a room |
| POST | `/buildings/{bid}/mode` | set home or away |
| POST | `/buildings/{bid}/geofence` | send a geofence arrive or leave event |
| GET | `/buildings/{bid}/rules` | list rules and their state |
| PUT | `/buildings/{bid}/rules/{rule_id}` | enable or disable a rule |
| POST | `/buildings/{bid}/safety/reset` | reset the safety lock |
| GET, POST | `/sim` | read or set the hour, real-time mode and speed |

`/ws/{bid}` is a WebSocket that pushes a full snapshot of the building every second.

## Dashboard

- Building selector, one card per room (grouped by floor in two-storey houses) with live icons for every device.
- Icons show state and level: glowing lights with brightness, spinning fans, open windows in red, blinking smoke alarm, a small dot on each room's EOA64 master.
- Room panel: groups, pushbuttons (with their configured actions), lights, blinding lights (level and mode), AC, fans, windows, garage door and simulated sensors.
- Building panel: home / away mode, geofence arrive / leave, optional rules (R22), scenes, ATIOS groups with SmartCore usage, infrastructure list and notifications.
- Header: time-of-day slider, live clock, simulation speed, smoke alarm trigger / reset, live status line and connection indicator.
- A "Resume auto" button appears on rooms with an active manual override.
- Error messages from the backend appear as a toast.

## Notes and limits

- Values marked as estimated in `devices.json` and `rules.json` (fade times, ranges, sunrise and sunset, thresholds) are assumptions and can be tuned.
- The ESB64 is modelled as a blinding / accent light per control logic v4.1. Check the datasheet before relying on this reading.
- DALI wiring is not simulated; ATIOS groups and scenes are logical groups mapped to real devices.
- The smoke sensor and its rules are a simulation only and not a replacement for a certified fire alarm.