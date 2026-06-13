# Hangar pads — implementation plan

**Ruling (user, 2026-06-12):** hangar buildings join the patches as pads,
treated like terminals — aprons weld edges with them and grade smoothly to
their edges.

**Rulings register (user, 2026-06-12, supersedes the v1 draft):**

1. **`ROLE_TERMINAL` is renamed `ROLE_BUILDING`** to avoid confusion (the
   role covers terminals, hangars, towers — any flat building pad).
2. **Hangars feed into the SAME list as terminals** and are treated
   identically. No provenance field, no hangar-specific rules.
3. **A taxilane that intersects a hangar STOPS at the building edge** and
   shares edge/nodes/elevation with the pad for a smooth transition — just
   like aprons do. (Most hangars have apron on at least one side.)
4. **Hangars get groundside, same rules as terminals**: pavement that
   touches the building but does NOT connect back to a runway through
   pavement is groundside.
5. **Gate ON once implemented** so the user can test in-sim.

**Model statement:** a hangar is a building-class structure: a flat,
fixed-floor building standing on (or beside) apron pavement. The
surrounding apron must arrive at its edges smoothly, and the pad takes the
level the settled apron hands it (the s80 apron-follows model). Nothing
about the *elevation* treatment is hangar-specific — the work is the
rename, ingestion, and the taxilane-stops-at-the-door rule.

---

## 1. Current state (verified, dev @a692c63)

- Building pads come **exclusively** from auto_patch's own OSM load
  (`aeroway=terminal` ways + multipolygon relations) via
  `_extract_osm_terminals` (`terminals.py:547`). The driver's
  `building_data` dict is looked up but **never forwarded** — no change
  needed; the pipeline's own OSM cache already contains every `aeroway=*`
  feature, including hangars.
- **Hangars are already pads at some airports**: when an airport has NO
  explicit `aeroway=terminal`, the extractor falls back to
  `{terminal, hangar, tower}` (`terminals.py:574–584`, user 2026-04-28).
- The fallback guard exists for a *measured* reason: at airports WITH
  terminals (SPJC), hangars "overlap pavement and would cause overlap-clip
  to malform sloping rects." Ruling 3 resolves this properly: the lane
  stops at the building; pads never fight rects for the same area.
- Everything downstream of pad creation is **role-driven and generic**:
  weld + full conformance (`pipeline.py:208–229`), depressed-road carve
  (`pipeline.py:1754–1804`), junction emit subtracting the pad union, and
  the whole phase-2 lifecycle — TRANSPARENT entry (`unified_jacobi.py:1727`),
  INHERIT median→flat with complex grouping (`:2096–2337`), pairwise grade
  resolution at 150 m cutoff, measured acceptance (`:2507–2559`),
  corridor-plane attractor, FIELD apron-lane passes (`network_profile.py`),
  and `_warn_terminal_chord_law`. ~75 `ROLE_TERMINAL` references
  package-wide.
- `BuiltShape.role` IS serialized into emitted patches
  (`layout.py:767` `"role": s.role`); `tools/check_grade.py` reads it back
  through `ROLE_GRADE_LIMITS`; compare-target fixtures embed
  `role='terminal'`.

## 2. Step A — rename `ROLE_TERMINAL` → `ROLE_BUILDING` (ruling 1)

Rename both the constant and its value (`"terminal"` → `"building"`):

- `layout.py:190` constant + `AEROWAY_FOR_ROLE` key (aeroway output value
  is already `"building"` — unchanged on disk there).
- `config.py:540` `ROLE_GRADE_LIMITS` key → `"building"`; KEEP a legacy
  `"terminal"` alias key (same limit object) so `check_grade` still
  validates pre-rename patches on disk.
- Literal `"terminal"` sites: `verification.py:43,491`, `bridges.py:252`,
  `finalize.py:111`, `pipeline.py:3296`, `interior_path.py:58`.
- Tests: `test_junction_invariants.py:271`, `test_junction_rules.py:352`,
  `test_compare_target.py:104,148` baseline keys.
- `tools/compare_target.py`: normalize legacy `role='terminal'` →
  `"building"` when READING target fixtures, so targets need no re-cut.
- `network_profile.py` has ZERO references — no collision with the
  concurrent uncommitted work there.
- Function/variable names that say "terminal" (`_extract_osm_terminals`,
  `terminal_union`, `_warn_terminal_chord_law`, `TERMINAL_*` config knobs)
  are NOT renamed in this pass — the ruling targets the ROLE constant;
  a broader identifier sweep would collide with uncommitted WIP and
  churn the diff. Can follow up later if wanted.

Verification: full suite; compare-target green via the read-side
normalization. Emitted patches change bytes (`role='building'`) — expected
and user-ordered; Ortho4XP itself consumes the `aeroway` tag, which is
unchanged.

## 3. Step B — ingestion: hangars into the same pad list (ruling 2)

Gate `HANGAR_PADS` (env `O4_HANGAR_PADS`, **default ON** per ruling 5;
OFF = pre-change behavior, byte-identical — proof required).

`_extract_osm_terminals`: when the gate is ON, `aeroway=hangar` is ALWAYS
in the accepted tag set (terminal-airports included). `tower` keeps the
existing fallback-only behavior (only when no explicit terminal exists).
Existing filters reused unchanged: ≥100 m² area, centroid inside the
row-130 boundary, ≥2 m vertex simplification. No dedupe machinery needed —
one extraction site, one tag-set decision.

## 4. Step C — taxilanes stop at the building edge (ruling 3)

The lane ends at the hangar door and welds to the pad edge, exactly as
rects end against aprons today:

1. **Centerline trim (pre-rect-build):** subtract the building-pad union
   from taxi centerlines before rect construction, so each rect's end
   chord lands ON the pad boundary. Implementation point: where
   centerlines are clipped against the apt.dat pavement union for Phase
   C0 (investigate `pavement/centerlines.py` / `pavement/rects.py` for
   the exact site). Segments fully inside a pad drop; segments crossing
   it split at the boundary.
2. **Rect-vs-pad overlap precedence — pad wins:** rects built from
   trimmed centerlines may still overlap a pad (rect half-width sweeps a
   corner). Clip rect polygons against the pad union the same way rects
   yield to runways today (investigate the existing fixed-shape
   overlap-clip `_drop_overlap_against_fixed_shapes` — add pads to the
   fixed set or mirror its mechanism).
3. **Weld + conformance (existing machinery):** the unify pass already
   welds near-coincident airside vertices and inserts full conformance —
   the trimmed rect end shares nodes with the pad edge, giving one
   altitude per shared vertex = the smooth transition of ruling 3.
4. **Junction emit:** unchanged — pad union is already subtracted from
   junction coverage, so no junction paints over a hangar.

## 5. Step D — groundside (ruling 4)

No special-casing: with hangars in the same pad list under the same role,
`groundside.py`'s probes treat them as terminals automatically — pavement
touching a building pad with no pavement route back to a runway is
groundside. Verify on a fixture build (the rule as restated by the user
matches the existing terminal groundside logic; confirm, don't assume).

## 6. Validation protocol

- Gate-off byte-identical on all fixture builds (after the Step-A rename
  lands as the new baseline).
- Suite vs baseline 326p/3f — the 3 reds are the known pre-existing set
  (compare_target_spjc, pavement_grade SPLP + HECA); no new failures.
- Invariants: HECA 05C 108.70, 05L 57.9–62.8 smooth, KPHL rows co-level,
  CYXY+SPJC grade gates green, deterministic across PYTHONHASHSEED.
- Gate-on measurement per fixture: pad counts, walls/within counts vs
  baseline, chord-law warn census, per-axis audit.
- Compare-target: read-side legacy normalization (Step A) keeps targets
  valid; gate-on shape-count drift inside the ±5 % floors is expected —
  re-cut targets only if floors trip AND the new output is verified good.
- Ship: gate ON (ruling 5), user tests in-sim.

## 7. Risks

- **Sloping-rect malformation** (the 2026-04-28 class) — addressed at the
  root by ruling 3: lanes stop at the door, pads and rects never contest
  area. The rect-end-on-pad-edge weld is the new surface to watch
  (degenerate short rects when a centerline barely pokes into a pad —
  drop rects under the existing min-length).
- **Pad-count blowup at GA fields** (T-hangar rows): INHERIT pairwise
  resolution is O(n²) on pad complexes. Measure on fixtures; cap only if
  real.
- **Chord-law warn noise**: hangars served by lanes add warns
  (validator-only); read the census before declaring green.
- **Rename fallout**: any external script grepping `role='terminal'` in
  emitted patches breaks — mitigated by legacy alias on every read path
  in-repo.
