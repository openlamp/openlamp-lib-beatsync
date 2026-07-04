# Why MIDI — for musicians, not for Stream Deck

## Who this is for

Controlling LED lamps over MIDI is **not a Stream Deck topic** — the Stream Deck
drives the engine directly, natively. This overlay is for the **MIDI community of
musicians** who already own physical controllers (pads, faders, footswitches) and
play with them on stage. Those controllers cost €20-80, every DAW recognizes them
with no drivers, and they are made for live, low-latency, hands-on control.

So the point is concrete: **use the MIDI gear you already play to fire lamp color
commands, live, mid-song** — a footswitch for a blackout on the drop, a pad per
color per section, a fader for brightness. No extra hardware, no screen, all local.

That is the only thing this overlay does today, and the only thing being built.

## Channels subdivide ONE service (not one service per channel)

MIDI channels here are a way to **subdivide a single service** — the lamps — into
groups (channel per lamp group: front / back / L1 / L2). They are **not** a way to
address different kinds of device.

If one day we drive *other* services (Home Assistant, DMX, …), the clean model is
**one virtual MIDI device per service** — a separate virtual port, each with its own
channels-as-groups — not "channel 5 = HA, channel 6 = DMX" on a shared port. One
port = one service; channels within it = that service's groups.

## Future services (roadmap only — nothing built)

Just directions worth remembering; none implemented:

- **DMX / Art-Net** — pro moving heads, PARs, fog, strobes (the real "pro lighting"
  step beyond consumer LED).
- **OSC** — QLab, Resolume, TouchDesigner, lighting desks.
- **Home Assistant** — its API already abstracts hundreds of integrations.
- **Shelly** (local HTTP), **MQTT** (Zigbee2MQTT / ESPHome / Tasmota).

Each would be its own virtual MIDI device, added as a small separate frontend when
there is real hardware to validate it against. Everything stays local and offline.
