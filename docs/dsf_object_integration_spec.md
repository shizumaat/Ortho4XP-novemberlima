# DSF custom-object integration — multi-agent implementation spec

**Reads with:** [`docs/dsf_object_anchor_plan.md`](dsf_object_anchor_plan.md). That document is the
*investigation*: the physics, the evidence, the fixes that do not work, and the correctness rules
learned the hard way. This document is the *build order*. Neither stands alone. Every agent must read
the plan's §4 (why the obvious fixes fail) and §7.3 (why both phases are required) before writing a
line of code, and §8 (correctness rules) before touching the geometry path.

**Amended:** [§10](#10-amendments--2026-07-08-review) records a same-day second-model review; where an
amendment conflicts with any section above or with the partition document, **the amendment wins**.

**And with:** [`docs/obj8_structure_partition.md`](obj8_structure_partition.md) — *what is a structure?*
The plan treats the component-grouping gap as a tuning knob. It is not: the correct partition is the
connected components of the ε-contact graph, and the prototype's 2 m gap works only because it happens
to be coarser than that. **W2 and W4 must read it in full before starting**; it supersedes the plan's
§8.3 gap table and rewrites invariants I-2, I-6 and I-8 (see §5 of that document for the exact
delta against this spec).

**Baseline:** `dev`. Code references below were re-verified against the working tree on 2026-07-08 and
supersede the plan's §12 table where they differ.

**Shape of the work:** two independently valuable capabilities that share one geometry library.

| | Phase 1 — footprints | Phase 2 — the y-bake |
|---|---|---|
| what | OBJ8 structures become building pads | `.obj` vertices offset so each structure sits on its own ground |
| when | layout time, inside `build_airport_pavement` | post-build, after `MESH.build_mesh(tile)` |
| needs the mesh? | no | **yes** — it is the whole point |
| writes | nothing outside the build dir | third-party pack `.obj` files, in place |
| flag | `O4_DSF_OBJECT_BUILDINGS` (default off) | `O4_DSF_OBJECT_REANCHOR` (default off) |
| KCLT stake | ~105 buildings auto_patch cannot currently see | median base error 3.06 m → 0.12 m |

---

## 0. Rulings in force

These are decided. Do not relitigate them; if implementation reveals one is wrong, stop and escalate
rather than quietly choosing differently.

**R1 — Write target (user, 2026-07-08).** Phase 2 rewrites pack `.obj` files **in place**, with
`<name>.anchor_bak` originals. No shadow pack, no DSF rewrite. Geometry is always read from the
backup, never from the live file, so the operation is byte-idempotent and `--restore` is exact.

**R2 — Staleness (user, 2026-07-08).** Phase 2 **auto re-bakes after every mesh build** whenever the
flag is on. The provenance sidecar is written and `--check` is kept, but provenance is a *diagnostic*,
not a gate. Rationale: re-baking is cheap, reads from the backup, and cannot stack. A stale bake is a
visibly broken airport; a gate that hard-fails a 40-minute tile build is worse than just redoing the
30-second bake.

**R3 — Footprint ring shape (user, 2026-07-08).** **Convex hull ships first** as the Phase 1 default,
so the pad-grading interaction can be measured on gate airports immediately. The faithful
triangle-union ring lands behind a **second flag**, `O4_DSF_OBJECT_FOOTPRINT_UNION`, once that
measurement exists. This closes plan open-question #2 as *staged*, not *answered*.

**R4 — Precedence (user, 2026-07-08).** Phase 1 footprints are **additive**. They enter the same DSF
building pool as `.fac` facades and `.agp` hangars and are *not* given priority over OSM or anything
else. This is load-bearing and slightly counter-intuitive, so see §2.3: the existing
`_cluster_dsf_building_facades` union already collapses an OBJ8 ring that overlaps a `.fac` facade
into one polygon, and `_combine_building_sources` already drops the OSM building underneath. Additive
at the reader gets override-like behaviour for free, without a new overlap predicate.

**R5 — Buildings are FLAT** (user, prior ruling). Unchanged. OBJ8 footprints are building pads and
obey it.

**R6 — No abbreviations in identifiers** (user, standing, all projects). `heading_degrees`, not
`hdg`. The existing `obj8_geometry.py` already complies — match it. Config-constant suffixes `_M` /
`_M2` are the established local exception (`DSF_FACADE_MERGE_GAP_M`); keep those.

**Still open, and *not* blocking (plan §10):** whether X-Plane honours `OBJECT_MSL` for scenery
objects (#4); whether Phase 1 actually drives the Phase 2 residual to zero (#6 — W8 measures it);
whether the pattern generalises beyond the Nimbus pack (#7 — W8 audits it). The redistribution
question (#1) is closed by R1: corrected packs are a local artifact and must never be redistributed;
W5 writes that warning into the provenance sidecar.

---

## 1. Scope and non-goals

**In scope**

* Promote the four `tools/` prototypes into `src/auto_patch/` as a supported library.
* Generalise the KCLT-hard-coded re-anchor into a discovery-driven facility.
* Phase 1: OBJ8 structure footprints as a building source.
* Phase 2: post-mesh y-bake, auto-run, in-place, reversible.
* A generalised CLI (`tools/reanchor_dsf_objects.py`) over the same library.

**Explicitly out of scope**

* Splitting objects into one placement per structure (plan §4.3). Verified working; kept as the
  fallback if the y-bake proves insufficient. Do not build it.
* Rewriting any `.dsf`. Phase 2 touches `.obj` only.
* `OBJECT_MSL` conversion (plan §4.4). Unverified against the sim.
* The 41 KCLT terminal-layer objects (`paredes`/`techos`/`vidrios`/`suelos`/`modulos`). They contain
  12 `ANIM_begin` blocks and are the first real test of I-11/I-12. W2 must make them *representable*;
  landing them is a follow-up, gated by `O4_DSF_OBJECT_ALLOW_ANIM` (default off, refuse-and-report).

**Non-goal that will tempt you:** moving the anchor. Plan §4.1 measured it — for `Charlotte_Airport_007_ALB.obj`
the centroid anchor is *worse* than the status quo (10.18 m vs 9.95 m worst error), because it lands
in the 1644 m gap between two building groups. A placement carries exactly one elevation. No anchor
scheme fixes a multi-structure object.

---

## 2. Architecture

### 2.1 Module map

New modules are **owned by exactly one workstream** so parallel agents never collide.

```
src/auto_patch/
  obj8_reader.py       NEW  W2  parse OBJ8; solid/draped split; positional commands; ANIM/LOD
  mesh_sampler.py      NEW  W3  barycentric elevation lookup over Data<tile>.mesh
  object_anchor.py     NEW  W4  anchor-group discovery; world-ENU partition; per-object deltas
  object_rebake.py     NEW  W5  backups, y rewrite, provenance, restore, hash guard
  object_footprints.py NEW  W6  structures -> lon/lat rings (hull; union behind a flag)
  post_mesh.py         NEW  W7  the post-mesh stage entry point

  config.py            EDIT W1  flags (lands first, alone)
  dsf_reader.py        EDIT W6  read_dsf_object_buildings(); resource resolution
  pipeline.py          EDIT W6  wire Phase 1 into the DSF building pool
  driver.py            EDIT W7  emit the Phase 2 worklist sidecar

src/O4_Tile_Utils.py   EDIT W7  three lines: call post_mesh after MESH.build_mesh
tools/reanchor_dsf_objects.py NEW W7  CLI over object_rebake
```

`tools/obj8_geometry.py`, `tools/mesh_elevation_sampler.py`, `tools/dsf_object_anchor_audit.py`,
`tools/reanchor_kclt_terminal_bakes.py` remain in place until W8 signs off, then the two library
prototypes are deleted and the two tools re-pointed at `src/auto_patch/`. Deleting them earlier
destroys the only working reference implementation.

### 2.2 Data flow

```
                    ┌─ Phase 1 (layout time, no mesh) ────────────────────────┐
build_poly_file →   │ dsf_reader.read_dsf_object_buildings                    │
  generate_auto_    │   → obj8_reader.load_object_file                        │
  patches           │   → object_anchor.discover_anchor_groups                │
                    │   → object_anchor.partition_structures  (world ENU)     │
                    │   → object_footprints.structure_ring     (hull|union)   │
                    │   → (outer_ring, holes, role="object")                  │
                    │ pipeline: append to dsf_building_polys                  │
                    │ driver:   write o4_object_anchor_worklist.json          │
                    └────────────────────────────────────────────────────────┘
                                     ↓  (grading uses the new pads)
MESH.build_mesh(tile)  →  Tiles/zOrtho4XP_<tile>/Data<tile>.mesh
                                     ↓
                    ┌─ Phase 2 (post-mesh) ──────────────────────────────────┐
post_mesh.rebake_   │ read worklist  → mesh_sampler.MeshElevationSampler      │
  dsf_objects(tile) │   → object_anchor.structure_deltas (per object!)        │
                    │   → object_rebake.apply / .check / .restore            │
                    │   → <pack>/.o4_reanchor_provenance.json                │
                    └────────────────────────────────────────────────────────┘
MASK.build_masks → DSF.build_dsf
```

**Why the worklist sidecar exists.** `build_all` (`src/O4_Tile_Utils.py:287`) has a `tile`, and
nothing else. Re-deriving the X-Plane root at that point means going
`CFG.cifp_data_path → cifp_reader.xplane_root_from_cifp_path` (there is no direct `xp_root` config
key), then re-listing airports, then `apt_dat_reader.find_all_airport_apt_dats` →
`osm_load._pick_best_apt_dat_against_osm` → `dsf_reader.find_associated_dsf` → `_pack_root_for_dsf`.
All of that is already done inside the driver. Phase 1 writes down what it learned; Phase 2 reads it.
This also lets the CLI (`tools/reanchor_dsf_objects.py`) consume the same worklist, and lets Phase 2
be tested with a hand-written JSON file and zero X-Plane install.

Write the worklist at the point in `driver.py` where `xp_root` is resolved (≈`:549`), **before** the
rebuild-skip gate at `:564` — otherwise an all-current tile silently produces no worklist and Phase 2
does nothing.

### 2.3 Why "additive" (R4) is sufficient

This deserves spelling out, because a reviewer will file it as a bug.

`pipeline.py:1177-1216` collects everything `read_dsf_buildings` returns into `dsf_building_polys`
(metres). Then at `pipeline.py:2080-2085`:

```python
if DSF_BUILDINGS and dsf_building_polys:
    dsf_seed_polys = _cluster_dsf_building_facades(dsf_building_polys)
    osm_terminal_polys = _combine_building_sources(
        dsf_seed_polys, osm_terminal_polys, DSF_BUILDING_OSM_OVERLAP_FRAC)
```

* `_cluster_dsf_building_facades` (`terminals.py:811`) does `buffer(+gap).buffer(-gap)`, takes
  connected components, fills holes, and DP-simplifies. An OBJ8 ring sitting on top of the `.fac`
  facades of the same terminal **unions with them** — one polygon, one pad.
* `_combine_building_sources` (`terminals.py:888`) emits the DSF pool verbatim, and admits an OSM
  building only when `intersection / osm.area < DSF_BUILDING_OSM_OVERLAP_FRAC`. The OSM duplicate is
  already dropped.

So additive-at-the-reader yields spatial-override-at-the-layout, using machinery that already exists
and is already tested. Do **not** add an overlap predicate.

Two consequences to watch, both for W8:

1. `_cluster_dsf_building_facades` drops polygons under `DSF_MIN_BUILDING_AREA_M2`. Rooftop clutter
   that survived the elevated-structure filter will vanish here anyway. Good.
2. The 513 m KCLT terminal complex is 112,230 m². Its hull is a *very* large flat pad, and R5 says
   buildings are flat. W8 must confirm it does not fight apron or runway jurisdiction. If it does, the
   mitigation is `O4_DSF_OBJECT_MAX_FOOTPRINT_AREA_M2` (skip-and-report above the cap), **not** a
   quiet clip.

### 2.4 The one generalisation the prototype does not have

`tools/reanchor_kclt_terminal_bakes.py:313-327` asserts all eight placements share a bit-identical
anchor and then pools their vertices in that single shared local frame. **That assumption does not
generalise** — the 41 terminal-layer objects share a heading but have two anchors ~10 m apart
(plan §8.1).

The correct model, and the single most likely place for a subtle bug:

> A structure's ground elevation is a property of the **structure**.
> The y offset applied to a vertex is a property of the **(structure, object)** pair, because X-Plane
> puts each object's `y = 0` plane at the terrain under **that object's own anchor**.

```
delta(structure S, object O) = ground_under(centroid(S)) − ground_under(anchor(O))
```

When two objects contribute geometry to one structure — which is exactly the KCLT case, where walls
and roof of the same building live in different texture-page bakes — they receive **different** deltas,
and the walls still meet the roof, because each delta is measured from its own object's `y = 0` plane.
Collapsing this to a single per-structure delta happens to be correct only when all contributing
objects share an anchor. It silently tears geometry apart when they do not.

Partition in a **common world ENU frame** (project each object's vertices through *its own* placement),
then map back per-object, per-vertex. The `y` offset is unaffected by the heading rotation, which is
about the `y` axis.

---

## 3. Frozen contracts

These are the parallelism enabler. **W1 lands them as type stubs with full docstrings and
`NotImplementedError` bodies before any other workstream starts.** After that they may not change
without a note in this file and a ping to every downstream owner.

### 3.1 `obj8_reader` (owner W2)

```python
class ObjectPlacement(NamedTuple):        # unchanged from tools/obj8_geometry.py
    definition_index: int
    resource_path: str
    longitude: float
    latitude: float
    heading_degrees: float

class PositionalCommand(NamedTuple):
    """A non-VT OBJ8 command carrying its own (x, y, z) — LIGHT_*, VLIGHT,
    SMOKE_*, EMITTER, MAGNET.  ``line_index`` addresses the source file;
    ``y_token_index`` is the whitespace-token position of the y value."""
    line_index: int
    keyword: str
    x: float
    y: float
    z: float
    y_token_index: int

class ObjectGeometry(NamedTuple):
    vertices: list[tuple[float, float, float]]
    solid_triangles: list[tuple[int, int, int]]
    draped_triangles: list[tuple[int, int, int]]
    positional_commands: list[PositionalCommand]
    animation_block_count: int          # count of ANIM_begin
    level_of_detail_count: int          # count of ATTR_LOD
    vertex_line_indices: list[int]      # VT line index, parallel to `vertices`

    @property
    def has_solid_geometry(self) -> bool: ...
    def solid_reach_metres(self) -> float: ...

def load_object_file(path: str) -> ObjectGeometry: ...
def connected_components(vertices, triangles) -> list[list[tuple[int,int,int]]]: ...
def group_components_into_structures(vertices, components, gap_metres=2.0,
                                     grid_cell_metres=60.0) -> list[list[...]]: ...
def area_weighted_centroid(vertices, triangles) -> tuple[float, float, float]: ...
def horizontal_bounding_box(vertices, triangles) -> tuple[float,float,float,float]: ...
def local_offset_to_lonlat(anchor_latitude, anchor_longitude, heading_degrees,
                           local_x, local_z) -> tuple[float, float]: ...
def lonlat_to_local_offset(anchor_latitude, anchor_longitude, heading_degrees,
                           latitude, longitude) -> tuple[float, float]: ...   # NEW, inverse
def read_dsf_object_placements(dsf_text_lines, accept_resource=None) -> list[ObjectPlacement]: ...
def resolve_object_resource(resource_path, pack_root, xplane_root) -> str | None: ...
```

`vertex_line_indices` and `PositionalCommand.line_index` exist so W5 can rewrite exactly the tokens it
must, preserving every byte of whitespace and decimal precision elsewhere. W5 must not re-parse.

### 3.2 `mesh_sampler` (owner W3)

```python
class MeshElevationSampler:
    def __init__(self, mesh_path: str,
                 bounds: tuple[float, float, float, float],   # (min_lon, min_lat, max_lon, max_lat)
                 margin_degrees: float = 0.002) -> None: ...

    def elevation_at(self, latitude: float, longitude: float) -> float:
        """Barycentric elevation.  Raises OutsideMeshError outside every
        retained triangle.  There is NO nearest-vertex fallback."""

    def elevation_at_or_none(self, latitude, longitude) -> float | None: ...
```

**Change from the prototype, and it is a correctness change.**
`tools/mesh_elevation_sampler.py:156-159` falls back to the nearest mesh vertex when a point lands
outside every retained triangle. That silently returns a plausible number for a structure that has
walked off the edge of the tile, and a plausible number is exactly what you must not have. Callers
skip-and-report instead (I-13).

### 3.3 `object_anchor` (owner W4)

```python
@dataclass(frozen=True)
class Structure:
    triangles_by_resource: dict[str, list[tuple[int, int, int]]]
    surface_area_m2: float
    centroid_latitude: float
    centroid_longitude: float
    minimum_base_y_by_resource: dict[str, float]
    is_ground_touching: bool          # min over resources of base y <= ELEVATED_BASE_M

@dataclass(frozen=True)
class AnchorGroup:
    """Objects whose geometry must be partitioned together: their world
    footprints interact, so a structure may span several of them."""
    placements: list[ObjectPlacement]          # exactly one per resource
    resolved_paths: dict[str, str]             # resource_path -> file on disk

@dataclass(frozen=True)
class RebakeDecision:
    structures: list[Structure]
    delta_by_resource_and_vertex: dict[str, dict[int, float]]
    anchor_ground_by_resource: dict[str, float]
    skipped: list[tuple[str, str]]             # (resource_path, reason)

def discover_anchor_groups(placements, resolved, *, proximity_m, heading_tolerance_deg) -> list[AnchorGroup]: ...
def partition_structures(group, geometry_by_resource, *, gap_metres) -> list[Structure]: ...
def structure_deltas(group, geometry_by_resource, structures, sampler) -> RebakeDecision: ...
```

`structure_deltas` is where §2.4 lives. It is pure: sampler in, numbers out, no file I/O.

### 3.4 Worklist sidecar — `Patches/<lon lat>/o4_object_anchor_worklist.json`

Written by W7 (driver), read by W7 (post_mesh) and the CLI. Version it from day one.

```json
{
  "version": 1,
  "tile": "+35-081",
  "xplane_root": "/Users/noah/X-Plane 12",
  "airports": [
    {
      "icao": "KCLT",
      "dsf_path": ".../Nimbus.../Earth nav data/+30-090/+35-081.dsf",
      "dsf_mtime": 1749584021.123456,
      "pack_root": ".../Custom Scenery/Nimbus Simulation - KCLT V1.4 - Charlotte XP12",
      "anchor_groups": [
        {
          "resources": ["Terminals/Hangar/Charlotte_Airport_001_ALB.obj", "..."],
          "placements": [
            {"resource_path": "...", "longitude": -80.935041390,
             "latitude": 35.207360571, "heading_degrees": 86.095674}
          ]
        }
      ],
      "skipped": [["otros/cone_short.obj", "512 placements"]]
    }
  ]
}
```

Stale if any `dsf_mtime` disagrees with disk → Phase 2 re-derives the groups itself rather than
trusting the file. Cheap; the DSF text dump is already memoised on `(abspath, mtime)`.

### 3.5 Provenance sidecar — `<pack_root>/.o4_reanchor_provenance.json`

Extends the prototype's. The `written_sha256` field is new and closes a real hole (I-14).

```json
{
  "version": 1,
  "warning": "These .obj files were modified by Ortho4XP auto_patch to match locally graded terrain. DO NOT REDISTRIBUTE. Run tools/reanchor_dsf_objects.py --restore to undo.",
  "mesh": "/.../Data+35-081.mesh",
  "mesh_size": 123456789,
  "mesh_mtime": 1749584021,
  "gap_metres": 2.0,
  "structures": 105,
  "vertices_offset": 41230,
  "objects": {
    "Terminals/Hangar/Charlotte_Airport_001_ALB.obj": {
      "anchor": [35.207360571, -80.935041390, 86.095674],
      "anchor_ground_m": 219.83,
      "backup_sha256": "…",
      "written_sha256": "…"
    }
  }
}
```

---

## 4. Workstreams

Dependency graph. `→` means *must be merged before*.

```
W0 (read) ─→ W1 (contracts+flags) ─┬─→ W2 obj8_reader ──┬─→ W4 object_anchor ─┬─→ W5 object_rebake ─┐
                                   ├─→ W3 mesh_sampler ─┘                     │                     ├─→ W7 wiring ─→ W8 measurement
                                   └────────────────────────────→ W6 footprints + Phase 1 wiring ───┘
```

W2 and W3 are fully parallel. W6 needs only W2 (footprints never touch the mesh — plan §7.1). W5 needs
W4. W7 serialises because it edits shared files.

---

### W0 — Read (every agent, before starting)

`docs/dsf_object_anchor_plan.md` in full. Then, for your own workstream, the source files named in
your "reads" list. Report back the one fact in the plan that most surprised you; if it is not §4.1 or
§7.3, read those again.

---

### W1 — Contracts and configuration
**Owns:** `src/auto_patch/config.py`; creates all six new modules as stubs.
**Depends on:** nothing. **Lands alone, first, on `dev`.** Nobody else starts until this is merged.

Deliverable: every signature in §3 exists, fully docstringed, body `raise NotImplementedError`.
Every flag exists. `pytest --collect-only` is clean. Nothing behaves differently.

Flags, matching the idiom at `config.py:1304-1321` exactly — a `#` comment block with a slug and a
user-ruling date, `_os.environ.get(...) == "1"`, and a statement of what OFF restores. All names added
to `__all__` (`config.py:13`).

```python
# (20260708) DSF OBJECT BUILDINGS (user 2026-07-08): scenery authors bake
# many buildings into one ``.obj`` whose DSF placement anchor may sit
# hundreds of metres from any geometry.  auto_patch cannot see those
# buildings at all today (``_building_role_for_def`` requires ``.fac``, and
# ``is_agp_building_def`` whitelists ``.agp``) — ~105 of them at KCLT.
# ``dsf_reader.read_dsf_object_buildings`` parses the OBJ8, partitions it
# into structures, and feeds their footprints into the SAME building pool as
# the ``.fac`` facades (role ``"object"``).  Additive: no source is
# overridden (user 2026-07-08); the existing facade clustering unions any
# overlap.  Default OFF until measured on the gate airports.  Has no effect
# unless DSF_BUILDINGS is also ON (shares the building path).
DSF_OBJECT_BUILDINGS = _os.environ.get("O4_DSF_OBJECT_BUILDINGS", "0") == "1"

# Convex hull is the shipped footprint (user 2026-07-08: measure the pad
# interaction before paying for fidelity).  The union of the projected
# solid triangles is faithful for L-shaped terminals but needs a simplify
# tolerance and a hole policy.  OFF restores the hull.
DSF_OBJECT_FOOTPRINT_UNION = _os.environ.get("O4_DSF_OBJECT_FOOTPRINT_UNION", "0") == "1"

# (20260708) DSF OBJECT RE-ANCHOR (user 2026-07-08): post-mesh, rewrite the
# ``y`` column of each structure's vertices by
# ``ground_under(structure) - ground_under(anchor)`` so every structure sits
# on its own terrain.  Writes IN PLACE into the scenery pack, keeping
# ``<name>.anchor_bak`` originals; geometry is always re-read from the
# backup, so the operation is byte-idempotent and cannot stack.  Re-runs
# after every mesh build (user 2026-07-08) because the offsets encode one
# specific built mesh.  Corrected packs MUST NOT be redistributed.
# OFF leaves every pack byte-identical.
DSF_OBJECT_REANCHOR = _os.environ.get("O4_DSF_OBJECT_REANCHOR", "0") == "1"

# Refuse (and report) objects containing ANIM_begin: a per-structure delta
# applied inside an animation block can break its rotation pivot.  ON gives
# each animation block the single delta of the structure containing its
# geometry.  The 41 KCLT terminal-layer objects are the first real test.
DSF_OBJECT_ALLOW_ANIM = _os.environ.get("O4_DSF_OBJECT_ALLOW_ANIM", "0") == "1"

# Detector floor.  A compact, correctly anchored object has a solid reach of
# a few metres; 57 of KCLT's 334 definitions exceed 25 m.
DSF_OBJECT_MIN_REACH_M = float(_os.environ.get("O4_DSF_OBJECT_MIN_REACH_M", "25"))

# Single-link bounding-box merge distance, components -> structures.  2 m =
# "physically touching".  Measured (plan §8.3): 0.5 m -> 116 structures,
# 2 m -> 105, 20 m -> 50 with sub-buildings 11 m off the ground.
DSF_OBJECT_STRUCTURE_GAP_M = float(_os.environ.get("O4_DSF_OBJECT_STRUCTURE_GAP_M", "2"))

# Vertices within this height of a structure's own base form its footprint;
# above it, roof overhang would inflate the pad.
DSF_OBJECT_FOOTPRINT_HEIGHT_M = float(_os.environ.get("O4_DSF_OBJECT_FOOTPRINT_HEIGHT_M", "1.5"))

# A structure whose lowest vertex sits above this rests on something else —
# rooftop clutter, a canopy, a jetbridge.  It contributes no footprint and
# inherits the offset of the ground-touching structure supporting it.
DSF_OBJECT_ELEVATED_BASE_M = float(_os.environ.get("O4_DSF_OBJECT_ELEVATED_BASE_M", "0.5"))

# Skip-and-report a footprint larger than this rather than laying a flat pad
# across half an airfield.  0 disables the cap.  KCLT's terminal complex is
# 112,230 m2 — see the spec §2.3.
DSF_OBJECT_MAX_FOOTPRINT_AREA_M2 = float(
    _os.environ.get("O4_DSF_OBJECT_MAX_FOOTPRINT_AREA_M2", "0"))

# Placements closer than this, with headings agreeing to
# DSF_OBJECT_HEADING_TOLERANCE_DEG, are one anchor group: their geometry may
# share structures and must be partitioned together.  The eight KCLT bakes
# share an anchor exactly; the 41 terminal-layer objects sit ~10 m apart.
DSF_OBJECT_ANCHOR_PROXIMITY_M = float(_os.environ.get("O4_DSF_OBJECT_ANCHOR_PROXIMITY_M", "50"))
DSF_OBJECT_HEADING_TOLERANCE_DEG = float(
    _os.environ.get("O4_DSF_OBJECT_HEADING_TOLERANCE_DEG", "0.01"))
```

**One trap, and it is in `dsf_reader.py` today.** Line 51 does `from .config import AGP_BUILDINGS` at
module import, freezing the value and defeating `monkeypatch`. Line 247's `_classify_pavement_def`
instead does a *function-local* `from .config import DSF_SURFACE_POLYGONS`, re-reading the attribute
per call. Two idioms, same file. **New flags use the function-local import** so the tests in §7 can
drive them.

**Acceptance:** `git diff --stat` touches `config.py` plus six new stub files. Suite result identical
to baseline (see §8 on the 19 pre-existing failures).

---

### W2 — `obj8_reader`
**Owns:** `src/auto_patch/obj8_reader.py`, `tests/test_obj8_reader.py`.
**Depends on:** W1. **Reads:** `tools/obj8_geometry.py`, `src/O4_Vector_Map.py:1175` (`keep_obj8`),
plan §8.5–§8.8.

Port `tools/obj8_geometry.py` essentially verbatim — it is correct and its docstrings encode the
gotchas. Then add:

1. **`positional_commands`** (plan §8.6, I-10). `LIGHT_NAMED`, `LIGHT_CUSTOM`, `LIGHT_PARAM`,
   `LIGHT_SPILL_CUSTOM`, `VLIGHT`, `SMOKE_BLACK`, `SMOKE_WHITE`, `EMITTER`, `MAGNET`. Their `(x, y, z)`
   columns differ per keyword. **Derive the column table from the OBJ8 specification and verify it
   against the objects on disk** — do not guess. Ship the table as a module constant with a comment
   citing where each row was confirmed. Offsetting `VT` but not these detaches an apron floodlight
   from its mast.
2. **`vertex_line_indices`** and `PositionalCommand.line_index` / `y_token_index`, so W5 rewrites
   tokens without re-parsing.
3. **`animation_block_count`, `level_of_detail_count`** (plan §8.7).
4. **`lonlat_to_local_offset`** — the inverse of `local_offset_to_lonlat`, needed by W4 to map a
   world-frame structure back into each object's local frame.
5. **Draped/solid vertex sharing** (I-9): if any vertex index is used by both a draped and a solid
   triangle, the object is un-correctable. Expose `has_mixed_draped_solid_vertices`.

**Do not** "clean up" `load_object_file`'s whitespace tolerance. `line.startswith("VT ")` silently
dropped 232 of 334 KCLT definitions (plan §8.8). Split on whitespace, always.

**Acceptance:**
* Tab-separated `VT`/`IDX` parse (golden: a real XPlane2Blender export fixture).
* `ATTR_draped` triangles excluded from `solid_triangles`.
* **Rotation sign golden.** `Charlotte_Airport_007_ALB.obj`'s hangar lands at `35.216591, -80.929272`.
  The wrong sign puts it 1021 m away. This test is worth more than the rest combined.
* `lonlat_to_local_offset(local_offset_to_lonlat(p)) == p` to 1e-6 m over random headings.
* Vertex welding merges duplicated seam positions into one component.
* Gap chaining: two boxes 3 m apart are one structure at gap 5, two at gap 2.
* Every `PositionalCommand` keyword round-trips its y through `y_token_index`.

---

### W3 — `mesh_sampler`
**Owns:** `src/auto_patch/mesh_sampler.py`, `tests/test_mesh_sampler.py`.
**Depends on:** W1. **Reads:** `tools/mesh_elevation_sampler.py`, `src/O4_Mesh_Utils.py:329-390`
(`write_mesh_file`) and `:880-905` (the reader).

Port, then make two changes.

1. **Remove the nearest-vertex fallback.** Raise `OutsideMeshError`; add `elevation_at_or_none`.
   See §3.2.
2. **Fix the format docstring.** The prototype and the plan both call the vertex line's 4th column a
   "tag". It is a hardcoded `0` (`O4_Mesh_Utils.py:346-355`) and is never read back. The *triangle*
   line's 4th column is a real terrain-type attribute. Triangle vertex indices are 1-based —
   confirmed at `O4_Mesh_Utils.py:901-905`.

Performance: the current implementation scans candidate triangles with a linear `numpy.nonzero` over
every retained triangle, per query. That is fine for the prototype's few hundred lookups. Phase 2 at a
large airport will do a few thousand. Measure before optimising; if it bites, a uniform grid over
triangle bounding boxes is the obvious answer, and `group_components_into_structures` already contains
the bucket idiom to copy.

**Acceptance:**
* Querying at 300 random mesh vertices returns each vertex's own elevation to 0.000000 m.
* 200/200 edge-midpoint queries fall within their endpoints' bracket.
* A point outside the retained triangles raises `OutsideMeshError` (regression against the old
  fallback).
* Synthetic two-triangle mesh fixture written by hand; no X-Plane install required.

---

### W4 — `object_anchor`
**Owns:** `src/auto_patch/object_anchor.py`, `tests/test_object_anchor.py`.
**Depends on:** W2, W3. **Reads:** plan §8.1–§8.4, spec §2.4, `tools/reanchor_kclt_terminal_bakes.py:333-487`.

The heart of the work. Three functions, all pure.

**`discover_anchor_groups`** — group placements by anchor proximity (`DSF_OBJECT_ANCHOR_PROXIMITY_M`)
**and** heading agreement (`DSF_OBJECT_HEADING_TOLERANCE_DEG`). Plan §8.1. Single-link union-find over
placements, same idiom as `group_components_into_structures`.

**`partition_structures`** — project **every object's** solid vertices into a common world ENU frame
using **its own** placement. Weld, take connected components per object, then
`group_components_into_structures` across the pooled world-frame geometry. A structure may therefore
carry triangles from several resources: that is `Structure.triangles_by_resource`. Discard draped
triangles before partitioning (I-9).

Reuse trick from the prototype (`reanchor_kclt_terminal_bakes.py:348-364`): offset each object's vertex
indices into one shared index space so `group_components_into_structures` is reused verbatim. Keep
`resource_of_shared_vertex` to map back.

**`structure_deltas`** — §2.4. For each structure:
* `ground = sampler.elevation_at(centroid_latitude, centroid_longitude)`; on `OutsideMeshError`,
  skip-and-report (I-13).
* `is_ground_touching = min_over_resources(min_y) <= DSF_OBJECT_ELEVATED_BASE_M`.
* Elevated structures inherit from their supporter (I-8): the ground-touching structure whose
  horizontal bounding box contains their centroid, else the nearest by centroid distance. Note the
  bug in the prototype's fallback at `reanchor_kclt_terminal_bakes.py:437-442` — it compares
  `candidate["centroid"][1]` (which is `centroid_z`) against `centroid_z`, correctly, but only because
  `centroid` is a 2-tuple `(x, z)`. Preserve that carefully or, better, use named fields.
* `delta[resource][vertex_index] = ground − anchor_ground_by_resource[resource]`.
* Refuse the whole group if no structure is ground-touching.

**Acceptance:**
* Two objects with **different anchors** contributing to one structure receive **different deltas**,
  and their shared world-frame vertices land at the same absolute elevation. This is the §2.4 test and
  the prototype cannot pass it.
* An elevated structure inherits its supporter's delta.
* Structures with a centroid outside the mesh are skipped, not sampled.
* Anchor grouping: two placements 10 m apart with equal heading → one group; 10 m apart with 5°
  heading difference → two groups.

---

### W5 — `object_rebake`
**Owns:** `src/auto_patch/object_rebake.py`, `tests/test_object_rebake.py`.
**Depends on:** W4. **Reads:** `tools/reanchor_kclt_terminal_bakes.py` (all of it), R1, R2.

`apply(decision, pack_root, mesh_path) -> RebakeReport`, plus `check(pack_root, mesh_path)` and
`restore(pack_root)`.

Port `_rewrite_vertex_elevations` (`:229-272`) — it preserves each `VT` line's original whitespace and
decimal precision by splitting with `re.split(r"([ \t]+)", …)` and replacing exactly one token. Extend
it to `PositionalCommand` lines using `y_token_index`.

**New: the pack-update hash guard (I-14).** The prototype's backup is created once and thereafter
trusted forever. If the user updates or re-downloads the pack, the live `.obj` is a *new original* and
the stale `.anchor_bak` is a *different building*. On every run:

```
live_sha == recorded written_sha  → normal: re-bake from backup
live_sha == recorded backup_sha   → someone restored: re-bake from backup
neither                           → THE PACK CHANGED.
                                    Move the stale backup to <name>.anchor_bak.orphaned,
                                    re-backup from live, re-bake, log loudly.
```

Also: `apply` must verify it can write to the pack before touching anything (`os.access(W_OK)` on the
directory), and refuse the whole group otherwise — a half-baked group is torn geometry.

**Acceptance (all four already verified for the prototype; keep them true):**
* **Byte-idempotent** — two consecutive `apply` runs produce byte-identical files.
* **Reversible** — `restore` yields files byte-identical to the backups.
* Only the `y` column of `VT` (and positional-command) lines changes; `x`/`z`/normals/UVs and every
  other line are byte-identical; line counts unchanged; no non-finite values.
* Provenance `check` reports `STALE` after the mesh's size or mtime changes.
* Hash guard: mutate the live file behind the tool's back → orphaned backup, loud log, correct rebake.
* `DSF_OBJECT_ALLOW_ANIM=0` and `animation_block_count > 0` → refuse-and-report, file untouched.

---

### W6 — Footprints and Phase 1 wiring
**Owns:** `src/auto_patch/object_footprints.py`, and the edits to `src/auto_patch/dsf_reader.py` and
`src/auto_patch/pipeline.py`. `tests/test_dsf_object_buildings.py`.
**Depends on:** W2 only. Runs in parallel with W3/W4/W5.
**Reads:** plan §7.1, R3, R4, spec §2.3; `dsf_reader.py:199,259,354,654-670,720-776`;
`pipeline.py:1177-1216, 2080-2085`; `terminals.py:811,888`; `tests/test_dsf_buildings.py`.

**`object_footprints.structure_ring(structure, geometry_by_resource, placements) -> list[(lon, lat)] | None`**

* Take solid vertices with `y <= min_y(structure) + DSF_OBJECT_FOOTPRINT_HEIGHT_M`. If fewer than 3
  qualify, fall back to all solid vertices of the structure (covers low flat objects).
* Project to lon/lat through each vertex's **own** placement.
* Hull (default) or, under `DSF_OBJECT_FOOTPRINT_UNION`, `shapely.unary_union` of the projected
  triangles, `buffer(0)`-repaired, exterior only, DP-simplified. (shapely and numpy are already
  dependencies; no `requirements.txt` change, and therefore no installer/ONBOARDING/wheels churn.)
* Return `None` for a non-ground-touching structure — rooftop clutter is not a building pad.
* Return `None` above `DSF_OBJECT_MAX_FOOTPRINT_AREA_M2` when the cap is enabled, and report.
* Ring is **unclosed** and in `(lon, lat)`, matching `read_dsf_buildings`'s existing contract.

**`dsf_reader.read_dsf_object_buildings(dsf_path, cache_dir=None, xplane_root=None)`**

Returns the same 3-tuple shape as `read_dsf_buildings`: `(outer_ring, holes, role)` with
`role = "object"` and `holes = []` (hull has none; the union ring drops interiors in v1).

* Reuse `_load_dsf_text` (memoised on `(abspath, mtime)`, `:354`).
* Reuse `obj8_reader.read_dsf_object_placements`, accepting only `.obj` with
  `solid_reach_metres() >= DSF_OBJECT_MIN_REACH_M`.
* **`read_dsf_buildings` does not currently call `_pack_root_for_dsf`** (only `read_dsf_pavements`
  does, `:628`). You need it: a pack-relative resource such as
  `Terminals/Hangar/Charlotte_Airport_007_ALB.obj` resolves to nothing through `library.txt` alone.
  Use `obj8_reader.resolve_object_resource(resource, pack_root, xplane_root)` — pack-relative wins,
  which is how X-Plane itself resolves it. This is the pattern already at `dsf_reader.py:199`
  (`_resource_surface_attribute`); consider lifting it into a shared helper rather than a third copy.
* **Multi-placement definitions are FINE here** and this is the asymmetry to internalise: `N`
  placements of one `.obj` are `N` buildings, each with its own footprint. Plan §8.2's refusal applies
  only to the **y-bake** (Phase 2), where the correction differs per placement and cannot be baked
  into a shared file. `otros/cone_short.obj` has 512 placements: Phase 1 may footprint them (they fail
  the reach filter anyway); Phase 2 must never touch it.
* Add a resolved-geometry cache keyed on `(abspath, mtime)`. Note that `_surface_attribute_cache`,
  `_LIB_INDEX_CACHE` and `_AGP_CACHE` have **no invalidation** — do not copy that.

**`pipeline.py` wiring.** At `:1177`, beside the existing `read_dsf_buildings` loop:

```python
if DSF_BUILDINGS and DSF_OBJECT_BUILDINGS:
    for b_outer, b_holes, _b_role in _DSFR.read_dsf_object_buildings(
            dsf, xplane_root=xplane_root):
        ...   # identical downstream: Polygon, buffer(0), to metres, bbox, centroid gate
```

Nothing else changes. Role `"object"` passes the only role test in the loop
(`if _b_role == "bridge" and not TERM_BRIDGE_GROUPING`), lands in `dsf_building_polys`, and flows
through `_cluster_dsf_building_facades` → `_combine_building_sources` → `ROLE_BUILDING` exactly like a
`.fac` facade. That is R4, and §2.3 explains why it is enough. **Refactor the two loops into one
helper rather than duplicating twelve lines.**

**Acceptance:**
* Synthetic-DSF test, harness pattern (b) from `tests/test_dsf_buildings.py:36-46`: fake `.dsf` +
  pre-seeded, mtime-backdated `.dsf.text`, `monkeypatch.setattr(D, "_dsftool_path", lambda: "/bin/true")`.
  Two synthetic `.obj` boxes under `tmp_path` → two rings, correct lon/lat, correct winding.
* Pack-relative resolution beats `library.txt`.
* Flag off → `read_dsf_object_buildings` is never called; `pipeline` output byte-identical to baseline.
* KCLT: ≥100 structures footprinted, none overlapping a runway polygon.

---

### W7 — Post-mesh stage, driver worklist, CLI
**Owns:** `src/auto_patch/post_mesh.py`, `tools/reanchor_dsf_objects.py`, and the edits to
`src/auto_patch/driver.py` and `src/O4_Tile_Utils.py`. `tests/test_post_mesh.py`.
**Depends on:** W5 **and** W6 (it writes the worklist that Phase 1's discovery populates).
**Reads:** `O4_Tile_Utils.py:287-300`, `driver.py:110-159, 485, 549-573, 668`,
`O4_File_Names.py:201`, `verification.py:1795-1874`.

**The hook.** `build_all` (`O4_Tile_Utils.py:287`) is:

```python
VMAP.build_poly_file(tile)      # auto_patch Phase 1 runs inside here
MESH.build_mesh(tile)           # writes Data<tile>.mesh
MASK.build_masks(tile)
DSF.build_dsf(tile, ...)        # reads Data<tile>.mesh back
```

Insert after `MESH.build_mesh(tile)`'s red-flag check, and in the same place in `build_tile_list`
(`:360,365`). Keep the core diff to a guarded three-line call — memory's core-footprint audit says
minimise fork changes:

```python
try:
    AUTOPATCH_POST_MESH.rebake_dsf_objects(tile)
except Exception:
    _LOGGER.exception("auto_patch post-mesh re-anchor failed")
```

Two facts that make this hook point safe, both checked:
* `sort_mesh` (`O4_Mesh_Utils.py:802`) is a **GUI-only manual action** (Shift-click,
  `O4_GUI_Utils.py:312`). It is not in `build_all`. Nothing rewrites the mesh between `build_mesh` and
  `build_dsf`.
* `Data<tile>.mesh` survives every `cleaning_level` (`O4_Mesh_Utils.py:767-793`). It is the only
  surviving intermediate.

The mesh path is `FNAMES.mesh_file(tile.build_dir, tile.lat, tile.lon)` — i.e.
`Tiles/zOrtho4XP_<tile>/Data<tile>.mesh`, **not** the `Custom Scenery/zOrtho4XP_<tile>/…` path the
prototype hard-codes (`reanchor_kclt_terminal_bakes.py:140-142`). That path is the installed symlink.
Use `FNAMES`.

**`post_mesh.rebake_dsf_objects(tile)`**
1. Return immediately unless `DSF_OBJECT_REANCHOR` (function-local config import).
2. Read `Patches/<lon lat>/o4_object_anchor_worklist.json`. Absent → nothing to do, return.
3. Per airport: verify `dsf_mtime`; if stale, re-derive the anchor groups from the DSF.
4. Build one `MeshElevationSampler` per airport, bounded by the union of the groups' reaches + 200 m.
5. `object_anchor.structure_deltas` → `object_rebake.apply`.
6. Report through the established channel: a `counts`-style dict, one `UI.vprint(1, …)` summary line,
   full detail appended to the per-tile debug log. Follow `verification.verify_and_log`
   (`verification.py:1795`): **runtime validators are pure reporters** and a broken one never fails a
   build. Log the "restart X-Plane, objects are cached" line once per pack.

**`driver.py`** — emit the worklist where `xp_root` is resolved (≈`:549`), **before** the rebuild-skip
gate at `:564`. Mind `_DRIVER_EXC` (`:22-27`): it deliberately omits `NameError`/`AttributeError`/
`ImportError` so typos and broken imports propagate. Mirror that.

**`tools/reanchor_dsf_objects.py`** — the generalised CLI, replacing the hard-coded prototype. Args:
`--worklist PATH | (--dsf PATH --mesh PATH --pack-root PATH)`, `--gap`, `--dry-run`, `--check`,
`--restore`. Proper module docstring (memory: persistent tools live in `tools/` with a docstring; `/tmp`
is temporary by nature). Keep `tools/dsf_object_anchor_audit.py` and re-point its imports at
`src/auto_patch/`; its docstring's `base err` caveat — that the metric assumes `y = 0` is the ground
plane, which stops holding once a bake is applied — must survive the move.

**Acceptance:**
* Flag off → `build_all` byte-identical, `rebake_dsf_objects` returns before reading anything.
* Hand-written worklist JSON + synthetic mesh + synthetic `.obj` under `tmp_path` → correct offsets,
  no X-Plane install.
* Worklist with a stale `dsf_mtime` triggers re-derivation.
* An exception inside `rebake_dsf_objects` never fails the tile.

---

### W8 — Measurement and sign-off
**Owns:** `docs/dsf_object_anchor_plan.md` (results update), the regression fixtures.
**Depends on:** W7. Nothing merges to `master` before this.

Four questions, each with a number attached.

**M1 — Phase 2 alone reproduces the prototype.** Restore the KCLT pack, run the pipeline with
`O4_DSF_OBJECT_REANCHOR=1` and `O4_DSF_OBJECT_BUILDINGS=0`. Per-**component** metric over the 576
ground-touching components must match plan §5.1: median ≤ 0.15 m, p90 ≤ 0.9 m, mean ≤ 0.35 m.

**Verify per component, never per structure centroid.** The centroid check is tautological — it proves
only that the offset was applied. The plan's author reported a flawless `0.000000 m` residual while the
audit simultaneously showed `001`/`002`/`003` more than 7 m off (plan §8.3). The metric is
`|(terrain(anchor) + min_y(component)) − ground_under(component)|`.

**M2 — plan open question #6: does Phase 1 drive the residual to zero?** Argued in §7.3, never
measured. Run with both flags on. The prediction is that the 8.08 m maximum — the single connected
513 m terminal complex on 8 m of mesh slope — collapses, because a flat pad now sits under it. Report
the number. **If it does not collapse, say so.** That is the most valuable finding available here, and
it invalidates §7.3 rather than the measurement.

**M3 — Phase 1 does not damage grading.** Full builds at KCLT, KDFW, HECA, CYXY, SPJC. Compare the
grade scoreboard against the current baseline (CYXY 0 · SPJC 0 · SPLP 0/tile · HECA 2+2plane ·
KDFW 41). Any regression is a Phase 1 bug, not an infeasible airport — memory's standing principle:
*no airport is legitimately infeasible; every grade violation is a solver miss.* Watch specifically for
the 112,230 m² pad (§2.3).

**M4 — plan open question #7: does the pattern generalise?** Run the audit over KDFW, CYUL, HECA.
Everything in the plan was derived from one pack. Report the count of definitions with reach > 25 m,
shared anchors, and multi-placement definitions per pack.

Also fold the corrections this spec found back into the plan: the mesh vertex 4th column is a
hardcoded `0` and not a tag (§3.2); the plan's §12 line for `is_agp_building_def` says it "rejects
`.obj`" when it in fact whitelists `.agp` by suffix; and §5's formula needs the per-object qualifier
from §2.4.

---

## 5. Invariant register

Every rule from plan §8, plus four this spec adds. Each has exactly one owner and one test. An agent
whose acceptance tests pass but whose invariants are untested has not finished.

| # | invariant | source | owner | test |
|---|---|---|---|---|
| I-1 | Object pooling = **world axis-aligned-bounding-box (AABB) overlap** of candidate placements, not anchor proximity | partition §3.0 | W4 | `test_object_anchor` |
| I-2 | Partition in **authored space**: world XZ from each object's own placement, `y` = authored `v.y` | partition §3.1 | W4 | §2.4 test |
| I-3 | y offset is per **(structure, object)**, from that object's own anchor | §2.4 | W4 | §2.4 test |
| I-4 | Phase 2 refuses any definition with >1 `OBJECT` placement | §8.2 | W5 | `test_object_rebake` |
| I-5 | Phase 1 **accepts** multi-placement definitions (N placements = N buildings) | §7.1 | W6 | `test_dsf_object_buildings` |
| I-6 | Structure = connected component of the ε-contact graph (3D AABB broad + surface narrow), ε = 0.25 m | partition §2–3 | W4 | `test_object_anchor`, M1 |
| I-7 | Never verify per-structure-centroid | §8.3 | W8 | M1 |
| I-8 | A structure with **no** ground-touching part inherits its supporter's delta (attached clutter is already subsumed by the contact graph) | partition §3.4 | W4 | `test_object_anchor` |
| I-9 | `ATTR_draped` triangles excluded from partition and never offset; mixed draped/solid vertices ⇒ refuse-and-report | §8.5 | W2, W4 | `test_obj8_reader` |
| I-10 | `LIGHT_*`/`VLIGHT`/`SMOKE_*`/`EMITTER`/`MAGNET` y coordinates move with their structure | §8.6 | W2, W5 | `test_obj8_reader`, `test_object_rebake` |
| I-11 | One delta per `ANIM_begin` block, else refuse (default: refuse) | §8.7 | W5 | `test_object_rebake` |
| I-12 | `ATTR_LOD` copies share a structure; do not let them distort the area-weighted centroid | §8.7 | W2, W4 | `test_object_anchor` |
| I-13 | A structure whose centroid lies outside the mesh is **skipped**, never nearest-vertex sampled | spec §3.2 | W3, W4 | `test_mesh_sampler` |
| I-14 | A pack update orphans the backup; detect by hash and re-backup loudly | spec §3.5 | W5 | `test_object_rebake` |
| I-15 | Byte-idempotent (read from backup) and exactly reversible | §5 | W5 | `test_object_rebake` |
| I-16 | Only the `y` token changes; whitespace, precision and line count preserved | §8.8 | W5 | `test_object_rebake` |
| I-17 | Split OBJ8 lines on whitespace, never `startswith("VT ")` | §8.8 | W2 | `test_obj8_reader` |
| I-18 | Both flags off ⇒ byte-identical build | R1–R3 | W6, W7 | A/B |
| I-19 | A structure whose **ground span** exceeds tolerance is refused and reported as needing a pad — never split | partition §3.5 | W4 | `test_object_anchor` |
| I-20 | A contact pair that cannot be **proved absent** keeps its edge (merge on doubt; tearing is unrecoverable, over-merging costs centimetres) | partition §3.3 | W4 | `test_object_anchor` |
| I-21 | Hard-tear check (V1): any two world-coincident vertices in the pooled geometry land at the same post-bake **rendered elevation** `ground(anchor(O)) + v.y_after` — NOT the same delta, which multi-anchor structures legitimately violate — verified from the *output*, independent of the partition | partition §4 | W4, W5 | `test_object_anchor` |

---

## 6. Test plan

Two harness patterns already exist in the tree; use them, do not invent a third.

**(a) In-memory line list** — `tests/test_agp_reader.py:198-214`. A module-level list of
newline-terminated DSF instruction strings with the expected verdict as an inline comment, fed
straight into a text-taking function. Works because `_read_dsf_object_placements` is typed
`lines: list[str]`, not a path. Use for `obj8_reader.read_dsf_object_placements` and for
`mesh_sampler`, whose reader should likewise accept lines.

**(b) Fake `.dsf` + pre-seeded `.dsf.text`** — `tests/test_dsf_buildings.py:36-46`. Write a placeholder
`.dsf` and a real `.dsf.text` under `tmp_path`, **backdate the `.dsf` mtime** so `_load_dsf_text`
reuses the cache, and `monkeypatch.setattr(D, "_dsftool_path", lambda: "/bin/true")`. Use for
`read_dsf_object_buildings`.

Fixtures to build once, in `tests/fixtures/obj8/`, and share:

* `two_boxes_tab_separated.obj` — tab-delimited `VT`/`IDX`, two 10 m boxes 3 m apart.
* `draped_decal.obj` — one `ATTR_draped` quad, y range 0.000..0.000. Modelled on
  `ground_marks/llantas_conjunto.obj`, which produced a bogus 10 m finding before I-9 existed.
* `lit_mast.obj` — a box plus a `LIGHT_NAMED` at y = 12. I-10's canary.
* `animated_door.obj` — one `ANIM_begin` block. I-11.
* `two_lod_hangar.obj` — `ATTR_LOD` copies. I-12.
* `flat_two_triangle.mesh` — hand-written, known elevations. W3.

Integration gates (W8): M1's per-component metric is a **regression gate**, checked in as a fixture of
expected quantiles. Idempotency, `--restore` byte-equality, and provenance `STALE`-after-rebuild are
all already verified for the prototype — keep them green.

Before blaming any failure on this work: `dev` carries **19 pre-existing suite failures**
(stash-verified). Bisect, or A/B against a stash, before fixing. Build values are deterministic, but
`.osm` node **emission order** is hash-seed dependent — pin `PYTHONHASHSEED` for byte-level A/B.

---

## 7. Sequencing and merge protocol

```
  W1 ────────────────────────────────────────────── merge to dev, alone
   ├── W2 ──┬── W4 ── W5 ──┐
   ├── W3 ──┘              ├── W7 ── W8
   └── W6 ─────────────────┘
```

* **W1 merges alone.** Everyone rebases on it. Contracts in §3 are frozen at that moment.
* **W2, W3, W6 run concurrently.** Disjoint files. W6 needs only `obj8_reader`'s signatures, which W1
  supplied as stubs — it can start immediately and integrate against W2's real implementation on
  rebase.
* **W4 then W5** — sequential, W5 consumes `RebakeDecision`.
* **W7 last.** It is the only workstream touching `driver.py`, `pipeline.py`'s neighbours, and
  `O4_Tile_Utils.py`. If W6 and W7 must overlap, W6 owns `pipeline.py` and W7 owns `driver.py`; they
  do not both edit either.

**Git, and this has bitten before.** Parallel agent sessions commit into the **same checkout**.
`git add -A` will sweep up another agent's half-finished work. **Stage by explicit path, always.** If
you use a worktree, be aware that `conftest.py` defeats worktree isolation — the main `src/` lands at
`sys.path[0]`, so your tests may be importing someone else's module.

Each workstream: one commit, own files only, message names the workstream and the invariants it
closes. Do not commit or push unless asked.

---

## 8. Risk register

| risk | severity | mitigation |
|---|---|---|
| Per-object delta collapsed to per-structure (§2.4) | **tears geometry** | I-3 test with two differing anchors; the prototype cannot pass it |
| Rotation sign flipped | **1021 m error** | W2 golden test against `007`'s hangar at `35.216591, -80.929272` |
| `startswith("VT ")` reintroduced | silently drops 70% of objects | I-17 |
| Nearest-vertex fallback returns a plausible wrong elevation | silent | I-13; the fallback is deleted, not guarded |
| Pack update orphans `.anchor_bak` | corrupts a paid pack | I-14 hash guard |
| 112,230 m² flat pad fights apron grading | grade regressions | M3; `DSF_OBJECT_MAX_FOOTPRINT_AREA_M2` escape hatch |
| Mesh path taken from `Custom Scenery/` symlink not `tile.build_dir` | bakes against the wrong mesh | W7 uses `FNAMES.mesh_file` |
| Worklist not written when the rebuild-skip gate fires | Phase 2 silently no-ops | W7 writes it before `driver.py:564` |
| Module-level `from .config import …` freezes the flag | tests cannot drive it | W1: function-local imports for all new flags |
| Verification measures the centroid | reports 0.000000 m while 7 m wrong | I-7; the plan's author already made this mistake |

---

## 9. Definition of done

* Both flags default off; with both off the build is byte-identical to `dev`. (I-18)
* Every invariant I-1…I-18 has a passing test naming it.
* M1 reproduces plan §5.1's numbers through the pipeline rather than the prototype.
* M2 is **answered with a number**, whichever way it falls.
* M3 shows no grade-scoreboard regression at KCLT, KDFW, HECA, CYXY, SPJC.
* M4 reports the audit over three further packs.
* `tools/obj8_geometry.py` and `tools/mesh_elevation_sampler.py` deleted; `dsf_object_anchor_audit.py`
  re-pointed; `reanchor_kclt_terminal_bakes.py` replaced by `reanchor_dsf_objects.py`.
* `docs/dsf_object_anchor_plan.md` updated: open questions #1 (closed by R1), #2 (staged by R3), #6
  (answered by M2), #7 (answered by M4); the three factual corrections from §4/W8.
* No new dependency, therefore no `requirements.txt` / installer / ONBOARDING / wheels change.

---

## 10. Amendments — 2026-07-08 review

A second-model review of this spec plus `docs/obj8_structure_partition.md` against the code facts
gathered the same day. **Where an amendment conflicts with text above or with the partition document,
the amendment wins.** Each is tagged with the workstream it binds.

### A1 — Module placement: the partition is W2's, and Phases 1 and 2 must share it *(W1, W2, W4, W6)*

The contact-graph partition is mesh-free, and both W6 (footprints are per *structure*) and W4 (deltas
are per structure) need it. The §4 dependency claim "W6 needs only W2" was true when structures came
from `group_components_into_structures` and silently became false when the partition moved to W4's
`partition_structures`. Repair, not workaround:

* New module `src/auto_patch/obj8_partition.py`, **owned by W2**, exporting `weld_parts`,
  `contact_graph(parts, epsilon)` (broad 3D-AABB + surface narrow phase), and
  `connected_structures`. W1's stubs include it.
* W4's `partition_structures` becomes a thin composition over it; W4 keeps deltas, inheritance,
  ground-span classification. §2.1's module map and §3.1's contract are amended accordingly;
  `group_components_into_structures` is **not** ported.
* Consequence that §7.3 quietly requires: **Phase 1 pads and Phase 2 deltas must be computed from the
  same partition.** A pad flattened under a differently-partitioned structure is not flat under the
  structure the delta seats. Sharing the module makes this structural rather than aspirational.

### A2 — I-14 orphan rule would destroy the KCLT originals; migration required *(W5 — critical)*

The Nimbus pack is **live-baked today** by the prototype: `.anchor_bak` originals exist and the
provenance JSON is prototype-format, recording **no hashes**. Applied naively, §3.5's orphan rule
("live matches neither recorded hash → move backup aside, re-backup from live") fires on first
contact, enshrines the *baked* files as originals, and orphans the true ones. Amended rule:

```
recorded hashes exist  → three-way logic as specified (I-14)
no recorded hashes     → NEVER orphan.  An existing .anchor_bak is authoritative
                         (prototype semantics: created once from the pristine file).
                         Adopt it, compute both hashes, upgrade provenance to v1.
no backup at all       → live file is the original; back it up; proceed.
```

`test_object_rebake` gains a fixture reproducing the exact live state: baked files + prototype
provenance + backups.

### A3 — I-19 semantics: bake-and-flag, not refuse *(W4, W5; rewrites partition doc §3.5)*

Partition doc §3.5 says a structure whose ground span exceeds tolerance is "refused". But its own §1
table (max 7.18 m) was measured with the 513 m terminal complex *baked*, and the user's in-sim
verification included it baked. Refusing reverts the complex to anchor elevation — roughly 3 m median
error everywhere, strictly worse than a zero-centred ±8 m tail. Amended:

* **Always bake the best single delta** (a large structure then behaves exactly like a correctly
  anchored standalone object — the plan §5's stated caveat), **and** report `needs_pad` with the
  ground span when span exceeds `DSF_OBJECT_PAD_FLAG_SPAN_M` (default 2 m).
* Reserve do-not-bake for the chain pathology only, decided by arithmetic, not thresholds: skip iff
  the single-delta residual distribution is worse than the uncorrected one (both computable before
  writing anything).
* I-19 is amended to match. `Structure.is_correctable` becomes `needs_pad: bool` + `skip_reason: str | None`.

### A4 — Hook inside `build_mesh`, not its callers *(W7)*

Hooking `build_all`/`build_tile_list` misses the GUI's per-step Mesh button
(`O4_GUI_Utils.py:312` → `MESH.build_mesh` directly) and Shift-click `sort_mesh` — which rewrites the
mesh. The 1.19 m staleness incident (plan §7.4) came from exactly such an out-of-band rebuild. Amended:
one guarded call at the **end of `O4_Mesh_Utils.build_mesh`** (after `write_mesh_file` and cleanup) and
one at the end of `sort_mesh`. One core-file touchpoint instead of two, strictly better coverage;
`build_dsf` ordering is unaffected because Phase 2 never writes the mesh.

### A5 — Worklist slimmed; main process writes it *(W7)*

§3.4's schema stores anchor groups, which requires parsing every candidate `.obj` at layout time — and
placing that before the rebuild-skip gate (§2.2) charges an all-current tile the full OBJ parse cost
every build. Amended: the worklist carries **identification only** —
`{icao, dsf_path, dsf_mtime, pack_root, xplane_root}` — and `post_mesh` performs discovery itself
(its geometry caches key on `(abspath, mtime)`, so repeat builds are cheap, and the stale-mtime
re-derivation path in §2.2 stops being a special case because derivation is the only path). Airports
build in a ProcessPool (`driver.py:668`): the worklist is written **once, by the main process**,
never by workers.

### A6 — Provenance keys per (pack, mesh), not per pack *(W5)*

A pack whose corrected objects span more than one tile bakes against more than one mesh. The sidecar's
top-level `mesh`/`mesh_size`/`mesh_mtime` become a `meshes` map keyed by tile, and each object entry
names its tile. The eight-bake case is single-tile; do not bake that assumption in.

### A7 — Theorem exactness caveat *(W2 docstring; no design change)*

The anchor cancellation in partition doc §2.1 equates our mesh sample at the anchor with X-Plane's
render-time terrain sample. Those agree only up to DSF elevation-pool quantization
(`build_dsf` re-encodes `Data<tile>.mesh`), a per-object constant of ~centimetres. Within one object
the assembly is exact regardless (single anchor). State it in `obj8_partition.py`'s docstring so nobody
chases a 2 cm "tear" across objects.

### A8 — Contract-drift tripwire *(W1)*

W1 additionally ships `tests/test_contracts.py`: `inspect.signature` assertions over every §3 (as
amended) name. Any agent that drifts a frozen contract breaks one shared test immediately, instead of
breaking a sibling's integration two workstreams later.

### A9-preamble — naming ruling broadened *(all workstreams)*

User ruling 2026-07-08: **no abbreviations anywhere** — code, documents, tables, discussion, commit
messages. Domain proper names (DSF, OBJ8, OSM, DEM) are names, not abbreviations; technical acronyms
(AABB, ENU) are expanded at first use per document. Statistical notation (p50, p90) is notation. The
`_M`/`_M2` suffix survives **only** on `config.py` constants, matching the surrounding idiom (R6);
every new function parameter and dataclass field writes `_metres` / `_square_metres` in full.

### A9 — Oracle before implementations *(W2, W8)*

The two scratchpad probes behind the partition doc's §1 table are promoted to
`tools/obj8_partition_audit.py` **as part of W2**, not W8: Pareto table (fidelity vs tears per
partition), ε-plateau scan, V1 hard-tear check in its amended rendered-elevation form (I-21). W4
develops against this oracle; W8 reruns it per pack. It reads `.anchor_bak` originals when present so
it stays truthful on a live-baked pack.

### A10 — Contracts and flags as actually landed by W1 *(supersedes the §3 signatures and the §4 W1 flag block where they differ)*

W1 is the contract-landing step, so the amendments are reconciled *there*, once, rather than leaving
each downstream agent to merge §3 against §10 mentally. The deltas:

**Flags.**
* `DSF_OBJECT_CONTACT_EPSILON_M` (default `0.25`) **replaces** `DSF_OBJECT_STRUCTURE_GAP_M`,
  `DSF_OBJECT_ANCHOR_PROXIMITY_M` and `DSF_OBJECT_HEADING_TOLERANCE_DEG`. Structures are the connected
  components of the ε-contact graph (I-6) and pooling is world-AABB overlap with the same ε margin
  (I-1) — the gap and the anchor-proximity/heading knobs have nothing left to tune.
* `DSF_OBJECT_PAD_FLAG_SPAN_M` (default `2`) added per A3: a baked structure whose ground span exceeds
  it is reported `needs_pad`.
* Everything else exactly as listed in §4 W1.

**Renames and moves.**
* `AnchorGroup` → `ObjectPool`, `discover_anchor_groups` → `discover_object_pools` — pooling is a
  world-geometry property, not an anchor property (I-1). `discover_object_pools` takes the parsed
  geometry (it needs world bounding boxes), with an `epsilon_metres` margin.
* `src/auto_patch/obj8_partition.py` (A1) owns `weld_parts`, `contact_graph`, `connected_structures`;
  `weld_parts` subsumes §3.1's `connected_components`, and `group_components_into_structures` is not
  ported at all.
* `Structure` fields per A3 and the partition document §5: `ground_span_metres: float | None`
  (None until Phase 2 has a mesh), `needs_pad: bool`, `skip_reason: str | None`,
  `inherited_from_structure_index: int | None`; `surface_area_square_metres` written in full
  (A9-preamble).

`tests/test_contracts.py` (A8) asserts the *amended* signatures; where this list and any earlier
section disagree, that test file is the tiebreak.

### A11 — Second gate pack: HECA (Tai Models), and the ground-plate filter it forces *(W6-follow-up, W8)*

User installed `Custom Scenery/c_EGY - 100_airport - HECA Cairo (Tai Models)` (2026-07-08) — a
different developer, authored against one flat elevation, with a built `+30+031` mesh on disk.
Audited: **341 object definitions, 3,222 terrain-draped placements, 201 definitions needing
re-anchoring** (KCLT: 57), worst base error **+38 m**, reaches to 2.7 km. **189 objects share ONE
anchor** — and the bakes are split **by material** (`door.obj`, `brick.obj`, `concrete_1/2/3.obj`,
`black_glass_NOANPHA.obj`, `titles_1.obj`…), so a single terminal's structure will span *dozens* of
resources, exercising pooling and multi-resource structures far harder than KCLT's eight
texture-page bakes. Dozens of `no base` files (signs, ceilings, glass) hold no ground-level geometry
at all and only make sense pooled — the real test of inheritance (I-8). Three anchor groups, one at
heading 359.79° vs 0.00° — the world-frame pooling case (I-1, I-2). Multi-placement cases present
(`redWhiteBarier_P2.obj` × 27, `jet_Blash_*` × 3 — I-4/I-5).

M4 is therefore answered on installed packs: **HECA (Tai Models) joins KCLT as a mandatory W8 gate**,
and every W8 measurement (M1-style per-component metric, M3 grade scoreboard) runs on both.

**The design gap it exposes:** `Airport/ground/heca_ground_polygon.obj` — reach 2,139 m, base error
+26 m — is a *solid ground plate*, not a building. Phase 2 must y-bake it (a mis-elevated ground
plate is exactly a float/sink artifact), but Phase 1 must **not** lay a 2 km building pad under it,
and the area cap defaults to disabled. Principled fix, since a building has walls and a plate does
not: `structure_ring` returns `None` for a structure whose vertical extent
(`maximum_y − minimum_base_y`) is below `DSF_OBJECT_MIN_BUILDING_HEIGHT_M` (new flag, default
`2.5`). Signs, decals and plates fall out naturally; every real building passes. Flag added to the
A10 roster and the contract test.

**Baseline caution:** `_pick_best_apt_dat_against_osm` prefers any Custom Scenery pack containing
the airport, so installing this pack may silently change which `apt.dat` the HECA build selects —
the HECA grade-scoreboard baseline must be **re-cut before** M3 comparisons, or a shifted baseline
will masquerade as a Phase 1 regression.

### A12 — Third gate pack: LEMD (Aerosoft, FSX conversion) — the multi-anchor case at scale *(W4, W8)*

User named LEMD as a sloped-field case with visibly floating buildings (2026-07-08). Pack:
`Custom Scenery/Aerosoft - LEMD Madrid - 1 - Airport`, DSF `+40-004`, built mesh on disk. Audited:
**356 definitions, 2,962 terrain-draped placements, 244 needing re-anchoring** (HECA: 201,
KCLT: 57), reaches to **3.8 km**, base errors **−50.00 m to +28.33 m** — 142 definitions sunken,
39 floating, exactly the two-sided signature of a genuinely sloped field (KCLT and HECA err
one-way).

**What LEMD alone provides:** two co-anchored families of **121 and 97 objects whose anchors sit
roughly 6 m apart** (40.492764,−3.564788 vs 40.492820,−3.564793, both heading 0) at the same
terminal complex. Their geometry will interleave into shared structures, making LEMD the first
*production* instance of the spec-section-2.4 case — a structure spanning objects with different
anchors, where the per-(structure, object) delta is not a theoretical nicety but the difference
between seated buildings and sheared ones. The I-3/I-21 verification (equal post-bake rendered
elevation, unequal deltas) has its real-world test here. KCLT's 41 terminal-layer objects were the
hypothetical; LEMD ships 218.

Also present: `LEMD_OBJ-Ground-FSX-*.obj` solid ground plates (~−10 m sunken), confirming the A11
vertical-extent filter generalises beyond one pack; and a third authoring toolchain (FSX
conversion) after Nimbus's native X-Plane workflow and Tai Models — the M4 generalisation question
now spans three developers.

**W8 gate set is now KCLT + HECA + LEMD.** Every measurement (per-component residual, hard-tear
check, grade scoreboard) runs on all three. Same apt.dat baseline caution as A11.

**Oracle results (same day, `tools/obj8_partition_audit.py` on the OldTerminal pool, 1,638
ground-touching parts):**

| partition | structures | residual p50 | >0.5 m | abutment separations |
|---|---|---|---|---|
| no correction | – | **10.01** | 1638/1638 | – |
| 2D gap 2 m (prototype) | 44 | 2.58 | 1334 | 0 |
| contact graph ε 0.25 m | 624 | **0.76** | 1071 | 2658, max 7.25 m |

Three findings, none blocking Wave 2:

1. **The theorem holds on the multi-anchor pack**: 35,235 shared world positions, zero torn,
   rendered-elevation spread 0.000000000 m — across the 6-metre-apart anchor families.
2. **Phase 1 pads matter far more here than at KCLT.** Post-correction residual p50 is 0.76 m
   against KCLT's 0.08 m: LEMD's ground genuinely varies *within* structures. This is the §7.3
   mechanism at 10× the KCLT magnitude; M2's measurement is the main event at LEMD.
3. **The V3 induced-separation question (partition document §6.2) is now urgent, and ε has no
   universal knee.** 2,658 pairs of parts within a 25 cm box gap end in different structures and
   separate by up to 7.25 m. On a slope, *some* of that is correct — adjacent separate buildings
   must sit at different elevations, and opening that gap is the point of the fix — but pairs that
   are really one building (FSX conversions abound in close-but-not-touching assembly) are visible
   tears. The ε scan shows a smooth decline at LEMD (0.02 → 2,239; 0.25 → 624; 1.0 → 320) with no
   KCLT-style knee. W8 must produce the V3 report per pack and decide between a larger default ε,
   a per-pack ε, or weak-contact analysis — measured in-sim, not argued.

### A13 — Wave 2 escalation resolutions *(manager patch after W4/W5 merge)*

Three items workstream W5 escalated rather than resolving unilaterally, settled here:

1. **`RebakeDecision.anchor_by_resource`** (new field, `dict[str, (latitude, longitude,
   heading_degrees)]`, default empty). `apply` has no placements, so the provenance sidecar's
   per-object `anchor` was unobtainable on fresh bakes. `structure_deltas` populates it from the
   pool's placements (one line — it holds them); `apply` records it, falling back to a
   prototype-era recorded anchor during A2 migration. The contract-tripwire field list is updated
   in the same commit, per the amendment protocol.
2. **Invariant I-4's enforcement point is Phase 2 discovery (workstream W7), not the rebake
   layer.** The decision carries no placement counts; `post_mesh` must exclude any resource with
   more than one terrain-draped `OBJECT` placement before building decisions (mirroring
   `read_dsf_object_buildings`'s singleton-pool handling on the Phase 1 side, where multi-placement
   is *accepted*). `object_rebake` keeps its only-available defence: two decision resources
   normalising to the same on-disk file are both skipped.
3. **`check()` for a version-1 sidecar lacking the queried mesh's tile returns `"STALE"`**, not
   `"NONE"`: a bake exists but not against this mesh, and under ruling R2 the cheap re-bake is
   always the right response. Documented as the defined semantics.

### A14 — Discovery reads the backup, and two W7 findings *(post-W7)*

1. **Phase 2 discovery parses `<name>.anchor_bak` when it exists, falling back to the live file.**
   The section-4-W7 pipeline as literally written (resolve → parse the live `.obj`) is **not
   idempotent on a live-baked pack**: the second run reads already-offset geometry, sees base
   ``y ≈ +delta``, and misclassifies every corrected structure as elevated. This is ruling R1
   applied consistently — geometry is *always* read from the backup — and `object_rebake.apply`
   and the audit oracle already behaved this way; vertex ordering is identical in both files so
   deltas line up. Locked in by W7's full-path idempotency test.
2. **Per-pool sampler construction re-reads the whole `Data<tile>.mesh`.** Harmless at KCLT's pool
   count; at HECA/LEMD (dozens of pools) it may dominate Phase 2 runtime. W8 measures; the fix, if
   needed, is a per-mesh-path read cache under the sampler without touching its contract.
3. The sampler retains only triangles with **a vertex** inside the query bounds, so a coarse mesh
   whose giant triangles merely overlap the window yields "no triangles inside bounds".
   Irrelevant on dense airport meshes; `post_mesh` treats it as a per-pool skip-and-report.

### A15 — Base and global scenery are never rebaked *(found live in the first production run)*

The first KCLT production build revealed a policy gap: the tile's small airports resolve to the
**Global Airports** DSF, whose static airliners and library hangars pass the 25 m reach floor —
a large, *correctly anchored* object has a large reach; the metric conflates size with
mis-anchoring — and Phase 2 attempted to pool objects inside the base simulator install. Only
`apply`'s unwritable-directory precheck stopped a write into `Global Scenery`. On a writable
install (common on macOS) that would have modified the base simulator, and baking a **shared
library object** with one airport's offsets would corrupt it for every other airport using it.
Permission luck is not policy. Two guards, both in the shared discovery function (so the
command line inherits them):

1. **Pack guard**: a `pack_root` with a `Global Scenery` or `Resources` path component is
   skipped-and-reported wholesale. Only Custom Scenery packs are rebake candidates.
2. **Containment guard**: a resource whose resolved physical path is not **inside** the pack that
   owns the DSF (i.e. it resolved through `library.txt` into another pack) is skipped-and-reported;
   pack-local siblings still bake.

Corollary recorded for honesty: the reach floor is a *mis-anchoring detector* with false positives
on large correctly-anchored objects. Inside a custom pack those false positives are benign — the
centroid-to-anchor delta of a correctly anchored structure is approximately zero, so the bake is a
no-op — which is why no further filtering is needed there.

### A16 — First in-sim findings at KCLT, diagnosed *(follow-up work toward the dev merge)*

The user's in-sim pass found two defects; both are diagnosed with production data, neither blocks
the merge, both are recorded here with fix designs.

1. **Detached buildings float above their groundside pavement** (user report at
   35.205352, −80.9300188; measured: three building pads pinned flat at 213.3–213.6 m beside a
   groundside surface spanning 204.6–212.6 m). Now that Phase 1 supplies buildings completely
   separated from airside pavement, a `ROLE_BUILDING` pad with no airside/terminal coupling takes
   its flat altitude from a source that ignores the groundside surface it abuts, and no route
   profile reaches it. **Fix design:** a building pad whose boundary abuts groundside pavement and
   which has no airside coupling welds its flat altitude to the groundside surface along the shared
   edge (minimum or contact-length-weighted mean), and the groundside solve treats the pad edge as
   an anchor so the two agree exactly. Solver work in the groundside/building-frontage area — the
   existing "spine does not rise to serve a building across an apron" family.
2. **A valid footprint was dropped at pipeline admission** (user report at 35.2172031, −80.927082).
   The structure exists, is ground-touching, 12 m tall, and `structure_ring` returns a 15-vertex
   ring — but no pad reaches the patch. The admission path's one silent dropper is the
   boundary-CENTROID gate, and the location sits at the airport's edge among boundary ribbons.
   **Fix design:** for the OBJ8 source, admit when the footprint polygon *intersects* the boundary
   gate rather than requiring centroid containment (parameter on the shared admission helper —
   `.fac` behavior unchanged), and report per-source admission counts so gate drops are visible.
3. **Degenerate-edge not-a-number in the narrow phase** (found live during the HECA build,
   fixed same day): a duplicated triangle corner made the point-triangle edge test divide 0/0, and
   the not-a-number propagated through the pair's minimum — flipping genuine contact to
   proved-apart, in violation of I-20. Sanitised to contact; regression-tested. The KCLT
   eight-bake pool was unaffected (byte-identical partition), but minor pools changed: the pack
   was re-applied with fixed code, and **KCLT owes one fresh full cycle** so Phase 1 pads and the
   Phase 2 bake share one partition again (amendment A1's invariant).

### A17 — Building districts as graded surfaces *(design, user-initiated after the HECA in-sim pass)*

HECA's groundside carries practically a small town of buildings on a slope. Per-building flat pads
at per-building elevations produce a terraced patchwork: steps between neighbouring pads, buildings
seated consistently *within* a pad but discontinuously *across* them. The user proposes larger pads
or another smoothing technique. A single large flat pad is ruled out by measurement (plan section
4.2: one elevation across sloped ground = perimeter retaining walls, up to 15 m in the original
test). The design that fits the existing solver:

**District surfaces.** Cluster building pads whose spacing is below a district gap (~30–50 m,
measured, not guessed) into a *district*: one groundside-role surface spanning the cluster's
union region, solved with the existing groundside grade rules (smooth, 4% caps, welded to adjacent
groundside pavement per A16.1). Individual building pads then sit FLAT at their *local district
elevation* — inset into a gently graded town surface instead of terraced against raw terrain.
Phase 2 samples the district surface, so every structure in the town seats consistently and the
ground between buildings flows.

One mechanism then covers three open concerns:
* A16.1 — detached buildings weld to groundside (a district IS the weld, generalised);
* the V3 abutment-separation worry — neighbouring structures' grounds become continuous, so
  adjacent buildings stop stepping;
* this A17 town case.

Alternative considered: keep per-building pads and couple neighbouring pads in the grade graph
(maximum step between adjacent pads, in the spine-fairing style). Cheaper, but it smooths pad
elevations without smoothing the terrain BETWEEN pads, so the town still looks terraced from the
ground. District surfaces are the recommendation.

Separate, unresolved by this: the elevated-railway mega-chain (hinge cut, I-19) and the
authored-buried-base fork (base-seating delta variant) — both pending in-sim calibration
coordinates.

### A18 — OBJECT_AGL is subject to the distant-anchor disease; the plan's exclusion was wrong *(found by user in-sim calibration at HECA)*

The plan (section 2 and section 8.8) and every reader built on it deliberately skip ``OBJECT_AGL``:
"carries an explicit elevation and so is not subject to the distant anchor problem." **False.** An
AGL placement resolves to ``terrain(anchor) + elevation`` — terrain-relative *at the anchor only*.
Geometry a kilometre from the anchor inherits the anchor's terrain exactly like a plain ``OBJECT``,
offset by a constant. HECA ships **183 of its 216 AGL placements on the single 189-object family
anchor** (user's floaters measured 2/10/20 m; predicted ``anchor_ground − local_ground − 0.52``
matches). KCLT's 2,480 AGL placements are ≤3 per anchor, compact — which is why the assumption
survived every KCLT gate. LEMD has zero.

Fix: ``ObjectPlacement`` gains ``above_ground_level_metres`` (default ``0.0``; plain ``OBJECT``
rows keep it zero); the reader accepts ``OBJECT_AGL`` rows (heading in column five); the delta
becomes ``ground(S) − (ground(anchor(O)) + above_ground_level_metres)``; Phase 1 and Phase 2
discovery accept AGL placements under the same reach floor and I-4 counting. ``OBJECT_MSL``
remains excluded (zero instances across all three packs) but is now *reported* when present,
never silent. Contract tripwire updated in the same commit per protocol.

### A19 — Structure-skip visibility and the A3 guard on mega-structures *(same calibration)*

Forty-nine plain-OBJECT HECA resources (reaches 1.2–1.9 km) were absent from decisions AND from
the skip report — structure-level skips (A3 arithmetic, blocking-resource, outside-mesh) never
surfaced at the resource level, a no-silent-caps violation. And the A3 do-not-bake guard evaluated
a mega-structure's correction using the ground at its *centroid* — one unrepresentative sample for
a kilometre-wide web — rather than the best available single offset. Fix bundle:

1. A structure's seating elevation, when it has ground-touching parts, is the **median of those
   parts' grounds** (the optimal single rigid offset), not the centroid sample. Centroid remains
   the fallback for part-less and inherited structures.
2. Every structure-level skip surfaces into the skip report with the affected resources.
3. The A3 guard applies only below a structure-diameter bound (a mega-web always bakes with its
   best offset and flags ``needs_pad``); the full mega-web fix remains the hinge cut (A16/A17
   queue, now demanded by both HECA's terminal web and LEMD's railway chain).

### A20 — Normalize elevation mode to on-ground *(user ruling 2026-07-08, refined by measurement)*

User ruling: adapted airports must not inherit manual ``OBJECT_AGL`` / ``OBJECT_MSL`` elevation
values from the old airport's terrain assumptions. Measurement refines "all": KCLT carries 1,938
AGL placements above +3 m (to +22.8 m) — deliberately elevated mast and rooftop objects that
on-ground forcing would sink through their hosts — while the ±1 m band (HECA 187, KCLT 449) is
exactly the hand-tweaked-against-old-terrain class.

Design: for everything the pipeline BAKES, amendment A18's delta already cancels the authored
offset exactly; normalization targets the un-bakeable residue (multi-placement, animation-refused,
sub-reach objects). Transform ``OBJECT_AGL idx lon lat elev heading`` to ``OBJECT idx lon lat
heading`` when ``|elev|`` is within ``DSF_OBJECT_ON_GROUND_BAND_M`` (default 1 m); keep larger
offsets; report any ``OBJECT_MSL`` loudly (zero instances across the gate packs). This crosses the
DSF-rewrite line for the first time: DSFTool text round-trip (measured ~7 cm pool quantization,
plan section 4.3) with the full safety kit — DSF backup, provenance hashes, byte-idempotent
re-derivation from the backup, ``--restore`` — behind a default-off flag
(``O4_DSF_OBJECT_NORMALIZE_ELEVATION_MODE``), soak-tested per pack. Queued with A17 and the hinge
cut; pull forward only if the un-bakeable ±1 m residue proves visually significant in-sim.

### A21 — Per-structure baking within a partially-skipped resource *(2026-07-16 KBNA calibration)*

Amendment A19's resource-level visibility entry ("N structure(s) skipped (others baked)") landed
in ``RebakeDecision.skipped``, and ``object_rebake.apply`` refuses every resource listed there —
so ONE tiny structure tripping the A3 residual guard un-baked its whole resource. Real case:
``KBNA_Terminal-part13``/``part33`` in "US-KBNA Nashville Airport" are each one huge terminal
structure (thousands of vertices, delta well-determined) plus 1–3 tiny detached pieces; a ~2 mm A3
loss on a tiny piece left the whole terminal floating (then reverted-to-authored by the exclusion
reversion pass — correct, but strictly worse than baking the good structures). 26 of the KBNA
pack's resources were in this state.

Fix: baking is per STRUCTURE. ``structure_deltas`` already computes deltas per (structure,
object) and writes none for a skipped structure, so ``delta_by_resource_and_vertex`` is exactly
the partial bake; the aggregation now lists a resource in ``skipped`` only when EVERY structure
carrying it was skipped (resource-level refusals — I-9, I-13 anchor, I-4 — are unchanged).
``apply`` bakes the passing structures' vertices from ``.anchor_bak`` as always — the skipped
structures' vertices simply carry no delta and keep their authored y — and byte-idempotence
(I-15) holds unchanged. Positional commands inside a skipped structure's box take offset 0
(authored); an ``ANIM`` block spanning a skipped and a baked structure still refuses the resource
(differing offsets, I-11).

Visibility moves with the bake: the report gains ``partially_baked`` (resource, summary), the
per-airport console line counts them, and the provenance sidecar entry of a partially-baked
object records each skipped structure (centroid, surface area, reason) under
``structures_skipped`` — per-resource entries carry per-structure detail. ``structures_baked``
no longer counts a skipped structure whose resource was written for its siblings. Multi-placement
refusal (I-4) is untouched: it is resource-level and upstream of pooling.
