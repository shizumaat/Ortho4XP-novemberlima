# Junction smoothing — taxiway shoulder absorption

Build evidence: dev @69ffc54 + WIP, HECA. User report: junctions bumpy again
after the smooth s78 network-profile build; pointed at apron **#253** "near J1
and G", corners ≈ (30.1300569, 31.4086814)…(30.1269789, 31.4098056).

## 1. Root cause (confirmed)

The user's region (26 043 m², local-frame centroid ≈ (-2429, 2201)) is **100 %
inside apron #253** (398 k m²) with taxiway **G's centerline running 372 m
through it**. Source coverage of the region: **apt.dat row-110 70 % + DSF 30 %**.

#253 carries the airport's worst within-shape wall: **19.9 % `86.0 → 81.2`,
Δe = 4.8 m over 24 m** (mirror side #264). It has **0 taxi rects / 0 junctions**
inside it — no corridor grading, so the route walls across the flat apron.

**Not** the s80 apron-follows family (gate-off was worse), **not** the
absorption-drop (`_drop_primary_parallels_embedded_in_pavement` didn't fire).

**Mechanism (user, 2026-06-13):** the apt.dat/DSF carries **separate pavement
pieces for the taxiway's shoulders** — exactly like runway shoulders. Taxiway G
+ its shoulder strips make the paved area wide, so:
- the taxi-rect builder's code-letter-width rect sits fully inside the wide
  pavement → ≥2 corners off the pavement boundary → rejected as "apron-interior"
  → **no rect built for G here**;
- the residual wide pavement becomes apron #253, swallowing G and junction J1.

G *is* built as rects where it leaves the shoulder pavement (cross_connector `G`
#51/#55/#57, stubs G1/G2). The hole is only where the shoulders widen it.

## 2. The fix — mirror the runway shoulder logic for taxiways

Runways already solve this exact problem (s80, user 2026-06-12, KPHL/HECA):
- `_detect_runway_shoulders` (`pavement/runways.py:894`) — whole-polygon: long
  thin pavement parallel to and touching the runway is folded into the runway
  emit; absorbed polys removed from `pav_polys` so they don't re-emit as
  junctions.
- `_detect_runway_shoulder_extent` (`:1029`) — extent-based: perpendicular walk
  outward from each edge through the source union; a consistent shoulder-range
  strip is absorbed. Gated `RUNWAY_SHOULDER_EXTENT` (`O4_SHOULDER_EXTENT`),
  constants `RUNWAY_SHOULDER_EXTENT_*` in `config.py:289-306`.

**Build the taxiway analog: `_detect_taxiway_shoulders` / extent pass.** For each
taxiway corridor (centerline / candidate rect):
1. Walk perpendicular from each long edge through the source pavement union at
   `STATION_M` spacing, `STEP_M` resolution.
2. A side is a **shoulder** when a consistent (coverage ≥ `MIN_COVERAGE`) strip
   of width in `[MIN_M, MAX_M]` extends past the code-letter edge — and the
   extension is **bounded** (shoulder ≤ ~taxiway width, the runway pass's
   "ext < runway_width" rule) so real aprons are NOT absorbed.
3. **Widen the taxi rect to the measured extent** before the apron residue is
   computed, absorbing the shoulder pieces into the corridor (and removing any
   discrete shoulder polygons from `pav_polys`, mirroring the runway pass).

Effect: G's rect now spans its shoulders → its corners land on the pavement
boundary → it is kept as a directional corridor; junction J1 emerges where
corridors cross; apron #253 shrinks to the actual apron. The existing
NETWORK_PROFILE_MODEL then grades G/J1 as smooth corridors and the apron conforms
around them.

### Taxiway-shoulder envelope (new `config.py` constants)
Mirror `RUNWAY_SHOULDER_EXTENT_*`, sized for taxiways: code-letter taxiway widths
run ~10.5 m (B) to ~25 m (F); a generous shoulder envelope is ~half the taxiway
width per side. Start: `STATION_M 15`, `STEP_M 1`, `MIN_M 1.5`, `MAX_M` ≈ taxiway
half-width (cap ~12 m), `MIN_COVERAGE 0.8`, and a side-extension cap = taxiway
width. Gate `TAXIWAY_SHOULDER_EXTENT` (`O4_TAXI_SHOULDER_EXTENT`, default ON once
measured; OFF = byte-identical).

### Insertion point
At taxi-rect construction (`pavement/rects.py` / the rect-build call in
`pipeline.py`), so the widened rect exists *before* the apron-interior rejection
and *before* `pav_union − rects` forms the apron residue. The shoulder
measurement reads the same source union the runway pass uses.

## 3. Discriminators / risks
- **Don't absorb real aprons.** The bounded side-extension (shoulder ≤ taxiway
  width) + coverage-consistency is what separates a shoulder from a parking apron
  fanning off the taxiway (same logic that lets the runway pass reject CYXY's
  "Apron 1 and E").
- **Don't over-widen into adjacent pavement** (another taxiway / true apron
  running alongside): clamp at `MAX_M` and require the strip to be a *consistent*
  parallel band, not a widening fan.
- **SPJC** balloon apron must not fragment; **CYXY** coverage must hold.
- Node parity at the widened-rect seam (apron = pav_union − rects stays
  overlap-free — the property the no-absorption model already guarantees).

## 4. Validation protocol
Per-axis audit (`tests/test_pavement_grade.py`); invariants 05C 108.70 / 05L /
A4 / A5; SPJC + CYXY gates GREEN; gate-off byte-identical; deterministic; suite
baseline. Target: #253 19.9 % wall gone, HECA worst within-shape leaves the apron
family, G/J1 emit as corridor + junction. Probe: rebuild HECA, confirm a taxi
rect + junction now cover the region (-2429, 2201) and #253's area drops.
