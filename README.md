# openlamp-midi — MIDI overlay for LumiDeck

A **MIDI frontend** for the [LumiDeck](https://github.com/openlamp/lumideck) — see the [OpenLamp umbrella](https://github.com/openlamp/openlamp)) engine:
drive your smart lamps (and, soon, anything else) from **Ableton, Bome, Logic, or
any cheap physical MIDI controller** — and from the Stream Deck via its MIDI plugin.

[![PyPI — openlamp-midi](https://img.shields.io/pypi/v/openlamp-midi?label=openlamp-midi&color=3775A9&logo=pypi&logoColor=white)](https://pypi.org/project/openlamp-midi/)

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
`com.openlamp.lumideck-midi.plist` (launchd). The LumiDeck engine (Stream Deck
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

### Landing the flash *on* the beat — latency learning + anticipation

There is always a delay between "beatsync decides to flash" and "the lamp
physically changes": the HTTP round-trip to the engine, plus the WLED hardware's
reaction time (~45 ms). Left uncompensated the flash lands *after* the beat.
beatsync learns that delay and fires **early** so the light change coincides with
the beat:

1. **Learn the network delay.** Every command POST to the engine
   (`127.0.0.1:8377`) is timed and folded into an exponential moving average
   `rtt = 0.2·sample + 0.8·rtt`. This is the *network + engine* share of the delay
   (small on a local machine, larger over Wi-Fi or a loaded engine — so it's worth
   measuring, not hard-coding).
2. **Add the hardware floor.** A fixed lamp-reaction constant
   (`--latency-bias`, default **45 ms** — the measured WLED floor) is added:
   `L = rtt + bias`. Tune the bias by eye: raise it if flashes still feel late,
   lower it if they feel early.
3. **Fire `L` ms before each beat.**
   - **MIDI clock:** `L` is converted to whole MIDI clocks (24 per quarter, so
     `clocks = round(L / clock_period)`), and the tick fires that many clocks
     *before* the beat boundary.
   - **Ableton Link:** the shared timeline is polled; the tick fires the instant
     the next subdivision is `L` ms away (`(next_beat − now)·beat_duration ≤ L`).
4. **Guard rails.** `L` is capped at 200 ms (a bad measurement can never throw the
   flash wildly early), and a hard **≤ 4 commands/second** limiter protects the
   WLED firmware — a blink is *two* commands (bright + dark), so this is enforced
   per command, not per beat. Excess is dropped, never queued.

### Link vs MIDI clock — why Ableton Link is the better protocol

Both sources drive the same flash, but they carry **different information**, and
that difference decides whether the *downbeat* (the accent on beat 1) can land where
it should. Prefer **Link** whenever the DAW offers it.

| | **MIDI clock** (0xF8) | **Ableton Link** |
|---|---|---|
| What's on the wire | 24 anonymous ticks per quarter — *tempo only* | a continuously **shared timeline**: tempo **+ phase within the bar** |
| Where's the bar / beat 1 | **nowhere** — a tick is just a tick | `link.phase` is exact; `0` **is** the bar's downbeat |
| Tempo changes | followed tick-by-tick, but jittery (each tick is a separate event, subject to USB/driver scheduling) | smooth — you read a precise fractional beat position, not a tick count |
| Joining mid-song | you see only ticks; you've missed the one Start that marked bar 1 | you're **instantly in phase** — the timeline is absolute and shared |
| Multiple devices | each derives its own guess of "where beat 1 is" → they drift apart | all peers read the **same** phase → sample-accurate agreement |
| Cabling | a MIDI/USB route (here: a virtual port) | none — peers find each other on the LAN |

**The crux is the downbeat.** MIDI clock is a metronome with no score: it tells you
*how fast* but never *where in the bar you are*. We confirmed this by sniffing
Ableton during steady playback — it sends **only** clock (288 × 0xF8 over 6 s at
120 BPM), **no** Start, Continue, Stop, or Song Position. So under plain clock the
accent can only be *guessed*, and it drifts.

MIDI does carry the bar in two *occasional* messages, and beatsync uses both when
they appear:

- **Start (0xFA)** — sent once when you press Play. beatsync treats it as *bar 1*, so
  the accent locks — **but only if you press Play after arming beatsync**, from the
  top of a bar. Join mid-song and there's no Start to catch.
- **Song Position (0xF2)** — a 16th-note offset from song start; beatsync re-phases
  the bar from it. Reliable in theory, but many DAWs (Ableton under plain clock
  included) never send it.

**Ableton Link needs none of that.** Because the phase is shared continuously, the
downbeat is *always* known — `link.quantum` is set to the bar length and the accent
counter is seeded from `link.phase`, so beat 1 falls on Ableton's bar 1 the instant
you connect, with no Play/Stop dance and no drift between two lamps. That's why the
demos use Link for anything where the *accent* matters, and keep MIDI clock for the
simpler "just flash on every beat" case where the bar position is irrelevant.

**Rule of thumb:** flash-on-every-beat → either source. Accent on the downbeat →
**Link**, or a fresh **Play from bar 1** if you're stuck on MIDI clock.

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

Built by **[@Beennnn](https://github.com/Beennnn)** with the help of **Claude (Anthropic)**. Part of the **[OpenLamp](https://github.com/openlamp)**
family. Not affiliated with Tuya, Elgato, the WLED project, or any MIDI vendor.

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
