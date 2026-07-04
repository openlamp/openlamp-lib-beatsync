# MIDI as a universal encapsulation bus

The lamp bridge is one instance of a general pattern. MIDI is a **cheap, universal,
physical control layer** — hardware controllers (pads, faders, footswitches) cost
€20-80, and the Stream Deck can emit MIDI too. So the real product is:

> **A MIDI → anything router**, where the target is a pluggable backend.
> One mapping file, `channel/note/CC` as the address space, and each channel (or
> mapping entry) routes to a *backend*: lamps, Home Assistant, DMX, OSC, MQTT, HTTP…

The same €40 MIDI pad or Stream Deck then drives your **whole show and home**:
ch 1-4 = stage lamps, ch 5 = Home Assistant scenes, ch 6 = DMX movers,
ch 7 = Resolume visuals — from one surface, live.

## Backends worth encapsulating

### 🎭 Show / stage (your world)
| Backend | Protocol | Unlocks | Note |
|---|---|---|---|
| **DMX / Art-Net** | UDP Art-Net / USB (Enttec) | pro moving heads, PARs, fog, strobes | the pro upgrade path beyond consumer LED |
| **OSC** | UDP OSC | QLab, Resolume, TouchDesigner, lighting desks, Reaper | the lingua franca of show control |
| **Ableton Link** | Link | tempo sync across devices | complements MIDI clock |

### 🏠 Home / local devices (your HA setup)
| Backend | Protocol | Unlocks | Note |
|---|---|---|---|
| **Home Assistant** | HA REST/WS API | **everything in HA**: lights, switches, scenes, climate, covers, media, vacuum, notify | highest leverage — HA already abstracts 100s of integrations |
| **Shelly** | local HTTP/RPC | relays, roller shutters, power monitoring | you have many |
| **MQTT** | MQTT | Zigbee2MQTT, ESPHome, Tasmota, anything on the broker | one bridge, whole broker |
| **Tuya (beyond lamps)** | Tuya local | plugs, switches, blinds, climate | reuse the driver |

### 🎥 Media / apps
| Backend | Protocol | Unlocks | Note |
|---|---|---|---|
| **OBS** | obs-websocket | scenes, sources, recording | *(Stream Deck already does this natively — skip)* |
| **HTTP webhook** | generic HTTP | IFTTT-style, any REST endpoint | catch-all |

## Design: pluggable backends

The bridge already has the right shape — `mapping.json` maps *message → command*.
Generalize the *send* step into a **backend interface**:

```
backend.apply(command, target)   # lamps→OLS, ha→service call, dmx→channels, osc→address…
```

- Per-channel (or per-entry) backend selection: `"channels": {"1": "lamps:all",
  "5": "ha:scene", "6": "dmx:1-8"}`.
- Each backend is a small module (~50-100 lines): translate the generic command
  into its protocol and send locally.
- MIDI clock stays global (tempo) and can drive any tempo-aware backend.

Everything stays **local and offline** — the whole point of the stage use case.

## Recommended order (for your gear)

1. **Home Assistant backend** — biggest unlock (your entire home from a MIDI pad /
   Stream Deck), and it composes: HA can itself reach DMX, Shelly, MQTT.
2. **DMX / Art-Net backend** — the real "pro lighting" step beyond Tuya/WLED.
3. **OSC backend** — visuals (Resolume) + show control (QLab) for the live set.

MQTT / Shelly / generic-HTTP are easy follow-ons once the backend interface exists.
