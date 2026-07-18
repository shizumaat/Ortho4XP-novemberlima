# Build-time program board — drive to ≤60 s (OTHH class)

Status document for the per-airport build-time optimization program.
Owner targets (canonical: CLAUDE.md working-style item 6 and
`docs/specs/flat-airport-fast-path-spec.md` §3.5): **≤60 s cold
per-airport auto-patch wall at the OTHH/KDFW class, ≤10 s typical,
downloads excluded.** Enforcement: `tools/check_build_time.py` against
`tools/build_time_baselines.json`.

This board is the cross-session continuation point. Update the track
table (§4) when work lands; keep the retrospective (§2) as history.
Created 2026-07-18 from the four-audit retrospective (session
20260718, lead Fable 5); condensed findings inline so no session
transcript is needed to continue.

## 1. Measured state (2026-07-18, dev 0834fef)

**Current baselines (24d36f3, measured at 06f83ab): OTHH 380.5 s
(solve 222.3, emit-incl-tail 118.9), CYXY 49.2 s.** The store
undercount (record_build ran before the late projection + densify) is
FIXED in 06f83ab — the tail now lands in the emit phase.

**✓ 2026-07-18 PM anomaly RESOLVED (forensic run under X-Plane load —
counts valid, wall times not; quiet re-baseline still pending).**
Verdict: **T1a deferral ENGAGES** — late call `[scoped: 124 deferred,
23 expanded]` (was 26 pre-T1a); late contamination is bounded and real
(mid-fairing-moved 3,154 + value-changed 813 + zone halo from only 63
geometry-changed shapes → 7,628 of 131,864 nodes). Mid call defers
just 27 (solve→mid churn is huge: 759 geometry-changed shapes,
31,483 contaminated — expected). **The +19 s lives in the recapture
itself**: the new `snapshot` stage rivals the `seed` stage per call
(12.5 s mid + 13.9 s late under load ≈ 4.5–5 s quiet each ⇒ ~9–10 s
per build). The LATE-call recapture had no consumer (nothing projects
after it) — **removed** (`recapture_snapshot=False` at the late call
site, `final_grade_projection` keyword; CYXY stash A/B body
byte-identical, provenance-timestamp-only diff). Open question for a
QUIET machine: whether the MID-exit recapture (~5 s) buys back its
cost — the late `constraints` stage barely moved (16.9 s vs mid
16.3 s) despite 124 deferrals, i.e. deferred shapes are mostly cheap
small aprons. A/B = revert 7617e2e vs HEAD on a quiet machine before
any further T1a-class investment. Quiet re-baseline of CYXY+OTHH also
queued (expect OTHH ≈ −4–5 s from the dead-recapture removal).

OTHH sampled profile (413 s wall incl. ~8 % sampler overhead; report
regenerable via `tools/profile_airport_build.py OTHH` — keep its
`PHASE_STARTS` line numbers in sync with `pipeline.py` `_progress.step`
sites, they drift):

| Cost center | Sampled s | Where |
|---|---|---|
| per_surface_solve (constraint graph + chromatic solve) | 144 | `pipeline.py:5926` → `solve.py` / `one_solve.py` / `grade_graph.py` |
| final_grade_projection ×2 (mid 38.8 + late 39.9) | ~79 | `pipeline.py:6360` + `pipeline.py:6745` |
| _decompose_airside_holed_shapes (hole router, in solve phase) | 32.4 | `pipeline.py:5431` → `pavement/hole_router.py` |
| construct_adjacent_ground_presolve (reach band 27.2) | 31.4 | `pipeline.py:5908` → `adjacent_ground.py` |
| stitch_pavement_to_terminals | 15.8 | `pipeline.py:4501` |
| emit_gap_fill_spines | 14.8 | `pipeline.py:6525` |
| construction phases 3–4 | ~44 | rects/junctions/service |
| Leaf GEOS predicates across all of the above | ~148 | `contains` 83.8, `distance` 41.1, `intersects` 22.9 |
| Shapely Python-layer accessors | ~57 | `coords`/`exterior`/`has_z`/`has_m` |

## 2. Retrospective — why the program underdelivered (2026-07-18 audits)

Four parallel audits (plan-vs-actual accounting; wave 2c internals;
wave 3 internals; projection dedup study). Verdicts:

- **Wave 2 raster band fields: the one clean win** (band machinery
  74 → 1.2 s at OTHH) — but it *spent the plan's premise*: the
  "~700 s of band machinery" pool §3.5's arithmetic mines no longer
  exists. Remaining cost is flat and many-headed.
- **Wave 2c chromatic GS shipped incomplete vs its own survey**
  (`docs/research/routing_optimization_survey.md`): survey says color
  "once per airport (frozen graph)" with an active-set; implementation
  recolors on EVERY `feasibility_project` call (~9–12/build + per lazy
  round, sole call site `one_solve.py:548`) and the greedy coloring is
  **O(Σ write_degree²)** — quadratic at hubs (`forbidden |= s` +
  linear color scan, `one_solve.py:348-350`; lazy-expanded all-pair
  bodies make 300-node aprons into write-degree-299 near-cliques).
  ~32 s of its 38 s at OTHH is coloring; the numpy sweeps cost ~4 s.
  Never A/B'd at OTHH (only CYXY, where coloring is milliseconds).
- **Wave 3 shipped one lever of four at one site**: 423a53c vectorized
  only the hole-router visibility adjacency (55.7 → 31.5 s, 1.8× — the
  Θ(n²) pair count is untouched; 13 calls at OTHH, largest shapes
  dominate; per-pair prepared-GEOS `contains` is the wall). Prepared/
  STRtree bulk queries, single-pass restructuring, emit vectorization:
  never built. The spec's own acceptance (OTHH re-profile) was skipped
  at merge.
- **Tier 2 can structurally never fire at OTHH**: refusal set
  (`route_profile/flat_airport_fast_path.py:90-126`) includes crossing
  zones, bridge/tunnel plates, gap-fill spines, adjacent-ground bands —
  OTHH always has all four (`fast-path=refused(crossing-terrain zone
  present)`). Tier 1 partial laziness only (aprons 56 %, junctions
  76 %, seats 57 % certified). Spec §4.4 acceptance is unmeetable as
  written → needs an owner ruling (track T7).
- **Tier 3 narrowing, WSPD certificates, covering-radius, FH field:
  never started** (the "Tier 3 wave" commits were kernel replacements,
  not §3.4 narrowing).
- **~100 s of current cost was never planned**: the double final
  projection (late call added 2026-07-17, post-spec), adjacent-ground
  presolve, terminal stitching, gap-fill emission, and the chromatic
  coloring overhead the program itself introduced.
- **Projection dedup study**: warm seeding never helped because each
  `final_grade_projection` call rebuilds registry, grade context
  (incl. the per-shape law memo `ctx._sc_memo` — zero pair-work shared
  between solve/mid/late), law pairs, hard sets, coloring, and fairing
  adjacency regardless of violation count. The scoped-deferral
  machinery (`solve.py` `_scoped_projection_defer_ids`, default ON)
  defers almost nothing at the late call **only because the snapshot
  is never recaptured after the mid call** (capture sites: solve
  writeback ~1423 and fast path only) — OTHH defers 26 of ~350+ soft
  shapes against bounded real churn (335 weld vertices in 121 shapes).
- **Process root causes**: acceptance gates measured correctness at
  small airports instead of wall time at the target class; post-spec
  pipeline growth was unbudgeted (item 6 enforcement now exists);
  the store undercount hid the late projection from all phase numbers.

Accounting verdict: completing only the remaining *planned* work lands
OTHH ≈150–180 s. Reaching 60 s additionally requires the never-planned
items and a pair-generation collapse (§4 tracks T4–T6).

## 3. Verification discipline (all tracks)

- **No concurrent builds, ever** (200 GB incident 2026-07-18): check
  `ps aux | grep -E 'Ortho4XP|pytest|full_airport'` AND store
  `finished_at` timestamps before benchmarking.
- Benchmarks: one warm-up `tools/full_airport_build.py <ICAO>` per
  airport, then `venv/bin/python tools/check_build_time.py --run
  [--update-baselines] CYXY OTHH`; commit baselines only with the
  change that paid for them, explicit paths (`git commit -- <paths>`).
- Byte-identical changes: same-path stash A/B, foreground. Fixpoint-
  changing changes: counts-not-worse gate via `tools/check_grade.py`
  on CYXY first (ruled first test airport), then OTHH/HECA/EGLL/SPJC.
- Projection instrumentation exists: `O4_PROJ_TIMING=1` per-stage
  split (`solve.py` `_stage`), `O4_STEP_DEBUG=1` `[fp-chromatic]` and
  `[scoped:]`/`[scoped-scope]` deferral counters.
- **Correctness runs are LEDGERED (owner directive 2026-07-18):** run
  pytest / airport builds / check_grade via `venv/bin/python
  tools/run_with_ledger.py -- <command>` — results persist in
  `tools/run_ledger.jsonl` keyed by code-tree hash + argv + `O4_*` env,
  and an identical already-green run is skipped instead of re-run
  (`--history N` to inspect; `--artifact <path>` records OSM body
  hashes so byte-identity A/Bs can compare against the ledger without
  rebuilding the reference side). Timing runs (`check_build_time
  --run`, profilers) are NEVER wrapped or cached.

## 4. Track board

Status values: DONE / IN FLIGHT (session running 2026-07-18) /
QUEUED (specified, not started) / NEEDS RULING.

| # | Track | Expected at OTHH | Gate | Status |
|---|---|---|---|---|
| T0 | Move `record_build` after late projection + re-baseline | measurement integrity | tests + check_build_time | DONE 06f83ab + 24d36f3 (OTHH 380.5 / CYXY 49.2) |
| T1a | Late-projection deferral fix: recapture snapshot at projection exit (fairing-diff keys + `_capture_projection_snapshot` after `_writeback`, `solve.py` ~2520-2592; new `snapshot` stage in `O4_PROJ_TIMING`) | −12–20 s | counts-not-worse; `[scoped:]` deferral counts before/after (mid line unchanged, late ~26 → majority deferred) | COMMITTED 7617e2e; forensics 2026-07-18 PM (§1): deferral ENGAGES (late 26 → 124 deferred) but the recapture ≈ seed-stage cost ×2 calls explains most of the +19 s; dead LATE-call recapture removed; MID-recapture net value unproven — quiet A/B (revert 7617e2e vs HEAD) queued |
| T1b | Drop the MID projection behind new `O4_FINAL_PROJECTION_MID` gate (wrap `pipeline.py:6360`); reorder `relevel_pads`/ribbon/groundside fixups after late call | −~20 s if counts hold | one-env-var A/B after T1a lands; watch torn-weld class + frozen-feature bake values | QUEUED |
| T1c | Vectorize late-only hard-set scans (strip freeze `solve.py:2025-2031`, runway-boundary `2056-2060`) with STRtree `dwithin` | EGLL-class win (late 46–56 s there) | byte-identical | QUEUED |
| T2a | Chromatic: vectorized feasibility pre-check (skip coloring when already feasible) + exact-greedy hub coloring (per-node next-free counters, identical partition) + vectorized per-color array build (`one_solve.py`) | −25–30 s | byte-identical; oracle equality tests | COMMITTED 101d7b5 (oracle-equality + 60-instance exact A/B, 0 mismatches; hub coloring 876 ms -> 3.9 ms; quiet-machine A/B queued) |
| T2b | Chromatic ON/OFF A/B at OTHH (never run at target class; legacy worklist's active-set may win on nearly-feasible calls) | verdict on keeping chromatic | wall + counts, after T2a | QUEUED |
| T2c | Incremental coloring across lazy rounds (prefix-stable extension) | few s | byte-identical | DONE — included in 101d7b5 (state carried across lazy rounds) |
| T3a | Hole router R1 `relate_pattern` boundary prune + R2 sampled `contains_xy` prefilter (`pavement/hole_router.py` :259-307; scalar oracle untouched) | −10–14 s | adjacency-equality tests vs scalar oracle (5 new parity tests incl. sub-eps overlap kept + prefilter-miss case) | COMMITTED 2099b03 (26 tests green; synthetic ~1.8-2.0×; quiet-machine A/B queued) |
| T3b | R3 chunk threading (shapely-2 releases GIL; **per-thread prepared copies mandatory — shared prepared geometry segfaults GEOS 3.13**) | residual ÷ ~cores | byte-identical (ordered chunk assembly) | QUEUED |
| T3c | R4 `mid_edge` exclusion from pair enumeration (blocked nodes provably untraversed, `hole_router.py:784-861`; extras exempt; v1 paths keep full graph) | up to 4× on the router | cuts-parity test + counts A/B | QUEUED |
| T4 | Law-graph construction sharing: persist/reuse pair generation (`ctx._sc_memo`) across solve + both projections for canonically-unchanged shapes; then WSPD certificates (`docs/research/` cross-domain S1) if still needed | attacks the ~83 s `shape_constraints`/`classify_pair` + ~150 s leaf GEOS mass | counts-not-worse | QUEUED (design needed — the memo is context-lifetime today, `grade_graph.py:1396-1399`) |
| T5 | Adjacent-ground presolve on the raster field (reach band 27.2 s, `adjacent_ground.py` `_build_construct_reach_band`); stitching (15.8 s); gap-fill emission (14.8 s) | −30–45 s combined | per-item | QUEUED (never planned before; needs profiling-informed design) |
| T6 | Wave 3 levers (b)–(d): prepared/STRtree bulk queries, single-pass restructuring, emit vectorization; shapely accessor overhead (~57 s) | part of geometry ≤40 s | byte-identical where possible | QUEUED |
| T7 | Tier 2 refusal semantics at OTHH class: partial fast path vs retire §4.4 acceptance | unlocks or retires whole-airport collapse | — | NEEDS OWNER RULING |

Arithmetic: T1–T3 as specced ≈ −60–85 s → OTHH ~300 s true. T4–T6
carry the rest of the distance; 60 s is NOT reachable without T4 (pair
generation) and T5 (the never-planned emitters). Re-profile after each
wave lands (`tools/profile_airport_build.py OTHH`) — at the target
class, not CYXY.

## 5. Session continuation protocol

1. Read this board + `tools/build_time_baselines.json` provenance.
2. Verify machine quiet (§3) before any measurement.
3. Pick the highest OPEN track; agent briefs for T1–T3 exist in the
   2026-07-18 session; each brief must carry a build-time impact
   statement (item 6).
4. On landing: update §4 status, re-run `check_build_time`, commit
   code + baselines + this board together, explicit paths.
