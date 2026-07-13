# openlamp-midi — MIDI overlay for LumiDeck

A **MIDI frontend** for the [LumiDeck](https://github.com/openlamp/lumideck) — see the [OpenLamp umbrella](https://github.com/openlamp/openlamp)) engine:
drive your smart lamps (and, soon, anything else) from **Ableton, Bome, Logic, or
any cheap physical MIDI controller** — and from the Stream Deck via its MIDI plugin.

This repo is the **top layer** of a stack. It references the others:

| Layer | Repo | Role |
|---|---|---|
| **core** | [Beennnn/lumideck](https://github.com/openlamp/lumideck) — see the [OpenLamp umbrella](https://github.com/openlamp/openlamp)) → `core/` | standardized LED interface + **OpenLamp State (OLS)** contract + engine |
| **streamdeck** | [Beennnn/lumideck](https://github.com/openlamp/lumideck) — see the [OpenLamp umbrella](https://github.com/openlamp/openlamp)) → `streamdeck/` | Elgato Stream Deck plugin |
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

## Install & run

```bash
pip install openlamp-midi          # pulls python-rtmidi; gives two commands:
pip install "openlamp-midi[link]"  # + Ableton Link support for beatsync (native build)

lumideck-midi                      # opens the virtual "LumiDeck" MIDI port
beatsync --source midi --port Ableton --action flash --colors rouge
```

Or run the scripts straight from a checkout without installing:

```bash
pip install python-rtmidi
python3 lumideck_midi.py
```

Route your DAW/controller output to `LumiDeck`. Autostart via
`com.benlab.openlamp-midi.plist` (launchd). The LumiDeck engine (Stream Deck
plugin from the core repo) must be running.

## Tempo & beat sync — `beatsync.py`

A second frontend in this repo, focused on **rhythm**: it follows an external
**MIDI clock** (24 ppqn, Start/Stop/Continue) or an **Ableton Link** session and
flashes / pulses / colour-cycles the lamps **on the beat** — locked to the music,
no cable needed for Link. Same target as `lumideck_midi.py`: it POSTs to the
engine's local API on `127.0.0.1:8377`, respecting the ~4 commands/second the WLED
firmware can ack (it drops excess ticks rather than choke the lamps).

```bash
pip install python-rtmidi          # MIDI clock source
pip install aalink                 # optional — Ableton Link source (native build)

python3 beatsync.py --list-ports
python3 beatsync.py --source midi --port Ableton --action flash --colors rouge
python3 beatsync.py --source link --bpm 120 --action pulse --accent
```

Subdivisions (`--sub 1|2|4`), per-beat action (`flash` / `cycle` / `pulse`),
accent on beat 1, lamp/group targeting. Ctrl-C restores the lamps. Full options
in the file's header docstring.

## Publishing to PyPI (maintainer)

Releases are published to PyPI by **Trusted Publishing (OIDC)** — no API token is
stored in the repo. `.github/workflows/publish.yml` builds the sdist + wheel and
publishes on each GitHub **Release**.

One-time setup (yours — I don't touch PyPI credentials):

1. **Create the PyPI project via a Pending Publisher** (works before the first
   upload). On <https://pypi.org> → *Your account* → *Publishing* → *Add a pending
   publisher*, fill in:
   - PyPI Project Name: `openlamp-midi`
   - Owner: `openlamp` · Repository: `midi`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
2. **Add the `pypi` environment** on GitHub: repo → *Settings* → *Environments* →
   *New environment* → `pypi` (optionally require a reviewer for extra safety).
3. **Cut a release**: bump `version` in `pyproject.toml`, commit, then create a
   GitHub Release (tag e.g. `v0.1.0`). The workflow builds and publishes — check
   the *Actions* tab. Done: `pip install openlamp-midi` works worldwide.

For TestPyPI first, duplicate the publisher on <https://test.pypi.org> and add a
`repository-url: https://test.pypi.org/legacy/` to the publish step.

## Credits

Built by **BenLab** with the help of **Claude (Anthropic)**. Part of the LumiDeck
project. Not affiliated with Tuya, Elgato, the WLED project, or any MIDI vendor.

## Works with any class-compliant MIDI controller

Any controller your Mac sees as a MIDI device works out of the box — route it to
the `LumiDeck` virtual port (directly, or through your DAW). Typical stage picks:

- **Foot controllers** (hands stay on your instrument): Hotone **Ampero Control**
  (4 footswitches, ~80 EUR), Morningstar MC6/MC8, Behringer FCB1010 (10 switches
  + 2 expression pedals, the classic). Map switches to blackout / restore /
  scene recalls per song section.
- **Pads**: Novation Launchpad Mini, Akai APC mini (~80-100 EUR) — one pad per
  color per group; the 8x8 grid maps naturally to 8 colors x channels.
- **Faders/knobs**: Korg nanoKONTROL2 (~60 EUR) — hue / saturation / brightness
  on three faders (CC 3/4/1) = paint any color live with one hand.
- **Keys**: any MIDI keyboard — notes 60-67 are the color palette; velocity is
  ignored, so nothing fires by accident while playing softly... on another channel.
- **Multi-FX pedals that send MIDI**: Hotone Ampero II Stomp, Line 6 HX Stomp —
  a patch change on your guitar board can also switch the stage color.
- **From the DAW**: Ableton Live clips (one MIDI track per lamp-group channel),
  or MIDI clock for tempo-synced pulses.

No drivers, no config on the controller side: it just sends notes/CC — the
mapping lives in `mapping.json` on the computer.

## Family & the one-host rule

This overlay talks to the engine's local API (`/cmd` on 127.0.0.1:8377) served by
[openlamp-engine](https://github.com/openlamp/engine) — either its headless
daemon (no Stream Deck needed: `run-headless.sh`) or the
[lumideck](https://github.com/openlamp/lumideck) plugin. Run ONE host at a time.
Family map: [openlamp](https://github.com/openlamp/openlamp).
