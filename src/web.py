#!/usr/bin/env python3
"""octoi web UI — WiFi SLAM  ·  http://localhost:8201"""
import os, json, time, re, threading, socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse

PORT     = 8201
TSV_PATH = "/tmp/octoi_positions.tsv"
BUF_DIR  = "/tmp/octoi_ingest"
POLL_S   = 0.5

# ── OUI vendor lookup ─────────────────────────────────────────────────────────
_oui: dict[str, str] = {}
def _load_oui():
    _S = re.compile(r",?\s+(Inc\.?|LLC|Ltd\.?|Co\.?|Corp\.?|GmbH|Technologies|"
                    r"International|Group|Networks?|Systems?|Electronics?)$", re.I)
    try:
        with open("/usr/share/hwdata/oui.txt", errors="replace") as f:
            for line in f:
                m = re.match(r"([0-9A-Fa-f]{2}-[0-9A-Fa-f]{2}-[0-9A-Fa-f]{2})\s+\(hex\)\s+(.*)", line)
                if m:
                    _oui[m.group(1).replace("-", ":").lower()] = \
                        _S.sub("", m.group(2).strip()).strip(" ,.")
    except OSError:
        pass
_load_oui()

# ── data cache ────────────────────────────────────────────────────────────────
_lock    = threading.Lock()
_cache: list = []
_updated = threading.Event()  # set whenever cache refreshes

def _load() -> list:
    rows: list[dict] = []
    try:
        for line in open(TSV_PATH):
            p = line.strip().split("\t")
            if len(p) < 4: continue
            rows.append({
                "mac":   p[0],
                "x":     float(p[1]),
                "y":     float(p[2]),
                "rssi":  float(p[3]),
                "node":  p[4] if len(p) > 4 else "",
                "ssid":  p[5] if len(p) > 5 else "",
                "bssid": p[6] if len(p) > 6 else "",
            })
    except Exception:
        pass

    meta: dict[str, dict] = {}
    try:
        for fn in os.listdir(BUF_DIR):
            if not fn.endswith(".jl"): continue
            try:
                lines = open(os.path.join(BUF_DIR, fn)).readlines()[-400:]
            except Exception:
                continue
            for raw in lines:
                try: obj = json.loads(raw.strip())
                except Exception: continue
                src = obj.get("src", "")
                ts  = float(obj.get("ts", 0))
                if src and ts > meta.get(src, {}).get("ts", 0):
                    meta[src] = {
                        "ts":   ts,
                        "sub":  obj.get("sub", 0),
                        "ssid": obj.get("ssid", "") or meta.get(src, {}).get("ssid", ""),
                    }
    except Exception:
        pass

    now = max((v["ts"] for v in meta.values()), default=time.time())
    for r in rows:
        m = meta.get(r["mac"], {})
        r["age"]    = now - m.get("ts", now)
        r["ssid"]   = r["ssid"] or m.get("ssid", "")
        r["vendor"] = _oui.get(r["mac"][:8].lower(), "")
        r["fsub"]   = m.get("sub", 0)
        # bssid already in TSV; keep as-is
    return rows

def _poll():
    while True:
        d = _load()
        with _lock:
            _cache[:] = d
        _updated.set()
        time.sleep(POLL_S)

threading.Thread(target=_poll, daemon=True).start()

# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="dark">
<title>OCTOI \u25c8 WiFi SLAM</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0 }
html, body { height: 100%; background: #0d0b09; color: #c8a97a;
             font: 13px/1.5 'JetBrains Mono', 'Fira Mono', monospace; overflow: hidden }
body { display: flex; flex-direction: column }

/* header */
#hdr { display: flex; align-items: center; gap: 12px; padding: 5px 14px;
       background: #110f09; border-bottom: 1px solid #251c0e; flex-shrink: 0 }
#hdr h1 { font-size: 14px; color: #d4956a; letter-spacing: 2px; white-space: nowrap }
#hdr .spacer { flex: 1 }
#hdr #status { font-size: 11px; color: #5a4a2a }

/* toolbar buttons */
.btn { background: #1c1710; border: 1px solid #3a2e1a; color: #a07848;
       font: 12px/1 'JetBrains Mono', monospace; padding: 4px 10px;
       border-radius: 4px; cursor: pointer; white-space: nowrap; transition: background .15s }
.btn:hover { background: #2a2010; color: #c8a060 }
.btn:active { background: #342810 }

/* layout */
#main { display: flex; flex: 1; overflow: hidden }
#map-wrap { flex: 1; position: relative; overflow: hidden }
canvas { display: block; width: 100%; height: 100%; cursor: crosshair }

/* sidebar */
#sidebar { width: 260px; flex-shrink: 0; background: #0f0d08;
           border-left: 1px solid #1e1810; display: flex; flex-direction: column; overflow: hidden }
#sidebar h2 { font-size: 10px; color: #6a5030; letter-spacing: 1px; text-transform: uppercase;
              padding: 8px 10px 4px; border-bottom: 1px solid #1e1810; flex-shrink: 0 }
#devlist { flex: 1; overflow-y: auto; padding: 4px 0 }
#devlist::-webkit-scrollbar { width: 4px }
#devlist::-webkit-scrollbar-track { background: #0f0d08 }
#devlist::-webkit-scrollbar-thumb { background: #2a2010; border-radius: 2px }

.dev { display: flex; align-items: flex-start; gap: 8px; padding: 5px 10px;
       border-bottom: 1px solid #17140e; transition: opacity .3s; cursor: pointer }
.dev:hover { background: #181410 }
.dev .sym { font-size: 16px; line-height: 1; padding-top: 1px; flex-shrink: 0 }
.dev .info { overflow: hidden; min-width: 0 }
.dev .vendor { font-size: 12px; font-weight: bold; white-space: nowrap;
               overflow: hidden; text-overflow: ellipsis }
.dev .ssid   { font-size: 11px; color: #9a7a50; white-space: nowrap;
               overflow: hidden; text-overflow: ellipsis }
.dev .mac    { font-size: 10px; color: #4a3820 }
</style>
</head>
<body>
<div id="hdr">
  <h1>OCTOI \u25c8 WiFi SLAM</h1>
  <button class="btn" onclick="doRotate(-1)">&#8634; &minus;45&deg;</button>
  <button class="btn" onclick="doRotate(1)">&#8635; +45&deg;</button>
  <button class="btn" onclick="doReset()">&#x2715; Reset</button>
  <div class="spacer"></div>
  <span id="count"></span>
  <span id="status">connecting\u2026</span>
</div>
<div id="main">
  <div id="map-wrap"><canvas id="c"></canvas></div>
  <div id="sidebar">
    <h2>Devices</h2>
    <div id="devlist"></div>
  </div>
</div>
<script>
const c = document.getElementById('c');
const ctx = c.getContext('2d');
let W = 0, H = 0;
let data = [];
let zoom = 1, panX = 0, panY = 0, rot = 0;
let dragging = false, dragStart = null, panStart = null;
let bounds = null;  // locked after first fit; only reset by doReset/doRotate

// smooth rotation animation
let rotTarget = 0, rotAnimId = null;
function animateRot() {
  const diff = rotTarget - rot;
  if (Math.abs(diff) < 0.1) { rot = rotTarget; rotAnimId = null; draw(); return; }
  rot += diff * 0.18;
  draw();
  rotAnimId = requestAnimationFrame(animateRot);
}
function startRot(target) {
  rotTarget = target;
  if (!rotAnimId) rotAnimId = requestAnimationFrame(animateRot);
}

const PALETTE = [
  '#7b4a1e','#8b6014','#5a3810','#6b5828','#8a7060',
  '#6a4838','#b08050','#8a6838','#7a4838','#6a7030',
  '#a07848','#506020','#788040','#5a8070','#8a8870',
  '#5a6080','#a05848','#507058','#6a5068','#5a5840'
];
const OUI_COLORS = {};
let colorIdx = 0;
function ouiColor(mac) {
  const k = mac.slice(0, 8);
  if (!OUI_COLORS[k]) OUI_COLORS[k] = PALETTE[colorIdx++ % PALETTE.length];
  return OUI_COLORS[k];
}

const SYMS = {
  router:'\\u{1F4E1}', probe:'\\u{1F4F6}', phone:'\\u{1F4F1}', laptop:'\\u{1F4BB}',
  home:'\\u{1F3E0}', camera:'\\u{1F4F7}', tv:'\\u{1F4FA}', printer:'\\u{1F5A8}',
  data:'\\u{1F4BE}', unknown:'\u25e6'
};
const DC_PATS = [
  ['home',    /bt-|sky|virgin|talktalk|plusnet|ee-|\bvm\b/i],
  ['phone',   /iphone|android|pixel|galaxy|huawei|oneplus/i],
  ['laptop',  /laptop|macbook|surface|thinkpad|xps|dell/i],
  ['camera',  /\bcam\b|camera|nest|\bring\b|arlo|reolink/i],
  ['tv',      /\btv\b|firetv|roku|chromecast|appletv|bravia/i],
  ['printer', /printer|hp-|epson|canon|brother/i],
];
function deviceClass(ssid, fsub) {
  if (ssid) for (const [cls, re] of DC_PATS) if (re.test(ssid)) return cls;
  if (fsub === 8 || fsub === 5) return 'router';
  if (fsub === 4) return 'probe';
  return 'unknown';
}

function resize() {
  const wr = document.getElementById('map-wrap');
  W = c.width  = wr.clientWidth  * devicePixelRatio;
  H = c.height = wr.clientHeight * devicePixelRatio;
  c.style.width  = wr.clientWidth  + 'px';
  c.style.height = wr.clientHeight + 'px';
  draw();
}
new ResizeObserver(resize).observe(document.getElementById('map-wrap'));

function calcBounds(vis) {
  if (!vis.length) return { xmin:-1, xmax:1, ymin:-1, ymax:1 };
  let tw = 0, cx = 0, cy = 0;
  for (const d of vis) { const w = Math.pow(10, d.rssi / 10); tw += w; cx += d.x*w; cy += d.y*w; }
  cx /= tw; cy /= tw;
  const xs = vis.map(d => d.x), ys = vis.map(d => d.y);
  return { xmin: Math.min(...xs)-0.1, xmax: Math.max(...xs)+0.1,
           ymin: Math.min(...ys)-0.1, ymax: Math.max(...ys)+0.1 };
}

function worldToScreen(wx, wy, b) {
  const pad = 60 * devicePixelRatio;
  let fx = (wx - b.xmin) / (b.xmax - b.xmin || 1e-9);
  let fy = (wy - b.ymin) / (b.ymax - b.ymin || 1e-9);
  let sx = pad + fx * (W - pad*2);
  let sy = pad + fy * (H - pad*2);
  // rotate around canvas centre
  if (rot) {
    const a = rot * Math.PI / 180, cA = Math.cos(a), sA = Math.sin(a);
    const dx = sx - W/2, dy = sy - H/2;
    sx = W/2 + dx*cA - dy*sA;
    sy = H/2 + dx*sA + dy*cA;
  }
  // zoom + pan around canvas centre
  sx = W/2 + (sx - W/2)*zoom + panX * devicePixelRatio;
  sy = H/2 + (sy - H/2)*zoom + panY * devicePixelRatio;
  return [sx, sy];
}

function draw() {
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#0d0b09';
  ctx.fillRect(0, 0, W, H);

  // subtle grid
  const step = 80 * devicePixelRatio;
  ctx.strokeStyle = '#161210'; ctx.lineWidth = 1;
  for (let x = 0; x < W; x += step) { ctx.beginPath(); ctx.moveTo(x,0); ctx.lineTo(x,H); ctx.stroke(); }
  for (let y = 0; y < H; y += step) { ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(W,y); ctx.stroke(); }

  const vis = data.filter(d => d.age < 120);
  if (!vis.length) {
    ctx.fillStyle = '#5a4a2a';
    ctx.font = (14*devicePixelRatio)+'px monospace';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('no signal \u2014 is pucks.uc running?', W/2, H/2);
    return;
  }

  if (!bounds) bounds = calcBounds(vis);
  const b = bounds;
  // weakest first so brightest renders on top
  const sorted = [...vis].sort((a, b2) => a.rssi - b2.rssi);
  const dpr = devicePixelRatio;
  const placed = [];

  for (const d of sorted) {
    const [sx, sy] = worldToScreen(d.x, d.y, b);
    if (sx < -40*dpr || sx > W+40*dpr || sy < -40*dpr || sy > H+40*dpr) continue;

    const age   = d.age;
    const alpha = age < 2 ? 1.0 : age < 20 ? 0.75 : Math.max(0.15, 1-(age-20)/40);
    const col   = ouiColor(d.mac);
    const cls   = deviceClass(d.ssid, d.fsub);
    const isEmoji = cls !== 'unknown';
    const size  = (age < 5 ? 20 : 16) * dpr;

    ctx.globalAlpha = alpha;

    // glow for fresh
    if (age < 8) {
      const r = 22*dpr, glow = ctx.createRadialGradient(sx,sy,2,sx,sy,r);
      glow.addColorStop(0, col + '60');
      glow.addColorStop(1, col + '00');
      ctx.fillStyle = glow;
      ctx.beginPath(); ctx.arc(sx, sy, r, 0, Math.PI*2); ctx.fill();
    }

    if (isEmoji) {
      ctx.font = size + 'px serif';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillStyle = col;
      ctx.fillText(SYMS[cls], sx, sy);
    } else {
      // simple dot for unknown
      ctx.fillStyle = col;
      ctx.beginPath(); ctx.arc(sx, sy, 5*dpr, 0, Math.PI*2); ctx.fill();
    }

    // label — avoid overlaps
    const label = (d.ssid || d.mac.slice(0, 11)).slice(0, 16);
    const lx = sx + 14*dpr, ly = sy - 5*dpr;
    const lw = label.length * 7 * dpr;
    if (!placed.some(p => Math.abs(p[0]-lx) < lw && Math.abs(p[1]-ly) < 14*dpr)) {
      ctx.font = (10*dpr)+'px monospace';
      ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      ctx.fillStyle = col;
      ctx.globalAlpha = alpha * 0.9;
      ctx.fillText(label, lx, ly);
      placed.push([lx, ly]);
    }
  }
  ctx.globalAlpha = 1;

  // ── network membership lines ──────────────────────────────────────────────
  // For each device with a known bssid, draw a line to the AP (if present).
  // Use the AP's OUI colour at low alpha so lines are visible but not dominant.
  const byMac = {};
  for (const d of vis) byMac[d.mac] = d;

  ctx.save();
  ctx.lineWidth = 1 * dpr;
  for (const d of vis) {
    if (!d.bssid || !byMac[d.bssid] || d.bssid === d.mac) continue;
    const ap = byMac[d.bssid];
    const [sx1, sy1] = worldToScreen(d.x,  d.y,  b);
    const [sx2, sy2] = worldToScreen(ap.x, ap.y, b);
    const age = Math.max(d.age, ap.age);
    const alpha = age < 10 ? 0.35 : Math.max(0.08, 0.35 * (1 - age/120));
    ctx.globalAlpha = alpha;
    ctx.strokeStyle = ouiColor(d.bssid);
    ctx.beginPath(); ctx.moveTo(sx1, sy1); ctx.lineTo(sx2, sy2); ctx.stroke();
  }
  ctx.restore();

  // sidebar — only rebuild if data changed to avoid losing scroll position
  const devlist = document.getElementById('devlist');
  const listKey = vis.map(d=>d.mac+d.age.toFixed(0)).join();
  if (devlist._lastKey !== listKey) {
    devlist._lastKey = listKey;
    devlist.innerHTML = [...vis]
      .sort((a, b2) => b2.rssi - a.rssi)
      .slice(0, 50)
      .map(d => {
        const col = ouiColor(d.mac);
        const cls = deviceClass(d.ssid, d.fsub);
        const alpha = d.age < 10 ? 1 : 0.45;
        const ssidHtml = d.ssid
          ? `<div class="ssid">${escHtml(d.ssid.slice(0,24))}</div>` : '';
        return `<div class="dev" data-mac="${d.mac}" style="opacity:${alpha}" onclick="centreOnMac('${d.mac}')">
          <div class="sym">${SYMS[cls]}</div>
          <div class="info">
            <div class="vendor" style="color:${col}">${escHtml(d.vendor||'Unknown')}</div>
            ${ssidHtml}
            <div class="mac">${d.mac}</div>
          </div>
        </div>`;
      }).join('');
  }

  document.getElementById('count').textContent = vis.length + ' sources \u00b7 ';

  // ── minimap overlay (bottom-left, transparent) ──────────────────────────
  const MM = { x: 12*dpr, w: 140*dpr, h: 100*dpr };
  MM.y = H - MM.h - 12*dpr;
  const pad2 = 6*dpr;

  // background panel
  ctx.save();
  ctx.globalAlpha = 0.55;
  ctx.fillStyle = '#0d0b09';
  ctx.strokeStyle = '#3a2e1a';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect(MM.x, MM.y, MM.w, MM.h, 4*dpr);
  ctx.fill(); ctx.stroke();
  ctx.globalAlpha = 1;

  // "OVERVIEW" label
  ctx.font = (8*dpr)+'px monospace';
  ctx.fillStyle = '#4a3820';
  ctx.textAlign = 'left'; ctx.textBaseline = 'top';
  ctx.fillText('OVERVIEW', MM.x + pad2, MM.y + 3*dpr);

  // all points in world coords → minimap coords
  const allB = calcBounds(vis);
  function toMini(wx, wy) {
    const fx = (wx - allB.xmin) / (allB.xmax - allB.xmin || 1e-9);
    const fy = (wy - allB.ymin) / (allB.ymax - allB.ymin || 1e-9);
    return [
      MM.x + pad2 + fx * (MM.w - pad2*2),
      MM.y + pad2*2 + fy * (MM.h - pad2*3),
    ];
  }

  for (const d of vis) {
    const [mx, my] = toMini(d.x, d.y);
    const alpha2 = d.age < 10 ? 0.9 : Math.max(0.2, 1 - d.age/120);
    ctx.globalAlpha = alpha2;
    ctx.fillStyle = ouiColor(d.mac);
    ctx.beginPath(); ctx.arc(mx, my, 2.5*dpr, 0, Math.PI*2); ctx.fill();
  }

  // viewport rect showing current view
  if (zoom > 1.05) {
    // reverse-map the canvas corners back to world coords
    function screenToWorld(sx, sy) {
      // undo zoom+pan
      let wx2 = (sx - W/2 - panX*dpr) / zoom + W/2;
      let wy2 = (sy - H/2 - panY*dpr) / zoom + H/2;
      // undo rotation
      if (rot) {
        const a = -rot * Math.PI/180, cA = Math.cos(a), sA = Math.sin(a);
        const dx = wx2 - W/2, dy = wy2 - H/2;
        wx2 = W/2 + dx*cA - dy*sA;
        wy2 = H/2 + dx*sA + dy*cA;
      }
      const b2 = allB;
      const pad3 = 60*dpr;
      return [
        b2.xmin + ((wx2 - pad3) / (W - pad3*2)) * (b2.xmax - b2.xmin),
        b2.ymin + ((wy2 - pad3) / (H - pad3*2)) * (b2.ymax - b2.ymin),
      ];
    }
    const [wx0,wy0] = screenToWorld(0, 0);
    const [wx1,wy1] = screenToWorld(W, H);
    const [vx0,vy0] = toMini(wx0, wy0);
    const [vx1,vy1] = toMini(wx1, wy1);
    ctx.globalAlpha = 0.7;
    ctx.strokeStyle = '#c8a060';
    ctx.lineWidth = 1;
    ctx.strokeRect(vx0, vy0, vx1-vx0, vy1-vy0);
  }

  ctx.globalAlpha = 1;
  ctx.restore();
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── SSE push — data arrives the instant the server has it ─────────────────────
(function connect() {
  const es = new EventSource('/events');
  es.onmessage = e => {
    try {
      data = JSON.parse(e.data);
      document.getElementById('status').textContent = new Date().toLocaleTimeString();
      draw();
    } catch(err) {}
  };
  es.onerror = () => {
    document.getElementById('status').textContent = 'reconnecting…';
    es.close();
    setTimeout(connect, 2000);
  };
})();

// ── controls ──────────────────────────────────────────────────────────────────
function doRotate(dir) {
  const vis = data.filter(d => d.age < 120);
  bounds = calcBounds(vis);
  rotTarget = rotTarget + dir * 45;
  if (!rotAnimId) rotAnimId = requestAnimationFrame(animateRot);
}
function doReset() {
  zoom = 1; panX = 0; panY = 0; rotTarget = 0;
  bounds = calcBounds(data.filter(d => d.age < 120));
  if (!rotAnimId) rotAnimId = requestAnimationFrame(animateRot);
  else draw();
}

function centreOnMac(mac) {
  const d = data.find(x => x.mac === mac);
  if (!d || !bounds) return;
  const b = bounds;
  const dpr = devicePixelRatio;
  const pad = 60 * dpr;
  // world → normalised → screen (without current pan/zoom) → offset from centre
  const fx = (d.x - b.xmin) / (b.xmax - b.xmin || 1e-9);
  const fy = (d.y - b.ymin) / (b.ymax - b.ymin || 1e-9);
  const sx0 = pad + fx * (W - pad*2);
  const sy0 = pad + fy * (H - pad*2);
  // apply rotation
  let sx = sx0, sy = sy0;
  if (rot) {
    const a = rot * Math.PI/180, cA = Math.cos(a), sA = Math.sin(a);
    const dx = sx0 - W/2, dy = sy0 - H/2;
    sx = W/2 + dx*cA - dy*sA;
    sy = H/2 + dx*sA + dy*cA;
  }
  // pan so that point lands at canvas centre
  panX = (W/2 - sx) / dpr;
  panY = (H/2 - sy) / dpr;
  draw();
}

// double-click to zoom in (2×) centred on click point
c.addEventListener('dblclick', e => {
  const rect = c.getBoundingClientRect();
  const cx = (e.clientX - rect.left) * devicePixelRatio;
  const cy = (e.clientY - rect.top)  * devicePixelRatio;
  const factor = 2;
  // zoom towards click point
  panX = (panX - (cx/devicePixelRatio - W/2/devicePixelRatio)) * factor + (cx/devicePixelRatio - W/2/devicePixelRatio);
  panY = (panY - (cy/devicePixelRatio - H/2/devicePixelRatio)) * factor + (cy/devicePixelRatio - H/2/devicePixelRatio);
  zoom = Math.min(20, zoom * factor);
  draw();
});

window.addEventListener('wheel', e => {
  e.preventDefault();
  zoom = Math.max(0.1, Math.min(20, zoom * (e.deltaY < 0 ? 1.15 : 0.87)));
  draw();
}, { passive: false });

let _downTime = 0;
c.addEventListener('mousedown', e => {
  _downTime = Date.now();
  dragging = true;
  dragStart = { x: e.clientX, y: e.clientY };
  panStart  = { x: panX, y: panY };
});
window.addEventListener('mousemove', e => {
  if (!dragging) return;
  panX = panStart.x + (e.clientX - dragStart.x);
  panY = panStart.y + (e.clientY - dragStart.y);
  draw();
});
window.addEventListener('mouseup', () => dragging = false);

// pinch zoom
let lastPinchDist = 0;
c.addEventListener('touchstart', e => {
  if (e.touches.length === 2) lastPinchDist = 0;
}, { passive: true });
c.addEventListener('touchmove', e => {
  if (e.touches.length === 2) {
    e.preventDefault();
    const d = Math.hypot(
      e.touches[0].clientX - e.touches[1].clientX,
      e.touches[0].clientY - e.touches[1].clientY
    );
    if (lastPinchDist) zoom = Math.max(0.1, Math.min(20, zoom * d / lastPinchDist));
    lastPinchDist = d;
    draw();
  }
}, { passive: false });

window.addEventListener('keydown', e => {
  if (e.key === 'r') doRotate(1);
  if (e.key === 'R') doRotate(-1);
  if (e.key === 'z' || e.key === 'Z') doReset();
});
</script>
</body>
</html>"""

# ── HTTP server ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/data":
            with _lock:
                body = json.dumps(_cache).encode()
            self._respond(200, "application/json", body)
        elif path == "/events":
            self._sse()
        elif path in ("/", "/index.html"):
            self._respond(200, "text/html; charset=utf-8", HTML.encode())
        else:
            self.send_response(404); self.end_headers()

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                _updated.wait(timeout=2)
                _updated.clear()
                with _lock:
                    payload = json.dumps(_cache)
                msg = f"data: {payload}\n\n".encode()
                self.wfile.write(msg)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _respond(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class ReusableTCPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


if __name__ == "__main__":
    with _lock: _cache[:] = _load()
    with ReusableTCPServer(("", PORT), Handler) as srv:
        print(f"  octoi \u25c8 http://localhost:{PORT}")
        srv.serve_forever()
