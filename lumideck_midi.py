#!/usr/bin/env python3
"""LumiDeck MIDI bridge — pilote les lampes depuis Ableton / Bome / tout DAW.

C'est un FRONTAL de plus (comme le CLI et Stream Deck) : il ne parle pas Tuya,
il ouvre un port MIDI virtuel "LumiDeck", traduit le MIDI entrant en commandes
OpenLamp State et les POSTe a l'API locale du plugin (127.0.0.1:8377). Le moteur
(qui detient les connexions persistantes) fait le reste — reponse immediate.

Dans Ableton / Bome / Logic : le port "LumiDeck" apparait comme sortie MIDI.
Route-y des notes / CC / program change (voir mapping.json).

Mapping par defaut (editable dans mapping.json, canal 1 par defaut) :
  - Notes 60-67 (C3..G3)  -> 8 couleurs (palette Kelly)
  - Note 48 (C2)          -> OFF, note 50 -> ON, note 52 -> toggle
  - Note 53 -> blackout, note 55 -> restore
  - CC 1 (mod wheel)      -> intensite 0-100 %
  - CC 2                  -> temperature blanc (chaud->froid)
  - Program Change 0-N    -> scenes/snapshots nommes (liste "programs")
  - MIDI clock (0xF8)     -> tempo: BPM auto (allume/eteint via "clock_tempo")

Lancement : python3 lumideck_midi.py   (Ctrl-C pour quitter)
Autostart : voir com.benlab.lumideck-midi.plist (launchd).
"""
import json, os, sys, time, threading, colorsys, urllib.request, urllib.parse
import rtmidi

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "mapping.json")
API = "http://127.0.0.1:8377"

DEFAULT = {
    "port_name": "LumiDeck",
    # UN CANAL MIDI PAR GROUPE (Benoit 2026-07-04) : chaque canal route vers un
    # groupe/lampe. "all" = toutes. Les groupes sont definis dans tuya-lamps.json
    # ("groups": {"front": ["L1"], ...}) et developpes cote moteur.
    "channels": {
        "1": "all", "2": "front", "3": "back", "4": "L1", "5": "L2"
    },
    # une note = une commande OLS (n'importe quelle string du protocole marche ici)
    "notes": {
        # 8 couleurs
        "60": "jaune", "61": "violet", "62": "orange", "63": "bleuclair",
        "64": "rouge", "65": "vert", "66": "rose", "67": "bleu",
        # alimentation / etats
        "48": "off", "50": "on", "52": "toggle", "53": "blackout", "55": "restore",
        # modes & animations (Tuya + moteur)
        "56": "mode:music",                        # Tuya : mode musique
        "57": "animstop",                          # stoppe cycle/flash/tempo
        "58": "flash:blanc@300",                   # flash blanc court
        "59": "cycle:jaune,violet,rouge,vert@800", # cycle de couleurs
    },
    # CC -> reglage continu (faders/knobs).
    #   hue/sat = couleur avancee (spectre + saturation) · bri · cct (blanc)
    #   fx/sx/ix = effet WLED : numero d'effet, vitesse, intensite
    "cc": {"1": "bri", "2": "cct", "3": "hue", "4": "sat",
           "5": "fx", "6": "sx", "7": "ix"},
    "programs": [],                   # ["night","read",...] -> scene:<nom>
    "clock_tempo": True               # MIDI clock -> commande tempo:<bpm>
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
        print("  ! API injoignable (plugin lance ?) :", e)

class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.clock_ticks = 0
        self.clock_t0 = None
        self.last_bpm = None
        self.hue = {}                 # teinte courante par canal (mode couleur avance)
        self.sat = {}                 # saturation courante par canal
        self.fx = {}                  # effet WLED courant par canal : {fx,sx,ix}

    def _target(self, chan):
        # canal MIDI -> cible (nom de groupe/lampe, ou "all"). Retourne la liste
        # a passer a l'API ([] = toutes). Compat : ancien "channel"/"lamps".
        chans = self.cfg.get("channels")
        if chans is not None:
            tgt = chans.get(str(chan))
            if tgt is None:
                return None            # canal non mappe -> ignore
            return [] if tgt in ("all", "*", "") else [tgt]
        want = self.cfg.get("channel", 1)
        if want and chan != want:
            return None
        return self.cfg.get("lamps") or []

    def on_note(self, note, on, target):
        m = self.cfg["notes"].get(str(note))
        if not m or not on:           # on n'agit que sur note-on (velocite > 0)
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
            # COULEUR AVANCEE AU FADER : teinte et/ou saturation continues -> RGB.
            # Le fader "hue" balaie tout le spectre (0=rouge...127=rouge), "sat"
            # va du blanc a la couleur pure. Etat par canal pour combiner les deux.
            if kind == "hue":
                self.hue[chan] = val / 127.0
            else:
                self.sat[chan] = val / 127.0
            h = self.hue.get(chan, 0.0)
            s = self.sat.get(chan, 1.0)
            r, g, b = colorsys.hsv_to_rgb(h, s, 1.0)
            send('{"col":[%d,%d,%d]}' % (round(r*255), round(g*255), round(b*255)), target)
        elif kind in ("fx", "sx", "ix"):
            # EFFET WLED au fader (ignore par les lampes Tuya) : numero d'effet,
            # vitesse, intensite — combines par canal.
            f = self.fx.setdefault(chan, {"fx": 0, "sx": 128, "ix": 128})
            f[kind] = round(val / 127 * (200 if kind == "fx" else 255))
            send('{"fx":%d,"sx":%d,"ix":%d}' % (f["fx"], f["sx"], f["ix"]), target)

    def on_program(self, prog, target):
        # Program Change -> rappel d'une ambiance memorisee. Selon l'entree :
        #   "night"       -> scene:night   (scene nommee Tuya, capturee)
        #   "ps:5" ou "5" -> preset:5      (preset WLED)
        #   "snap:song3"  -> snap:song3    (snapshot moteur, toutes lampes)
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
        # 24 ticks MIDI = 1 noire -> BPM. On envoie une commande tempo quand le
        # BPM change de facon notable (evite de spammer le moteur). Cible = groupe
        # du canal "clock_channel" de la config (defaut : toutes).
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
        if status == 0xF8:            # clock : pas de canal -> tempo global (toutes)
            self.on_clock(); return
        typ, chan = status & 0xF0, (status & 0x0F) + 1
        target = self._target(chan)
        if target is None:            # canal non mappe
            return
        if typ == 0x90:               # note on (velocite 0 = note off)
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
