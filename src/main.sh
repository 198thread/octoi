#!/usr/bin/env bash
# main.sh — WiFi Graph SLAM TUI
# Deploys pucks.uc to routers, ingests data, computes SLAM, renders TUI

set -euo pipefail

# ── config ──────────────────────────────────────────────────────────────────
LEASES=/var/lib/misc/dnsmasq.leases
PUCKS=./pucks.uc
PORT=8420
RING_SIZE=$((64 * 1024 * 1024))   # 64MB ring buffer ceiling (in lines)
POLL_MS=800
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5)

# ── parse leases ─────────────────────────────────────────────────────────────
mapfile -t IPS < <(awk '{print $3}' "$LEASES")
mapfile -t MACS < <(awk '{print $2}' "$LEASES")
mapfile -t NAMES < <(awk '{print $4}' "$LEASES")
N=${#IPS[@]}

# ── vendor OUI table (colour mapping) ────────────────────────────────────────
declare -A OUI_COLOR
OUI_COLOR["3c:28:6d"]="\033[38;5;214m"   # Google/Coral — orange
OUI_COLOR["70:3a:cb"]="\033[38;5;82m"    # TP-Link — green
OUI_COLOR["1c:f2:9a"]="\033[38;5;75m"    # Ubiquiti — blue
RESET="\033[0m"
BOLD="\033[1m"

oui_color() { local oui="${1:0:8}"; echo "${OUI_COLOR[$oui]:-\033[38;5;255m]}"; }

# ── deploy + start pucks.uc on all devices ───────────────────────────────────
deploy_device() {
    local ip=$1
    # if port is already live, skip — don't stack processes
    if timeout 0.7 bash -c "exec 3<>/dev/tcp/${ip}/${PORT}; cat <&3" 2>/dev/null | grep -q '"rssi"'; then
        return 0
    fi
    # ensure ucode socket module is present
    ssh "${SSH_OPTS[@]}" "root@${ip}" \
        "[ -f /usr/lib/ucode/socket.so ] || opkg install ucode-mod-socket 2>/dev/null" 2>/dev/null
    # copy via stdin (OpenWrt has no sftp-server)
    ssh "${SSH_OPTS[@]}" "root@${ip}" "cat - > /tmp/pucks.uc" < "$PUCKS" 2>/dev/null || return 1
    # kill ALL ucode instances (including D-state holders) via port, then restart
    ssh "${SSH_OPTS[@]}" "root@${ip}" \
        "fuser -k ${PORT}/tcp 2>/dev/null; kill -9 \$(ps | awk '/ucode/{print \$1}') 2>/dev/null; sleep 1
         setsid ucode /tmp/pucks.uc >/tmp/pucks.log 2>&1 </dev/null &" \
        2>/dev/null
    return 0
}

deploy_all() {
    printf "\n${C_AMBER}── waking the pucks ──${RST}\n"
    local pids=()
    for i in "${!IPS[@]}"; do
        deploy_device "${IPS[$i]}" & pids+=($!)
        say "${IPS[$i]}  (${MACS[$i]})  … nudged"
    done
    for p in "${pids[@]}"; do wait "$p" 2>/dev/null || true; done
    sleep 2
    ok "all ${N} nodes prodded into existence"
}


# ── ingest from one device ────────────────────────────────────────────────────
# writes JSON lines to /tmp/octoi_buf_<ip>.jl (shared file per device)
BUF_DIR=/tmp/octoi_ingest
mkdir -p "$BUF_DIR"

ingest_device() {
    local ip=$1 tag=$2
    local buf="${BUF_DIR}/${ip}.jl"
    # pull ~0.7s of data; inject "node" field by stripping trailing } and appending
    timeout 0.7 bash -c "exec 3<>/dev/tcp/${ip}/${PORT}; cat <&3" 2>/dev/null \
        | grep '^{' \
        | sed "s/}\$/,\"node\":\"${tag}\"}/" >> "$buf" 2>/dev/null
    # cap file at 50000 lines
    if [[ -f "$buf" ]]; then
        local lc; lc=$(wc -l < "$buf")
        if (( lc > 50000 )); then
            tail -n 30000 "$buf" > "${buf}.tmp" && mv "${buf}.tmp" "$buf"
        fi
    fi
}

ingest_all() {
    local pids=()
    for i in "${!IPS[@]}"; do
        ingest_device "${IPS[$i]}" "${NAMES[$i]:-${IPS[$i]}}" & pids+=($!)
    done
    for p in "${pids[@]}"; do wait "$p" 2>/dev/null || true; done
}

# ── SLAM: vectorised least-squares RSSI multilateration ──────────────────────
# Reads all buf files, groups by src MAC, computes 2D positions.
# Output: /tmp/octoi_positions.tsv  (src_mac  x  y  rssi_avg  node  ssid)

slam() {
python3 - <<'PYEOF'
import os, json, math, pickle
from collections import defaultdict

BUF_DIR      = "/tmp/octoi_ingest"
OUT          = "/tmp/octoi_positions.tsv"
STATE        = "/tmp/octoi_slam.pkl"
SNAP_WINDOW  = 0.30   # seconds — observations within this window are one snapshot
WIN_SNAPS    = 30     # keep last N snapshots per device for position averaging
EMA_ALPHA    = 0.4    # how fast position estimate tracks new snapshot fixes
WIN_LINES    = 800    # max raw (ts,rssi) lines kept per node per device in ring

# ── load persistent state ─────────────────────────────────────────────────────
try:
    with open(STATE, "rb") as f:
        state = pickle.load(f)
except Exception:
    state = {}

offsets     = state.get("offsets", {})
prev_pos    = state.get("prev_pos", {})   # src -> (x, y)
ssids       = state.get("ssids", {})
n_path_cal  = state.get("n_path", 2.8)
bssid_votes = state.get("bssid_votes", {})  # src -> {bssid -> count}
# rings[node][src] = [(ts, rssi), ...]
_rings_raw = state.get("rings", {})
rings = defaultdict(lambda: defaultdict(list))
for nd, sd in _rings_raw.items():
    for src2, lst in sd.items():
        rings[nd][src2] = lst

# ── incremental read ──────────────────────────────────────────────────────────
node_files = sorted(f for f in os.listdir(BUF_DIR) if f.endswith(".jl"))
for fn in node_files:
    path = os.path.join(BUF_DIR, fn)
    node = fn[:-3]
    try:
        fsize = os.path.getsize(path)
    except OSError:
        continue
    off = offsets.get(fn, 0)
    if off > fsize:
        off = 0
        rings[node] = defaultdict(list)
    if off == fsize:
        continue
    try:
        with open(path, "rb") as fh:
            fh.seek(off)
            chunk = fh.read()
            offsets[fn] = fh.tell()
    except OSError:
        continue
    for raw in chunk.split(b"\n"):
        raw = raw.strip()
        if not raw or raw[0:1] != b"{":
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        src   = obj.get("src", "")
        rssi  = obj.get("rssi")
        ts    = obj.get("ts")
        ssid  = obj.get("ssid")
        bssid = obj.get("bssid")
        if not src or rssi is None or ts is None:
            continue
        buf = rings[node][src]
        buf.append((float(ts), float(rssi)))
        if len(buf) > WIN_LINES:
            del buf[:-WIN_LINES]
        if ssid:
            ssids[src] = ssid
        # track most-seen bssid per src (vote by count)
        if bssid and bssid != src:
            bssid_votes = state.setdefault("bssid_votes", {})
            key = src
            bssid_votes.setdefault(key, {})
            bssid_votes[key][bssid] = bssid_votes[key].get(bssid, 0) + 1

# ── collect all sources and nodes ────────────────────────────────────────────
all_srcs  = {src for nd_map in rings.values() for src in nd_map}
all_nodes = sorted(rings.keys())
if not all_srcs:
    open(OUT, "w").close()
    with open(STATE, "wb") as f:
        pickle.dump({"offsets": offsets, "prev_pos": prev_pos,
                     "rings": {nd: dict(sd) for nd,sd in rings.items()},
                     "ssids": ssids, "n_path": n_path_cal,
                     "bssid_votes": bssid_votes}, f)
    exit(0)

# ── assign node anchors ───────────────────────────────────────────────────────
K = len(all_nodes)
R_anchor = 10.0
anchors = {}
for k, nd in enumerate(all_nodes):
    angle = 2 * math.pi * k / max(K, 1)
    anchors[nd] = (math.cos(angle) * R_anchor, math.sin(angle) * R_anchor)

# ── snapshot extraction ───────────────────────────────────────────────────────
# For each src: collect all (ts, rssi, node) observations, sort by ts,
# then cluster into windows of SNAP_WINDOW seconds.  Each cluster where
# ≥2 nodes are represented is one position snapshot.
def get_snapshots(src):
    obs = []
    for nd in all_nodes:
        for ts, rssi in rings[nd].get(src, []):
            obs.append((ts, rssi, nd))
    obs.sort()
    if not obs:
        return []

    snapshots = []
    i = 0
    while i < len(obs):
        t0 = obs[i][0]
        j = i
        while j < len(obs) and obs[j][0] - t0 <= SNAP_WINDOW:
            j += 1
        window = obs[i:j]
        # one rssi per node in this window: take the median
        node_rssis = defaultdict(list)
        for ts, rssi, nd in window:
            node_rssis[nd].append(rssi)
        snap = {}
        for nd, vals in node_rssis.items():
            vals.sort()
            n = len(vals)
            snap[nd] = (vals[n//2] + vals[~(n//2)]) / 2
        if len(snap) >= 2:
            snapshots.append((t0, snap))
        # advance to next non-overlapping window
        i = j
    return snapshots

# ── path-loss model ───────────────────────────────────────────────────────────
A_ref = -40.0
def rssi_to_dist(rssi):
    exp = (A_ref - rssi) / (10 * n_path_cal)
    return max(0.3, min(60.0, 10 ** exp))

# ── trilaterate one snapshot ──────────────────────────────────────────────────
def trilaterate(snap):
    """snap: {node -> rssi}  →  (x, y) or None"""
    pts = [((anchors[nd][0], anchors[nd][1]), rssi_to_dist(rssi))
           for nd, rssi in snap.items() if nd in anchors]
    if len(pts) < 2:
        return None
    # all-pairs Chan-Ho linearisation
    rows, rhs, wts = [], [], []
    for i in range(len(pts)):
        (xi, yi), di = pts[i]
        for j in range(i+1, len(pts)):
            (xj, yj), dj = pts[j]
            rows.append([2*(xj-xi), 2*(yj-yi)])
            rhs.append(xj**2+yj**2 - xi**2-yi**2 + di**2 - dj**2)
            wts.append(1.0 / max(di + dj, 0.5))
    sw00 = sum(wts[k]*rows[k][0]**2        for k in range(len(rows)))
    sw01 = sum(wts[k]*rows[k][0]*rows[k][1] for k in range(len(rows)))
    sw11 = sum(wts[k]*rows[k][1]**2        for k in range(len(rows)))
    sb0  = sum(wts[k]*rows[k][0]*rhs[k]    for k in range(len(rows)))
    sb1  = sum(wts[k]*rows[k][1]*rhs[k]    for k in range(len(rows)))
    det  = sw00*sw11 - sw01**2
    if abs(det) < 1e-9:
        return None
    return (sw11*sb0 - sw01*sb1) / det, (sw00*sb1 - sw01*sb0) / det

# ── auto-calibrate n_path from inter-node observations ───────────────────────
# Nodes that are anchors also appear as src MACs in the data (their own beacons).
# We know their true anchor-to-anchor distances, so we can fit n_path.
anchor_macs = set(all_nodes)  # node IPs double as src identifiers
cal_ns = []
for src in all_srcs:
    if src not in anchors:
        continue
    for nd in all_nodes:
        if nd == src:
            continue
        buf = rings[nd].get(src, [])
        if not buf:
            continue
        # median rssi from this node for this anchor src
        vals = sorted(r for _, r in buf)
        med = (vals[len(vals)//2] + vals[~(len(vals)//2)]) / 2
        ax, ay = anchors[src]
        bx, by = anchors[nd]
        true_d = math.sqrt((ax-bx)**2+(ay-by)**2)
        if true_d > 0.5:
            n_est = (A_ref - med) / (10 * math.log10(true_d))
            if 1.5 < n_est < 5.0:
                cal_ns.append(n_est)

if len(cal_ns) >= 3:
    n_path_cal = 0.3 * n_path_cal + 0.7 * (sum(cal_ns) / len(cal_ns))

# ── position each device via snapshot trilateration ──────────────────────────
results = []
for src in sorted(all_srcs):
    snaps = get_snapshots(src)

    # Compute best_node and avg_rssi from most recent snapshot
    recent_snap = snaps[-1][1] if snaps else {}
    avg_rssi = sum(recent_snap.values()) / len(recent_snap) if recent_snap else -100.0
    best_node = max(recent_snap, key=recent_snap.get) if recent_snap else (all_nodes[0] if all_nodes else "")

    if not snaps:
        continue

    # Trilaterate each snapshot; keep last WIN_SNAPS fixes
    fixes = []
    for ts, snap in snaps[-WIN_SNAPS:]:
        pos = trilaterate(snap)
        if pos is None:
            continue
        px, py = pos
        r = math.sqrt(px**2 + py**2)
        if r <= R_anchor * 3.0:   # discard diverged fixes
            fixes.append((ts, px, py))

    if not fixes:
        # fallback: place near strongest node's anchor, jittered by MAC hash
        # so single-node devices don't all stack on the same anchor point
        if recent_snap:
            ax, ay = anchors.get(best_node, (0.0, 0.0))
            h = hash(src) & 0xffff
            jitter_r = 0.8 + (h & 0xff) / 255.0 * 1.2   # 0.8–2.0m
            jitter_a = (h >> 8) / 256.0 * 2 * math.pi
            px = ax + math.cos(jitter_a) * jitter_r
            py = ay + math.sin(jitter_a) * jitter_r
        else:
            continue
    else:
        # Weighted mean of fixes, weighted by recency exp(-λ*(t_max-t))
        LAM = math.log(2) / 30.0
        t_max = fixes[-1][0]
        tw = px = py = 0.0
        for ts, fx, fy in fixes:
            w = math.exp(-LAM * (t_max - ts))
            tw += w; px += w*fx; py += w*fy
        px /= tw; py /= tw

    # EMA with previous position for inter-run smoothing
    if src in prev_pos:
        ox, oy = prev_pos[src]
        px = EMA_ALPHA * px + (1-EMA_ALPHA) * ox
        py = EMA_ALPHA * py + (1-EMA_ALPHA) * oy

    prev_pos[src] = (px, py)
    # best bssid: most-voted, but never self (AP entries point to themselves)
    votes = bssid_votes.get(src, {})
    best_bssid = max(votes, key=votes.get) if votes else ""
    results.append((src, px, py, avg_rssi, best_node, ssids.get(src, ""), best_bssid))

# ── write output atomically ───────────────────────────────────────────────────
tmp = OUT + ".tmp"
with open(tmp, "w") as fh:
    for row in results:
        fh.write("\t".join(str(v) for v in row) + "\n")
os.replace(tmp, OUT)

# ── persist state ─────────────────────────────────────────────────────────────
with open(STATE, "wb") as f:
    pickle.dump({"offsets": offsets, "prev_pos": prev_pos,
                 "rings": {nd: dict(sd) for nd,sd in rings.items()},
                 "ssids": ssids, "n_path": n_path_cal,
                 "bssid_votes": bssid_votes}, f)
PYEOF
}

# ── TUI: delegated to tui.py ──────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── startup output helpers ────────────────────────────────────────────────────
C_AMBER="\033[38;5;136m"; C_TAN="\033[38;5;180m"
C_MUTE="\033[38;5;101m";  C_OK="\033[38;5;108m"
C_WARN="\033[38;5;167m";  RST="\033[0m"

say()  { printf "${C_TAN}  %s${RST}\n" "$*"; }
ok()   { printf "${C_OK}  ✓ %s${RST}\n" "$*"; }
warn() { printf "${C_WARN}  ⚠ %s${RST}\n" "$*"; }
hdr()  { printf "\n${C_AMBER}── %s ──${RST}\n" "$*"; }

# ── test: data ingestion ──────────────────────────────────────────────────────
test_ingestion() {
    hdr "sniffing the airwaves"
    ingest_all
    local total=0
    for ip in "${IPS[@]}"; do
        local f="${BUF_DIR}/${ip}.jl"
        local lc=0
        [[ -f "$f" ]] && lc=$(wc -l < "$f")
        total=$(( total + lc ))
        if (( lc > 0 )); then
            ok "${ip} is gossiping (${lc} frames overheard)"
        else
            warn "${ip} is suspiciously quiet"
        fi
    done
    if (( total > 0 )); then
        ok "total haul: ${total} frames — not bad for standing still"
    else
        warn "nothing yet. give the routers a moment to remember they exist"
    fi
}

# ── test: SLAM computation ────────────────────────────────────────────────────
test_slam() {
    hdr "doing maths (the fun kind)"
    slam
    local tsv=/tmp/octoi_positions.tsv
    if [[ -f "$tsv" ]]; then
        local n; n=$(wc -l < "$tsv")
        ok "triangulated ${n} signal sources — geometry still works, apparently"
    else
        warn "slam produced nothing. the maths may have taken a personal day"
    fi
}

# ── background ingest+slam loop ───────────────────────────────────────────────
ingest_slam_loop() {
    while true; do
        ingest_all
        slam
        sleep "$(echo "scale=3; $POLL_MS / 1000" | bc)"
    done
}

# ── main ──────────────────────────────────────────────────────────────────────
main() {
    deploy_all

    sleep 3
    test_ingestion
    slam
    test_slam

    printf "\n${C_TAN}  handing over to the pretty part… → http://localhost:8201${RST}\n\n"
    sleep 0.6

    # run ingest+slam in its own process group so we can kill the whole tree
    set -m
    ingest_slam_loop &
    INGEST_PID=$!
    set +m
    cleanup() {
        kill -- -"$INGEST_PID" 2>/dev/null
        kill "$INGEST_PID" 2>/dev/null
        printf "\033[?25h\033[0m\nStopped.\n"
        exit 0
    }
    trap cleanup INT TERM

    python3 "${SCRIPT_DIR}/web.py"
    cleanup
}

main "$@"
