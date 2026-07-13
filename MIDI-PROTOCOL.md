# LumiDeck MIDI protocol

How MIDI messages map to lamp actions. The bridge opens a virtual MIDI **input**
port named `LumiDeck`; anything you route there (from Ableton, Bome, Logic, a
hardware controller…) is translated into OpenLamp State commands and sent to the
running LumiDeck engine. All mappings live in `mapping.json`.

## One MIDI channel per group

**Each MIDI channel targets one lamp group** — this is the core routing idea.
Define groups once in the engine config `tuya-lamps.json`:

```json
"groups": { "front": ["L1"], "back": ["L2"], "wash": ["L1", "L2"] }
```

Then map channels to them in `mapping.json`:

```json
"channels": {
  "1": "all",      // channel 1 drives every lamp
  "2": "front",    // channel 2 → group "front"
  "3": "back",     // channel 3 → group "back"
  "4": "L1",       // channel 4 → a single lamp
  "5": "L2"
}
```

- A channel value can be a **group name**, a **single lamp name**, or `"all"`.
- A channel **not listed** is ignored (so unrelated MIDI on other channels won't
  touch your lights).
- In Ableton: put each group's clips on a MIDI track set to the matching channel.

## Message map (per channel)

| MIDI message | Bytes | Action | OLS command |
|---|---|---|---|
| **Note On** 60-67 (C3-G3) | `9n 3C-43 vv` | Set color | `jaune`…`bleu` |
| Note On 48 / 50 / 52 | `9n 30/32/34` | OFF / ON / toggle | `off` / `on` / `toggle` |
| Note On 53 / 55 | `9n 35/37` | Blackout / Restore | `blackout` / `restore` |
| **Control Change 1** (mod wheel) | `Bn 01 vv` | Brightness 0-100 % | `{"bri": 0-255}` |
| **Control Change 2** | `Bn 02 vv` | White temperature warm→cold | `{"cct": 0-255}` |
| **Control Change 3** | `Bn 03 vv` | Color — continuous **hue** sweep (whole spectrum) | `{"col":[r,g,b]}` |
| **Control Change 4** | `Bn 04 vv` | Color **saturation** (white→pure) | `{"col":[r,g,b]}` |
| Note On 56 | `9n 38` | Tuya music mode | `mode:music` |
| Note On 57 | `9n 39` | Stop animations | `animstop` |
| Note On 58 | `9n 3A` | Flash (white, 300 ms) | `flash:blanc@300` |
| Note On 59 | `9n 3B` | Color cycle | `cycle:…@800` |
| **Control Change 5** | `Bn 05 vv` | WLED effect number | `{"fx":…}` |
| **Control Change 6** | `Bn 06 vv` | WLED effect speed | `{"sx":…}` |
| **Control Change 7** | `Bn 07 vv` | WLED effect intensity | `{"ix":…}` |
| **Program Change** 0-N | `Cn pp` | Recall scene / WLED preset / snapshot | `scene:` / `preset:` / `snap:` |
| **MIDI Clock** | `F8` | Tempo pulse on the beat | `tempo:<bpm>` |

`n` = channel nibble (0-15 → channels 1-16). Note On with velocity 0 counts as
Note Off (ignored — actions fire on note-on only).

### Notes → colors (default)

| Note | Name | Color |
|---|---|---|
| 60 | C3 | yellow |
| 61 | C#3 | purple |
| 62 | D3 | orange |
| 63 | D#3 | light blue |
| 64 | E3 | red |
| 65 | F3 | green |
| 66 | F#3 | pink |
| 67 | G3 | blue |

All note/CC/program numbers are remappable in `mapping.json` (`"notes"`, `"cc"`,
`"programs"`).

### Advanced color from faders

Notes give you the 8 fixed stage colors; **CC gives continuous color**. Assign a
fader to **`hue`** (CC 3) to sweep the whole spectrum, and optionally another to
**`sat`** (CC 4) for white↔saturated. With three faders — **hue + sat + bri**
(CC 1) — one hand paints any color at any brightness live, per group (per channel).
Hue/sat are tracked per channel, so each group keeps its own color.

`"cc"` values you can map: `bri`, `cct`, `hue`, `sat`.

### Program Change → scenes, WLED presets, snapshots

Each entry in `"programs"` decides what PC #i recalls:

| Entry | Recalls | For |
|---|---|---|
| `"night"` | `scene:night` | a Tuya scene captured in the panel |
| `"5"` or `"ps:5"` | `preset:5` | a WLED preset |
| `"snap:song3"` | `snap:song3` | a whole-rig snapshot |
| `"preset:12"` | `preset:12` | (explicit) |

Example: `"programs": ["night", "read", "5", "snap:song3"]`.

## Coverage — every engine action reachable over MIDI

| Action | Tuya | WLED | MIDI |
|---|:-:|:-:|---|
| Color (8 palette) | ✅ | ✅ | notes 60-67 |
| Color (continuous) | ✅ | ✅ | CC 3 hue + CC 4 sat |
| Brightness | ✅ | ✅ | CC 1 |
| White temperature | ✅ | (RGB-CCT) | CC 2 |
| ON / OFF / toggle | ✅ | ✅ | notes 48/50/52 |
| Blackout / restore | ✅ | ✅ | notes 53/55 |
| Scene (named, captured) | ✅ | — | Program Change |
| Preset | (stored scene) | ✅ 1-250 | Program Change (`ps:N`/`N`) |
| Snapshot recall | ✅ | ✅ | Program Change (`snap:name`) |
| Music mode | ✅ | — | note 56 |
| WLED effect (fx/sx/ix) | — | ✅ | CC 5/6/7 |
| Cycle / flash | ✅ | ✅ | notes 58/59 |
| Tempo / pulse | ✅ | ✅ | MIDI clock |
| Stop animation | ✅ | ✅ | note 57 |
| Group targeting | ✅ | ✅ | MIDI channel |

Tuya lamps silently skip WLED-only actions (effects) and vice-versa — same
"skip what you can't do" rule as the rest of LumiDeck. Not exposed over MIDI
(rarely live-relevant, use a key/API): `countdown` (auto-off timer),
`snap:save` (record a snapshot), `psave` (save a WLED preset).

### MIDI Clock → tempo

If `"clock_tempo": true`, the bridge measures incoming clock (24 ticks = 1 beat)
and issues `tempo:<bpm>` so the lamps pulse in time. It targets the group of
`"clock_channel"` (default 1). BPM is clamped to the engine's 20-120 pulse range.

## Full default `mapping.json`

```json
{
  "port_name": "LumiDeck",
  "channels": {"1": "all", "2": "front", "3": "back", "4": "L1", "5": "L2"},
  "notes": {
    "60": "jaune", "61": "violet", "62": "orange", "63": "bleuclair",
    "64": "rouge", "65": "vert", "66": "rose", "67": "bleu",
    "48": "off", "50": "on", "52": "toggle", "53": "blackout", "55": "restore",
    "56": "mode:music", "57": "animstop",
    "58": "flash:blanc@300", "59": "cycle:jaune,violet,rouge,vert@800"
  },
  "cc": {"1": "bri", "2": "cct", "3": "hue", "4": "sat", "5": "fx", "6": "sx", "7": "ix"},
  "programs": [],
  "clock_tempo": true,
  "clock_channel": 1
}
```

## Value resolution (7-bit MIDI, full dynamics preserved)

MIDI carries **7-bit** values (0-127); the engine and OpenLamp State use **8-bit**
(0-255), same as WLED's JSON API. The bridge maps with **exact endpoint scaling**
`round(v x 255 / 127)` — so 0 -> 0 and 127 -> **255**: the full dynamic range is
preserved (a naive x2 would top out at 254 and never reach true full).

The coarser 7-bit steps (~0.8 % each) sit below the eye's ~1 % discrimination
threshold for brightness — imperceptible in practice. So 7-bit control loses
nothing that matters on stage; what must never be lost is the dynamic range,
and the endpoint-exact mapping guarantees it.

## Design note

The MIDI bridge is a **frontend**, not part of the plugin — it only speaks the
public local API (`GET /cmd?c=…&lamps=…`). It never talks to a lamp directly; the
engine owns the persistent connections, so MIDI-triggered changes are as instant
as key presses, and everything (Stream Deck, CLI, MIDI) stays in sync.

## Beyond a DAW “recording lamp”

Studio recording / on-air lamps such as the [Punchlight USB RGB recording lamp](https://www.thomann.fr/punchlight_recording_lamp_usb_rgb.htm)
turn a single fixture red while your DAW is recording — one lamp, a handful of
states, driven by a companion app. **openlamp-midi does the same job and a great
deal more**, from the same DAW, over plain MIDI:

- **Any number of WLED lamps**, not one USB fixture — one per channel, in named
  groups, or all at once.
- **Full colour, white/CCT, brightness, WLED effects, palettes, scenes and
  snapshots** — not just red/green/off. A note, CC or Program Change selects any of them.
- **Tempo-reactive**: flash / pulse / colour-cycle **on the beat** from MIDI clock
  or Ableton Link (see [`beatsync.py`](beatsync.py)) — a fixed recording lamp can't.
- **Reproduces the recording-lamp cue trivially**: map a Program Change (or note)
  that your DAW fires on record/play/stop to a scene — red on record, green on
  play, off on stop — while the *same* rig still does full show lighting.
- **100 % local** — no cloud, no vendor app: it's just MIDI → the local API.

In other words, the "on-air lamp" is just one preset of a full, richer
stage-lighting bus.
