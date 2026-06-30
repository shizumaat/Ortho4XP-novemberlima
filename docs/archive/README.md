# docs/archive — superseded & retired plan docs

This folder holds plan documents whose work is **done-by-other-means, abandoned, or
superseded by a later architecture**. They are kept for history, not as live plans.

Produced by the 2026-06-30 docs/ plan audit. Any *still-relevant* unfinished items
that were buried in these (or in the in-place superseded docs below) have been lifted
into **[../OPEN_ITEMS.md](../OPEN_ITEMS.md)** — start there for live work.

## Physically archived here (no inbound code references)
| Doc | Status | Why |
|---|---|---|
| `interior_path_entries.md` | RETIRED | Documents `auto_patch/interior_path.py` + the `O4_INTERIOR_PATH` gate, both **deleted** in the audit cleanup (orphaned by the route_profile/grade_law rewrite). The across-grass concern it targeted is now handled by `grade_law` reach-bands. |
| `junction_smoothing_plan.md` | ABANDONED | Proposed `_detect_taxiway_shoulders` / `TAXIWAY_SHOULDER_EXTENT` — never built, no successor. The HECA junction-bump problem moved to the grade_law / one-graph work. |
| `heca_zero_violations_plan.md` | SUPERSEDED | Its 5-step HECA-to-0 plan (incl. the Step 5 route-justified validator ruling) was never built as written; the 72-violation census predates the one-graph + route_profile + anisotropic-edges rewrite that pursued the same goal differently. |

## Superseded but kept IN PLACE (still referenced by source comments)
These carry a ⚠ SUPERSEDED banner at their top; they were left in `docs/` because live
code comments cite them by path. Successor in parentheses.

- `route_field_model.md` (→ network_profile → route_profile → `anisotropic_edge_handling_plan.md`)
- `network_profile_model.md` (→ route_profile / grade_graph → `anisotropic_edge_handling_plan.md`)
- `route_profile_solver_status.md` (→ grade_law / one-graph consolidation; live model = `anisotropic_edge_handling_plan.md`)
- `apron_back_edge_ramps.md` (→ `taxi_slack_terminals.md` / `TAXI_SLACK_TERMINALS`)
- `apron_terminal_attraction_plan.md` (never built → `taxi_slack_terminals.md`)
- `grade_law_consolidation_handover.md`, `_2.md`, `_3.md` (→ `grade_law_consolidation_handover_4.md`)
- `single_grade_graph.md` (DONE — model is live; solve relocated into `route_profile`/`grade_graph`)
- `m4_constraint_graph_findings.md` (resolved by commit `e22e39e` + grade_law unification)
- `auto_patch_tier2_plan.md` (Features A/B done-by-other-means; C/D never built — see OPEN_ITEMS)
- `TEST_PLAN_SPJC.md` (manual protocol obsolete; SPJC now covered by pytest fixtures)
