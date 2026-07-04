# Why MIDI — and future ideas (roadmap only)

## Why MIDI, today: a physical controller on stage

LumiDeck is built by a musician, for the stage. On stage you already hold
**physical MIDI controllers** — pads, faders, footswitches — plugged into your DAW.
They cost €20-80, every OS and DAW recognizes them with no drivers, and they're
made for live, low-latency, hands-on control.

So the point of the MIDI overlay is simple and concrete: **use the MIDI gear you
already play with to fire lamp color commands, live, mid-song** — a footswitch for
a blackout on the drop, a pad per color for each section, a fader for brightness.
No extra hardware, no screen to look at, everything local and offline.

That is the only thing the overlay does today, and the only thing we're building
now: **MIDI → lamp colors** (and the rest of the lamp actions).

## Future ideas (NOT implemented — roadmap only)

The bridge does one job today: it turns a MIDI message into a lamp command. The
same shape could, one day, turn a MIDI message into **something other than a lamp**.
Imagine the same stage controller, where each MIDI channel talks to a different
thing:

- channel 1-4 → stage lamps *(today)*
- channel 5 → Home Assistant scenes
- channel 6 → DMX moving heads / fog / strobes
- channel 7 → Resolume visuals

Nothing below is built. These are just directions worth remembering.

### Show / stage
- **DMX / Art-Net** (Enttec USB or Art-Net UDP) — pro moving heads, PARs, fog,
  strobes. The real "pro lighting" step beyond consumer LED.
- **OSC** — QLab, Resolume, TouchDesigner, lighting desks, Reaper.

### Home / local devices
- **Home Assistant** — its API already abstracts hundreds of integrations
  (lights, switches, scenes, climate, covers, media, vacuum), so one link would
  reach the whole home.
- **Shelly** (local HTTP), **MQTT** (Zigbee2MQTT / ESPHome / Tasmota),
  **Tuya beyond lamps** (plugs, blinds, climate).

### Media
- **HTTP webhook** — generic catch-all. *(OBS is skipped: the Stream Deck already
  controls it natively.)*

### How it could extend, in plain terms

The bridge is like a switchboard: today every line rings "lamps". The idea would be
to let each MIDI channel ring a different department, by adding a small, separate
piece of code per target ("send to lamps", "send to Home Assistant", "send to
DMX"). The MIDI front and the mapping file wouldn't change — only where the last
step delivers the command. Everything would stay local and offline (the stage
requirement). **But again: not now — lamps only.**

### Rough priority, if we ever pursue this
1. Home Assistant (biggest reach; it can itself talk to DMX/Shelly/MQTT).
2. DMX / Art-Net (true pro lighting).
3. OSC (visuals + show control).
