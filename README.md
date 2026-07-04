# openlamp-midi — MIDI overlay for LumiDeck

A **MIDI frontend** for the [LumiDeck](https://github.com/Beennnn/lumideck) — see the [OpenLamp umbrella](https://github.com/Beennnn/openlamp)) engine:
drive your smart lamps (and, soon, anything else) from **Ableton, Bome, Logic, or
any cheap physical MIDI controller** — and from the Stream Deck via its MIDI plugin.

This repo is the **top layer** of a stack. It references the others:

| Layer | Repo | Role |
|---|---|---|
| **core** | [Beennnn/lumideck](https://github.com/Beennnn/lumideck) — see the [OpenLamp umbrella](https://github.com/Beennnn/openlamp)) → `core/` | standardized LED interface + **OpenLamp State (OLS)** contract + engine |
| **streamdeck** | [Beennnn/lumideck](https://github.com/Beennnn/lumideck) — see the [OpenLamp umbrella](https://github.com/Beennnn/openlamp)) → `streamdeck/` | Elgato Stream Deck plugin |
| **midi** | **this repo** | MIDI → OLS overlay |

The overlay never talks to a device directly. It opens a virtual MIDI port
`LumiDeck`, translates incoming MIDI into OLS commands, and calls the engine's
**local API** (`http://127.0.0.1:8377/cmd`). The engine (from the core) owns the
persistent device connections, so MIDI-triggered changes are as instant as a key
press — and stay in sync with the Stream Deck.

## For musicians, not Stream Deck users

Controlling lamps over MIDI is **not a Stream Deck feature** (the Stream Deck plugin
drives the engine directly). This overlay targets the **MIDI musician community** —
people who already own physical MIDI controllers and want to fire lamp colors from
them, live on stage. See [ENCAPSULATION.md](ENCAPSULATION.md).

## Why MIDI

MIDI is the cheapest, most ubiquitous **physical control layer**: €20-80 pads,
faders and footswitches, real-time, recognized by every OS and DAW, no drivers.
This overlay turns any of them — or a Stream Deck — into a lamp/show controller.

- **One MIDI channel per lamp group** (channels map in `mapping.json`).
- Notes → colors/power/animations, CC → brightness/temperature/**continuous hue**,
  Program Change → scenes/presets/snapshots, MIDI clock → tempo.
- Full mapping and coverage matrix: **[MIDI-PROTOCOL.md](MIDI-PROTOCOL.md)**.

## Beyond lamps — a universal encapsulation bus

The lamp bridge is one instance of a general pattern: **MIDI → any local backend**
(Home Assistant, DMX/Art-Net, OSC, MQTT, Shelly…), so one cheap controller drives
your whole show and home. See **[ENCAPSULATION.md](ENCAPSULATION.md)**.

## Run

```bash
pip install python-rtmidi        # only dependency
python3 lumideck_midi.py         # opens the virtual "LumiDeck" MIDI port
```

Route your DAW/controller output to `LumiDeck`. Autostart via
`com.benlab.openlamp-midi.plist` (launchd). The LumiDeck engine (Stream Deck
plugin from the core repo) must be running.

## Credits

Built by **BenLab** with the help of **Claude (Anthropic)**. Part of the LumiDeck
project. Not affiliated with Tuya, Elgato, the WLED project, or any MIDI vendor.

## Family & the one-host rule

This overlay talks to the engine's local API (`/cmd` on 127.0.0.1:8377) served by
[openlamp-engine](https://github.com/Beennnn/openlamp-engine) — either its headless
daemon (no Stream Deck needed: `run-headless.sh`) or the
[lumideck](https://github.com/Beennnn/lumideck) plugin. Run ONE host at a time.
Family map: [openlamp](https://github.com/Beennnn/openlamp).
