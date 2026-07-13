#!/usr/bin/env python3
"""LumiDeck MIDI bridge — drive the lamps from Ableton / Bome / any DAW.

It is one more FRONTEND (like the CLI and Stream Deck): it never speaks Tuya. It
opens a virtual MIDI port "LumiDeck", translates incoming MIDI into OpenLamp State
commands and POSTs them to the plugin's local API (127.0.0.1:8377). The engine
(which holds the persistent connections) does the rest — instant response.

In Ableton / Bome / Logic the "LumiDeck" port shows up as a MIDI output. Route
notes / CC / program change to it (see mapping.json).

Default mapping (editable in mapping.json; one channel per group):
  - Notes 60-67 (C3..G3)  -> 8 colors (Kelly palette)
  - Note 48 -> OFF, note 50 -> ON, note 52 -> toggle
  - Note 53 -> blackout, note 55 -> restore
  - Notes 56-59 -> music mode / animstop / flash / cycle
  - CC 1 (mod wheel)      -> brightness 0-100 %
  - CC 2                  -> white temperature (warm->cold)
  - CC 3 / 4              -> continuous hue / saturation (advanced color)
  - CC 5 / 6 / 7          -> WLED effect number / speed / intensity
  - Program Change 0-N    -> named scenes / WLED presets / snapshots ("programs")
  - MIDI clock (0xF8)     -> tempo: auto BPM (toggled by "clock_tempo")

Run: python3 lumideck_midi.py   (Ctrl-C to quit)
Autostart: see com.openlamp.lumideck-midi.plist (launchd).
"""
import json, os, sys, time, threading, colorsys, urllib.request, urllib.parse
import rtmidi

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "mapping.json")
API = "http://127.0.0.1:8377"

DEFAULT = {
    "port_name": "LumiDeck",
    # ONE MIDI CHANNEL PER GROUP: each channel routes to a group/lamp. "all" =
    # every lamp. Groups are defined in the engine config tuya-lamps.json
    # ("groups": {"front": ["L1"], ...}) and expanded engine-side.
    "channels": {
        "1": "all", "2": "front", "3": "back", "4": "L1", "5": "L2"
    },
    # a note = one OLS command (any protocol string works here)
    "notes": {
        # 8 colors
        "60": "jaune", "61": "violet", "62": "orange", "63": "bleuclair",
        "64": "rouge", "65": "vert", "66": "rose", "67": "bleu",
        # power / states
        "48": "off", "50": "on", "52": "toggle", "53": "blackout", "55": "restore",
        # modes & animations (Tuya + engine)
        "56": "mode:music",                        # Tuya music mode
        "57": "animstop",                          # stop cycle/flash/tempo
        "58": "flash:blanc@300",                   # short white flash
        "59": "cycle:jaune,violet,rouge,vert@800", # color cycle
    },
    # CC -> continuous control (faders/knobs).
    #   hue/sat = advanced color (full spectrum + saturation) · bri · cct (white)
    #   fx/sx/ix = WLED effect: effect number, speed, intensity
    "cc": {"1": "bri", "2": "cct", "3": "hue", "4": "sat",
           "5": "fx", "6": "sx", "7": "ix"},
    "programs": [],                   # ["night","read",...] -> scene:<name>
    "clock_tempo": True               # MIDI clock -> tempo:<bpm> command
}

def load_cfg():
    if os.path.exists(CONFIG):
        c = dict(DEFAULT); c.update(json.load(open(CONFIG))); return c
    json.dump(DEFAULT, open(CONFIG, "w"), indent=2)
    return dict(DEFAULT)

def send(cmd, lamps):
    q = urllib.parse.quote(cmd)
    if lamps:
        q += "&lamps=" + ",".join(lamps)
    try:
        urllib.request.urlopen(API + "/cmd?c=" + q, timeout=3).read()
    except Exception as e:
        print("  ! API unreachable (is the plugin running?):", e)

class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.clock_ticks = 0
        self.clock_t0 = None
        self.last_bpm = None
        self.hue = {}                 # current hue per channel (advanced color)
        self.sat = {}                 # current saturation per channel
        self.fx = {}                  # current WLED effect per channel: {fx,sx,ix}

    def _target(self, chan):
        # MIDI channel -> target (group/lamp name, or "all"). Returns the list to
        # pass to the API ([] = all). Back-compat with the old "channel"/"lamps".
        chans = self.cfg.get("channels")
        if chans is not None:
            tgt = chans.get(str(chan))
            if tgt is None:
                return None            # unmapped channel -> ignore
            return [] if tgt in ("all", "*", "") else [tgt]
        want = self.cfg.get("channel", 1)
        if want and chan != want:
            return None
        return self.cfg.get("lamps") or []

    def on_note(self, note, on, target):
        m = self.cfg["notes"].get(str(note))
        if not m or not on:           # act on note-on only (velocity > 0)
            return
        print("  ch->%s  note %d -> %s" % (target or "all", note, m))
        send(m, target)

    def on_cc(self, num, val, target, chan):
        kind = self.cfg["cc"].get(str(num))
        if kind == "bri":
            send('{"bri":%d}' % round(val / 127 * 255), target)
        elif kind == "cct":
            send('{"cct":%d}' % round(val / 127 * 255), target)
        elif kind in ("hue", "sat"):
            # ADVANCED COLOR ON A FADER: continuous hue and/or saturation -> RGB.
            # The "hue" fader sweeps the whole spectrum (0=red...127=red), "sat"
            # goes from white to pure color. State per channel to combine both.
            if kind == "hue":
                self.hue[chan] = val / 127.0
            else:
                self.sat[chan] = val / 127.0
            h = self.hue.get(chan, 0.0)
            s = self.sat.get(chan, 1.0)
            r, g, b = colorsys.hsv_to_rgb(h, s, 1.0)
            send('{"col":[%d,%d,%d]}' % (round(r*255), round(g*255), round(b*255)), target)
        elif kind in ("fx", "sx", "ix"):
            # WLED effect on a fader (Tuya lamps skip it): effect number, speed,
            # intensity — combined per channel.
            f = self.fx.setdefault(chan, {"fx": 0, "sx": 128, "ix": 128})
            f[kind] = round(val / 127 * (200 if kind == "fx" else 255))
            send('{"fx":%d,"sx":%d,"ix":%d}' % (f["fx"], f["sx"], f["ix"]), target)

    def on_program(self, prog, target):
        # Program Change -> recall a stored look. Depending on the entry:
        #   "night"       -> scene:night   (a captured Tuya scene)
        #   "ps:5" or "5" -> preset:5      (a WLED preset)
        #   "snap:song3"  -> snap:song3    (an engine snapshot, all lamps)
        progs = self.cfg.get("programs") or []
        if not (0 <= prog < len(progs)):
            return
        name = str(progs[prog])
        if name.startswith("snap:") or name.startswith("preset:"):
            cmd = name
        elif name.startswith("ps:"):
            cmd = "preset:" + name[3:]
        elif name.isdigit():
            cmd = "preset:" + name
        else:
            cmd = "scene:" + name
        print("  program", prog, "->", cmd)
        send(cmd, target)

    def on_clock(self):
        # 24 MIDI ticks = 1 beat -> BPM. Emit a tempo command only when the BPM
        # changes noticeably (avoids spamming the engine). Target = the group of
        # the config "clock_channel" (default: all).
        if not self.cfg.get("clock_tempo"):
            return
        now = time.monotonic()
        self.clock_ticks += 1
        if self.clock_ticks % 24 != 0:
            return
        if self.clock_t0 is not None:
            bpm = round(60.0 / (now - self.clock_t0))
            if 20 <= bpm <= 300 and bpm != self.last_bpm:
                self.last_bpm = bpm
                tgt = self._target(self.cfg.get("clock_channel", 1)) or []
                send("tempo:%d" % max(20, min(120, bpm)), tgt)
        self.clock_t0 = now

    def dispatch(self, msg):
        status = msg[0]
        if status == 0xF8:            # clock: no channel -> global tempo (all)
            self.on_clock(); return
        typ, chan = status & 0xF0, (status & 0x0F) + 1
        target = self._target(chan)
        if target is None:            # unmapped channel
            return
        if typ == 0x90:               # note on (velocity 0 = note off)
            self.on_note(msg[1], msg[2] > 0, target)
        elif typ == 0x80:             # note off
            self.on_note(msg[1], False, target)
        elif typ == 0xB0:             # control change
            self.on_cc(msg[1], msg[2], target, chan)
        elif typ == 0xC0:             # program change
            self.on_program(msg[1], target)

def main():
    cfg = load_cfg()
    br = Bridge(cfg)
    midi_in = rtmidi.MidiIn()
    port = midi_in.open_virtual_port(cfg["port_name"])
    midi_in.ignore_types(timing=False)   # on VEUT l'horloge MIDI (0xF8)
    def cb(event, data=None):
        msg, _ = event
        try:
            br.dispatch(msg)
        except Exception as e:
            print("  ! erreur dispatch:", e)
    midi_in.set_callback(cb)
    print("LumiDeck MIDI bridge — port virtuel '%s' ouvert." % cfg["port_name"])
    print("Route ta sortie MIDI dessus dans Ableton / Bome. Ctrl-C pour quitter.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\narret.")
    finally:
        midi_in.close_port()

if __name__ == "__main__":
    main()
