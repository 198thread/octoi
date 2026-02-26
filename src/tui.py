#!/usr/bin/env python3
"""
octoi tui.py — WiFi Graph SLAM  ·  retro-modern dark-mode
Keys:  + / -  zoom      z  reset zoom      r  rotate 45°      q  quit
"""
import os, sys, json, math, time, tty, termios, select, signal, re, threading
from collections import defaultdict
try:
    import numpy as _np
    _NUMPY = True
except ImportError:
    _NUMPY = False

# ══════════════════════════════════════════════════════════════════════════════
#  BRANDING
# ══════════════════════════════════════════════════════════════════════════════
TITLE    = "OCTOI  ◈  WiFi SLAM"
GRID_CH  = "·"
GRID_FG  = 234

# Coffee palette: warm muted browns, ambers, taupes — nothing neon
PALETTE = [
    130,  # saddle brown
    136,  # dark goldenrod
    94,   # brown
    101,  # dark khaki-brown
    138,  # rosy brown
    95,   # mauve-brown
    180,  # tan
    137,  # peru-ish
    131,  # indian red-brown
    142,  # olive-khaki
    173,  # light salmon-brown
    100,  # olive
    143,  # dark khaki
    109,  # cadet blue-grey (one cool accent)
    145,  # light grey-taupe
    103,  # slate blue-grey
    167,  # muted coral
    108,  # sage
    139,  # medium purple-brown
    102,  # grey-brown
]

DEVICE_SYMS = {
    "router":  ("📡", "A"),
    "probe":   ("📶", "^"),
    "phone":   ("📱", "P"),
    "laptop":  ("💻", "L"),
    "home":    ("🏠", "H"),
    "camera":  ("📷", "C"),
    "tv":      ("📺", "T"),
    "printer": ("🖨", "I"),
    "data":    ("💾", "D"),
    "unknown": ("◦",  "·"),
}

_DC_PATTERNS = [
    ("home",    re.compile(r"bt-|sky|virgin|talktalk|plusnet|ee-|vm", re.I)),
    ("phone",   re.compile(r"iphone|android|pixel|galaxy|huawei|oneplus", re.I)),
    ("laptop",  re.compile(r"laptop|macbook|surface|thinkpad|xps|dell", re.I)),
    ("camera",  re.compile(r"cam|camera|nest|ring|arlo|reolink", re.I)),
    ("tv",      re.compile(r"\btv\b|firetv|roku|chromecast|appletv|bravia", re.I)),
    ("printer", re.compile(r"printer|hp-|epson|canon|brother", re.I)),
]

def device_class(ssid: str, ftype: int, fsub: int) -> str:
    if ssid:
        for cls, pat in _DC_PATTERNS:
            if pat.search(ssid): return cls
    if fsub in (8, 5): return "router"
    if fsub == 4:      return "probe"
    if ftype == 2:     return "data"
    return "unknown"

# ══════════════════════════════════════════════════════════════════════════════
#  OUI  (hwdata — same as Wireshark)
# ══════════════════════════════════════════════════════════════════════════════
OUI_DB_PATH = "/usr/share/hwdata/oui.txt"
_oui_vendor: dict[str, str] = {}

def _load_oui_db():
    _SHORT = re.compile(
        r",?\s+(Inc\.?|LLC|Ltd\.?|Co\.?|Corp\.?|GmbH|Technologies|International"
        r"|Group|Networks?|Systems?|Electronics?)$", re.I)
    try:
        with open(OUI_DB_PATH, errors="replace") as f:
            for line in f:
                if "(hex)" not in line: continue
                m = re.match(r"([0-9A-Fa-f]{2}-[0-9A-Fa-f]{2}-[0-9A-Fa-f]{2})\s+\(hex\)\s+(.*)", line)
                if m:
                    oui = m.group(1).replace("-", ":").lower()
                    v   = _SHORT.sub("", m.group(2).strip()).strip(" ,.")
                    _oui_vendor[oui] = v or "?"
    except OSError: pass

_load_oui_db()

def vendor_name(mac: str) -> str:
    return _oui_vendor.get(mac[:8].lower(), "")

_oui_color: dict[str, int] = {}
_color_idx = 0

def oui_color(mac: str) -> int:
    global _color_idx
    key = mac[:8].lower()
    if key not in _oui_color:
        _oui_color[key] = PALETTE[_color_idx % len(PALETTE)]
        _color_idx += 1
    return _oui_color[key]

# ══════════════════════════════════════════════════════════════════════════════
#  ANSI HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def A(fg=None, bold=False) -> str:
    c = ["1"] if bold else []
    if fg is not None: c.append(f"38;5;{fg}")
    return f"\033[{';'.join(c)}m" if c else ""

R      = "\033[0m"
CLEAR  = "\033[2J\033[H"
HIDE_C = "\033[?25l"
SHOW_C = "\033[?25h"
GOTO   = lambda r, c: f"\033[{r};{c}H"

# ══════════════════════════════════════════════════════════════════════════════
#  CANVAS  — diff-based renderer: only emits changed cells
# ══════════════════════════════════════════════════════════════════════════════
Cell = tuple  # (char, fg, bold)
BLANK: Cell = (" ", 0, False)

class Canvas:
    def __init__(self, w: int, h: int):
        self.w = w; self.h = h
        self._g: list[list[Cell]] = [[BLANK] * w for _ in range(h)]
        self._prev: list[list[Cell]] | None = None  # previous frame

    def clear(self):
        self._g = [[BLANK] * self.w for _ in range(self.h)]

    def put(self, x, y, ch, fg, bold=False):
        if 0 <= y < self.h and 0 <= x < self.w:
            self._g[y][x] = (ch, fg, bold)
            # wide chars (emoji etc.) occupy 2 columns — blank the next cell
            # so diff_render will clear the right half when overwritten
            if ord(ch) > 0x2E7F and x + 1 < self.w:
                self._g[y][x + 1] = BLANK

    def text(self, x, y, s, fg, bold=False):
        for i, ch in enumerate(s):
            self.put(x + i, y, ch, fg, bold)

    def box(self, x0, y0, x1, y1, fg=238, title=""):
        for x in range(x0, x1 + 1):
            self.put(x, y0, "─", fg); self.put(x, y1, "─", fg)
        for y in range(y0, y1 + 1):
            self.put(x0, y, "│", fg); self.put(x1, y, "│", fg)
        self.put(x0, y0, "╭", fg); self.put(x1, y0, "╮", fg)
        self.put(x0, y1, "╰", fg); self.put(x1, y1, "╯", fg)
        if title:
            tx = x0 + 2
            for ch in f" {title} ":
                if tx < x1: self.put(tx, y0, ch, 180, True); tx += 1  # warm title

    def hline(self, x0, x1, y, ch, fg):
        for x in range(x0, x1 + 1): self.put(x, y, ch, fg)

    def grid(self, x0, y0, x1, y1, step=5):
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                if (x - x0) % step == 0 and (y - y0) % step == 0:
                    self.put(x, y, GRID_CH, GRID_FG)

    def diff_render(self, force_full=False) -> str:
        """Emit only changed cells using cursor positioning — ~30fps friendly."""
        parts: list[str] = []
        prev = self._prev
        cur_row = cur_col = -1
        cur_fg = cur_bold = None

        for y, row in enumerate(self._g):
            for x, cell in enumerate(row):
                if not force_full and prev is not None and prev[y][x] == cell:
                    continue
                ch, fg, bold = cell
                # move cursor only when not adjacent
                if y != cur_row or x != cur_col:
                    parts.append(GOTO(y + 1, x + 1))
                    cur_row, cur_col = y, x
                # emit style only when changed
                if fg != cur_fg or bold != cur_bold:
                    parts.append(A(fg if fg else None, bold))
                    cur_fg, cur_bold = fg, bold
                parts.append(ch)
                cur_col += 1

        # snapshot for next diff
        self._prev = [list(row) for row in self._g]
        if cur_fg is not None or cur_bold:
            parts.append(R)
        return "".join(parts)

# ══════════════════════════════════════════════════════════════════════════════
#  GEOMETRY
# ══════════════════════════════════════════════════════════════════════════════
def rotate(x, y, deg):
    a = math.radians(deg); ca, sa = math.cos(a), math.sin(a)
    return x*ca - y*sa, x*sa + y*ca

def world2cell(x, y, xmin, xmax, ymin, ymax, W, H):
    fx = (x - xmin) / max(xmax - xmin, 1e-9)
    fy = (y - ymin) / max(ymax - ymin, 1e-9)
    return (max(1, min(W - 2, int(fx * (W - 3)) + 1)),
            max(1, min(H - 2, int(fy * (H - 3)) + 1)))

def world2cells_batch(xs, ys, xmin, xmax, ymin, ymax, W, H):
    """Batch coordinate transform — uses numpy if available, else list comp."""
    dx = max(xmax - xmin, 1e-9); dy = max(ymax - ymin, 1e-9)
    if _NUMPY:
        ax = _np.asarray(xs, dtype=_np.float32)
        ay = _np.asarray(ys, dtype=_np.float32)
        px = _np.clip(((ax - xmin) / dx * (W - 3) + 1).astype(_np.int32), 1, W - 2)
        py = _np.clip(((ay - ymin) / dy * (H - 3) + 1).astype(_np.int32), 1, H - 2)
        return px.tolist(), py.tolist()
    px = [max(1, min(W-2, int((x-xmin)/dx*(W-3))+1)) for x in xs]
    py = [max(1, min(H-2, int((y-ymin)/dy*(H-3))+1)) for y in ys]
    return px, py

# ══════════════════════════════════════════════════════════════════════════════
#  DATA — background thread refreshes cache every POLL_S seconds
# ══════════════════════════════════════════════════════════════════════════════
BUF_DIR  = "/tmp/octoi_ingest"
TSV_PATH = "/tmp/octoi_positions.tsv"
POLL_S   = 0.8

class DataCache:
    def __init__(self):
        self._lock     = threading.Lock()
        self._rows     = []
        self._src_meta = {}
        self._dirty    = True

    def refresh(self):
        rows = self._load_positions()
        meta = self._load_src_meta()
        with self._lock:
            self._rows = rows; self._src_meta = meta; self._dirty = True

    def get(self):
        with self._lock:
            self._dirty = False
            return list(self._rows), dict(self._src_meta)

    @property
    def dirty(self):
        with self._lock: return self._dirty

    @staticmethod
    def _load_src_meta():
        result: dict[str, tuple] = {}
        try:
            fns = [f for f in os.listdir(BUF_DIR) if f.endswith(".jl")]
        except OSError:
            return result
        for fn in fns:
            try: lines = open(os.path.join(BUF_DIR, fn)).readlines()[-600:]
            except OSError: continue
            for raw in lines:
                try: obj = json.loads(raw.strip())
                except: continue
                src  = obj.get("src", "");  ts = float(obj.get("ts", 0))
                ft   = int(obj.get("type", 0)); fs = int(obj.get("sub", 0))
                ssid = obj.get("ssid", "")
                if src and ts > result.get(src, (0,))[0]:
                    ssid = ssid or result.get(src, (0,0,0,""))[3]
                    result[src] = (ts, ft, fs, ssid)
        return result

    @staticmethod
    def _load_positions():
        rows = []
        try:
            for line in open(TSV_PATH):
                p = line.strip().split("\t")
                if len(p) < 4: continue
                src, x, y, rssi = p[0], float(p[1]), float(p[2]), float(p[3])
                ssid = p[5] if len(p) > 5 else ""
                rows.append((src, x, y, float(rssi), ssid))
        except: pass
        return rows

def _data_thread(cache: DataCache, stop: threading.Event):
    while not stop.is_set():
        cache.refresh()
        stop.wait(POLL_S)

# ══════════════════════════════════════════════════════════════════════════════
#  KEY INPUT
# ══════════════════════════════════════════════════════════════════════════════
class KeyReader:
    def __init__(self):
        self.fd  = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
    def read(self) -> str | None:
        if select.select([sys.stdin], [], [], 0)[0]:
            return os.read(self.fd, 4).decode("utf-8", errors="ignore")
        return None
    def restore(self):
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN TUI
# ══════════════════════════════════════════════════════════════════════════════
FRAME_S = 1 / 30   # ~33 ms target frame time

class OctoiTUI:
    def __init__(self):
        self.zoom      = 1.0
        self.rotation  = 0.0
        self.running   = True
        self.frame     = 0
        self._cv: Canvas | None = None
        self._tw = self._th = 0
        self._last_zoom = 1.0
        self._last_rot  = 0.0

    def handle_key(self, ch: str):
        if   ch in ("q","Q","\x03"): self.running = False
        elif ch in ("+","="):        self.zoom = min(self.zoom * 1.4, 20.0)
        elif ch in ("-","_"):        self.zoom = max(self.zoom / 1.4, 1.0)
        elif ch in ("z","Z"):        self.zoom = 1.0
        elif ch in ("r","R"):        self.rotation = (self.rotation + 45) % 360

    def _dot(self, cv, cx, cy, mac, ssid, ft, fs, age, big=True):
        dc   = device_class(ssid, ft, fs)
        sym  = DEVICE_SYMS[dc][0 if big else 1]
        fg   = oui_color(mac)
        bold = age < 2
        if not bold and age > 8:
            # gentle fade toward dim — stay in coffee tones
            steps = min(int((age - 8) / 4), 6)
            fg    = max(232, fg - steps * 3)
        cv.put(cx, cy, sym, fg, bold)
        return fg, bold

    def render(self, rows, src_meta) -> str:
        try:    TW, TH = os.get_terminal_size()
        except: TW, TH = 120, 40

        # full redraw when terminal resizes or view state changes
        force_full = False
        if TW != self._tw or TH != self._th:
            self._cv = Canvas(TW, TH); self._tw = TW; self._th = TH
            force_full = True
        if self.zoom != self._last_zoom or self.rotation != self._last_rot:
            force_full = True
            self._last_zoom = self.zoom
            self._last_rot  = self.rotation
        cv = self._cv
        cv.clear()

        # ── filter & age ─────────────────────────────────────────────────────
        now = max((v[0] for v in src_meta.values()), default=time.time())
        visible = []
        for src, x, y, rssi, ssid in rows:
            ts, ft, fs, smeta_ssid = src_meta.get(src, (0, 0, 0, ""))
            ssid = ssid or smeta_ssid
            age  = now - ts
            if age > 60: continue
            visible.append((src, x, y, rssi, ssid, age, ft, fs))

        if not visible:
            cv.text(2, TH // 2, "no signal — is pucks.uc running?", 101)
            return HIDE_C + cv.diff_render(force_full)

        # ── rotate (batched) ─────────────────────────────────────────────────
        if self.rotation:
            a = math.radians(self.rotation); ca, sa = math.cos(a), math.sin(a)
            if _NUMPY:
                rxs = _np.asarray([v[1] for v in visible], dtype=_np.float32)
                rys = _np.asarray([v[2] for v in visible], dtype=_np.float32)
                nx = rxs*ca - rys*sa; ny = rxs*sa + rys*ca
                visible = [
                    (v[0], float(nx[i]), float(ny[i]), *v[3:])
                    for i, v in enumerate(visible)
                ]
            else:
                visible = [
                    (src, x*ca - y*sa, x*sa + y*ca, rssi, ssid, age, ft, fs)
                    for src, x, y, rssi, ssid, age, ft, fs in visible
                ]

        xs, ys, rssis = [v[1] for v in visible], [v[2] for v in visible], [v[3] for v in visible]

        # ── weighted centroid centre ──────────────────────────────────────────
        weights  = [10 ** (r / 10) for r in rssis]
        tot_w    = sum(weights)
        cx_world = sum(x * w for (_, x, *_), w in zip(visible, weights)) / tot_w
        cy_world = sum(y * w for (_, _, y, *_), w in zip(visible, weights)) / tot_w

        xspan = max(max(xs) - min(xs), 1e-6)
        yspan = max(max(ys) - min(ys), 1e-6)
        hw = xspan / (2 * self.zoom); hh = yspan / (2 * self.zoom)
        xmin = cx_world - hw; xmax = cx_world + hw
        ymin = cy_world - hh; ymax = cy_world + hh

        wxmin = min(xs) - 0.1; wxmax = max(xs) + 0.1
        wymin = min(ys) - 0.1; wymax = max(ys) + 0.1

        # ── layout: [MAP] [MINI] [LEGEND] ────────────────────────────────────
        MINI_W = 24
        MINI_H = min(TH - 2, 20)
        LEG_W  = 40
        MAP_W  = max(30, TW - MINI_W - LEG_W - 2)
        MAP_H  = max(12, TH - 2)

        mx0 = MAP_W;         mx1 = mx0 + MINI_W - 1
        lx0 = mx1 + 1;      lx1 = min(TW - 1, lx0 + LEG_W - 1)

        # ── main map ─────────────────────────────────────────────────────────
        zoom_lbl = f"×{self.zoom:.1f}" if self.zoom > 1.05 else ""
        rot_lbl  = f"↻{int(self.rotation)}°" if self.rotation else ""
        mtitle   = f" {TITLE}  {rot_lbl}{zoom_lbl}".rstrip()
        cv.box(0, 0, MAP_W - 1, MAP_H - 1, fg=101, title=mtitle)
        cv.grid(1, 1, MAP_W - 2, MAP_H - 2, step=5)

        # ── minimap ───────────────────────────────────────────────────────────
        cv.box(mx0, 0, mx1, MINI_H - 1, fg=94, title=" overview ")
        imx0 = mx0 + 1; imx1 = mx1 - 1
        imy0 = 1;        imy1 = MINI_H - 2
        mini_iw = imx1 - imx0 + 1; mini_ih = imy1 - imy0 + 1

        # ── legend ────────────────────────────────────────────────────────────
        cv.box(lx0, 0, lx1, MAP_H - 1, fg=94, title=" devices ")
        lci  = lx0 + 1
        lrow = 1
        hdr  = f"{'':2s}{'Vendor':12s} {'MAC':11s} {'SSID':12s}"[:lx1 - lci]
        cv.text(lci, lrow, hdr, 143, bold=True); lrow += 1
        cv.hline(lci, lx1 - 1, lrow, "─", 94);   lrow += 1

        # ── place main-map dots (batch transform) ────────────────────────────
        placed: dict[str, tuple] = {}
        sorted_vis = sorted(visible, key=lambda v: v[3], reverse=True)
        _sv_xs = [v[1] for v in sorted_vis]; _sv_ys = [v[2] for v in sorted_vis]
        _pxs, _pys = world2cells_batch(_sv_xs, _sv_ys, xmin, xmax, ymin, ymax, MAP_W, MAP_H)
        for i, (src, x, y, rssi, ssid, age, ft, fs) in enumerate(sorted_vis):
            px, py = _pxs[i], _pys[i]
            fg, bold = self._dot(cv, px, py, src, ssid, ft, fs, age, big=True)
            placed[src] = (px, py, fg, bold, ssid, rssi, age, ft, fs)

        # ── MAC labels on main map ────────────────────────────────────────────
        label_boxes: list[tuple] = []
        for src, (px, py, fg, bold, ssid, rssi, age, ft, fs) in placed.items():
            label = (ssid.strip() if ssid else src[:11])[:13]
            for dy in (0, -1, 1):
                ly = py + dy
                lx = px + 1
                if not (1 <= ly < MAP_H - 1 and lx + len(label) < MAP_W - 1): continue
                if any(not (lx + len(label) <= bx0 or lx >= bx1 or ly + 1 <= by0 or ly >= by1)
                       for bx0, by0, bx1, by1 in label_boxes): continue
                cv.text(lx, ly, label, fg, bold=False)
                label_boxes.append((lx, ly, lx + len(label), ly + 1))
                break

        # ── minimap dots (batch transform) ───────────────────────────────────
        _mv_xs = [v[1] for v in visible]; _mv_ys = [v[2] for v in visible]
        _mpxs, _mpys = world2cells_batch(_mv_xs, _mv_ys, wxmin, wxmax, wymin, wymax, mini_iw+2, mini_ih+2)
        for i, (src, x, y, rssi, ssid, age, ft, fs) in enumerate(visible):
            mpx = _mpxs[i] + imx0 - 1; mpy = _mpys[i] + imy0 - 1
            if imx0 <= mpx <= imx1 and imy0 <= mpy <= imy1:
                self._dot(cv, mpx, mpy, src, ssid, ft, fs, age, big=False)

        # viewport rect (when zoomed)
        if self.zoom > 1.05:
            tmx = lambda wx: imx0 + int((wx - wxmin) / (wxmax - wxmin) * (mini_iw - 1))
            tmy = lambda wy: imy0 + int((wy - wymin) / (wymax - wymin) * (mini_ih - 1))
            vx0 = max(imx0, tmx(xmin)); vx1 = min(imx1, tmx(xmax))
            vy0 = max(imy0, tmy(ymin)); vy1 = min(imy1, tmy(ymax))
            for vx in range(vx0, vx1 + 1):
                cv.put(vx, vy0, "─", 145); cv.put(vx, vy1, "─", 145)
            for vy in range(vy0, vy1 + 1):
                cv.put(vx0, vy, "│", 145); cv.put(vx1, vy, "│", 145)
            cv.put(vx0, vy0, "╭", 145); cv.put(vx1, vy0, "╮", 145)
            cv.put(vx0, vy1, "╰", 145); cv.put(vx1, vy1, "╯", 145)

        # ── legend rows ───────────────────────────────────────────────────────
        max_dev = MAP_H - lrow - 5
        for src, (px, py, fg, bold, ssid, rssi, age, ft, fs) in \
                sorted(placed.items(), key=lambda kv: kv[1][5], reverse=True)[:max_dev]:
            dc     = device_class(ssid, ft, fs)
            sym    = DEVICE_SYMS[dc][1]
            vend   = vendor_name(src)[:12]
            ssid_s = (ssid.strip() if ssid else "—")[:12]
            row_fg = fg if age < 10 else 95
            line   = f"{sym:2s}{vend:12s} {src[:11]:11s} {ssid_s:12s}"[:lx1 - lci]
            cv.text(lci, lrow, line, row_fg, bold=(age < 2)); lrow += 1

        # ── vendor colour key ─────────────────────────────────────────────────
        lrow += 1
        cv.hline(lci, lx1 - 1, lrow, "─", 94); lrow += 1
        cv.text(lci, lrow, "Vendors", 143, bold=True); lrow += 1
        for oui, color in list(_oui_color.items())[:MAP_H - lrow - 1]:
            vend = _oui_vendor.get(oui, oui)[:16]
            cv.text(lci, lrow, f"■ {vend}", color, bold=True); lrow += 1

        # ── hint bar ─────────────────────────────────────────────────────────
        hint = f" +/- zoom  z reset  r rotate  q quit  │  {len(visible)} sources  f{self.frame}"
        cv.text(0, TH - 1, hint[:TW - 1], 95)

        self.frame += 1
        return HIDE_C + cv.diff_render(force_full)

# ══════════════════════════════════════════════════════════════════════════════
#  EVENT LOOP
# ══════════════════════════════════════════════════════════════════════════════
def main():
    cache  = DataCache()
    stop   = threading.Event()
    thread = threading.Thread(target=_data_thread, args=(cache, stop), daemon=True)
    thread.start()

    tui  = OctoiTUI()
    keys = KeyReader()
    signal.signal(signal.SIGTERM, lambda *_: setattr(tui, "running", False))
    signal.signal(signal.SIGWINCH, lambda *_: None)

    # prime canvas with full clear
    sys.stdout.write(CLEAR + HIDE_C); sys.stdout.flush()

    try:
        while tui.running:
            t0 = time.monotonic()
            ch = keys.read()
            if ch: tui.handle_key(ch)

            rows, meta = cache.get()
            out = tui.render(rows, meta)
            if out:
                sys.stdout.write(out); sys.stdout.flush()

            elapsed = time.monotonic() - t0
            wait    = max(0.0, FRAME_S - elapsed)
            if wait:
                time.sleep(wait)

    finally:
        stop.set()
        keys.restore()
        sys.stdout.write(SHOW_C + R + "\n"); sys.stdout.flush()

if __name__ == "__main__":
    main()
