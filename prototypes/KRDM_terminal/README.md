# KRDM terminal prototype — research-driven OBJ8 building generation

Two eras are generated from one composer:
- `KRDM_terminal.obj` — the terminal as it stands (2024): retained lodge,
  2010 HNTB lounge, walkway strips, gate canopies.
- `KRDM_terminal_2028.obj` — with the 2025-2028 Skanska / Hennebery Eddy
  expansion: the old NE walkway replaced by a two-level mass-timber
  concourse (charcoal roof with solar array, timber solar-fin screen
  whose top edge traces the Cascade silhouette), five jet bridges, a
  white processor block, and a rebuilt NE ground-boarding run with
  landside connector. Sources: TACP 2021 figs 7-4/7-5 (scaled at
  0.20 m/px against the known 202.3 m apron edge), flyrdm.com official
  renderings (research/expansion facts JSONs in scratchpad research).
  The scenery pack places the 2028 object; swap the OBJECT_DEF in the
  DSF to revert to the 2024 model.

A prototype of the pipeline: research agents gather a real building's
footprint, dimensions, and facade facts; a parametric generator
(`tools/obj8_building_gen`) composes a fully textured X-Plane OBJ8 model
from those facts; `tools/obj8_preview` renders it for visual critique
against the reference photos.

Subject: the passenger terminal at Redmond Municipal Airport / Roberts
Field (KRDM), Redmond, Oregon — the "high-desert lodge" terminal with
its stepped sage-green roofline (Barber Barrett Turner Architects) and
the 2009 HNTB two-story glazed airside departure lounge.

## Contents

- `build_krdm_terminal.py` — the composer; regenerate with
  `venv/bin/python prototypes/KRDM_terminal/build_krdm_terminal.py`
- `research/footprint.json` — OSM way 104478704 footprint (202 m apron
  frontage), converted to X-Plane object coordinates
- `research/facade.json` — structured facade facts (massing, roof forms,
  material colors, heights) distilled from reference photos
- `research/photos_index.json` — sources of the reference photos
  (photos themselves are reference-only and are not stored in the repo)
- `output/KRDM_terminal.obj` + `KRDM_terminal.png` — the model (~800
  vertices, 390 triangles, one 2048 px atlas)
- `output/preview.html` — interactive three.js preview
  (`venv/bin/python tools/obj8_preview/obj8_to_html.py output/KRDM_terminal.obj -o output/preview.html`)
- `output/KRDM_Terminal_Prototype/` — installable scenery pack (note:
  the installed KRDM package also places its own terminal object —
  disable one or the other to avoid two overlapping terminals)
  (overlay DSF placing the object at 44.253096 N, 121.160848 W)

## Install

Copy `output/KRDM_Terminal_Prototype` into `X-Plane 12/Custom Scenery/`
and make sure it sorts above any other +44-122 overlay you want it to
coexist with (scenery_packs.ini). The terminal appears on the west
apron at KRDM.

## Known limitations (prototype scope)

- Heights were photo-estimated, then cross-checked against the existing
  KRDM package terminal mesh (`c_USA - 100_airport - KRDM Roberts Field`,
  x-plane_skp_test-terminal.obj): parapet 6.5 m, lounge roof 11.5-13.0 m,
  landside shed ridges up to 10.6 m, walkway canopies 4.7-5.5 m.
- The detached ATC tower is deliberately not modeled (separate object).
- No night lighting (`TEXTURE_LIT`), no tapered pier profiles, flat
  procedural textures only — no photo-derived material.
- The 2025–2028 concourse expansion (Hennebery Eddy/RS&H) is NOT
  modeled; the model reflects the terminal as photographed 2010-2024.
- X-Plane's default KRDM flatten/apron may not align perfectly with the
  OSM footprint; nudge via WED if needed.
