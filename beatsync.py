#!/usr/bin/env python3
"""
beatsync — drive OpenLamp/WLED lamps in time with an external musical clock.

Where this sits in the OpenLamp stack
-------------------------------------
This is a MIDI/Link frontend of the openlamp-midi overlay — the same layer as
lumideck_midi.py, which maps MIDI notes/CC to lamp commands. beatsync focuses on
*tempo*: it follows an external MIDI clock or an Ableton Link session and drives
the lamps on the beat. Like every OpenLamp frontend it never talks to a device
directly — it POSTs OpenLamp State commands to the engine's local API on
127.0.0.1:8377 (the LumiDeck plugin or the headless daemon owns the persistent
connections). It stays a standalone helper on purpose: MIDI (python-rtmidi) and
Ableton Link (aalink) are native C/C++ extensions, deliberately kept out of the
pure-Python, zero-toolchain LumiDeck plugin binary.

Three clock sources, same lamp mapping
--------------------------------------
  --source midi   follow an external MIDI clock (24 ppqn: Start/Stop/Continue,
                  Song-Position). Any DAW, drum machine, or Bome routing that
                  sends MIDI clock works — including Ableton via a virtual port.
  --source link   join an Ableton Link session (phone-locked tempo + phase,
                  no cable). Requires `pip install aalink`.
  --source tap    BE the clock: tap a MIDI note (pad / footswitch / key) along to
                  the music and beatsync derives the tempo from your taps, then
                  free-runs the lamps on it. No DAW/clock needed (acoustic, jam).
                  Each tap re-seeds the downbeat. Optional --tap-note to pick the note.
                  Taps also arrive as UDP datagrams (--tap-udp-port) so a Stream Deck
                  key can be the tap pad with no MIDI at all.
  --source taplink  tap to SET the Ableton Link tempo: your taps become the master
                  tempo for the whole Link session — Ableton Live + every Link app +
                  the lamps follow. Same tap inputs as --source tap. Needs aalink.

The hardware cap that shapes everything
---------------------------------------
Measured on the reference WLED bulbs (lumideck PROTOCOL.md, 2026-07-03): the firmware
drops the session beyond ~4 acknowledged commands/second. beatsync sends **one**
API call per tick and refuses (with a warning) a subdivision×tempo that would
exceed ~4 ticks/s, so the lamps never choke mid-set. That is also why the
default action is `flash:` (one API call that the engine expands + auto-reverts
itself) rather than a two-call bri pulse.

Landing the accent on the downbeat (`--accent`)
-----------------------------------------------
`--accent` flashes beat 1 of the bar in a distinct colour. Whether that beat 1
is the *real* downbeat depends entirely on what the clock source carries:

  Ableton Link  — the source shares the bar's PHASE continuously. We set
                  link.quantum = beats-per-bar and seed the beat counter from
                  link.phase, so the accent falls on Ableton's bar 1 the instant
                  we connect — exact, no drift, even across two lamps. This is the
                  reliable path for the downbeat.
  MIDI clock    — carries TEMPO ONLY; a 0xF8 tick says nothing about where the bar
                  is (confirmed: Ableton emits only clock while playing). The bar
                  is recovered from two occasional messages: Start (0xFA), treated
                  as bar 1 — reliable if you press Play *after* arming beatsync;
                  and Song Position (0xF2), which re-phases the bar when the DAW
                  sends it. Under plain clock with neither, the accent is a guess.

Landing the flash *on* the beat (latency anticipation)
------------------------------------------------------
There is a delay between "decide to flash" and "the lamp physically changes"
(HTTP round-trip + the lamp's reaction). beatsync learns it — an EMA of the POST
round-trip plus a fixed hardware bias (`--latency-bias`, default 30 ms — measured on
Athom WLED bulbs at ~25-37 ms avg via lamp-bench) — and fires that many milliseconds
EARLY so the light change coincides with the beat. Capped at 200 ms so a bad
measurement can't throw the flash wildly early. Full write-up in the repo README.

Usage
-----
  python3 sync/beatsync.py --list-ports
  python3 sync/beatsync.py --source midi --port "IAC" --action flash --colors rouge
  python3 sync/beatsync.py --source midi --port Bome --sub 2 --action cycle \
        --colors rouge,bleu,vert --accent --lamps front
  python3 sync/beatsync.py --source link --bpm 120 --action pulse
  python3 sync/beatsync.py --source tap --port "IAC" --tap-note 60 --accent
  python3 sync/beatsync.py --source taplink --midi-clock-out --accent   # tap → Link + MIDI clock out

Ctrl-C to stop (lamps are restored to their pre-sync state on exit).
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import signal
import sys
import threading
import time
import urllib.request
import urllib.parse

DEFAULT_API = "http://127.0.0.1:8377"
# Firmware ceiling — the WLED lamp drops acked commands (and, pushed harder, its
# ESP can crash/reboot) past ~4 COMMANDS/second. A blink is 2 commands (bright +
# dark), so this is enforced per-command, not per-beat (Benoit 2026-07-13, after a
# too-fast strobe knocked a lamp offline). Excess commands are dropped, never queued.
MAX_CMDS_PER_SEC = 4.2   # tiny headroom so an evenly-spaced 120-bpm blink (≈4/s) isn't
                         # clipped by jitter, still well under the ~5-6/s that crashes the ESP
MAX_TICKS_PER_SEC = 4.2
# The engine's colour vocabulary (lamp.py COLORS). Used only to validate input.
# name → RGB, so beat flashes POST a raw patch with tt:0 (INSTANT, no fade) instead
# of the engine's flash: alias — a lamp configured with a transition (e.g. 400 ms)
# would otherwise fade each flash, making it soft and land off-beat (Benoit 2026-07-13).
COLORS_RGB = {
    "jaune": (255, 210, 0), "violet": (150, 70, 170), "orange": (255, 125, 0),
    "bleuclair": (130, 195, 255), "rouge": (230, 0, 40), "vert": (0, 200, 80),
    "rose": (255, 130, 170), "bleu": (0, 100, 200), "blanc": (255, 255, 255),
    "cyan": (0, 200, 200), "magenta": (200, 0, 200), "turquoise": (0, 200, 160),
}
KNOWN_COLORS = COLORS_RGB   # `in` checks still work; validation unchanged


# --------------------------------------------------------------------------- #
#  Lamp bus — turns beat ticks into local-API calls, rate-limited.            #
# --------------------------------------------------------------------------- #
class LampBus:
    """Sends one command per tick to the local API, respecting MAX_TICKS_PER_SEC.

    `action` decides what a tick does:
      flash  — flash:<colour>@<ms>   (1 API call; engine reverts itself)
      pulse  — bri 255 then bri back (2 calls; only safe at low subdivision)
      cycle  — advance through --colors, one colour per tick (1 call)
      off    — no lamp write, tick is just logged (dry-run / metronome check)
    On `--accent`, the first tick of each bar uses the accent colour / full bri.
    """

    def __init__(self, api, lamps, action, colors, accent, flash_ms, lat_bias, verbose):
        self.api = api.rstrip("/")
        self.lamps = lamps                       # "" == all lamps
        self.action = action
        self.colors = colors or ["blanc"]
        self.accent = accent
        self.flash_ms = flash_ms
        self.lat_bias = lat_bias                 # fixed lamp-reaction ms added to the learned RTT
        self.verbose = verbose
        self._cycle_i = 0
        self._last_send = 0.0
        self._saved = None                       # pre-sync snapshot for restore
        self._min_gap = 1.0 / MAX_TICKS_PER_SEC
        self._rtt_ema = None                     # learned round-trip to the engine (ms)
        self._sent = collections.deque()         # timestamps of recent commands (rate cap)

    def _gate(self):
        # HARD per-command rate cap: protects the lamp firmware. Returns False (drop)
        # when the last second already holds MAX_CMDS_PER_SEC commands.
        now = time.monotonic()
        while self._sent and now - self._sent[0] > 1.0:
            self._sent.popleft()
        if len(self._sent) >= MAX_CMDS_PER_SEC:
            return False
        self._sent.append(now)
        return True

    # -- latency learning ------------------------------------------------------
    def _note_rtt(self, ms):
        # EMA of the observed POST round-trip — the *network+engine* share of the
        # beat→light delay (the lamp-reaction share is the fixed lat_bias).
        self._rtt_ema = ms if self._rtt_ema is None else 0.2 * ms + 0.8 * self._rtt_ema

    def anticip_ms(self):
        # how far BEFORE the beat to fire so the light lands ON it. Capped so a
        # bad measurement can never push the flash more than 200 ms early.
        return max(0.0, min(200.0, (self._rtt_ema or 0.0) + self.lat_bias))

    # -- low-level API helpers ------------------------------------------------
    def _lamps_q(self):
        return ("?lamps=" + urllib.parse.quote(self.lamps)) if self.lamps else ""

    def _cmd(self, c):
        """GET /cmd?c=...  — the alias/engine command channel (flash, colours…)."""
        if not self._gate():
            return False
        url = f"{self.api}/cmd?c={urllib.parse.quote(c)}"
        if self.lamps:
            url += "&lamps=" + urllib.parse.quote(self.lamps)
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                json.loads(r.read() or b"{}")
            return True
        except Exception as e:
            if self.verbose:
                print("  ! api /cmd failed:", e, file=sys.stderr)
            return False

    def _patch(self, st):
        """POST /json/state — a raw WLED-style patch (instant tt:0 strobe, pulse…)."""
        if not self._gate():
            return False
        url = f"{self.api}/json/state{self._lamps_q()}"
        data = json.dumps(st).encode()
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=1.5) as r:
                r.read()
            self._note_rtt((time.monotonic() - t0) * 1000.0)
            return True
        except Exception as e:
            if self.verbose:
                print("  ! api /json/state failed:", e, file=sys.stderr)
            return False

    # -- lifecycle ------------------------------------------------------------
    def arm(self):
        """Snapshot current lamp state so we can restore it on exit."""
        try:
            with urllib.request.urlopen(f"{self.api}/status", timeout=2) as r:
                self._saved = json.loads(r.read())
            print(f"→ armed on {len(self._saved)} lamp(s); "
                  f"action={self.action} colors={','.join(self.colors)} "
                  f"target={self.lamps or 'all'}")
        except Exception as e:
            print("! could not reach local API at", self.api, "-", e, file=sys.stderr)
            print("  Is Stream Deck (or the headless daemon) running? See daemon/.",
                  file=sys.stderr)
            return False
        return True

    def restore(self):
        if not self._saved:
            return
        # Best-effort: put each lamp back to on/off + brightness it had.
        for name, s in self._saved.items():
            try:
                if s.get("on"):
                    self._patch({"on": True, "bri": round(s.get("bri", 60) * 2.55)})
                else:
                    self._patch({"on": False})
            except Exception:
                pass
        print("← restored lamps to pre-sync state.")

    # -- the tick -------------------------------------------------------------
    def tick(self, beat_in_bar, is_downbeat, bpm):
        now = time.monotonic()
        if now - self._last_send < self._min_gap:
            # Over the firmware cap for this instant — skip rather than choke it.
            return
        self._last_send = now

        if self.verbose:
            mark = "▶" if is_downbeat else "·"
            print(f"  {mark} beat {beat_in_bar}  {bpm:5.1f} bpm")

        if self.action == "off":
            return
        if self.action in ("flash", "cycle"):
            if self.action == "cycle":
                col = self.colors[self._cycle_i % len(self.colors)]
                self._cycle_i += 1
                hi = 255
            else:
                col = self.colors[0]
                hi = 255
                if self.accent and is_downbeat and len(self.colors) > 1:
                    col = self.colors[1]                    # colour accent: 2nd colour on the downbeat
                elif self.accent and len(self.colors) == 1 and not is_downbeat:
                    hi = 120                                # linked accent: one colour, off-beats softer so
                                                            # the downbeat punches brighter (same hue)
            self._strobe(col, hi)
        elif self.action == "pulse":
            hi = 255 if (not self.accent or is_downbeat) else 180
            self._patch({"bri": hi, "tt": 0})
            threading.Timer(self.flash_ms / 1000.0,
                            lambda: self._patch({"bri": 40, "tt": 0})).start()

    def _strobe(self, colname, hi=255):
        # BRUTAL beat hit: instant full-bright colour (tt:0 = no fade, snaps ON the
        # beat), then instant drop to dark so each beat is a sharp punch, not a fade.
        # hi < 255 = a softer hit (used for the off-beats of a single-colour accent).
        rgb = list(COLORS_RGB.get(colname, (255, 255, 255)))
        self._patch({"on": True, "col": rgb, "bri": hi, "tt": 0})
        threading.Timer(self.flash_ms / 1000.0,
                        lambda: self._patch({"bri": 0, "tt": 0})).start()   # dark between hits


# --------------------------------------------------------------------------- #
#  MIDI clock source (python-rtmidi, 24 ppqn).                                 #
# --------------------------------------------------------------------------- #
class MidiClockSource:
    """Follows an external MIDI clock. 24 clocks = 1 quarter note.

    Fires `on_tick(beat_in_bar, is_downbeat, bpm)` every (24/sub) clocks while
    the transport is running (after Start/Continue, until Stop). BPM is
    estimated from the clock interval over a short rolling window.
    """
    CLOCK, START, CONTINUE, STOP, SPP = 0xF8, 0xFA, 0xFB, 0xFC, 0xF2

    def __init__(self, port_hint, sub, beats_per_bar, on_tick, anticip=None):
        self.port_hint = port_hint
        self.clocks_per_tick = max(1, round(24 / sub))
        self.beats_per_bar = beats_per_bar
        self.on_tick = on_tick
        self.anticip = anticip or (lambda: 0.0)   # ms to fire BEFORE the beat (latency comp)
        self._midiin = None
        # Tick as soon as clock flows: many DAWs (Ableton) only emit clock while
        # PLAYING and send Start once — if beatsync connects mid-playback it never
        # sees that Start, so default to running and let Stop (0xFC) halt it. Start
        # just re-zeroes the beat position (Benoit 2026-07-13, verified w/ Ableton).
        self._running = True
        self._clock_count = 0          # clocks since transport start
        self._next_boundary = 1        # index of the next subdivision to fire (anticipated)
        self._times = []               # recent clock timestamps for bpm estimate
        self._stop = threading.Event()

    @staticmethod
    def list_ports():
        import rtmidi
        return rtmidi.MidiIn().get_ports()

    def _open(self):
        import rtmidi
        m = rtmidi.MidiIn()
        ports = m.get_ports()
        if not ports:
            raise RuntimeError("no MIDI input ports found. Open a virtual port "
                               "(Audio MIDI Setup → IAC Driver) or route via Bome.")
        idx = 0
        if self.port_hint:
            matches = [i for i, p in enumerate(ports)
                       if self.port_hint.lower() in p.lower()]
            if not matches:
                raise RuntimeError(f"no MIDI port matches '{self.port_hint}'. "
                                   f"Available: {ports}")
            idx = matches[0]
        m.open_port(idx)
        # CRUCIAL: rtmidi ignores timing (clock) bytes by default — re-enable.
        m.ignore_types(sysex=True, timing=False, active_sense=True)
        m.set_callback(self._on_midi)
        self._midiin = m
        print(f"→ MIDI clock: listening on '{ports[idx]}' "
              f"(sub={24 // self.clocks_per_tick if self.clocks_per_tick else 1}/quarter)")

    def _bpm(self):
        if len(self._times) < 2:
            return 0.0
        span = self._times[-1] - self._times[0]
        if span <= 0:
            return 0.0
        per_clock = span / (len(self._times) - 1)
        return 60.0 / (per_clock * 24)

    def current_bpm(self):
        return self._bpm()

    def _on_midi(self, event, _data=None):
        msg, _dt = event
        if not msg:
            return
        status = msg[0]
        if status == self.START:
            self._running = True
            self._clock_count = 0
            self._next_boundary = 1        # Start = bar 1 → the accent locks to the DAW's downbeat
            self._times.clear()
        elif status == self.SPP and len(msg) >= 3:
            # Song Position (0xF2): 16th-note position from song start. Re-phase the bar
            # so the accent aligns even when we join mid-song (if the DAW sends it — Ableton
            # under plain clock does NOT, so a fresh Play/Start is the reliable path).
            sub_index = int(round(((msg[2] << 7) | msg[1]) / 4.0 * self.sub))
            self._clock_count = 0
            self._next_boundary = sub_index + 1
        elif status == self.CONTINUE:
            self._running = True
        elif status == self.STOP:
            self._running = False
        elif status == self.CLOCK and self._running:
            t = time.monotonic()
            self._times.append(t)
            if len(self._times) > 24:
                self._times.pop(0)
            cpt = self.clocks_per_tick
            # anticipation expressed in CLOCKS: fire this many clocks before the
            # boundary so the light lands on it (per_clock ≈ beat/24).
            per_clock_ms = ((self._times[-1] - self._times[0]) / (len(self._times) - 1) * 1000.0
                            if len(self._times) > 1 else 0.0)
            ac = min(cpt - 1, round(self.anticip() / per_clock_ms)) if per_clock_ms > 0 else 0
            if (self._clock_count + ac) >= self._next_boundary * cpt:
                i = self._next_boundary
                sub_per_beat = max(1, round(24 / cpt))
                beat = (i // sub_per_beat) % self.beats_per_bar
                is_down = (i % (sub_per_beat * self.beats_per_bar) == 0)
                try:
                    self.on_tick(beat + 1, is_down, self._bpm())
                except Exception as e:
                    print("  ! tick handler error:", e, file=sys.stderr)
                self._next_boundary += 1
            self._clock_count += 1

    def run(self):
        self._open()
        print("  (ticking whenever MIDI clock flows — press Play in your DAW; Stop halts)")
        try:
            while not self._stop.wait(0.2):
                pass
        finally:
            if self._midiin:
                self._midiin.close_port()

    def stop(self):
        self._stop.set()


# --------------------------------------------------------------------------- #
#  Ableton Link source (aalink, asyncio). Optional dependency.                #
# --------------------------------------------------------------------------- #
class LinkClockSource:
    """Joins an Ableton Link session: shared tempo + beat phase, no MIDI cable.

    Requires `pip install aalink` (native build). Fires the same on_tick as the
    MIDI source, awaiting each subdivision via Link's phase clock.
    """
    def __init__(self, bpm, sub, beats_per_bar, on_tick, anticip=None):
        self.bpm = bpm
        self.sub = max(1, sub)
        self.beats_per_bar = beats_per_bar
        self.on_tick = on_tick
        self.anticip = anticip or (lambda: 0.0)   # ms to fire before the beat
        self._stop = False
        self._cur_bpm = bpm                       # updated from Link each tick (for MIDI-clock-out)
        self._pending_tempo = None                # a control message asked to set the Link tempo

    def current_bpm(self):
        return self._cur_bpm

    def set_tempo(self, bpm):
        # Control channel (e.g. the Stream Deck intensity dial): write the WHOLE Link
        # session tempo — Ableton + every Link app + the lamps follow. Applied by the
        # run loop (aalink's tempo setter must run inside its asyncio loop).
        self._pending_tempo = max(30.0, min(300.0, float(bpm)))

    def run(self):
        try:
            import asyncio
            from aalink import Link
        except ImportError:
            print("! Ableton Link support needs the 'aalink' package:\n"
                  "    pip install aalink\n"
                  "  (native build; needs a C++ toolchain — Xcode CLT on macOS).",
                  file=sys.stderr)
            sys.exit(2)

        async def loop():
            link = Link(self.bpm)                          # aalink uses the running loop (loop arg deprecated)
            link.enabled = True
            print(f"→ Ableton Link: session joined @ {self.bpm} bpm "
                  f"(sub={self.sub}/quarter). Start any Link app to lock phase.")
            step = 1.0 / self.sub
            getphase = (lambda: link.phase()) if callable(getattr(link, "phase", None)) else (lambda: link.phase)
            try:
                link.quantum = float(self.beats_per_bar)   # bar length = our accent period
            except Exception:
                pass
            # Lock to Link's phase clock with sync(), then each subdivision: ONE precise
            # sleep to fire `anticip` ms before the next boundary (no polling jitter), then
            # sync() again to re-lock to the exact boundary — so we never drift and the
            # commands stay evenly spaced (which also stops the rate cap dropping them).
            await link.sync(step)
            # Seed the counter from Link's PHASE (0 = bar downbeat, shared with Ableton) so
            # the accent lands on the real "1", not an arbitrary start beat (Benoit 2026-07-13).
            i = int(round(float(getphase()) / step))
            while not self._stop:
                if self._pending_tempo is not None:            # control channel set a new tempo
                    try:
                        link.tempo = self._pending_tempo       # propagates to Ableton + all Link peers
                    except Exception as e:
                        print("  ! set link tempo:", e, file=sys.stderr)
                    self._pending_tempo = None
                tempo = float(link.tempo); self._cur_bpm = tempo
                sub_dur = (60.0 / max(1e-6, tempo)) * step
                anticip_s = min(sub_dur * 0.9, self.anticip() / 1000.0)
                i += 1                                  # the boundary we fire for
                await asyncio.sleep(max(0.0, sub_dur - anticip_s))   # wake `anticip` before it
                beat = (i // self.sub) % self.beats_per_bar
                is_down = (i % (self.sub * self.beats_per_bar) == 0)
                try:
                    self.on_tick(beat + 1, is_down, tempo)
                except Exception as e:
                    print("  ! tick handler error:", e, file=sys.stderr)
                await link.sync(step)                   # re-lock to boundary i (kills drift)

        try:
            import asyncio
            asyncio.run(loop())
        except KeyboardInterrupt:
            pass

    def stop(self):
        self._stop = True


class TapTempoSource:
    """Tap a MIDI note to SET the tempo — no external clock needed.

    Unlike the MIDI-clock and Link sources (which FOLLOW an external tempo), here YOU
    are the clock: tap a pad / footswitch / key along to the music and beatsync derives
    the BPM from your tap intervals, then free-runs the lamps at that tempo. Each tap
    re-seeds the downbeat, so the accent always tracks your taps. Ideal when there's no
    DAW clock to follow (acoustic set, jam). Nothing flashes until the first two taps
    give a tempo. A gap > 2 s between taps starts a fresh estimate (you can re-tap a new
    tempo any time). BPM clamped to 30–300.
    """
    def __init__(self, port_hint, sub, beats_per_bar, on_tick, tap_note=None,
                 anticip=None, init_bpm=120.0, udp_port=8378):
        self.port_hint = port_hint
        self.sub = max(1, sub)
        self.beats_per_bar = beats_per_bar
        self.on_tick = on_tick
        self.tap_note = tap_note                    # None = ANY note-on counts as a tap
        self.anticip = anticip or (lambda: 0.0)
        self.bpm = init_bpm
        self.udp_port = udp_port                     # 0 disables; else a datagram = a tap
        self._taps = collections.deque(maxlen=5)    # recent tap times → rolling BPM
        self._armed = False                         # flash only once we have a tempo
        self._reset = threading.Event()             # a tap fell → re-seed the downbeat
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._midiin = None
        self._udp = None

    def _open_midi(self):
        import rtmidi
        m = rtmidi.MidiIn(); ports = m.get_ports()
        if not ports:
            raise RuntimeError("no MIDI input ports found. Open a virtual port "
                               "(Audio MIDI Setup → IAC Driver) or route via Bome.")
        matches = [i for i, p in enumerate(ports) if self.port_hint.lower() in p.lower()]
        if not matches:
            raise RuntimeError(f"no MIDI port matches '{self.port_hint}'. Available: {ports}")
        m.open_port(matches[0]); m.set_callback(self._on_midi); self._midiin = m
        tgt = "any note" if self.tap_note is None else f"note {self.tap_note}"
        print(f"→ Tap tempo: tap {tgt} on '{ports[matches[0]]}' to set the tempo.")

    def _start_udp(self):
        # A local UDP socket so ANY tool (e.g. the Stream Deck plugin, on each key press)
        # can send a tap without emitting MIDI — the payload is ignored, arrival = a tap.
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", self.udp_port)); s.settimeout(0.3)
        self._udp = s
        def _loop():
            while not self._stop.is_set():
                try: s.recvfrom(16)
                except socket.timeout: continue
                except OSError: break
                self._tap()
        threading.Thread(target=_loop, daemon=True).start()
        print(f"→ Tap tempo: also listening for taps on udp 127.0.0.1:{self.udp_port} "
              f"(the deck key can tap here).")

    def _on_midi(self, event, _data=None):
        msg, _dt = event
        if not msg or len(msg) < 3:
            return
        if (msg[0] & 0xF0) == 0x90 and msg[2] > 0:          # note-on, velocity > 0
            if self.tap_note is None or msg[1] == self.tap_note:
                self._tap()

    def set_tempo(self, bpm):
        # Control channel (e.g. the Stream Deck intensity dial): set the tempo directly,
        # no tap needed. Re-seeds the downbeat so it flashes right away. In taplink mode
        # the run loop then writes this to the Link session (Ableton follows).
        with self._lock:
            self.bpm = max(30.0, min(300.0, float(bpm)))
            self._armed = True
        self._reset.set()

    def _tap(self):
        now = time.monotonic()
        with self._lock:
            self._taps.append(now)
            ivs = [b - a for a, b in zip(self._taps, list(self._taps)[1:]) if 0 < b - a < 2.0]
            if ivs:
                self.bpm = max(30.0, min(300.0, 60.0 / (sum(ivs) / len(ivs))))
                self._armed = True
        self._reset.set()                                    # this tap IS the downbeat

    def run(self):
        if self.port_hint:                          # MIDI-pad taps (optional)
            try:
                import rtmidi  # noqa: F401
            except ImportError:
                print("! MIDI tap needs python-rtmidi:\n    pip install python-rtmidi", file=sys.stderr)
                sys.exit(2)
            self._open_midi()
        if self.udp_port:                           # programmatic taps (the deck key)
            self._start_udp()
        if not self._midiin and not self._udp:
            print("! tap source needs a MIDI port (--port) or UDP (--tap-udp-port > 0).",
                  file=sys.stderr)
            sys.exit(2)
        print(f"  waiting for taps… (sub={self.sub}/quarter; nothing flashes until 2 taps set a tempo)")
        i, next_beat = 0, time.monotonic()
        while not self._stop.is_set():
            if self._reset.is_set():
                self._reset.clear()
                with self._lock:
                    bpm, armed = self.bpm, self._armed
                if armed:
                    self._fire(0, bpm)                       # the tap = beat 1, flash it now
                    i, next_beat = 1, time.monotonic() + (60.0 / bpm) / self.sub
                continue
            with self._lock:
                bpm, armed = self.bpm, self._armed
            if not armed:
                time.sleep(0.02); continue                   # waiting for the first taps
            sub_dur = (60.0 / bpm) / self.sub
            anticip_s = min(sub_dur * 0.9, self.anticip() / 1000.0)
            wait = (next_beat - anticip_s) - time.monotonic()
            if wait > 0.04:
                time.sleep(0.04); continue                   # short naps → stay responsive to taps
            if wait > 0:
                time.sleep(wait)
            self._fire(i, bpm)
            i += 1; next_beat += sub_dur

    def _fire(self, i, bpm):
        beat = (i // self.sub) % self.beats_per_bar
        is_down = (i % (self.sub * self.beats_per_bar) == 0)
        try:
            self.on_tick(beat + 1, is_down, bpm)
        except Exception as e:
            print("  ! tick handler error:", e, file=sys.stderr)

    def current_bpm(self):
        return self.bpm

    def stop(self):
        self._stop.set()
        if self._udp:
            try: self._udp.close()
            except Exception: pass


class TapLinkSource(TapTempoSource):
    """Tap to SET the **Ableton Link** tempo — you become the tempo master, natively.

    Same tap inputs as --source tap (a MIDI note and/or the deck key over UDP), but instead
    of free-running an internal clock, each tap writes the tempo of the shared Link session:
    Ableton Live and every other Link app adopt it, and the lamps follow Link's phase — so
    the WHOLE rig (DAW + lamps) locks to your taps. The downbeat stays on Link's shared bar
    (so the accent aligns with Ableton), the tempo is yours. Requires `pip install aalink`.
    """
    def run(self):
        try:
            import asyncio
            from aalink import Link
        except ImportError:
            print("! Tap→Link needs aalink:\n    pip install aalink", file=sys.stderr)
            sys.exit(2)
        if self.port_hint:                                  # MIDI-pad taps (optional)
            try:
                import rtmidi  # noqa: F401
                self._open_midi()
            except ImportError:
                pass
        if self.udp_port:                                   # deck-key taps
            self._start_udp()
        if not self._midiin and not self._udp:
            print("! tap→link needs a MIDI port (--port) or UDP (--tap-udp-port > 0).", file=sys.stderr)
            sys.exit(2)

        async def loop():
            link = Link(self.bpm); link.enabled = True
            try:
                link.quantum = float(self.beats_per_bar)
            except Exception:
                pass
            print(f"→ Tap → Ableton Link: tap to set the session tempo — Ableton + every Link "
                  f"app + the lamps follow. Joined at {self.bpm:.0f} bpm.")
            step = 1.0 / self.sub
            getphase = (lambda: link.phase()) if callable(getattr(link, "phase", None)) else (lambda: link.phase)
            await link.sync(step)
            i = int(round(float(getphase()) / step))        # seed from Link's shared bar phase
            while not self._stop.is_set():
                if self._reset.is_set():                    # a tap fell → push its BPM to the session
                    self._reset.clear()
                    with self._lock:
                        bpm = self.bpm
                    try:
                        link.tempo = float(bpm)             # propagates to Ableton + all Link peers
                    except Exception as e:
                        print("  ! set link tempo:", e, file=sys.stderr)
                tempo = float(link.tempo)
                sub_dur = (60.0 / max(1e-6, tempo)) * step
                anticip_s = min(sub_dur * 0.9, self.anticip() / 1000.0)
                i += 1
                await asyncio.sleep(max(0.0, sub_dur - anticip_s))
                beat = (i // self.sub) % self.beats_per_bar
                is_down = (i % (self.sub * self.beats_per_bar) == 0)
                try:
                    self.on_tick(beat + 1, is_down, tempo)
                except Exception as e:
                    print("  ! tick handler error:", e, file=sys.stderr)
                await link.sync(step)

        try:
            import asyncio
            asyncio.run(loop())
        except KeyboardInterrupt:
            pass


class MidiClockOut:
    """Emit standard MIDI clock (24 pulses per quarter) + Start/Stop on a MIDI OUT port,
    so external hardware set to 'external sync' follows beatsync's tempo — the deck becomes
    the master clock. The emitted tempo tracks whatever source drives beatsync (a tap, the
    Link session, …) via a bpm getter, so a tap tempo can drive your synths/drum machines
    too. Runs in its own thread alongside the lamp loop. If the port name isn't found (or is
    empty) a VIRTUAL 'beatsync clock' port is created for a DAW/gear to subscribe to.
    """
    def __init__(self, port_hint, get_bpm):
        self.port_hint = port_hint
        self.get_bpm = get_bpm
        self._stop = threading.Event()
        self._out = None

    def _open(self):
        import rtmidi
        m = rtmidi.MidiOut(); ports = m.get_ports()
        idx = None
        if self.port_hint:
            idx = next((i for i, p in enumerate(ports) if self.port_hint.lower() in p.lower()), None)
        if idx is not None:
            m.open_port(idx); name = ports[idx]
        else:
            m.open_virtual_port("beatsync clock"); name = "beatsync clock (virtual)"
        self._out = m
        print(f"→ MIDI clock OUT on '{name}' (24 ppqn + Start/Stop) — set your gear to external sync.")

    def run(self):
        try:
            import rtmidi  # noqa: F401
        except ImportError:
            print("! MIDI clock out needs python-rtmidi:\n    pip install python-rtmidi", file=sys.stderr)
            return
        self._open()
        try: self._out.send_message([0xFA])                 # Start
        except Exception: pass
        nxt = time.monotonic()
        while not self._stop.is_set():
            bpm = max(20.0, min(400.0, self.get_bpm() or 120.0))
            nxt += 60.0 / (bpm * 24.0)                       # 24 clocks / quarter
            dt = nxt - time.monotonic()
            if dt > 0:
                time.sleep(min(dt, 0.05))
                if time.monotonic() < nxt - 0.001:
                    continue                                 # not due yet (short nap for bpm changes)
            elif dt < -0.1:
                nxt = time.monotonic()                       # fell behind → resync
            try: self._out.send_message([0xF8])              # clock pulse
            except Exception: break

    def stop(self):
        self._stop.set()
        if self._out:
            try: self._out.send_message([0xFC])              # Stop
            except Exception: pass


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        prog="beatsync",
        description="Drive OpenLamp/WLED lamps in time with MIDI clock or Ableton Link.")
    p.add_argument("--tap-note", type=int, default=None,
                   help="for --source tap: only this MIDI note number (0-127) counts as "
                        "a tap; default = any note-on (any pad/key).")
    p.add_argument("--tap-udp-port", type=int, default=8378,
                   help="for --source tap: also accept taps as UDP datagrams on this local "
                        "port (a Stream Deck key can tap here); 0 disables. Default 8378.")
    p.add_argument("--ctrl-udp-port", type=int, default=0,
                   help="accept tempo-control datagrams on this local port: 'tempo <bpm>' "
                        "or 'nudge <±delta>' sets the tempo live (the Stream Deck intensity "
                        "dial drives it). link/tap/taplink only; 0 disables. Default 0.")
    p.add_argument("--midi-clock-out", nargs="?", const="", default=None, metavar="PORT",
                   help="ALSO emit MIDI clock (24 ppqn + Start/Stop) at beatsync's current "
                        "tempo, so external gear in 'external sync' follows the deck. Give a "
                        "MIDI out port name substring, or pass it bare to create a virtual "
                        "'beatsync clock' port. Works with any source (great with tap/taplink).")
    p.add_argument("--source", choices=["midi", "link", "tap", "taplink"], default="midi",
                   help="clock source (default: midi)")
    p.add_argument("--port", default="",
                   help="MIDI input port name substring (midi source). "
                        "Omit to use the first port; --list-ports to see them.")
    p.add_argument("--list-ports", action="store_true",
                   help="list MIDI input ports and exit")
    p.add_argument("--bpm", type=float, default=120.0,
                   help="initial/fallback tempo for Link (default: 120)")
    p.add_argument("--sub", type=int, default=1, choices=[1, 2, 4],
                   help="ticks per quarter note: 1=1/4, 2=1/8, 4=1/16 (default: 1)")
    p.add_argument("--bar", type=int, default=4, dest="beats_per_bar",
                   help="beats per bar, for the accent (default: 4)")
    p.add_argument("--action", choices=["flash", "cycle", "pulse", "off"],
                   default="flash", help="what a tick does (default: flash)")
    p.add_argument("--colors", default="rouge",
                   help="comma list; flash uses first (2nd = accent), "
                        "cycle rotates through all (default: rouge)")
    p.add_argument("--accent", action="store_true",
                   help="emphasise beat 1 of each bar")
    p.add_argument("--latency-bias", type=float, default=30.0,
                   help="fixed lamp-reaction ms added to the learned network RTT for "
                        "beat anticipation (default 30 — measured on Athom WLED bulbs, "
                        "~25-37 ms avg via lamp-bench; raise if flashes feel late, lower if early)")
    p.add_argument("--flash-ms", type=int, default=120,
                   help="flash/pulse duration in ms (default: 120)")
    p.add_argument("--lamps", default="",
                   help="lamp OR group names, comma-separated; empty = all")
    p.add_argument("--api", default=DEFAULT_API,
                   help=f"local API base URL (default: {DEFAULT_API})")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print every tick (metronome view)")
    return p


def _ctrl_listener(port, src):
    """Live tempo control over UDP: a datagram 'tempo <bpm>' sets the tempo, 'nudge <±d>'
    adjusts it relative to the current one. Lets the Stream Deck intensity dial drive the
    sync tempo while beat sync is running (link/tap/taplink sources)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
    except OSError as e:
        print("  ! tempo control: cannot bind udp", port, ":", e, file=sys.stderr)
        return
    s.settimeout(0.4)
    print(f"→ Tempo control: listening on udp 127.0.0.1:{port} ('tempo <bpm>' / 'nudge <±d>').")
    while True:
        try:
            data, _ = s.recvfrom(64)
        except socket.timeout:
            continue
        except OSError:
            break
        parts = data.decode("ascii", "ignore").strip().split()
        if len(parts) < 2:
            continue
        try:
            val = float(parts[1])
        except ValueError:
            continue
        if parts[0] == "tempo":
            src.set_tempo(val)
        elif parts[0] == "nudge":
            src.set_tempo(src.current_bpm() + val)


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.list_ports:
        try:
            ports = MidiClockSource.list_ports()
        except Exception as e:
            print("! could not enumerate MIDI ports:", e, file=sys.stderr)
            return 2
        if not ports:
            print("No MIDI input ports. Enable the IAC Driver in Audio MIDI Setup, "
                  "or route your DAW's clock through Bome / a virtual port.")
        else:
            print("MIDI input ports:")
            for i, p in enumerate(ports):
                print(f"  [{i}] {p}")
        return 0

    # Guard the firmware cap: ticks/s = sub * bpm / 60.
    est_bpm = args.bpm
    ticks_per_sec = args.sub * est_bpm / 60.0
    if ticks_per_sec > MAX_TICKS_PER_SEC:
        print(f"⚠ sub={args.sub} at ~{est_bpm:.0f} bpm = {ticks_per_sec:.1f} ticks/s, "
              f"over the ~{MAX_TICKS_PER_SEC:.0f}/s the lamps can ack. "
              f"beatsync will drop excess ticks; consider --sub 1 or a slower part.",
              file=sys.stderr)

    colors = [c.strip() for c in args.colors.split(",") if c.strip()]
    unknown = [c for c in colors if c not in KNOWN_COLORS]
    if unknown:
        print(f"⚠ unknown colour(s) {unknown}; the engine may ignore them. "
              f"Known: {', '.join(sorted(KNOWN_COLORS))}", file=sys.stderr)

    bus = LampBus(args.api, args.lamps, args.action, colors, args.accent,
                  args.flash_ms, args.latency_bias, args.verbose)
    if not bus.arm():
        return 1

    if args.source == "midi":
        src = MidiClockSource(args.port, args.sub, args.beats_per_bar, bus.tick, bus.anticip_ms)
    elif args.source in ("tap", "taplink"):
        cls = TapLinkSource if args.source == "taplink" else TapTempoSource
        src = cls(args.port, args.sub, args.beats_per_bar, bus.tick,
                  tap_note=args.tap_note, anticip=bus.anticip_ms,
                  init_bpm=args.bpm, udp_port=max(0, args.tap_udp_port))
    else:
        src = LinkClockSource(args.bpm, args.sub, args.beats_per_bar, bus.tick, bus.anticip_ms)

    clockout = None
    if args.midi_clock_out is not None:                     # emit MIDI clock at the source's tempo
        clockout = MidiClockOut(args.midi_clock_out, src.current_bpm)
        threading.Thread(target=clockout.run, daemon=True).start()

    if args.ctrl_udp_port > 0 and hasattr(src, "set_tempo"):
        threading.Thread(target=_ctrl_listener, args=(args.ctrl_udp_port, src),
                         daemon=True).start()

    def _sigint(*_):
        print("\n↓ stopping…")
        if clockout: clockout.stop()
        src.stop()
    signal.signal(signal.SIGINT, _sigint)

    try:
        src.run()
    finally:
        if clockout: clockout.stop()
        bus.restore()
    return 0


if __name__ == "__main__":
    sys.exit(main())
