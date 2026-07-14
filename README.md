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

## License

[MIT](LICENSE).
