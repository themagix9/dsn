/* Deep Space Network – Dashboard */
const { createApp, reactive, ref, computed, nextTick, onMounted, onBeforeUnmount } = Vue;

async function api(method, path, body) {
  const r = await fetch(path, {
    method, credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await r.json(); } catch (_) { /* leer */ }
  if (!r.ok) {
    const err = new Error((data && data.detail) || `HTTP ${r.status}`);
    err.status = r.status;
    throw err;
  }
  return data;
}

const fmt = {
  bytes(b) {
    if (b == null || isNaN(b)) return '–';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return (b >= 100 || i === 0 ? b.toFixed(0) : b.toFixed(1)) + ' ' + u[i];
  },
  duration(s) {
    s = Math.max(0, Math.round(s || 0));
    if (s < 90) return s + ' s';
    if (s < 5400) return Math.round(s / 60) + ' min';
    if (s < 172800) return (s / 3600).toFixed(1).replace('.0', '') + ' h';
    return Math.round(s / 86400) + ' Tage';
  },
  ago(ts) { return 'vor ' + fmt.duration(Date.now() / 1000 - ts); },
  time(ts) {
    const d = new Date(ts * 1000);
    const today = new Date().toDateString() === d.toDateString();
    return (today ? '' : d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit' }) + ' ') +
      d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  },
};

/* ---------------------------------------------------------------- Komponenten */
const DsnLogo = {
  template: `<svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 13.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Z"/><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M7.8 16.2a6 6 0 0 1 0-8.4M16.2 7.8a6 6 0 0 1 0 8.4M4.9 19.1a10 10 0 0 1 0-14.2M19.1 4.9a10 10 0 0 1 0 14.2"/></svg>`,
};

const NodeStatus = {
  props: ['n'],
  computed: {
    st() {
      const n = this.n;
      if (!n.online) return ['critical', 'Offline seit ' + fmt.duration(n.down_for)];
      if (n.fenced) return ['critical', 'Eingezäunt'];
      if (n.has_quorum === false) return ['warning', 'Kein Quorum'];
      if (n.drain) return ['warning', 'Drain'];
      return ['good', 'Online'];
    },
  },
  template: `<span class="pill" :class="st[0]"><span class="dot"></span>{{ st[1] }}</span>`,
};

const SVC_STATUS = {
  running: ['good', 'Läuft'], starting: ['info', 'Startet'], degraded: ['warning', 'Teilweise'],
  stopped: ['critical', 'Gestoppt'], missing: ['warning', 'Wird gestartet'], failed: ['critical', 'Fehler'],
  migrating: ['info', 'Wird verschoben'], node_offline: ['critical', 'Node offline'], disabled: ['', 'Deaktiviert'],
  unplaced: ['warning', 'Kein Node'], unknown: ['', 'Unbekannt'],
};
const SvcStatus = {
  props: ['s'],
  computed: { st() { return SVC_STATUS[this.s.status] || ['', this.s.status]; } },
  template: `<span class="pill" :class="st[0]"><span class="dot"></span>{{ st[1] }}</span>`,
};

const SEV = { info: ['info', 'Info'], warning: ['warning', 'Warnung'], critical: ['critical', 'Kritisch'] };
const EventList = {
  props: ['events'],
  methods: { time: fmt.time, sev(s) { return SEV[s] || SEV.info; } },
  template: `<div class="events">
    <div v-if="!events.length" class="empty">Keine Ereignisse.</div>
    <div class="event" v-for="e in events" :key="e.id">
      <span class="time">{{ time(e.ts) }}</span>
      <span><span class="pill" :class="sev(e.severity)[0]"><span class="dot"></span>{{ sev(e.severity)[1] }}</span></span>
      <span class="msg">{{ e.message }}</span>
    </div></div>`,
};

/* Aktionen für einen einzelnen Container (Docker-Ansicht) */
const ContainerActions = {
  props: ['c', 'admin', 'locked'],
  emits: ['act', 'logs', 'console', 'inspect'],
  template: `<div class="actions">
    <button class="btn sm" @click="$emit('logs', c)">Logs</button>
    <button class="btn sm" @click="$emit('inspect', c)">Details</button>
    <template v-if="admin">
      <button class="btn sm" v-if="c.state === 'running' && !c.agent" @click="$emit('console', c)">Konsole</button>
      <template v-if="!locked">
        <button class="btn sm" v-if="c.state !== 'running'" @click="$emit('act', c, 'start')">Start</button>
        <button class="btn sm" v-if="c.state === 'running'" @click="$emit('act', c, 'stop')">Stop</button>
        <button class="btn sm" @click="$emit('act', c, 'restart')">Neustart</button>
        <button class="btn sm danger" @click="$emit('act', c, 'remove')">Löschen</button>
      </template>
    </template>
  </div>`,
};

/* Liniendiagramm: eine y-Achse, 2px-Linien, Fadenkreuz + Tooltip beim Hovern */
const LineChart = {
  props: { series: Array, unit: String, max: Number, fmt: Function, height: { type: Number, default: 180 } },
  setup(props) {
    const el = ref(null);
    const width = ref(600);
    const hover = ref(null);
    let ro;
    onMounted(() => {
      ro = new ResizeObserver(([e]) => { width.value = Math.max(200, e.contentRect.width); });
      ro.observe(el.value);
    });
    onBeforeUnmount(() => ro && ro.disconnect());

    const pad = { l: 44, r: 12, t: 8, b: 22 };
    const valFmt = v => v == null ? '–' : props.fmt ? props.fmt(v) : (Math.round(v * 10) / 10) + ' ' + props.unit;

    const geo = computed(() => {
      const pts = props.series.flatMap(s => s.points);
      if (!pts.length) return null;
      const t0 = Math.min(...pts.map(p => p[0])), t1 = Math.max(...pts.map(p => p[0]));
      let vmax = props.max || Math.max(1, ...pts.map(p => p[1] ?? 0)) * 1.15;
      const w = width.value, h = props.height;
      const x = t => pad.l + (t1 === t0 ? 0 : (t - t0) / (t1 - t0)) * (w - pad.l - pad.r);
      const y = v => pad.t + (1 - v / vmax) * (h - pad.t - pad.b);
      const ticks = [0, 0.5, 1].map(f => ({ v: vmax * f, y: y(vmax * f) }));
      const xticks = [];
      const span = t1 - t0;
      const step = span > 4 * 3600 ? 3600 : span > 3600 ? 1800 : 600;
      for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) xticks.push({ t, x: x(t) });
      const paths = props.series.map(s => {
        let d = '', gap = true;
        let prev = null;
        for (const p of s.points) {
          if (p[1] == null || (prev && p[0] - prev > 60)) gap = true;
          if (p[1] != null) { d += (gap ? 'M' : 'L') + x(p[0]).toFixed(1) + ',' + y(p[1]).toFixed(1); gap = false; }
          prev = p[0];
        }
        return { name: s.name, color: s.color, d };
      });
      return { t0, t1, x, y, w, h, ticks, xticks, paths, vmax };
    });

    function onMove(ev) {
      const g = geo.value;
      if (!g) return;
      const rect = el.value.getBoundingClientRect();
      const px = ev.clientX - rect.left;
      const t = g.t0 + (px - pad.l) / (g.w - pad.l - pad.r) * (g.t1 - g.t0);
      const rows = props.series.map(s => {
        let best = null;
        for (const p of s.points) if (!best || Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
        return { name: s.name, color: s.color, p: best };
      }).filter(r => r.p);
      if (!rows.length) return;
      const tt = rows[0].p[0];
      hover.value = { x: g.x(tt), t: tt, rows, left: Math.min(g.x(tt) + 12, g.w - 170) };
    }
    const timeLbl = t => new Date(t * 1000).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
    return { el, geo, hover, onMove, valFmt, timeLbl, pad, fmtTime: fmt.time };
  },
  template: `<div class="chart" ref="el" @mousemove="onMove" @mouseleave="hover = null">
    <div class="legend" v-if="series.length > 1">
      <span v-for="s in series" :key="s.name"><i :style="{background: s.color}"></i>{{ s.name }}</span>
    </div>
    <svg v-if="geo" :viewBox="'0 0 ' + geo.w + ' ' + geo.h" :height="geo.h" role="img" :aria-label="series.map(s => s.name).join(', ')">
      <g v-for="tk in geo.ticks" :key="tk.v">
        <line class="gridline" :x1="pad.l" :x2="geo.w - pad.r" :y1="tk.y" :y2="tk.y"></line>
        <text class="axis-label" :x="pad.l - 6" :y="tk.y + 4" text-anchor="end">{{ fmt ? fmt(tk.v) : Math.round(tk.v) }}</text>
      </g>
      <text v-for="tk in geo.xticks" :key="tk.t" class="axis-label" :x="tk.x" :y="geo.h - 4" text-anchor="middle">{{ timeLbl(tk.t) }}</text>
      <path v-for="p in geo.paths" :key="p.name" class="series-line" :d="p.d" :stroke="p.color"></path>
      <template v-if="hover">
        <line class="crosshair" :x1="hover.x" :x2="hover.x" :y1="pad.t" :y2="geo.h - pad.b"></line>
        <circle v-for="r in hover.rows" :key="r.name" v-show="r.p[1] != null" :cx="hover.x" :cy="geo.y(r.p[1] || 0)" r="4"
          :fill="r.color" stroke="var(--surface)" stroke-width="2"></circle>
      </template>
    </svg>
    <div v-else class="empty">Keine Daten</div>
    <div class="tooltip" v-if="hover" :style="{left: hover.left + 'px', top: '24px'}">
      <div class="sub">{{ fmtTime(hover.t) }}</div>
      <div class="row" v-for="r in hover.rows" :key="r.name"><span class="sw" :style="{background: r.color}"></span>{{ r.name }}: <b>{{ valFmt(r.p[1]) }}</b></div>
    </div>
  </div>`,
};

/* ---------------------------------------------------------------- App */
const COMPOSE_TEMPLATE = `services:
  app:
    image: traefik/whoami:latest
    restart: unless-stopped
    ports:
      - "8080:80"
    volumes:
      - \${DSN_DATA}/data:/data
`;

createApp({
  components: { DsnLogo, NodeStatus, SvcStatus, EventList, LineChart, ContainerActions },
  setup() {
    const view = ref('loading');
    const tab = ref(localGet('dsn.tab') || 'overview');
    const ov = ref(null);
    const me = ref({});
    const wsOk = ref(true);
    const busy = ref(false);
    const setupNode = ref('');
    const loginForm = reactive({ username: '', password: '', totp: '', token: '', needTotp: false, error: '' });
    const svcForm = ref(null), logs = ref(null), nodeDetail = ref(null), join = ref(null), nodeForm = ref(null);
    const userForm = ref(null), toast = ref(null), copied = ref(false);
    const inv = reactive({ node: '', sub: 'containers', data: null, list: null, loading: false, error: '', newNet: '' });
    const dockerTabs = [{ id: 'containers', label: 'Container' }, { id: 'images', label: 'Images' },
      { id: 'volumes', label: 'Volumes' }, { id: 'networks', label: 'Netzwerke' }];
    const consoleBox = ref(null), inspectBox = ref(null), stackForm = ref(null), bulk = ref(null);
    let term = null, termWs = null, logTimer = null;
    const adopt = ref(null);
    const settings = ref(null), settingsMsg = reactive({ ok: '', error: '' }), users = ref([]);
    const totp = reactive({ uri: '', secret: '', qr: '', code: '', error: '' });
    const pw = reactive({ old: '', new: '', error: '', ok: '' });
    const eventFilter = ref('');
    const allEvents = ref([]);
    const theme = ref(localGet('dsn.theme') || 'auto');
    applyTheme();
    let ws = null, wsTimer = null;

    function localGet(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
    function localSet(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* egal */ } }
    function applyTheme() {
      if (theme.value === 'auto') document.documentElement.removeAttribute('data-theme');
      else document.documentElement.setAttribute('data-theme', theme.value);
    }
    function toggleTheme() {
      theme.value = { auto: 'dark', dark: 'light', light: 'auto' }[theme.value];
      localSet('dsn.theme', theme.value); applyTheme();
    }
    function notify(text, error = false) {
      toast.value = { text, error };
      setTimeout(() => { if (toast.value && toast.value.text === text) toast.value = null; }, error ? 8000 : 3500);
    }

    const isAdmin = computed(() => me.value.role === 'admin');
    const tabs = computed(() => [
      { id: 'overview', label: 'Übersicht' }, { id: 'services', label: 'Services' }, { id: 'containers', label: 'Docker' },
      { id: 'nodes', label: 'Nodes' },
      { id: 'events', label: 'Ereignisse' },
      ...(isAdmin.value ? [{ id: 'settings', label: 'Einstellungen' }] : []),
      { id: 'account', label: 'Konto' },
    ]);
    const workerNodes = computed(() => (ov.value ? ov.value.nodes.filter(n => n.role === 'worker') : []));
    const runningServices = computed(() => ov.value.services.filter(s => s.status === 'running').length);
    const problemServices = computed(() => ov.value.services.filter(s => s.enabled && !['running', 'starting', 'migrating'].includes(s.status)));
    const recentProblems = computed(() => ov.value.events.filter(e => e.severity !== 'info' && Date.now() / 1000 - e.ts < 86400).length);
    const filteredEvents = computed(() => {
      const src = allEvents.value.length ? allEvents.value : (ov.value ? ov.value.events : []);
      if (eventFilter.value === 'critical') return src.filter(e => e.severity === 'critical');
      if (eventFilter.value === 'warning') return src.filter(e => e.severity !== 'info');
      return src;
    });
    const nodeById = name => ov.value && ov.value.nodes.find(n => n.name === name);

    /* ---------- Session */
    async function boot() {
      try {
        me.value = await api('GET', '/api/me');
        view.value = 'app';
        connectWs();
        if (tab.value === 'settings') loadSettings();
      } catch (e) {
        const s = await api('GET', '/api/setup').catch(() => ({ needed: false, node: '?' }));
        setupNode.value = s.node;
        view.value = s.needed ? 'setup' : 'login';
      }
    }
    async function doLogin() {
      busy.value = true; loginForm.error = '';
      try {
        await api('POST', '/api/login', { username: loginForm.username, password: loginForm.password, totp: loginForm.totp });
        loginForm.password = loginForm.totp = ''; loginForm.needTotp = false;
        await boot();
      } catch (e) {
        if (e.message === 'TOTP_REQUIRED') { loginForm.needTotp = true; await nextTick(); }
        else loginForm.error = e.message;
      } finally { busy.value = false; }
    }
    async function doSetup() {
      busy.value = true; loginForm.error = '';
      try {
        await api('POST', '/api/setup', { token: loginForm.token, username: loginForm.username, password: loginForm.password });
        await new Promise(r => setTimeout(r, 800));
        await doLogin();
      } catch (e) { loginForm.error = e.message; } finally { busy.value = false; }
    }
    async function logout() {
      await api('POST', '/api/logout').catch(() => {});
      if (ws) { ws.onclose = null; ws.close(); }
      ov.value = null; view.value = 'login';
    }

    function connectWs() {
      clearTimeout(wsTimer);
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${location.host}/api/ws`);
      ws.onmessage = ev => { ov.value = JSON.parse(ev.data); wsOk.value = true; };
      ws.onclose = ev => {
        if (ev.code === 4401) { view.value = 'login'; return; }
        wsOk.value = false;
        wsTimer = setTimeout(connectWs, 3000);
      };
    }

    /* ---------- Services */
    async function editService(name) {
      if (!name) {
        svcForm.value = { isNew: true, name: '', description: '', enabled: true, compose: COMPOSE_TEMPLATE, envText: '',
          preferred: [], auto: true, constraintsText: '', sync: { mode: 'none', interval: 300, dump_service: '', dump_command: '' },
          dnsText: '', failback: 'global', error: '' };
        return;
      }
      const s = await api('GET', '/api/services/' + name);
      svcForm.value = {
        isNew: false, name: s.name, description: s.description, enabled: s.enabled, compose: s.compose,
        envText: Object.entries(s.env).map(([k, v]) => `${k}=${v}`).join('\n'),
        preferred: [...s.placement.preferred], auto: s.placement.auto,
        constraintsText: Object.entries(s.placement.constraints).map(([k, v]) => `${k}=${v}`).join('\n'),
        sync: { ...s.sync }, dnsText: s.dns.map(d => d.name + (d.proxied ? ' proxied' : '')).join('\n'),
        failback: s.failback === null ? 'global' : s.failback ? 'on' : 'off', error: '', adopted: s.adopted_from,
      };
    }
    const parseKV = text => Object.fromEntries(text.split('\n').map(l => l.trim()).filter(l => l && l.includes('='))
      .map(l => [l.slice(0, l.indexOf('=')).trim(), l.slice(l.indexOf('=') + 1).trim()]));
    function movePref(i, d) {
      const a = svcForm.value.preferred;
      [a[i], a[i + d]] = [a[i + d], a[i]];
    }
    async function saveService() {
      const f = svcForm.value;
      f.error = '';
      busy.value = true;
      try {
        await api('PUT', '/api/services/' + f.name.trim(), {
          description: f.description, enabled: f.enabled, compose: f.compose, env: parseKV(f.envText),
          placement: { preferred: f.preferred, auto: f.auto, constraints: parseKV(f.constraintsText) },
          sync: f.sync,
          dns: f.dnsText.split('\n').map(l => l.trim()).filter(Boolean).map(l => ({ name: l.split(/\s+/)[0], proxied: /\sproxied$/i.test(l) })),
          failback: f.failback === 'global' ? null : f.failback === 'on',
        });
        notify(`Service ${f.name} gespeichert`);
        svcForm.value = null;
      } catch (e) { f.error = e.message; } finally { busy.value = false; }
    }
    async function deleteService(name) {
      if (!confirm(`Service ${name} wirklich löschen? Der Stack wird gestoppt. Die Daten bleiben auf den Nodes liegen.`)) return;
      try { await api('DELETE', '/api/services/' + name); svcForm.value = null; notify(`${name} gelöscht`); }
      catch (e) { svcForm.value.error = e.message; }
    }
    async function moveService(name, ev) {
      const node = ev.target.value; ev.target.value = '';
      if (!node || !confirm(`${name} nach ${node} verschieben?\nAblauf: Stack stoppen → finaler Sync → Start auf ${node} → DNS umstellen.`)) return;
      try { await api('POST', `/api/services/${name}/move`, { node }); notify(`Verschiebung von ${name} nach ${node} angefordert`); }
      catch (e) { notify(e.message, true); }
    }
    async function serviceAction(name, action) {
      const label = { restart: 'Neustart', pull: 'Update (Images ziehen)', sync: 'Sync' }[action];
      notify(`${label} von ${name} läuft …`);
      try {
        const r = await api('POST', `/api/services/${name}/action`, { action });
        if (r.ok === false) notify(`${label} fehlgeschlagen: ${r.error || r.output || ''}`, true);
        else notify(`${label} von ${name} erfolgreich`);
      } catch (e) { notify(e.message, true); }
    }
    /* Logs-Fenster: loader() liefert den Text; "Live" lädt alle 3 s nach */
    function openLogs(name, loader) {
      clearInterval(logTimer);
      const box = reactive({ name, text: 'Lade …', follow: false, reload: null });
      box.reload = async () => {
        try { box.text = (await loader()) || '(leer)'; } catch (e) { box.text = 'Fehler: ' + e.message; }
        await nextTick();
        const pre = document.querySelector('pre.logs');
        if (pre && box.follow) pre.scrollTop = pre.scrollHeight;
      };
      logs.value = box;
      box.reload();
      logTimer = setInterval(() => {
        if (!logs.value || logs.value !== box) { clearInterval(logTimer); return; }
        if (box.follow) box.reload();
      }, 3000);
    }
    function showLogs(name) {
      openLogs(name, async () => (await api('GET', `/api/services/${name}/logs?tail=400`)).logs);
    }

    /* ---------- Docker-Verwaltung */
    const standaloneContainers = computed(() => (inv.data ? inv.data.containers.filter(c => !c.agent) : []));
    const dockerCall = (op, args = {}, node = inv.node) => api('POST', `/api/nodes/${node}/docker`, { op, args });
    async function loadInventory(node) {
      if (node !== inv.node) inv.sub = 'containers';
      Object.assign(inv, { node, loading: true, error: '' });
      try {
        if (inv.sub === 'containers') inv.data = await api('GET', `/api/nodes/${node}/inventory`);
        else inv.list = await dockerCall(inv.sub + '.list', {}, node);
      } catch (e) { inv.error = e.message; }
      finally { inv.loading = false; }
    }
    function setDockerTab(id) { inv.sub = id; inv.list = null; loadInventory(inv.node); }
    const refreshDocker = () => loadInventory(inv.node);
    async function dockerOp(op, args, confirmMsg) {
      if (confirmMsg && !confirm(confirmMsg)) return;
      try {
        const r = await dockerCall(op, args);
        notify(r && r.output ? r.output.split('\n').slice(-2).join(' ') : 'Erledigt');
        await loadInventory(inv.node);
      } catch (e) { notify(e.message, true); }
    }
    const stackKind = s => s.agent ? 'DSN-Agent' : s.managed ? 'HA-Service' : s.portainer ? 'Portainer-Stack'
      : isLocalStack(s) ? 'Stack (über DSN angelegt)' : 'Docker Compose';
    const stateLabel = st => ({ running: 'Läuft', partial: 'Teilweise', stopped: 'Gestoppt' })[st] || st;
    const isLocalStack = s => (s.config_files || '').includes('/local-stacks/');
    const shortId = id => (id || '').replace('sha256:', '').slice(0, 12);
    const removeImage = im => dockerOp('images.remove',
      { id: im.Repository !== '<none>' ? `${im.Repository}:${im.Tag}` : im.ID }, `Image ${im.Repository}:${im.Tag} löschen?`);
    const removeVolume = v => dockerOp('volumes.remove', { name: v.Name },
      `Volume ${v.Name} mit allen Daten ENDGÜLTIG löschen?`);
    const removeNetwork = n => dockerOp('networks.remove', { name: n.Name }, `Netzwerk ${n.Name} löschen?`);
    async function createNetwork() { await dockerOp('networks.create', { name: inv.newNet }); inv.newNet = ''; }

    async function containerAction(c, action) {
      const labels = { start: 'starten', stop: 'stoppen', restart: 'neu starten', remove: 'ENDGÜLTIG löschen' };
      if ((action === 'stop' || action === 'remove') && !confirm(`Container ${c.name} ${labels[action]}?`)) return;
      await dockerOp('containers.action', { id: c.id, action });
    }
    async function stackAction(project, action) {
      const msg = { stop: `Stack ${project} stoppen?`, down: `Stack ${project} entfernen? Container werden gelöscht, ` +
        `benannte Volumes bleiben erhalten.`, restart: null, start: null }[action];
      await dockerOp('stacks.action', { project, action }, msg);
    }
    function showContainerLogs(c) {
      const node = inv.node;
      openLogs(c.name, async () => (await dockerCall('containers.logs', { id: c.id, tail: 500 }, node)).logs);
    }
    async function inspectContainer(c) {
      inspectBox.value = { name: c.name, data: null };
      try { inspectBox.value.data = await dockerCall('containers.inspect', { id: c.id }); }
      catch (e) { inspectBox.value = null; notify(e.message, true); }
    }

    /* ---------- Konsole (xterm.js über WebSocket) */
    async function openConsole(c) {
      consoleBox.value = { name: c.name, node: inv.node, status: 'verbinde …' };
      await nextTick();
      const css = getComputedStyle(document.documentElement);
      term = new Terminal({ cursorBlink: true, fontSize: 13, fontFamily: css.getPropertyValue('--mono'),
        theme: { background: '#0d0d0d' }, scrollback: 5000 });
      const FitCls = window.FitAddon && (window.FitAddon.FitAddon || window.FitAddon);
      const fit = FitCls ? new FitCls() : null;
      if (fit) term.loadAddon(fit);
      term.open(document.getElementById('term'));
      if (fit) fit.fit();
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      termWs = new WebSocket(`${proto}://${location.host}/api/console?node=${encodeURIComponent(inv.node)}&container=${encodeURIComponent(c.id)}`);
      termWs.binaryType = 'arraybuffer';
      const sendSize = () => termWs.readyState === 1 && termWs.send(JSON.stringify({ t: 'r', cols: term.cols, rows: term.rows }));
      termWs.onopen = () => { consoleBox.value.status = 'verbunden'; sendSize(); term.focus(); };
      termWs.onmessage = ev => term.write(new Uint8Array(ev.data));
      termWs.onclose = () => { if (consoleBox.value) consoleBox.value.status = 'getrennt'; };
      term.onData(d => termWs.readyState === 1 && termWs.send(JSON.stringify({ t: 'i', d })));
      term.onResize(sendSize);
      window.addEventListener('resize', fitConsole);
      function fitConsole() { if (fit && term) fit.fit(); }
      consoleBox.value.cleanup = () => window.removeEventListener('resize', fitConsole);
    }
    function closeConsole() {
      if (termWs) { termWs.onclose = null; termWs.close(); }
      if (consoleBox.value && consoleBox.value.cleanup) consoleBox.value.cleanup();
      if (term) term.dispose();
      term = termWs = null;
      consoleBox.value = null;
    }

    /* ---------- Stacks anlegen / bearbeiten */
    async function openStack(name) {
      stackForm.value = { isNew: !name, node: inv.node, name: name || '', compose: COMPOSE_TEMPLATE, env: '', ha: false, error: '', busy: false };
      if (name) {
        try { Object.assign(stackForm.value, await dockerCall('stacks.get', { project: name })); }
        catch (e) { stackForm.value.error = e.message; }
      }
    }
    async function saveStack() {
      const f = stackForm.value;
      f.busy = true; f.error = '';
      try {
        if (f.ha) {
          const env = Object.fromEntries(f.env.split('\n').filter(l => l.includes('=')).map(l => [l.slice(0, l.indexOf('=')).trim(), l.slice(l.indexOf('=') + 1).trim()]));
          await api('PUT', '/api/services/' + f.name.trim(), { compose: f.compose, env,
            placement: { preferred: [f.node], auto: true, constraints: {} }, sync: { mode: 'periodic', interval: 300 } });
          notify(`HA-Service ${f.name} angelegt. Er startet gleich auf ${f.node}.`);
        } else {
          await dockerCall('stacks.deploy', { name: f.name.trim(), compose: f.compose, env: f.env }, f.node);
          notify(`Stack ${f.name} auf ${f.node} deployt`);
        }
        stackForm.value = null;
        loadInventory(inv.node);
      } catch (e) { f.error = e.message; } finally { if (stackForm.value) stackForm.value.busy = false; }
    }

    /* ---------- Ganzen Server schützen */
    const unprotectedItems = computed(() => {
      if (!inv.data || inv.sub !== 'containers') return [];
      const stacks = inv.data.stacks.filter(s => !s.agent && !s.managed && !s.adopted_by && s.state !== 'stopped')
        .map(s => ({ kind: 'stack', ref: s.name }));
      const singles = standaloneContainers.value.filter(c => !c.adopted_by && c.state === 'running')
        .map(c => ({ kind: 'container', ref: c.name }));
      return [...stacks, ...singles];
    });
    async function openBulk() {
      const node = inv.node;
      bulk.value = { node, loading: true, running: false,
        items: unprotectedItems.value.map(i => ({ ...i, include: true, name: '', plan: null, error: '', syncMode: 'periodic', state: 'pending' })) };
      for (const it of bulk.value.items) {
        try {
          it.plan = await api('POST', `/api/nodes/${node}/adopt/plan`, { kind: it.kind, ref: it.ref });
          it.name = it.plan.suggested_name;
          if (!it.plan.mounts.some(m => m.migrate)) it.syncMode = 'none';
          if (ov.value.services.some(s => s.name === it.name)) it.name += '-' + node;
        } catch (e) { it.error = e.message; it.include = false; }
      }
      bulk.value.loading = false;
    }
    async function runBulk() {
      const b = bulk.value;
      const todo = b.items.filter(i => i.include && i.plan);
      if (!confirm(`${todo.length} Einträge auf ${b.node} jetzt ins HA übernehmen?\nDie Originale werden dabei kurz gestoppt.`)) return;
      b.running = true;
      const others = workerNodes.value.filter(n => n.name !== b.node).map(n => n.name);
      for (const it of todo) {
        it.state = 'running';
        try {
          await api('POST', `/api/nodes/${b.node}/adopt`, { kind: it.kind, ref: it.ref, name: it.name.trim(),
            compose: it.plan.compose, env: it.plan.env, mounts: it.plan.mounts, containers: it.plan.containers,
            sync: { mode: it.syncMode, interval: 300 }, preferred: others });
          it.state = 'done';
        } catch (e) { it.state = 'failed'; it.error = e.message; }
      }
      if (todo.every(i => i.state === 'done')) {
        await api('PATCH', '/api/nodes/' + b.node, { protect_all: true }).catch(() => {});
      }
      b.running = false;
      notify(`${todo.filter(i => i.state === 'done').length} von ${todo.length} übernommen`, todo.some(i => i.state === 'failed'));
      loadInventory(b.node);
    }
    async function openAdopt(node, kind, ref) {
      adopt.value = { node, kind, ref, plan: null, error: '', busy: false, name: '', compose: '', env: {},
        sync: { mode: 'periodic', interval: 300, dump_service: '', dump_command: '' },
        preferred: workerNodes.value.filter(n => n.name !== node).map(n => n.name) };
      await replan();
      const a = adopt.value;
      if (a && a.plan) {
        a.name = a.plan.suggested_name;
        if (!a.plan.mounts.some(m => m.migrate)) a.sync.mode = 'none';
      }
    }
    async function replan(select) {
      const a = adopt.value;
      try {
        const plan = await api('POST', `/api/nodes/${a.node}/adopt/plan`, { kind: a.kind, ref: a.ref, select });
        Object.assign(a, { plan, compose: plan.compose, env: plan.env, error: '' });
      } catch (e) { a.error = e.message; }
    }
    function toggleMount(m, on) {
      const select = Object.fromEntries(adopt.value.plan.mounts.map(x => [x.key, x.key === m.key ? on : x.migrate]));
      replan(select);
    }
    async function confirmAdopt() {
      const a = adopt.value;
      const n = a.plan.mounts.filter(m => m.migrate).length;
      if (!confirm(`${a.ref} auf ${a.node} jetzt übernehmen?\n\nDas Original wird gestoppt, ${n} Datenquelle(n) werden kopiert, ` +
        `danach startet „${a.name}“ als DSN-Service. Bei großen Datenmengen kann das dauern.`)) return;
      a.busy = true; a.error = '';
      try {
        await api('POST', `/api/nodes/${a.node}/adopt`, {
          kind: a.kind, ref: a.ref, name: a.name.trim(), compose: a.compose, env: a.env, sync: a.sync,
          mounts: a.plan.mounts, containers: a.plan.containers, preferred: a.preferred });
        adopt.value = null;
        notify(`${a.ref} ist jetzt HA-geschützt als Service „${a.name}“`);
        loadInventory(a.node);
      } catch (e) { a.error = e.message; } finally { if (adopt.value) adopt.value.busy = false; }
    }
    async function restoreOriginal(name) {
      const af = svcForm.value.adopted;
      if (!confirm(`Original auf ${af.node} wiederherstellen?\n\nDer DSN-Service „${name}“ wird deaktiviert und das ursprüngliche ` +
        `Original wieder gestartet. Achtung: Das Original hat den Datenstand von VOR der Übernahme. Neuere Daten liegen ` +
        `weiterhin unter /srv/dsn/volumes/${name}/.`)) return;
      try {
        const r = await api('POST', `/api/services/${name}/restore`);
        svcForm.value = null;
        notify(r.ok ? 'Original wiederhergestellt' : 'Teilweise fehlgeschlagen: ' + r.errors.join(', '), !r.ok);
      } catch (e) { svcForm.value.error = e.message; }
    }

    /* ---------- Nodes */
    async function openNode(name) {
      nodeDetail.value = { name, history: [], cpuMem: [], rtt: [], net: [] };
      const h = await api('GET', `/api/nodes/${name}/history`).catch(() => []);
      const css = getComputedStyle(document.documentElement);
      const c1 = css.getPropertyValue('--series-1').trim(), c2 = css.getPropertyValue('--series-2').trim();
      const isMe = ov.value && ov.value.cluster.me === name;
      nodeDetail.value = {
        name, history: h,
        cpuMem: [{ name: 'CPU', color: c1, points: h.map(p => [p[0], p[1]]) },
                 { name: 'RAM', color: c2, points: h.map(p => [p[0], p[2]]) }],
        rtt: isMe ? [] : [{ name: 'RTT', color: c1, points: h.map(p => [p[0], p[3]]) }],
        net: [{ name: 'Empfangen', color: c1, points: h.map(p => [p[0], p[4]]) },
              { name: 'Gesendet', color: c2, points: h.map(p => [p[0], p[5]]) }],
      };
    }
    async function openJoin() {
      join.value = { ...(await api('GET', '/api/join')), os: 'token', token: '', address: '', busy: false, error: '',
        tried: [], found: null, form: null };
      copied.value = false;
    }
    async function updateNode(n) {
      if (!confirm(`DSN auf ${n.name} aktualisieren?\nDas neueste Image wird geladen und der Agent kurz neu gestartet. ` +
        `Deine Services laufen dabei weiter.`)) return;
      try { const r = await api('POST', `/api/nodes/${n.name}/update`); notify(`Update von ${n.name} gestartet (${r.image})`); }
      catch (e) { notify(e.message, true); }
    }
    async function pairCheck() {
      const j = join.value;
      Object.assign(j, { busy: true, error: '', found: null, tried: [] });
      try {
        const r = await api('POST', '/api/pair/check', { token: j.token, address: j.address });
        j.tried = r.tried;
        if (r.ok) {
          j.found = r;
          j.form = { name: r.info.hostname, role: 'worker', public_ip: '', leader_priority: 100, labels: '' };
        }
      } catch (e) { j.error = e.message; } finally { j.busy = false; }
    }
    async function pairComplete() {
      const j = join.value;
      j.busy = true; j.error = '';
      try {
        const labels = Object.fromEntries(j.form.labels.split('\n').filter(l => l.includes('='))
          .map(l => [l.slice(0, l.indexOf('=')).trim(), l.slice(l.indexOf('=') + 1).trim()]));
        await api('POST', '/api/pair/complete', { token: j.token, address: j.found.address, ...j.form, labels });
        notify(`${j.form.name} wird aufgenommen – der Server startet neu und erscheint in wenigen Sekunden`);
        join.value = null;
      } catch (e) { j.error = e.message; } finally { if (join.value) join.value.busy = false; }
    }
    function copy(text) { navigator.clipboard.writeText(text).then(() => { copied.value = true; }); }
    function editNode(n) {
      nodeForm.value = { name: n.name, public_ip: n.public_ip || '', leader_priority: n.leader_priority,
        labelsText: Object.entries(n.labels || {}).map(([k, v]) => `${k}=${v}`).join('\n'), error: '' };
    }
    async function saveNode() {
      const f = nodeForm.value;
      try {
        await api('PATCH', '/api/nodes/' + f.name, { public_ip: f.public_ip, leader_priority: f.leader_priority, labels: parseKV(f.labelsText) });
        nodeForm.value = null; notify('Node gespeichert');
      } catch (e) { f.error = e.message; }
    }
    async function toggleDrain(n) {
      const msg = n.drain ? `Drain für ${n.name} aufheben?` :
        `Drain für ${n.name} aktivieren?\nAlle Services werden kontrolliert auf andere Nodes verschoben.`;
      if (!confirm(msg)) return;
      try { await api('PATCH', '/api/nodes/' + n.name, { drain: !n.drain }); } catch (e) { notify(e.message, true); }
    }
    async function removeNode(n) {
      if (!confirm(`${n.name} aus dem Cluster entfernen? Danach auf dem Server den DSN-Container stoppen.`)) return;
      try { await api('DELETE', '/api/nodes/' + n.name); notify(`${n.name} entfernt`); } catch (e) { notify(e.message, true); }
    }

    /* ---------- Einstellungen & Benutzer */
    async function loadSettings() {
      settings.value = await api('GET', '/api/settings');
      users.value = await api('GET', '/api/users');
      allEvents.value = [];
    }
    async function saveSettings() {
      settingsMsg.ok = settingsMsg.error = ''; busy.value = true;
      try {
        settings.value.alerts.webhooks = settings.value.alerts.webhooks.filter(w => w.url);
        settings.value = await api('PATCH', '/api/settings', settings.value);
        settingsMsg.ok = 'Gespeichert und auf alle Nodes verteilt.';
      } catch (e) { settingsMsg.error = e.message; } finally { busy.value = false; }
    }
    async function testAlert() {
      busy.value = true; settingsMsg.ok = settingsMsg.error = '';
      try {
        const r = await api('POST', '/api/settings/test_alert');
        if (r.ok) settingsMsg.ok = 'Testalarm an alle konfigurierten Kanäle gesendet.';
        else settingsMsg.error = r.errors.join('\n');
      } catch (e) { settingsMsg.error = e.message; } finally { busy.value = false; }
    }
    async function saveUser() {
      const f = userForm.value;
      try {
        await api('POST', '/api/users', { username: f.username, password: f.password, role: f.role });
        userForm.value = null; users.value = await api('GET', '/api/users');
      } catch (e) { f.error = e.message; }
    }
    async function resetTotp(username) {
      if (!confirm(`2FA für ${username} entfernen?`)) return;
      await api('POST', '/api/users', { username, role: users.value.find(u => u.username === username).role, reset_totp: true });
      users.value = await api('GET', '/api/users');
    }
    async function deleteUser(username) {
      if (!confirm(`Benutzer ${username} löschen?`)) return;
      try { await api('DELETE', '/api/users/' + username); users.value = await api('GET', '/api/users'); }
      catch (e) { notify(e.message, true); }
    }

    /* ---------- Konto */
    async function beginTotp() {
      const r = await api('POST', '/api/me/totp/begin');
      const qr = qrcode(0, 'M'); qr.addData(r.uri); qr.make();
      Object.assign(totp, { uri: r.uri, secret: r.secret, qr: qr.createSvgTag(4, 0), error: '' });
    }
    async function enableTotp() {
      try {
        await api('POST', '/api/me/totp/enable', { code: totp.code });
        Object.assign(totp, { uri: '', secret: '', qr: '', code: '' });
        me.value = await api('GET', '/api/me');
        notify('2FA aktiviert');
      } catch (e) { totp.error = e.message; }
    }
    async function changePassword() {
      pw.error = pw.ok = '';
      try { await api('POST', '/api/me/password', { old: pw.old, new: pw.new }); pw.ok = 'Passwort geändert.'; pw.old = pw.new = ''; }
      catch (e) { pw.error = e.message; }
    }

    /* ---------- Helfer für Templates */
    const level = (v, crit, warn) => (v >= crit ? 'critical' : v >= warn ? 'warning' : '');
    const maxTemp = t => { const v = Object.values(t || {}); return v.length ? Math.max(...v).toFixed(0) + ' °C' : '–'; };

    Vue.watch(tab, async t => {
      localSet('dsn.tab', t);
      if (t === 'settings') loadSettings();
      if (t === 'containers' && !inv.node && ov.value) {
        const me = workerNodes.value.find(n => n.is_me && n.online) || workerNodes.value.find(n => n.online);
        if (me) loadInventory(me.name);
      }
      if (t === 'events') allEvents.value = await api('GET', '/api/events').catch(() => []);
    });

    boot();
    return {
      view, tab, tabs, ov, me, wsOk, busy, setupNode, loginForm, svcForm, logs, nodeDetail, join, nodeForm, userForm,
      toast, copied, settings, settingsMsg, users, totp, pw, eventFilter, filteredEvents, theme, isAdmin, workerNodes,
      runningServices, problemServices, recentProblems, nodeById,
      inv, adopt, standaloneContainers, loadInventory, openAdopt, toggleMount, confirmAdopt, restoreOriginal,
      dockerTabs, setDockerTab, refreshDocker, dockerOp, stackKind, stateLabel, isLocalStack, shortId, removeImage,
      removeVolume, removeNetwork, createNetwork, containerAction, stackAction, showContainerLogs, inspectContainer,
      consoleBox, openConsole, closeConsole, inspectBox, stackForm, openStack, saveStack, bulk, unprotectedItems,
      openBulk, runBulk, pairCheck, pairComplete, updateNode,
      doLogin, doSetup, logout, toggleTheme, editService, movePref, saveService, deleteService, moveService,
      serviceAction, showLogs, openNode, openJoin, copy, editNode, saveNode, toggleDrain, removeNode, saveSettings,
      testAlert, saveUser, resetTotp, deleteUser, beginTotp, enableTotp, changePassword, level, maxTemp,
      bytes: fmt.bytes, duration: fmt.duration, ago: fmt.ago,
    };
  },
}).mount('#app');
