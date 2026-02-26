import * as nl from "nl80211";
import * as rtnl from "rtnl";
import * as socket from "socket";
import * as uloop from "uloop";
import { open } from "fs";

// --- config ---
let RING_MAX_BYTES = 20 * 1024 * 1024;
let DWELL_MS       = 200;
let TCP_PORT       = 8420;
let BATCH_MS       = 200;

// --- helpers: read interface index and MAC from sysfs ---
function ifindex(name) {
	let f = open("/sys/class/net/" + name + "/ifindex", "r");
	if (!f) return null;
	let idx = int(trim(f.read("line")));
	f.close();
	return idx;
}

function ifmac(name) {
	let f = open("/sys/class/net/" + name + "/address", "r");
	if (!f) return null;
	let mac = trim(f.read("line"));
	f.close();
	return mac;
}

// --- radiotap parser ---
function parse_rt(pkt) {
	let rt_len = ord(pkt,2) | (ord(pkt,3)<<8);
	let off = 4;
	let words = [];
	while (true) {
		let w = ord(pkt,off)|(ord(pkt,off+1)<<8)|(ord(pkt,off+2)<<16)|(ord(pkt,off+3)<<24);
		push(words, w);
		off += 4;
		if (!(w & 0x80000000)) break;
	}

	let w0 = words[0];
	let flags = null, rate = null, chan_freq = null, rssi = null;

	// TSFT (bit 0): 8B, align 8
	if (w0 & 1) { off = int((off+7)/8)*8; off += 8; }

	// FLAGS (bit 1): 1B
	if (w0 & 2) { flags = ord(pkt,off); off++; }

	// RATE (bit 2): 1B x500kbps
	if (w0 & 4) { rate = ord(pkt,off) * 0.5; off++; }

	// CHANNEL (bit 3): align 2, 4B
	if (w0 & 8) { off = int((off+1)/2)*2; chan_freq = ord(pkt,off)|(ord(pkt,off+1)<<8); off += 4; }

	// skip FHSS (bit 4): 2B
	if (w0 & 16) { off += 2; }

	// DBM_ANTSIGNAL (bit 5): 1B signed
	if (w0 & (1<<5)) { let v = ord(pkt,off); rssi = v>127 ? v-256 : v; off++; }

	// DBM_ANTNOISE (bit 6): 1B
	if (w0 & (1<<6)) { off++; }

	// LOCK_QUALITY (bit 7): 2B
	if (w0 & (1<<7)) { off += 2; }

	// TX_ATTENUATION (bit 8): 2B
	if (w0 & (1<<8)) { off += 2; }

	// DB_TX_ATTENUATION (bit 9): 2B
	if (w0 & (1<<9)) { off += 2; }

	// DBM_TX_POWER (bit 10): 1B
	if (w0 & (1<<10)) { off++; }

	// ANTENNA (bit 11): 1B
	if (w0 & (1<<11)) { off++; }

	// DB_ANTSIGNAL (bit 12): 1B
	if (w0 & (1<<12)) { off++; }

	// DB_ANTNOISE (bit 13): 1B
	if (w0 & (1<<13)) { off++; }

	// RX_FLAGS (bit 14): align 2, 2B
	if (w0 & (1<<14)) { off = int((off+1)/2)*2; off += 2; }

	// TX_FLAGS (bit 15): align 2, 2B
	if (w0 & (1<<15)) { off = int((off+1)/2)*2; off += 2; }

	// RTS_RETRIES (bit 16): 1B
	if (w0 & (1<<16)) { off++; }

	// DATA_RETRIES (bit 17): 1B
	if (w0 & (1<<17)) { off++; }

	// MCS (bit 19): 3B
	if (w0 & (1<<19)) { off += 3; }

	// AMPDU (bit 20): align 4, 8B
	if (w0 & (1<<20)) { off = int((off+3)/4)*4; off += 8; }

	// VHT (bit 21): align 4, 12B
	if (w0 & (1<<21)) { off = int((off+3)/4)*4; off += 12; }

	// TIMESTAMP (bit 22): align 8, 12B
	if (w0 & (1<<22)) { off = int((off+7)/8)*8; off += 12; }

	// HE (bit 23): align 4, 12B
	if (w0 & (1<<23)) { off = int((off+3)/4)*4; off += 12; }

	// per-chain signals from ext words (bit 5 = antsignal, bit 11 = antnoise)
	// ath10k reports 4 ext words but only 2 are real chains; unused slots are
	// filled with a copy of the last valid value — they end up >>15 dB below
	// the MRC-combined signal and are filtered out below.
	let chains = [];
	for (let i = 1; i < length(words); i++) {
		let w = words[i];
		let sig = null, noise = null;
		if (w & (1<<5))  { let v = ord(pkt,off); sig   = v>127?v-256:v; off++; }
		if (w & (1<<11)) { let v = ord(pkt,off); noise = v>127?v-256:v; off++; }
		if (sig != null) push(chains, { sig, noise });
	}

	// Filter real chains: must be ≤ combined+3 dB (MRC combined is always ≥ any
	// individual chain) and ≥ combined-20 dB (beyond that it's padding/garbage).
	let real = (rssi != null)
		? filter(chains, c => c.sig <= rssi + 3 && c.sig >= rssi - 20)
		: chains;

	// Best signal: max of real per-chain values, fallback to combined rssi.
	// Using max mirrors MRC selection combining — the strongest chain dominates.
	let best_rssi = rssi;
	if (length(real) > 0) {
		let m = real[0].sig;
		for (let c in real) if (c.sig > m) m = c.sig;
		best_rssi = m;
	}

	return { flags, rate, chan_freq, rssi: best_rssi, chains: real, rt_len };
}

// --- parse_ssid: extract SSID from IE bytes using replace() ---
function parse_ssid(pkt, frame_off) {
	let fc  = ord(pkt,frame_off) | (ord(pkt,frame_off+1)<<8);
	let sub = (fc>>4) & 15;
	// beacon=8, probe response=5 have fixed params (12B); probe request=4 has none
	let ie_off = frame_off + 24 + (sub==8 || sub==5 ? 12 : 0);
	let plen = length(pkt);
	while (ie_off < plen - 1) {
		let tag  = ord(pkt, ie_off);
		let tlen = ord(pkt, ie_off+1);
		if (tag == 0 && tlen > 0) {
			return replace(substr(pkt, ie_off+2, tlen), /[^\x20-\x7e]/g, "?");
		}
		ie_off += 2 + tlen;
	}
	return null;
}

// --- band name table ---
let BAND_NAME = { "0": "2.4GHz", "1": "5GHz", "2": "6GHz" };

// --- detect best channel width from freq capability flags ---
function best_width(freqs) {
	let active = filter(freqs, f => !f.disabled);
	if (!length(active)) return { width: 2, no_80: true, no_160: true };
	let has80  = !!(filter(active, f => !f.no_80mhz)[0]);
	let has160 = !!(filter(active, f => !f.no_160mhz)[0]);
	switch (true) {
	case has160: return { width: 4, no_80: false, no_160: false };
	case has80:  return { width: 3, no_80: false, no_160: true };
	default:     return { width: 2, no_80: true,  no_160: true };
	}
}

// --- get_radios: returns array of radio dicts ---
function get_radios() {
	return map(
		nl.request(nl.const.NL80211_CMD_GET_WIPHY, nl.const.NLM_F_DUMP, { split_wiphy_dump: true }),
		w => {
			// find the first populated band and its index
			let band_idx = null;
			let band = null;
			for (let i = 0; i < length(w.wiphy_bands); i++) {
				if (w.wiphy_bands[i]) { band_idx = i; band = w.wiphy_bands[i]; break; }
			}
			let winfo = best_width(band.freqs);
			let can80 = !winfo.no_80;
			// build freq list: each entry { freq, c1 } for SET_CHANNEL
			let freqs = filter(map(band.freqs, f => {
				if (f.disabled) return null;
				if (can80  && f.no_80mhz)                        return null;
				if (!can80 && f.no_ht40_plus && f.no_ht40_minus) return null;
				let c1;
				switch (true) {
				case can80:
					// 6GHz base 5955; 5GHz upper/mid/lower blocks
					let base = f.freq >= 5955 ? 5955 :
					           f.freq >= 5745 ? 5745 :
					           f.freq >= 5500 ? 5500 : 5180;
					c1 = base + int((f.freq - base) / 80) * 80 + 30;
					break;
				case f.no_ht40_minus:
					c1 = f.freq + 10;
					break;
				default:
					c1 = f.freq - 10;
				}
				return { freq: f.freq, c1 };
			}), f => f != null);
			return {
				wiphy:      w.wiphy,
				wiphy_name: w.wiphy_name,
				band:       BAND_NAME[""+band_idx] ?? ("band"+band_idx),
				width:      winfo.width,
				freqs
			};
		}
	);
}

// --- monitor: ensure monitor iface exists for radio, tune to ch_idx ---
function monitor(radio, ch_idx) {
	let ch = radio.freqs[ch_idx];
	let existing = filter(
		nl.request(nl.const.NL80211_CMD_GET_INTERFACE, nl.const.NLM_F_DUMP, {}),
		i => i.wiphy == radio.wiphy && i.iftype == nl.const.NL80211_IFTYPE_MONITOR
	)[0];
	let iface = existing;
	if (!iface) {
		iface = nl.request(nl.const.NL80211_CMD_NEW_INTERFACE, 0, {
			wiphy: radio.wiphy, ifname: "mon" + radio.wiphy, iftype: nl.const.NL80211_IFTYPE_MONITOR
		});
	}
	// bring interface up (needed on both fresh create and re-use after process death)
	rtnl.request(rtnl.const.RTM_NEWLINK, 0, { dev: iface.ifname, flags: 1, change: 1 });
	nl.request(nl.const.NL80211_CMD_SET_CHANNEL, 0, {
		wdev: iface.wdev, wiphy_freq: ch.freq, channel_width: radio.width, center_freq1: ch.c1
	});
	return { ifname: iface.ifname, wdev: iface.wdev, freq: ch.freq, width: radio.width, c1: ch.c1 };
}

// --- pipe_out: TCP server; global state avoids nested functions ---
let _pipe_clients  = [];
let _pipe_pending  = [];
let _pipe_srv      = null;
let _pipe_batch_ms = 200;

function _pipe_flush() {
	if (length(_pipe_clients) > 0 && length(_pipe_pending) > 0) {
		let msg = "";
		for (let f in _pipe_pending) msg += f;
		_pipe_pending = [];
		_pipe_clients = filter(_pipe_clients, c => {
			let ok = c.send(msg);
			if (!ok) { uloop.handle(c, null); c.close(); }
			return ok;
		});
	} else {
		_pipe_pending = [];
	}
	uloop.timer(_pipe_batch_ms, _pipe_flush);
}

function _pipe_read_client(c) {
	let d = c.recv(1);
	if (d == null || d == "") {
		uloop.handle(c, null);
		c.close();
		_pipe_clients = filter(_pipe_clients, x => x != c);
	}
}

function _pipe_accept() {
	let c = _pipe_srv.accept();
	if (!c) return;
	push(_pipe_clients, c);
	uloop.handle(c, () => _pipe_read_client(c), uloop.ULOOP_READ);
}

function pipe_out(port, batch_ms) {
	_pipe_batch_ms = batch_ms;
	_pipe_srv = socket.create(socket.AF_INET, socket.SOCK_STREAM | socket.SOCK_NONBLOCK, 0);
	_pipe_srv.bind({ family: socket.AF_INET, address: "0.0.0.0", port });
	_pipe_srv.listen(4);
	uloop.handle(_pipe_srv, _pipe_accept, uloop.ULOOP_READ);
	uloop.timer(batch_ms, _pipe_flush);
	return {
		push:  frame => push(_pipe_pending, frame),
		close: () => {
			for (let c in _pipe_clients) { uloop.handle(c, null); c.close(); }
			uloop.handle(_pipe_srv, null);
			_pipe_srv.close();
		}
	};
}

// --- ring buffer (byte-bounded) ---
let _ring       = [];
let _ring_bytes = 0;
let _ring_max   = RING_MAX_BYTES;

function ring_push(line) {
	_ring_bytes += length(line);
	push(_ring, line);
	while (_ring_bytes > _ring_max && length(_ring) > 0) {
		let removed = splice(_ring, 0, 1);
		_ring_bytes -= length(removed[0]);
	}
}

// --- capture: bind raw packet sockets, register uloop handlers ---
function capture(monitors, piper) {
	for (let m in monitors) {
		let s = socket.create(socket.AF_PACKET, socket.SOCK_RAW, 768);
		s.bind({
			family:        socket.AF_PACKET,
			ifindex:       ifindex(m.ifname),
			hardware_type: 1,
			packet_type:   0,
			address:       ifmac(m.ifname)
		});
		let ifname = m.ifname;
		uloop.handle(s, () => {
			let msg = s.recvmsg(65535);
			if (!msg) return;
			let pkt = msg.data;
			let plen = length(pkt);
			let rt  = parse_rt(pkt);
			let off = rt.rt_len;
			// need at least 16 bytes of 802.11 header after radiotap
			if (off + 16 > plen) return;
			// sanity: rssi must be negative (dBm); drop corrupt/padding frames
			let rssi = rt.rssi ?? 0;
			if (rssi >= 0) return;
			let c = clock();
			let ts = sprintf("%.6f", c[0] + c[1]/1e9);
			let fc  = ord(pkt,off) | (ord(pkt,off+1)<<8);
			let typ = (fc>>2) & 3;
			let sub = (fc>>4) & 15;
			let ds  = (fc>>8) & 3;
			let src = sprintf("%02x:%02x:%02x:%02x:%02x:%02x",
				ord(pkt,off+10), ord(pkt,off+11), ord(pkt,off+12),
				ord(pkt,off+13), ord(pkt,off+14), ord(pkt,off+15));
			// drop null/broadcast source (malformed or AMPDU padding frames)
			if (src == "00:00:00:00:00:00" || src == "ff:ff:ff:ff:ff:ff") return;
			// extract BSSID from management (typ=0) and data (typ=2) frames only.
			// Control frames (typ=1) only have 2 addresses — addr3 is not a BSSID.
			//   ds=0 (IBSS/mgmt): a3=BSSID  ds=1 (to-AP): a1=BSSID
			//   ds=2 (from-AP):   a3=BSSID  ds=3 (WDS):    no BSSID
			let bssid = null;
			if (typ != 1 && ds != 3) {
				let boff = (ds == 1) ? off+4 : off+16;
				if (plen >= boff+6) {
					bssid = sprintf("%02x:%02x:%02x:%02x:%02x:%02x",
						ord(pkt,boff),ord(pkt,boff+1),ord(pkt,boff+2),
						ord(pkt,boff+3),ord(pkt,boff+4),ord(pkt,boff+5));
					if (bssid == "ff:ff:ff:ff:ff:ff" || bssid == "00:00:00:00:00:00") bssid = null;
				}
			}
			let ssid = (typ==0 && (sub==8||sub==5||sub==4)) ? parse_ssid(pkt, off) : null;
			// ts, rssi, src are critical; rest is ancillary
			let line = sprintf('{"ts":%s,"rssi":%d,"src":"%s","iface":"%s","rate":%s,"freq":%s,"type":%d,"sub":%d,"len":%d%s%s}\n',
				ts, rssi, src, ifname,
				rt.rate != null ? sprintf("%.1f", rt.rate) : "null",
				rt.chan_freq != null ? ""+rt.chan_freq : "null",
				typ, sub, plen,
				ssid   != null ? sprintf(',"ssid":"%s"',   ssid)   : "",
				bssid  != null ? sprintf(',"bssid":"%s"',  bssid)  : "");
			ring_push(line);
			if (piper) piper.push(line);
		}, uloop.ULOOP_READ);
	}
}

// --- channel hopping ---
let _hop_radios   = null;
let _hop_monitors = null;
let _hop_idx      = 0;

function _hop_tick() {
	for (let i = 0; i < length(_hop_radios); i++) {
		let r  = _hop_radios[i];
		let ch = r.freqs[_hop_idx % length(r.freqs)];
		nl.request(nl.const.NL80211_CMD_SET_CHANNEL, 0, {
			wdev: _hop_monitors[i].wdev, wiphy_freq: ch.freq,
			channel_width: r.width, center_freq1: ch.c1
		});
		_hop_monitors[i].freq = ch.freq;
	}
	_hop_idx++;
	uloop.timer(DWELL_MS, _hop_tick);
}

function start_hopping(radios, monitors) {
	_hop_radios   = radios;
	_hop_monitors = monitors;
	_hop_idx      = 0;
	uloop.timer(DWELL_MS, _hop_tick);
}

// --- main ---
let radios = get_radios();
print("radios:", length(radios), "\n");
for (let r in radios) print(" ", r.wiphy_name, r.band, "freqs:", length(r.freqs), "\n");
print("\n");

let monitors = map(radios, (r, i) => monitor(r, 0));
for (let m in monitors) print("mon:", m.ifname, "freq:", m.freq, "width:", m.width, "\n");
print("\n");

uloop.init();

let piper = pipe_out(TCP_PORT, BATCH_MS);
print("serving ring buffer on :", TCP_PORT, "\n\n");

capture(monitors, piper);
start_hopping(radios, monitors);

print("running... (Ctrl-C to stop)\n");
uloop.run();

piper.close();
