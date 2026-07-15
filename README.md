# openlamp-midi — Ableton Link / tempo for OpenLamp lamps

Flash your [OpenLamp](https://github.com/openlamp) lamps **on the beat**, phase-accurate,
from an **Ableton Link** session or a **MIDI clock**. 100 % local.

[![PyPI — openlamp-midi](https://img.shields.io/pypi/v/openlamp-midi?label=openlamp-midi&color=3775A9&logo=pypi&logoColor=white)](https://pypi.org/project/openlamp-midi/)

```bash
pip install "openlamp-midi[link]"          # [link] adds Ableton Link (native build)
python3 beatsync.py --source link --action pulse --accent
```

`beatsync.py` follows the tempo source and drives the lamps via the engine's local
API — pulsing on the beat, with **latency anticipation** (fires early so the light
lands *on* the beat) and an **accent on the downbeat** (exact under Link, which shares
the bar's phase). See `--help` for `--source link | taplink | clock | tap`.

## Scope

This package is **only** the tempo/beat layer. The **MIDI control** path — notes →
colours, CC → brightness/effects, Program Change → presets — moved into the engine
and now lives in [`openlamp/engine → midi.py`](https://github.com/openlamp/engine/blob/main/midi.py),
the reference implementation of the
**[wled-midi](https://github.com/openlamp/wled-midi)** convention. (Before v0.2.0 this
package also shipped a separate MIDI bridge; it was removed — use the engine's
`midi.py`.)

## Family

| Layer | Repo | Role |
|---|---|---|
| convention | [wled-midi](https://github.com/openlamp/wled-midi) | the MIDI↔WLED spec |
| engine | [engine](https://github.com/openlamp/engine) | drives the lamps + implements the convention (`midi.py`) |
| Ableton | [live](https://github.com/openlamp/live) | Ableton Live frontend |
| **tempo** | **this repo** | Ableton Link / MIDI-clock beat pulse |

Uses the engine's local API (`127.0.0.1:8377`). Requires the engine running.

## Architecture & the Ableton Link / GPL boundary

OpenLamp is split so that the one component touching **Ableton Link** is a small,
separate, auditable process — everything else is a separate program reached only over
a local HTTP API. This keeps the GPL reach of Link/aalink confined to a single place.

**How the pieces are wired**

- **This package (`openlamp-midi` / `beatsync.py`) runs as its own process.** It is a
  standalone CLI helper, launched independently — not embedded in, nor spawned by, the
  engine.
- **It talks to the engine only over the local HTTP API** on `127.0.0.1:8377` (plain
  `urllib` requests to `/cmd` and `/status`). There is **no** in-process call, FFI,
  shared memory, or linking between the two — the engine owns the persistent device
  connections; this helper only POSTs commands to it.
- **[Ableton Link](https://github.com/Ableton/link) is reached via
  [aalink](https://pypi.org/project/aalink/) and is imported *only here*** — a
  lazy, local `from aalink import Link` inside the Link source, gated behind the
  optional extra: `pip install "openlamp-midi[link]"`. The
  [engine](https://github.com/openlamp/engine) and the frontends contain **zero**
  Link/aalink references.

**What that means for licensing**

Ableton Link (and therefore aalink) is **GPLv2** — a component that loads it *in the
same process* forms a combined program governed by the GPL. In OpenLamp:

- The **only** process that ever combines with aalink is `beatsync.py` (this package).
  It is [MIT](LICENSE); since MIT is one-way compatible with the GPL, combining it with
  Link at runtime is fine and the effective combined program honors the GPL.
- The **engine** is a genuinely separate program that communicates only over the local
  HTTP API and carries no Link code, so it stays under its own (permissive) license —
  the GPL obligation does not propagate to it.
- The **frontends** (Stream Deck plugin, etc.) likewise reach the engine only over that
  same local HTTP API and never combine with Link, so the GPL never reaches them.

> Invariant: **GPL (via aalink/Link) is confined to one process — `beatsync.py`.** Every
> other OpenLamp component is a separate program reached only over local HTTP, so the
> GPL obligation never reaches the engine or the frontends.

Installing the `[link]` extra pulls in **aalink (GPLv2)**; the resulting combined
program (this helper + aalink + Link) is therefore governed by the **GPL**. Without the
extra, `openlamp-midi` is a pure-Python MIT package that does not touch Link at all.

## License

[MIT](LICENSE) — for this package's own source (`beatsync.py`).

Optional dependency: the `[link]` extra installs
[aalink](https://pypi.org/project/aalink/) (a Python binding to
[Ableton Link](https://github.com/Ableton/link)), which is **GPLv2**. When you install
that extra, the running combination is governed by the GPL as described above. The MIT
license covers this repository's code; it does not relicense aalink or Link.
