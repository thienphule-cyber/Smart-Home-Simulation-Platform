// Smart Building Digital Twin: 2D panel dashboard.
// State arrives from the backend over WebSocket; commands go through the REST API.
// Open this page from the backend server (http://localhost:<port>/frontend/).

const $ = id => document.getElementById(id);
const plan = $('plan'), panel = $('panel');
let bid = null, snap = null, selRoom = null, builtFor = null;
let cards = {}, els = {}, lastHtml = '';
let ws = null, wsToken = 0, toastTimer = null;
let pbRules = null; // "pushbuttons" section of data/rules.json (labels and actions)

// ---------- Icons (inline SVG, no files to download) ----------
const ICONS = {
  bulb: '<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z"/>',
  spot: '<path d="M8 3h8l2 6H6z"/><path d="M9 12l-3 9M15 12l3 9M12 12v9"/>',
  ac: '<rect x="3" y="4" width="18" height="8" rx="2"/><path d="M6 9h12M7 16c0 2 1.5 2 1.5 4M12 16c0 2 1.5 2 1.5 4M17 16c0 2 1.5 2 1.5 4"/>',
  fan: '<circle cx="12" cy="12" r="1.6"/><ellipse cx="12" cy="6.5" rx="2.2" ry="4"/><ellipse cx="12" cy="6.5" rx="2.2" ry="4" transform="rotate(120 12 12)"/><ellipse cx="12" cy="6.5" rx="2.2" ry="4" transform="rotate(240 12 12)"/>',
  window: '<rect x="4" y="3" width="16" height="18" rx="1"/><path d="M12 3v18M4 12h16"/>',
  person: '<circle cx="12" cy="7" r="3.5"/><path d="M5 21c0-4 3-7 7-7s7 3 7 7"/>',
  motion: '<circle cx="12" cy="12" r="2"/><path d="M7.5 7.5a6.5 6.5 0 0 0 0 9M16.5 7.5a6.5 6.5 0 0 1 0 9M4.5 4.5a10.5 10.5 0 0 0 0 15M19.5 4.5a10.5 10.5 0 0 1 0 15"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/>',
  thermo: '<path d="M10 14V5a2 2 0 0 1 4 0v9a4 4 0 1 1-4 0z"/>',
  drop: '<path d="M12 3c3 4 6 7 6 11a6 6 0 0 1-12 0c0-4 3-7 6-11z"/>',
  bell: '<path d="M6 17v-6a6 6 0 0 1 12 0v6l2 2H4zM10 21h4"/>',
  button: '<rect x="5" y="3" width="14" height="18" rx="2"/><circle cx="12" cy="9" r="2"/><circle cx="12" cy="16" r="2"/>',
  garage: '<path d="M3 21V9l9-6 9 6v12"/><path d="M7 21v-9h10v9M7 15h10M7 18h10"/>',
};
// ESB64 is a blinding / accent light (kind "light_blinding"), NOT a window blind
const KIND_ICON = {
  light_switched: 'bulb', light_dimmable: 'bulb', light_blinding: 'spot', ac: 'ac', fan: 'fan',
  window: 'window', pushbutton: 'button', garage: 'garage',
};
const SENSOR_ICON = { presence: 'person', motion: 'motion', lux: 'sun', temperature: 'thermo', humidity: 'drop', smoke: 'bell' };
const ORDER = ['light_switched', 'light_dimmable', 'light_blinding', 'ac', 'fan', 'window', 'garage', 'sensor', 'pushbutton'];
const AC_MODES = ['off', 'cool', 'heat', 'dry', 'fan', 'auto'];   // devices.json: AC_UNIT.modes
const FAN_SPEEDS = ['auto', 'low', 'medium', 'high'];            // devices.json: AC_UNIT.fan_speeds
const BLINDING_MODES = ['normal', 'blinding'];                   // devices.json: ESB64.modes
const PB_EVENTS = { press: 'Press', double_press: 'Double', long_press: 'Long' };

const icon = name =>
  `<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
const fmt = h => String(Math.floor(h)).padStart(2, '0') + ':' + String(Math.floor((h % 1) * 60)).padStart(2, '0');

// ---------- API ----------
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 4000);
}

async function api(path, body, method) {
  method = method || (body === undefined ? 'GET' : 'POST');
  let res;
  try {
    res = await fetch('/api' + path, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    toast('Cannot reach the server');
    throw err;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = typeof data.detail === 'string' ? data.detail : res.statusText;
    toast(msg); // e.g. safety lock, override, physical limits
    throw new Error(msg);
  }
  return data;
}
const post = (path, body = {}) => api(path, body);
const put = (path, body = {}) => api(path, body, 'PUT');
const B = () => `/buildings/${bid}`;
const cmd = (id, command, args = {}) => post(`${B()}/devices/${id}/command`, { command, args });
const input = (id, value) => post(`${B()}/devices/${id}/input`, { value });

// Run an action, then refresh the state right away (errors are already shown as toasts)
async function run(fn) {
  try { await fn(); } catch (_) { /* shown by api() */ }
  try { apply(await api(B())); } catch (_) { /* ignore */ }
  lastHtml = '';
  renderPanel(true);
}

// ---------- WebSocket ----------
function setConn(on) {
  $('conn').textContent = on ? 'live' : 'offline';
  $('conn').classList.toggle('ok', on);
}

function connect() {
  const token = ++wsToken;
  if (ws) ws.close();
  setConn(false);
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/${bid}`);
  ws.onopen = () => setConn(true);
  ws.onmessage = e => { if (token === wsToken) apply(JSON.parse(e.data)); };
  ws.onclose = () => {
    if (token !== wsToken) return; // replaced by a newer connection
    setConn(false);
    setTimeout(() => token === wsToken && connect(), 2000);
  };
}

function apply(s) {
  if (s.id !== bid) return;
  snap = s;
  if (builtFor !== s.id) buildPlan(s);
  update();
}

// ---------- Floor plan ----------
const chip = d => {
  const name = d.sensor ? SENSOR_ICON[d.sensor] : KIND_ICON[d.kind];
  return `<div class="dev k-${d.kind}${d.sensor ? ' s-' + d.sensor : ''}${d.eoa64 ? ' eoa' : ''}" data-d="${d.id}" ` +
    `title="${d.id}${d.eoa64 ? ' (EOA64 master)' : ''}">` + icon(name) + '<span class="val"></span></div>';
};

const card = r => {
  const devs = r.devices.slice().sort((a, b) => ORDER.indexOf(a.kind) - ORDER.indexOf(b.kind));
  return `<div class="card" tabindex="0" role="button" data-r="${r.id}">` +
    `<div class="head"><b>${r.name}</b><button class="resume" data-a="resume" data-r="${r.id}" hidden>Resume auto</button></div>` +
    `<div class="devs">${devs.map(chip).join('')}</div></div>`;
};

function buildPlan(s) {
  builtFor = s.id;
  cards = {}; els = {};
  const floors = {};
  s.rooms.forEach(r => (floors[r.floor] ||= []).push(r));
  const names = ['Ground floor', 'First floor', 'Second floor'];
  plan.innerHTML = Object.keys(floors).sort((a, b) => b - a).map(f =>
    `<section>${s.floors > 1 ? `<h2 class="floor">${names[f] || 'Floor ' + f}</h2>` : ''}` +
    `<div class="grid">${floors[f].map(card).join('')}</div></section>`).join('');
  s.rooms.forEach(r => {
    cards[r.id] = plan.querySelector(`.card[data-r="${r.id}"]`);
    r.devices.forEach(d => (els[d.id] = plan.querySelector(`.dev[data-d="${d.id}"]`)));
  });
}

// Update one icon from its device state
function paint(el, d) {
  const s = d.state;
  let on = false, text = '';
  switch (d.kind) {
    case 'light_switched': on = s.on; el.style.setProperty('--lvl', 1); break;
    case 'light_dimmable': on = s.on; text = s.level + '%'; el.style.setProperty('--lvl', s.level / 100); break;
    case 'light_blinding':
      on = s.on; text = s.level + '%';
      el.style.setProperty('--lvl', s.level / 100);
      el.classList.toggle('blinding', s.mode === 'blinding');
      break;
    case 'ac': on = s.on; text = s.setpoint + '°'; break;
    case 'fan': on = s.on; break;
    case 'window': el.classList.toggle('open', s.open); text = s.open ? 'open' : ''; break;
    case 'garage': on = s.position > 0; text = s.state; break;
    case 'sensor': {
      const v = s.value;
      if (d.sensor === 'presence' || d.sensor === 'motion') on = v;
      else if (d.sensor === 'smoke') {
        el.classList.toggle('alarm', v === 'alarm');
        el.classList.toggle('warn', v === 'fault');
        text = v === 'normal' ? '' : v;
      } else text = v + { lux: ' lx', temperature: '°', humidity: '%' }[d.sensor];
    }
  }
  el.classList.toggle('on', !!on);
  el.querySelector('.val').textContent = text;
}

function update() {
  const all = snap.rooms.flatMap(r => r.devices);
  for (const r of snap.rooms) {
    const c = cards[r.id];
    c.classList.toggle('sel', r.id === selRoom);
    c.classList.toggle('alarm', snap.safety);
    c.querySelector('.resume').hidden = !r.override;
    r.devices.forEach(d => paint(els[d.id], d));
  }
  const lights = all.filter(d => d.kind.startsWith('light_'));
  const countOn = k => all.filter(d => d.kind === k && d.state.on).length;
  const t = snap.time;
  $('status').textContent =
    `${snap.mode} mode | ${t.profile} | Lights on ${lights.filter(d => d.state.on).length}/${lights.length}` +
    ` | AC ${countOn('ac')} | Fans ${countOn('fan')}` +
    (snap.precheck ? ' | Kitchen smoke pre-check' : '') + (snap.safety ? ' | SAFETY LOCK' : '');
  $('smoke').textContent = snap.safety ? 'Reset smoke alarm' : 'Trigger smoke alarm';
  $('smoke').classList.toggle('alarm', snap.safety);
  if (document.activeElement !== $('hour')) {
    $('hour').value = t.hour;
    $('hourLabel').textContent = fmt(t.hour);
  }
  const speed = $('speed');
  if (document.activeElement !== speed && [...speed.options].some(o => +o.value === t.speed)) speed.value = t.speed;
  renderPanel(false);
}

function selectRoom(id) {
  selRoom = id;
  update();
  lastHtml = '';
  renderPanel(true);
}

// ---------- Side panel ----------
const btn = (text, data, opts = {}) =>
  `<button${opts.cls ? ` class="${opts.cls}"` : ''}${opts.title ? ` title="${opts.title}"` : ''} ` +
  `${Object.entries(data).map(([k, v]) => `data-${k}="${v}"`).join(' ')}>${text}</button>`;
const chk = (t, a, d, v) => `<label>${t}<input type="checkbox" data-a="${a}" data-d="${d}" ${v ? 'checked' : ''}></label>`;
const rng = (t, a, d, v, lo, hi, step = 1) =>
  `<label>${t}<small>${v}</small><input type="range" min="${lo}" max="${hi}" step="${step}" value="${v}" data-a="${a}" data-d="${d}"></label>`;
const drop = (t, a, d, list, v) =>
  `<label>${t}<select data-a="${a}" data-d="${d}">${list.map(x => `<option ${x === v ? 'selected' : ''}>${x}</option>`).join('')}</select></label>`;

// Label and available events of one pushbutton, taken from rules.json (pushbuttons)
function pbProfile(room, p) {
  if (!pbRules) return { label: '', prof: null, events: ['press'] };
  const list = pbRules.by_type[room.type];
  const idx = +p.id.slice(p.id.lastIndexOf('_') + 1);
  const prof = list && idx < list.length ? list[idx] : pbRules.default;
  return { label: prof.label || '', prof, events: Object.keys(PB_EVENTS).filter(e => prof[e]) };
}

function roomPanel(r) {
  const of = (...kinds) => r.devices.filter(d => kinds.includes(d.kind));
  const n = (list, i) => (list.length > 1 ? ` ${i + 1}` : '');
  const h = [`<h2>${r.name}</h2>`,
    `<p class="small">${r.type} | floor ${r.floor} | EOA64 master: ${r.master || 'none'}</p>`];
  if (r.override) h.push(`<p class="ovr">Manual control active ${btn('Resume auto', { a: 'resume', r: r.id })}</p>`);

  // Room groups (logical groups mapped to real devices)
  if (r.groups.length) h.push('<h3>Groups</h3>');
  r.groups.forEach(g => h.push(
    `<div class="row"><span>${g.name}</span>${['on', 'off'].map(c => btn(c, { a: 'group', g: g.id, c })).join('')}</div>`));

  // Physical pushbuttons: buttons follow the events defined in rules.json
  const pbs = of('pushbutton');
  if (pbs.length) h.push('<h3>Pushbuttons (F2T55E)</h3>');
  pbs.forEach((p, i) => {
    const { label, prof, events } = pbProfile(r, p);
    h.push(`<div class="row"><span>${label ? 'Pushbutton ' + label : 'Pushbutton' + n(pbs, i)}</span>` +
      events.map(e => btn(PB_EVENTS[e], { a: 'press', d: p.id, e }, { title: prof ? prof[e] : '' })).join('') + '</div>');
  });

  const sw = of('light_switched'), dm = of('light_dimmable');
  if (sw.length || dm.length) h.push('<h3>Lights</h3>');
  sw.forEach((d, i) => h.push(chk(`Switch${n(sw, i)}`, 'onoff', d.id, d.state.on)));
  dm.forEach((d, i) => h.push(rng(`Dimmer${n(dm, i)}`, 'level', d.id, d.state.level, 0, 100)));

  const bl = of('light_blinding');
  if (bl.length) h.push('<h3>Blinding lights (ESB64)</h3>');
  bl.forEach((d, i) => {
    h.push(chk(`Blinding light${n(bl, i)}`, 'onoff', d.id, d.state.on));
    h.push(rng('Level', 'level', d.id, d.state.level, 0, 100));
    h.push(drop('Mode', 'bmode', d.id, BLINDING_MODES, d.state.mode));
  });

  of('ac').forEach(d => h.push(
    '<h3>AC</h3>', chk('Power', 'power', d.id, d.state.on),
    rng('Setpoint (°C)', 'setpoint', d.id, d.state.setpoint, 16, 30),
    drop('Mode', 'mode', d.id, AC_MODES, d.state.mode),
    drop('Fan speed', 'fanspeed', d.id, FAN_SPEEDS, d.state.fan_speed)));

  const fans = of('fan');
  if (fans.length) h.push('<h3>Fans</h3>');
  fans.forEach((d, i) => h.push(chk(`Fan${n(fans, i)}`, 'onoff', d.id, d.state.on)));

  const wins = of('window');
  if (wins.length) h.push('<h3>Windows (FTKE)</h3>');
  wins.forEach((d, i) => h.push(chk(`Window${n(wins, i)} open`, 'input', d.id, d.state.open)));

  of('garage').forEach(d => h.push(
    '<h3>Garage door</h3>',
    `<div class="row"><span>${d.state.state}</span>${['open', 'close', 'stop'].map(c => btn(c, { a: 'cmd', d: d.id, c })).join('')}</div>`));

  const sens = of('sensor');
  if (sens.length) h.push('<h3>Sensors (simulated input)</h3>');
  sens.forEach(d => {
    const v = d.state.value;
    if (d.sensor === 'presence') h.push(chk('Presence', 'input', d.id, v));
    else if (d.sensor === 'motion') h.push(chk('Motion', 'input', d.id, v));
    else if (d.sensor === 'lux') h.push(rng('Lux', 'input', d.id, v, 0, 2000, 10));
    else if (d.sensor === 'temperature') h.push(rng('Temperature (°C)', 'input', d.id, v, -10, 50, 0.5));
    else if (d.sensor === 'humidity') h.push(rng('Humidity (%)', 'input', d.id, v, 0, 100));
    else if (d.sensor === 'smoke') h.push(drop('Smoke', 'input', d.id, ['normal', 'alarm', 'fault'], v));
  });
  return h.join('');
}

function buildingPanel() {
  const nt = snap.notifications.slice().reverse();
  const at = snap.atios, lim = at.limits;
  return '<h3>Mode</h3><div class="row wrap">' +
      ['home', 'away'].map(m => btn(m, { a: 'mode', m }, { cls: snap.mode === m ? 'active' : '' })).join('') + '</div>' +
    '<h3>Geofence (Apple Hub)</h3><div class="row wrap">' +
      btn('Arrive', { a: 'geo', e: 'arrive' }) + btn('Leave', { a: 'geo', e: 'leave' }) + '</div>' +
    (snap.optional_rules.length ? '<h3>Optional rules</h3>' +
      snap.optional_rules.map(r => chk(r.name, 'rule', r.id, r.enabled)).join('') : '') +
    '<h3>Scenes</h3><div class="row wrap">' + snap.scenes.map(s => btn(s.name, { a: 'scene', s: s.id })).join('') + '</div>' +
    '<h3>ATIOS groups</h3>' + snap.atios_groups.map(g =>
      `<div class="row"><span>${g.number} ${g.name} <small>(${g.members.length})</small></span>` +
      ['on', 'off'].map(c => btn(c, { a: 'group', g: g.id, c })).join('') + '</div>').join('') +
    `<p class="small">SmartCore: gear ${at.control_gear}/${lim.control_gear_max}, devices ${at.control_devices}/${lim.control_devices_max}, groups ${at.groups}/${lim.groups_max}</p>` +
    `<h3>Infrastructure</h3><p class="small">${snap.infrastructure.join(', ')}</p>` +
    '<h3>Notifications</h3>' + (nt.length ? nt.map(x => `<p class="small">${x.message}</p>`).join('') : '<p class="small">None</p>');
}

function renderPanel(force) {
  if (!force && panel.contains(document.activeElement)) return; // do not disturb a control in use
  const room = snap.rooms.find(r => r.id === selRoom);
  const html = (room ? roomPanel(room) : '<p class="hint">Click a room to control its devices.</p>') + buildingPanel();
  if (html === lastHtml) return;
  const top = panel.scrollTop;
  panel.innerHTML = html;
  panel.scrollTop = top;
  lastHtml = html;
}

// ---------- Events ----------
function onAction(e) {
  const d = e.target.closest('button[data-a]').dataset;
  const calls = {
    resume: () => post(`${B()}/rooms/${d.r}/resume`),
    scene: () => post(`${B()}/scenes/${d.s}`),
    group: () => post(`${B()}/groups/${d.g}/command`, { command: d.c }),
    cmd: () => cmd(d.d, d.c),
    mode: () => post(`${B()}/mode`, { mode: d.m }),
    geo: () => post(`${B()}/geofence`, { event: d.e }),
    press: async () => {
      const r = await post(`${B()}/pushbuttons/${d.d}/press`, { event: d.e });
      toast(r.action ? `${r.action} (${r.path} path)` : 'No action for this event');
    },
  };
  return run(calls[d.a]);
}

plan.addEventListener('click', e => {
  if (e.target.closest('button[data-a]')) return onAction(e);
  const c = e.target.closest('.card');
  if (c) selectRoom(c.dataset.r);
});
plan.addEventListener('keydown', e => {
  if ((e.key === 'Enter' || e.key === ' ') && e.target.classList.contains('card')) {
    e.preventDefault();
    selectRoom(e.target.dataset.r);
  }
});
panel.addEventListener('click', e => e.target.closest('button[data-a]') && onAction(e));

// Show the live value while a slider is dragged; the command is sent when it is released
panel.addEventListener('input', e => {
  if (e.target.type === 'range') e.target.closest('label').querySelector('small').textContent = e.target.value;
});
panel.addEventListener('change', e => {
  const t = e.target, a = t.dataset.a, d = t.dataset.d;
  if (!a || !d) return;
  const v = t.type === 'checkbox' ? t.checked : t.type === 'range' ? +t.value : t.value;
  const calls = {
    onoff: () => cmd(d, v ? 'on' : 'off'),
    level: () => cmd(d, 'set_level', { level: v }),          // dimmable and blinding lights
    bmode: () => cmd(d, 'set_mode', { mode: v }),            // blinding light: normal / blinding
    power: () => cmd(d, 'power', { on: v }),
    setpoint: () => cmd(d, 'set_temperature', { temperature: v }),
    mode: () => cmd(d, 'mode', { mode: v }),
    fanspeed: () => cmd(d, 'fan_speed', { fan_speed: v }),
    input: () => input(d, v),                                // sensors and window contacts
    rule: () => put(`${B()}/rules/${d}`, { enabled: v }),    // optional rules such as R22
  };
  t.blur();
  run(calls[a]);
});

// ---------- Header controls ----------
$('hour').addEventListener('input', e => ($('hourLabel').textContent = fmt(+e.target.value)));
$('hour').addEventListener('change', e => {
  e.target.blur();
  post('/sim', { hour: +e.target.value }).catch(() => {});
});
$('live').onclick = () => post('/sim', { real_time: true }).catch(() => {});
$('speed').onchange = e => {
  e.target.blur();
  post('/sim', { speed: +e.target.value }).catch(() => {});
};
$('smoke').onclick = () => run(async () => {
  const smokes = snap.rooms.flatMap(r => r.devices).filter(d => d.sensor === 'smoke');
  if (!snap.safety) await input(smokes[0].id, 'alarm'); // the backend runs rules R01 / R01b
  else {
    for (const d of smokes) await input(d.id, 'normal'); // the safety lock needs every smoke sensor back to normal
    await post(`${B()}/safety/reset`);
  }
});

// ---------- Start ----------
function switchBuilding(id) {
  bid = id; selRoom = null; builtFor = null; snap = null; lastHtml = '';
  plan.innerHTML = '';
  connect();
}

async function init() {
  let list;
  try {
    list = await api('/buildings');
  } catch (_) {
    plan.innerHTML = '<p class="hint">Cannot reach the backend. Start it (uvicorn main:app) and open the page from the server address.</p>';
    return;
  }
  try {  // pushbutton labels and actions come from data/rules.json (served at /data)
    pbRules = (await (await fetch('/data/rules.json')).json()).pushbuttons;
  } catch (_) { pbRules = null; }
  $('building').innerHTML = list.map(b => `<option value="${b.id}">${b.name}</option>`).join('');
  $('building').onchange = () => switchBuilding($('building').value);
  switchBuilding(list[0].id);
}
init();