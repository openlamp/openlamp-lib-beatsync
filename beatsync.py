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

Two clock sources, same lamp mapping
------------------------------------
  --source midi   follow an external MIDI clock (24 ppqn: Start/Stop/Continue,
                  Song-Position). Any DAW, drum machine, or Bome routing that
                  sends MIDI clock works — including Ableton via a virtual port.
  --source link   join an Ableton Link session (phone-locked tempo + phase,
                  no cable). Requires `pip install aalink`.

The hardware cap that shapes everything
---------------------------------------
Measured on the reference WLED bulbs (lumideck PROTOCOL.md, 2026-07-03): the firmware
drops the session beyond ~4 acknowledged commands/second. beatsync sends **one**
API call per tick and refuses (with a warning) a subdivision×tempo that would
exceed ~4 ticks/s, so the lamps never choke mid-set. That is also why the
default action is `flash:` (one API call that the engine expands + auto-reverts
itself) rather than a two-call bri pulse.

Usage
-----
  python3 sync/beatsync.py --list-ports
  python3 sync/beatsync.py --source midi --port "IAC" --action flash --colors rouge
  python3 sync/beatsync.py --source midi --port Bome --sub 2 --action cycle \
        --colors rouge,bleu,vert --accent --lamps front
  python3 sync/beatsync.py --source link --bpm 120 --action pulse

Ctrl-C to stop (lamps are restored to their pre-sync state on exit).
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
import urllib.request
import urllib.parse

DEFAULT_API = "http://127.0.0.1:8377"
# Firmware ceiling — one lamp session drops acked commands past this rate.
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

    def __init__(self, api, lamps, action, colors, accent, flash_ms, verbose):
        self.api = api.rstrip("/")
        self.lamps = lamps                       # "" == all lamps
        self.action = action
        self.colors = colors or ["blanc"]
        self.accent = accent
        self.flash_ms = flash_ms
        self.verbose = verbose
        self._cycle_i = 0
        self._last_send = 0.0
        self._saved = None                       # pre-sync snapshot for restore
        self._min_gap = 1.0 / MAX_TICKS_PER_SEC

    # -- low-level API helpers ------------------------------------------------
    def _lamps_q(self):
        return ("?lamps=" + urllib.parse.quote(self.lamps)) if self.lamps else ""

    def _cmd(self, c):
        """GET /cmd?c=...  — the alias/engine command channel (flash, colours…)."""
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
        """POST /json/state — a raw WLED-style patch (used by `pulse`)."""
        url = f"{self.api}/json/state{self._lamps_q()}"
        data = json.dumps(st).encode()
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=1.5) as r:
                r.read()
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
            else:
                col = self.colors[0]
                if self.accent and is_downbeat and len(self.colors) > 1:
                    col = self.colors[1]
            self._strobe(col)
        elif self.action == "pulse":
            hi = 255 if (not self.accent or is_downbeat) else 180
            self._patch({"bri": hi, "tt": 0})
            threading.Timer(self.flash_ms / 1000.0,
                            lambda: self._patch({"bri": 40, "tt": 0})).start()

    def _strobe(self, colname):
        # BRUTAL beat hit: instant full-bright colour (tt:0 = no fade, snaps ON the
        # beat), then instant drop to dark so each beat is a sharp punch, not a fade.
        rgb = list(COLORS_RGB.get(colname, (255, 255, 255)))
        self._patch({"on": True, "col": rgb, "bri": 255, "tt": 0})
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

    def __init__(self, port_hint, sub, beats_per_bar, on_tick):
        self.port_hint = port_hint
        self.clocks_per_tick = max(1, round(24 / sub))
        self.beats_per_bar = beats_per_bar
        self.on_tick = on_tick
        self._midiin = None
        # Tick as soon as clock flows: many DAWs (Ableton) only emit clock while
        # PLAYING and send Start once — if beatsync connects mid-playback it never
        # sees that Start, so default to running and let Stop (0xFC) halt it. Start
        # just re-zeroes the beat position (Benoit 2026-07-13, verified w/ Ableton).
        self._running = True
        self._clock_count = 0          # clocks since transport start (mod 24 handled)
        self._tick_index = 0           # subdivisions elapsed since start
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

    def _on_midi(self, event, _data=None):
        msg, _dt = event
        if not msg:
            return
        status = msg[0]
        if status == self.START:
            self._running = True
            self._clock_count = 0
            self._tick_index = 0
            self._times.clear()
        elif status == self.CONTINUE:
            self._running = True
        elif status == self.STOP:
            self._running = False
        elif status == self.CLOCK and self._running:
            t = time.monotonic()
            self._times.append(t)
            if len(self._times) > 24:
                self._times.pop(0)
            if self._clock_count % self.clocks_per_tick == 0:
                sub_per_beat = max(1, round(24 / self.clocks_per_tick))
                beat = (self._tick_index // sub_per_beat) % self.beats_per_bar
                is_down = (self._tick_index % (sub_per_beat * self.beats_per_bar) == 0)
                try:
                    self.on_tick(beat + 1, is_down, self._bpm())
                except Exception as e:
                    print("  ! tick handler error:", e, file=sys.stderr)
                self._tick_index += 1
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
    def __init__(self, bpm, sub, beats_per_bar, on_tick):
        self.bpm = bpm
        self.sub = max(1, sub)
        self.beats_per_bar = beats_per_bar
        self.on_tick = on_tick
        self._stop = False

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
            i = 0
            while not self._stop:
                # wait for the next subdivision boundary on the shared timeline
                await link.sync(step)
                beat = (i // self.sub) % self.beats_per_bar
                is_down = (i % (self.sub * self.beats_per_bar) == 0)
                try:
                    self.on_tick(beat + 1, is_down, float(link.tempo))
                except Exception as e:
                    print("  ! tick handler error:", e, file=sys.stderr)
                i += 1

        try:
            import asyncio
            asyncio.run(loop())
        except KeyboardInterrupt:
            pass

    def stop(self):
        self._stop = True


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        prog="beatsync",
        description="Drive OpenLamp/WLED lamps in time with MIDI clock or Ableton Link.")
    p.add_argument("--source", choices=["midi", "link"], default="midi",
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
    p.add_argument("--flash-ms", type=int, default=120,
                   help="flash/pulse duration in ms (default: 120)")
    p.add_argument("--lamps", default="",
                   help="lamp OR group names, comma-separated; empty = all")
    p.add_argument("--api", default=DEFAULT_API,
                   help=f"local API base URL (default: {DEFAULT_API})")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print every tick (metronome view)")
    return p


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
                  args.flash_ms, args.verbose)
    if not bus.arm():
        return 1

    if args.source == "midi":
        src = MidiClockSource(args.port, args.sub, args.beats_per_bar, bus.tick)
    else:
        src = LinkClockSource(args.bpm, args.sub, args.beats_per_bar, bus.tick)

    def _sigint(*_):
        print("\n↓ stopping…")
        src.stop()
    signal.signal(signal.SIGINT, _sigint)

    try:
        src.run()
    finally:
        bus.restore()
    return 0


if __name__ == "__main__":
    sys.exit(main())
