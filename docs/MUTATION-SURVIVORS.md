# Mutation survivors

The first 90 rows come from the survivor report of [run 44121537739](https://github.com/KristofferHM/flightops-mcp/actions/runs/44121537739). The rows for `dashboard/board.py` come from [run 44397823805](https://github.com/KristofferHM/flightops-mcp/actions/runs/44397823805) and, the last seven, from [run 44688906150](https://github.com/KristofferHM/flightops-mcp/actions/runs/44688906150). Ids are those of run 44688906150, the latest read; its `mutation-report` artifact has every diff. mutmut renumbers mutants as code moves, so rows are matched by function and change, not by id.

## Run 44688906150: 303 survivors against a ceiling of 99

The aircraft branch (`feat/aircraft-layer`, PR #17). Counts in that run: killed: 2892, no tests: 61, survived: 242.
"No tests" counts as surviving, so 303. All 303 diffs were read and each was judged on its own.

| Survivors | Verdict |
|---:|---|
| 99 | Same id and same change as a row already judged here: the 90 in `auth/` and the credit ledger, and 9 in `board.py`. Compared mechanically, function, id, removed line and added line. Judgement unchanged. |
| 61 | **Dead code, deleted.** All 61 `no tests`: `orthographic` (47), `on_near_side` (12), `cluster_level` (1), `unknown_shown_by_default` (1). |
| 97 | **A test file that never ran.** Killed by tests that already existed, once `tests/test_aircraft_layer.py` is in mutmut's selection. |
| 39 | **Missing tests**, written in `tests/test_board_arithmetic.py`. |
| 7 | New and judged behaviour-identical: the seven rows at the end of the table below. |

Ceiling arithmetic: **was 99. 99 of the 303 are rows already judged here. 61 removed with the dead code. 97 killed
by putting the existing test file in the selection. 39 killed by new tests. 7 new survivors judged equivalent.
99 + 7 = 106. Ceiling now 106.** The 106 are the 106 rows of the table at the end of this file.

### Why 204 were new

**The branch's own tests were not in the selection.** `[tool.mutmut] pytest_add_cli_args_test_selection` names
the test files mutmut runs. `tests/test_aircraft_layer.py`, which holds the tests for every function the branch
added to `board.py`, was never added to it. mutmut ran each new function against tests that do not call it, and
reported the 61 functions nothing else reaches as `no tests`. With the file in the list, 151 of the 204 new mutants
die without a new test, 54 of them in the dead functions below.

**Four of the new functions had no caller.** `orthographic`, `on_near_side`, `cluster_level` and
`unknown_shown_by_default` were Python copies of rules the page runs every frame in `board.js` (`aircraftPoint`,
`place`, `clusterCell`, `unknownShown`). Nothing outside the tests called them, so their tests proved the copy and
not the page. The copy had already drifted: `on_near_side` let a point 1e-9° past the limb through, `place` does
not. They are deleted, not tested. Their four tests in `tests/test_aircraft_layer.py` now read the page's own code,
as `test_the_page_culls_aircraft_with_the_airports_rule` already did:

| Was | Now |
|---|---|
| `test_an_aircraft_is_lifted_radially_by_its_altitude` (`board.orthographic`) | `test_the_page_lifts_an_aircraft_radially_and_an_unknown_altitude_not_at_all` (`aircraftPoint` in `board.js`) |
| `test_a_far_side_aircraft_is_culled_whatever_its_altitude` (`board.on_near_side`) | `test_the_page_culls_aircraft_with_the_airports_rule`, unchanged: it already held the page's `place` |
| `test_clusters_shrink_as_the_reader_zooms_in` (`board.cluster_level`) | same name, holds `clusterCell` and that the levels shrink |
| `test_unknowns_are_hidden_by_default_only_at_wide_zoom` (`board.unknown_shown_by_default`) | same name, holds `unknownShown`; the world-poll test pins the zoom the payload sends |

**The 39 missing tests** are invariants, not today's numbers (`tests/test_board_arithmetic.py`): the angle agrees
with an independent dot-product formula over 400 random pairs; an airport exactly on the edge of the view is in it;
a cap of zero or less asks about nothing; every airport is in exactly one cluster, counted once, and each cluster
sits in one cell of the grid; the badge is the worst category; altitude, speed and lift come through `_aircraft` and
anything not a finite number is unknown, never 0; the time heard is UTC on a host that is not.

**How the verdicts were checked.** mutmut forks per mutant and does not run on Windows, where this was done. Each of
the 213 board mutants was replayed from its diff in the CI report: applied to a copy of `board.py`, run against the
board's test files with `TZ=UTC` as on CI, then restored. Replayed against the commit CI mutated, with the old
selection, 210 of the 213 survive, as in the run. Of the three that died, the two `_heard_utc` zone mutants died
because that replay ran in this machine's zone, not UTC, and `x_clusters__mutmut_81` died once under load and
survives when run alone. Against this commit: 61 no longer apply, 136 die, and the 16 that live are the 9 old rows
and the 7 below.

A kill under load is not evidence on its own: `x_angular_distance_deg__mutmut_13` also died once by accident, in a
replay with the old tests, and is a missing test (it survives every old file alone). So every kill counted here
was run again alone: its named test, the first to fail, fails on the mutant and passes on this commit.

### All 204 new survivors

Status is the run's: `su` survived, `no` no tests. "selection" means killed by a test that already existed, once its
file is in the selection; "new test" means killed by a test written for it. The test named is the first that fails.

| Mutant | Status | Change | Verdict | Killed by, or why |
|---|---|---|---|---|
| `x__aircraft__mutmut_11` | survived | `_measure(entry.get("baro_altitude_m"))` → `None` | killed: new test | `test_board_arithmetic.py::test_altitude_speed_and_lift_come_from_the_answer` |
| `x__aircraft__mutmut_12` | survived | `entry.get("baro_altitude_m")` → `None` | killed: new test | `test_board_arithmetic.py::test_altitude_speed_and_lift_come_from_the_answer` |
| `x__aircraft__mutmut_13` | survived | `"baro_altitude_m"` → `None` | killed: new test | `test_board_arithmetic.py::test_altitude_speed_and_lift_come_from_the_answer` |
| `x__aircraft__mutmut_14` | survived | `"baro_altitude_m"` → `"XXbaro_altitude_mXX"` | killed: new test | `test_board_arithmetic.py::test_altitude_speed_and_lift_come_from_the_answer` |
| `x__aircraft__mutmut_15` | survived | `"baro_altitude_m"` → `"BARO_ALTITUDE_M"` | killed: new test | `test_board_arithmetic.py::test_altitude_speed_and_lift_come_from_the_answer` |
| `x__aircraft__mutmut_50` | survived | `entry.get("velocity_ms")` → `None` | killed: new test | `test_board_arithmetic.py::test_altitude_speed_and_lift_come_from_the_answer` |
| `x__aircraft__mutmut_51` | survived | `"velocity_ms"` → `None` | killed: new test | `test_board_arithmetic.py::test_altitude_speed_and_lift_come_from_the_answer` |
| `x__aircraft__mutmut_52` | survived | `"velocity_ms"` → `"XXvelocity_msXX"` | killed: new test | `test_board_arithmetic.py::test_altitude_speed_and_lift_come_from_the_answer` |
| `x__aircraft__mutmut_53` | survived | `"velocity_ms"` → `"VELOCITY_MS"` | killed: new test | `test_board_arithmetic.py::test_altitude_speed_and_lift_come_from_the_answer` |
| `x__aircraft__mutmut_56` | survived | `altitude` → `None` | killed: new test | `test_board_arithmetic.py::test_altitude_speed_and_lift_come_from_the_answer` |
| `x__heard_utc__mutmut_4` | survived | `valid` → `(valid) or True` | killed: new test | `test_board_arithmetic.py::test_an_unusable_contact_time_has_no_time_of_day_and_no_age[None]` |
| `x__heard_utc__mutmut_7` | survived | `UTC` → `None` | killed: new test | `test_board_arithmetic.py::test_the_time_heard_is_utc_whatever_the_hosts_zone` |
| `x__heard_utc__mutmut_9` | survived | `UTC` → `(removed)` | killed: new test | `test_board_arithmetic.py::test_the_time_heard_is_utc_whatever_the_hosts_zone` |
| `x__measure__mutmut_3` | survived | `math.isfinite(value)` → `(math.isfinite(value)) and False` | killed: new test | `test_board_arithmetic.py::test_altitude_speed_and_lift_come_from_the_answer` |
| `x__measure__mutmut_4` | survived | `math.isfinite(value)` → `(math.isfinite(value)) or True` | killed: new test | `test_board_arithmetic.py::test_a_figure_that_is_not_a_finite_number_is_unknown_never_zero[nan]` |
| `x_airports_in_view__mutmut_6` | survived | `180.0` → `181.0` | equivalent | `angle-ceiling` |
| `x_airports_in_view__mutmut_7` | survived | `+` → `-` | killed: selection | `test_aircraft_layer.py::test_the_view_is_a_circle_that_crosses_the_antimeridian` |
| `x_airports_in_view__mutmut_25` | survived | `<=` → `<` | killed: selection | `test_aircraft_layer.py::test_a_world_poll_asks_about_600_airports_and_draws_under_120_markers` |
| `x_airports_in_view__mutmut_30` | survived | `0` → `1` | killed: new test | `test_board_arithmetic.py::test_a_cap_of_zero_or_less_asks_about_nothing_and_still_counts[0]` |
| `x_angular_distance_deg__mutmut_5` | survived | `-` → `+` | killed: selection | `test_aircraft_layer.py::test_a_world_poll_asks_about_600_airports_and_draws_under_120_markers` |
| `x_angular_distance_deg__mutmut_7` | survived | `-` → `+` | killed: selection | `test_aircraft_layer.py::test_a_world_poll_asks_about_600_airports_and_draws_under_120_markers` |
| `x_angular_distance_deg__mutmut_12` | survived | `/` → `*` | killed: selection | `test_aircraft_layer.py::test_a_world_poll_asks_about_600_airports_and_draws_under_120_markers` |
| `x_angular_distance_deg__mutmut_13` | survived | `2` → `3` | killed: new test | `test_board_arithmetic.py::test_the_angle_agrees_with_an_independent_formula` |
| `x_angular_distance_deg__mutmut_16` | survived | `*` → `/` | killed: selection | `test_aircraft_layer.py::test_a_world_poll_asks_about_600_airports_and_draws_under_120_markers` |
| `x_angular_distance_deg__mutmut_21` | survived | `/` → `*` | killed: selection | `test_aircraft_layer.py::test_the_view_asks_about_the_nearest_airports_up_to_the_cap` |
| `x_angular_distance_deg__mutmut_22` | survived | `2` → `3` | killed: selection | `test_aircraft_layer.py::test_the_view_is_a_circle_that_crosses_the_antimeridian` |
| `x_angular_distance_deg__mutmut_26` | survived | `2` → `3` | killed: selection | `test_aircraft_layer.py::test_a_world_poll_asks_about_600_airports_and_draws_under_120_markers` |
| `x_angular_distance_deg__mutmut_32` | survived | `1.0` → `2.0` | equivalent | `root-clamp` |
| `x_board_airport__mutmut_30` | survived | `"iata":` → `"XXiataXX":` | killed: new test | `test_board_arithmetic.py::test_a_reported_airport_keeps_its_codes_and_type` |
| `x_board_airport__mutmut_31` | survived | `"iata":` → `"IATA":` | killed: new test | `test_board_arithmetic.py::test_a_reported_airport_keeps_its_codes_and_type` |
| `x_board_airport__mutmut_32` | survived | `"iata"` → `None` | killed: new test | `test_board_arithmetic.py::test_a_reported_airport_keeps_its_codes_and_type` |
| `x_board_airport__mutmut_33` | survived | `"iata"` → `"XXiataXX"` | killed: new test | `test_board_arithmetic.py::test_a_reported_airport_keeps_its_codes_and_type` |
| `x_board_airport__mutmut_34` | survived | `"iata"` → `"IATA"` | killed: new test | `test_board_arithmetic.py::test_a_reported_airport_keeps_its_codes_and_type` |
| `x_board_airport__mutmut_35` | survived | `"type":` → `"XXtypeXX":` | killed: new test | `test_board_arithmetic.py::test_a_reported_airport_keeps_its_codes_and_type` |
| `x_board_airport__mutmut_36` | survived | `"type":` → `"TYPE":` | killed: new test | `test_board_arithmetic.py::test_a_reported_airport_keeps_its_codes_and_type` |
| `x_board_airport__mutmut_37` | survived | `"type"` → `None` | killed: new test | `test_board_arithmetic.py::test_a_reported_airport_keeps_its_codes_and_type` |
| `x_board_airport__mutmut_38` | survived | `"type"` → `"XXtypeXX"` | killed: new test | `test_board_arithmetic.py::test_a_reported_airport_keeps_its_codes_and_type` |
| `x_board_airport__mutmut_39` | survived | `"type"` → `"TYPE"` | killed: new test | `test_board_arithmetic.py::test_a_reported_airport_keeps_its_codes_and_type` |
| `x_cluster_level__mutmut_1` | no tests | `<` → `<=` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_cluster_text__mutmut_3` | survived | `category` → `None` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_cluster_text__mutmut_4` | survived | `and` → `or` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_cluster_text__mutmut_6` | survived | `or` → `and` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_cluster_text__mutmut_8` | survived | `" · ".join(parts` → `"XX · XX".join(parts` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_cluster_text__mutmut_9` | survived | `"no report heard"` → `"XXno report heardXX"` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_cluster_text__mutmut_10` | survived | `"no report heard"` → `"NO REPORT HEARD"` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_2` | survived | `key = ( math.floor((airport["latitude"] + 90.0) / cell_deg), math.floo…` → `key = None` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_4` | survived | `/` → `*` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_5` | survived | `+` → `-` | equivalent | `whole-cell-offset` |
| `x_clusters__mutmut_8` | survived | `90.0` → `91.0` | killed: new test | `test_board_arithmetic.py::test_each_cluster_sits_inside_one_cell_of_the_grid[10.0]` |
| `x_clusters__mutmut_10` | survived | `/` → `*` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_11` | survived | `+` → `-` | equivalent | `whole-cell-offset` |
| `x_clusters__mutmut_14` | survived | `180.0` → `181.0` | killed: new test | `test_board_arithmetic.py::test_each_cluster_sits_inside_one_cell_of_the_grid[10.0]` |
| `x_clusters__mutmut_16` | survived | `key` → `None` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_28` | survived | `0` → `1` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_30` | survived | `0` → `1` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_31` | survived | `(airport.get("marker") or {}).get("category")` → `None` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_32` | survived | `"category"` → `None` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_34` | survived | `"marker"` → `None` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_35` | survived | `"marker"` → `"XXmarkerXX"` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_36` | survived | `"marker"` → `"MARKER"` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_37` | survived | `"category"` → `"XXcategoryXX"` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_38` | survived | `"category"` → `"CATEGORY"` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_40` | survived | `+=` → `=` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_41` | survived | `+=` → `-=` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_42` | survived | `1` → `2` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_43` | survived | `+=` → `=` | killed: selection | `test_aircraft_layer.py::test_a_world_poll_asks_about_600_airports_and_draws_under_120_markers` |
| `x_clusters__mutmut_44` | survived | `+=` → `-=` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_45` | survived | `1` → `2` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_47` | survived | `"category"` → `None` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_49` | survived | `"marker"` → `None` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_50` | survived | `"marker"` → `"XXmarkerXX"` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_51` | survived | `"marker"` → `"MARKER"` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_52` | survived | `"category"` → `"XXcategoryXX"` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_53` | survived | `"category"` → `"CATEGORY"` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_55` | survived | `worst = max( (a["marker"]["category"] for a in reported), key=CATEGORY…` → `worst = None` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_57` | survived | `key=CATEGORY_ORDER.index` → `key=None` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_59` | survived | `key=CATEGORY_ORDER.index,` → `(removed)` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_65` | survived | `majority = max( (category for category in CATEGORY_ORDER if counts[cat…` → `majority = None` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_67` | survived | `key=lambda category: (counts[category], CATEGORY_ORDER.index(category)…` → `key=None,` | killed: selection | `test_aircraft_layer.py::test_a_tie_for_the_majority_goes_to_the_worse_category` |
| `x_clusters__mutmut_69` | survived | `key=lambda category: (counts[category], CATEGORY_ORDER.index(category)…` → `(removed)` | killed: selection | `test_aircraft_layer.py::test_a_tie_for_the_majority_goes_to_the_worse_category` |
| `x_clusters__mutmut_71` | survived | `(counts[category], CATEGORY_ORDER.index(category))` → `None` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_76` | survived | `{ "latitude": round(sum(a["latitude"] for a in placed_by) / len(placed…` → `None` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_77` | survived | `"latitude":` → `"XXlatitudeXX":` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_78` | survived | `"latitude":` → `"LATITUDE":` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_80` | survived | `3` → `None` | killed: new test | `test_board_arithmetic.py::test_each_cluster_sits_inside_one_cell_of_the_grid[10.0]` |
| `x_clusters__mutmut_81` | survived | `sum(a["latitude"] for a in placed_by) / len(placed_by), 3` → `3` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_82` | survived | `3` → `(removed)` | killed: new test | `test_board_arithmetic.py::test_each_cluster_sits_inside_one_cell_of_the_grid[10.0]` |
| `x_clusters__mutmut_83` | survived | `/` → `*` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_87` | survived | `3` → `4` | killed: new test | `test_board_arithmetic.py::test_a_cluster_is_placed_at_the_mean_to_three_decimals` |
| `x_clusters__mutmut_88` | survived | `"longitude":` → `"XXlongitudeXX":` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_89` | survived | `"longitude":` → `"LONGITUDE":` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_91` | survived | `3` → `None` | killed: new test | `test_board_arithmetic.py::test_each_cluster_sits_inside_one_cell_of_the_grid[10.0]` |
| `x_clusters__mutmut_92` | survived | `sum(a["longitude"] for a in placed_by) / len(placed_by), 3` → `3` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_93` | survived | `3` → `(removed)` | killed: new test | `test_board_arithmetic.py::test_each_cluster_sits_inside_one_cell_of_the_grid[10.0]` |
| `x_clusters__mutmut_94` | survived | `/` → `*` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_98` | survived | `3` → `4` | killed: new test | `test_board_arithmetic.py::test_a_cluster_is_placed_at_the_mean_to_three_decimals` |
| `x_clusters__mutmut_99` | survived | `"reported":` → `"XXreportedXX":` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_100` | survived | `"reported":` → `"REPORTED":` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_101` | survived | `"unknown":` → `"XXunknownXX":` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_102` | survived | `"unknown":` → `"UNKNOWN":` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_103` | survived | `"counts":` → `"XXcountsXX":` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_104` | survived | `"counts":` → `"COUNTS":` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_105` | survived | `"majority":` → `"XXmajorityXX":` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_106` | survived | `"majority":` → `"MAJORITY":` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_107` | survived | `"worst":` → `"XXworstXX":` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_108` | survived | `"worst":` → `"WORST":` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_109` | survived | `"text":` → `"XXtextXX":` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_110` | survived | `"text":` → `"TEXT":` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_112` | survived | `unknown` → `None` | equivalent | `unread-argument` |
| `x_clusters__mutmut_113` | survived | `show_unknown=False` → `show_unknown=None` | equivalent | `falsy-flag` |
| `x_clusters__mutmut_117` | survived | `show_unknown=False` → `show_unknown=True` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_118` | survived | `"text_with_unknown":` → `"XXtext_with_unknownXX":` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_119` | survived | `"text_with_unknown":` → `"TEXT_WITH_UNKNOWN":` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_121` | survived | `unknown` → `None` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_122` | survived | `show_unknown=True` → `show_unknown=None` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_clusters__mutmut_126` | survived | `show_unknown=True` → `show_unknown=False` | killed: selection | `test_aircraft_layer.py::test_a_cluster_counts_each_category_and_shows_its_worst` |
| `x_globe_lift__mutmut_7` | survived | `0.0` → `1.0` | equivalent | `rounded-away` |
| `x_globe_lift__mutmut_13` | survived | `5` → `None` | killed: selection | `test_aircraft_layer.py::test_the_lift_is_proportional_to_altitude_and_clamped[6000.0-0.0175]` |
| `x_globe_lift__mutmut_14` | survived | `clamped / GLOBE_ALTITUDE_CEILING_M * GLOBE_MAX_LIFT, 5` → `5` | killed: selection | `test_aircraft_layer.py::test_the_lift_is_proportional_to_altitude_and_clamped[0.0-0.0]` |
| `x_globe_lift__mutmut_15` | survived | `5` → `(removed)` | killed: selection | `test_aircraft_layer.py::test_the_lift_is_proportional_to_altitude_and_clamped[6000.0-0.0175]` |
| `x_globe_lift__mutmut_16` | survived | `*` → `/` | killed: selection | `test_aircraft_layer.py::test_the_lift_is_proportional_to_altitude_and_clamped[6000.0-0.0175]` |
| `x_globe_lift__mutmut_17` | survived | `/` → `*` | killed: selection | `test_aircraft_layer.py::test_the_lift_is_proportional_to_altitude_and_clamped[6000.0-0.0175]` |
| `x_globe_lift__mutmut_18` | survived | `5` → `6` | killed: new test | `test_board_arithmetic.py::test_the_lift_is_given_to_five_decimals` |
| `x_hidden_note__mutmut_3` | survived | `"none hidden: every airport asked has a report"` → `"XXnone hidden: every airport asked has a reportXX"` | killed: selection | `test_aircraft_layer.py::test_the_hidden_count_and_the_lookups_are_always_worded` |
| `x_hidden_note__mutmut_4` | survived | `"none hidden: every airport asked has a report"` → `"NONE HIDDEN: EVERY AIRPORT ASKED HAS A REPORT"` | killed: selection | `test_aircraft_layer.py::test_the_hidden_count_and_the_lookups_are_always_worded` |
| `x_on_near_side__mutmut_1` | no tests | `lat` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_on_near_side__mutmut_2` | no tests | `lon` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_on_near_side__mutmut_3` | no tests | `centre_lat` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_on_near_side__mutmut_4` | no tests | `centre_lon` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_on_near_side__mutmut_5` | no tests | `lat, lon` → `lon` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_on_near_side__mutmut_6` | no tests | `lon, centre_lat` → `centre_lat` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_on_near_side__mutmut_7` | no tests | `centre_lat, centre_lon` → `centre_lon` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_on_near_side__mutmut_8` | no tests | `centre_lon` → `(removed)` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_on_near_side__mutmut_9` | no tests | `<=` → `<` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_on_near_side__mutmut_10` | no tests | `+` → `-` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_on_near_side__mutmut_11` | no tests | `90.0` → `91.0` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_on_near_side__mutmut_12` | no tests | `1e-9` → `1.000000001` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_1` | no tests | `not on_near_side(lat` → `on_near_side(lat` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_2` | no tests | `lat` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_3` | no tests | `lon` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_4` | no tests | `centre_lat` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_5` | no tests | `centre_lon` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_6` | no tests | `lat, lon` → `lon` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_7` | no tests | `lon, centre_lat` → `centre_lat` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_8` | no tests | `centre_lat, centre_lon` → `centre_lon` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_9` | no tests | `centre_lon` → `(removed)` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_10` | no tests | `math.radians(lat), math.radians(lon - centre_lon)` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_11` | no tests | `lat` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_12` | no tests | `lon - centre_lon` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_13` | no tests | `-` → `+` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_14` | no tests | `math.radians(centre_lat)` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_15` | no tests | `centre_lat` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_16` | no tests | `math.cos(phi) * math.sin(lam)` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_17` | no tests | `*` → `/` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_18` | no tests | `phi` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_19` | no tests | `lam` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_20` | no tests | `y = math.cos(phi0) * math.sin(phi) - math.sin(phi0) * math.cos(phi) * …` → `y = None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_21` | no tests | `-` → `+` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_22` | no tests | `*` → `/` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_23` | no tests | `phi0` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_24` | no tests | `phi` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_25` | no tests | `*` → `/` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_26` | no tests | `*` → `/` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_27` | no tests | `phi0` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_28` | no tests | `phi` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_29` | no tests | `lam` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_30` | no tests | `1.0 + (globe_lift(altitude_m) or 0.0)` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_31` | no tests | `+` → `-` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_32` | no tests | `1.0` → `2.0` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_33` | no tests | `or` → `and` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_34` | no tests | `altitude_m` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_35` | no tests | `0.0` → `1.0` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_36` | no tests | `x * scale` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_37` | no tests | `6` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_38` | no tests | `x * scale, 6` → `6` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_39` | no tests | `6` → `(removed)` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_40` | no tests | `*` → `/` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_41` | no tests | `6` → `7` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_42` | no tests | `y * scale` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_43` | no tests | `6` → `None` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_44` | no tests | `y * scale, 6` → `6` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_45` | no tests | `6` → `(removed)` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_46` | no tests | `*` → `/` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_orthographic__mutmut_47` | no tests | `6` → `7` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_shown_note__mutmut_1` | survived | `==` → `!=` | killed: selection | `test_aircraft_layer.py::test_a_world_poll_asks_about_600_airports_and_draws_under_120_markers` |
| `x_shown_note__mutmut_2` | survived | `0:` → `1:` | killed: new test | `test_board_arithmetic.py::test_the_shown_note_counts_what_it_draws` |
| `x_shown_note__mutmut_3` | survived | `"every airport asked has a report"` → `"XXevery airport asked has a reportXX"` | killed: new test | `test_board_arithmetic.py::test_the_shown_note_counts_what_it_draws` |
| `x_shown_note__mutmut_4` | survived | `"every airport asked has a report"` → `"EVERY AIRPORT ASKED HAS A REPORT"` | killed: new test | `test_board_arithmetic.py::test_the_shown_note_counts_what_it_draws` |
| `x_unknown_shown_by_default__mutmut_1` | no tests | `>=` → `>` | dead code | function deleted: no caller outside tests; the page does this in `board.js` |
| `x_viewport_note__mutmut_1` | survived | `<` → `<=` | killed: selection | `test_aircraft_layer.py::test_the_hidden_count_and_the_lookups_are_always_worded` |
| `x_viewport_note__mutmut_2` | survived | `f"showing {asked:,} of {in_view:,} nearest the centre"` → `None` | killed: selection | `test_aircraft_layer.py::test_the_hidden_count_and_the_lookups_are_always_worded` |
| `x_viewport_note__mutmut_3` | survived | `f"showing all {asked:,} in view"` → `None` | killed: selection | `test_aircraft_layer.py::test_the_hidden_count_and_the_lookups_are_always_worded` |
| `x_viewport_note__mutmut_4` | survived | `<` → `<=` | killed: selection | `test_aircraft_layer.py::test_the_hidden_count_and_the_lookups_are_always_worded` |
| `x_viewport_note__mutmut_5` | survived | `+=` → `=` | killed: selection | `test_aircraft_layer.py::test_the_hidden_count_and_the_lookups_are_always_worded` |
| `x_viewport_note__mutmut_6` | survived | `+=` → `-=` | killed: selection | `test_aircraft_layer.py::test_the_hidden_count_and_the_lookups_are_always_worded` |
| `x_viewport_note__mutmut_7` | survived | `"call" if calls == 1 else "calls"` → `None` | killed: selection | `test_aircraft_layer.py::test_the_hidden_count_and_the_lookups_are_always_worded` |
| `x_viewport_note__mutmut_8` | survived | `calls == 1` → `(calls == 1) and False` | killed: selection | `test_aircraft_layer.py::test_the_hidden_count_and_the_lookups_are_always_worded` |
| `x_viewport_note__mutmut_9` | survived | `calls == 1` → `(calls == 1) or True` | killed: selection | `test_aircraft_layer.py::test_the_hidden_count_and_the_lookups_are_always_worded` |
| `x_viewport_note__mutmut_10` | survived | `"call"` → `"XXcallXX"` | killed: selection | `test_aircraft_layer.py::test_the_hidden_count_and_the_lookups_are_always_worded` |
| `x_viewport_note__mutmut_11` | survived | `"call"` → `"CALL"` | killed: selection | `test_aircraft_layer.py::test_the_hidden_count_and_the_lookups_are_always_worded` |
| `x_viewport_note__mutmut_12` | survived | `==` → `!=` | killed: selection | `test_aircraft_layer.py::test_the_hidden_count_and_the_lookups_are_always_worded` |
| `x_viewport_note__mutmut_13` | survived | `1` → `2` | killed: selection | `test_aircraft_layer.py::test_the_hidden_count_and_the_lookups_are_always_worded` |
| `x_viewport_note__mutmut_14` | survived | `"calls"` → `"XXcallsXX"` | killed: selection | `test_aircraft_layer.py::test_the_hidden_count_and_the_lookups_are_always_worded` |
| `x_viewport_note__mutmut_15` | survived | `"calls"` → `"CALLS"` | killed: selection | `test_aircraft_layer.py::test_the_hidden_count_and_the_lookups_are_always_worded` |

The seven equivalent:

| Judgement | Mutant | Why it is behaviour-identical |
|---|---|---|
| `whole-cell-offset` | `x_clusters__mutmut_5`, `_11` | `lat − 90` for `lat + 90`, `lon − 180` for `lon + 180` in the cell key. The two differ by 180° or 360°, a whole number of cells at 10, 5 and 2.5°, so every key moves by the same integer: the same airports share a cell and the cells sort in the same order. In floating point the two round differently within one ulp of a cell edge (1e-14°), where neither is more right than the other; none of the 3,270 reference airports, the only coordinates the service passes, is affected at any of the three sizes (checked, and over 500,000 random points). |
| `unread-argument` | `x_clusters__mutmut_112` | `cluster_text(counts, None, show_unknown=False)`. With `show_unknown` False, `show_unknown and unknown` stops at the flag, so `unknown` is never read. |
| `falsy-flag` | `x_clusters__mutmut_113` | `show_unknown=None` for `False` in `text`. The flag is only ever the left side of an `and`, where `None` and `False` are both false. |
| `angle-ceiling` | `x_airports_in_view__mutmut_6` | `min(181.0, …)` for `min(180.0, …)` as the view's reach. `angular_distance_deg` is at most `degrees(2 * asin(1.0))`, which is exactly 180.0, so `<= 180` and `<= 181` admit the same airports. |
| `rounded-away` | `x_globe_lift__mutmut_7` | `max(1.0, …)` for `max(0.0, …)` as the lift's floor. It differs only below 1 m, where the lift is under 0.000003 and rounds to 0.0 at five decimals either way. |
| `root-clamp` | `x_angular_distance_deg__mutmut_32` | `min(2.0, √h)` for `min(1.0, √h)`. It differs only when `√h` rounds above 1, which needs `h ≥ 1 + 2⁻⁵¹`: two ulps above the largest true value, 1. Over 2,000,000 near-antipodal pairs the largest computed `h` was `1 + 2⁻⁵²`, whose root rounds to exactly 1.0. **This one is evidence, not proof**: no input is known that reaches the clamp, so no test can fail on it. The clamp stays, as it does in `haversine_nm`, because `asin` raises past 1. |

## Run 44522301960: 111 survivors against a ceiling of 100

Counts in that run: killed: 2609, survived: 111. All 111 diffs were read.

| Survivors | Verdict |
|---:|---|
| 95 | Same id and same change as a row already judged here. Judgement unchanged. |
| 4 | Already judged, renumbered by the branch's new code: `x__weather_line__mutmut_35` is now `_54`, `x_triage_view__mutmut_92/98/99` are now `_102/108/109`. Judgement unchanged. |
| 12 | **Missing tests**, all new on this branch. Killed by the tests listed under "Killed in this round". |
| 0 | New and judged behaviour-identical. |

One row left the table without a test: `x__snapshot_epoch__mutmut_9` (`zero-epoch`). The check moved into
`_valid_epoch`, and its `0 <=` mutant is killed in this run.

Ceiling arithmetic: **was 100. 1 no longer survives (`zero-epoch`, above). 0 new survivors judged equivalent. 12 killed
by new tests. 100 − 1 + 12 − 12 = 99. Ceiling now 99.** The 99 is the 99 rows of the table below, each with a judgement.

### Killed in this round

| Mutant | Change | Test that now fails it |
|---|---|---|
| `x_triage_view__mutmut_137` | `count_rows(None)` for `count_rows(payload["counts"])` | `test_the_panel_passes_the_why_not_lines_through` |
| `x_triage_view__mutmut_147` | `weather_rows(None)` for `weather_rows(payload["weather"])` | same |
| `x_triage_view__mutmut_155` | `leg_rows(None)` | same |
| `x_triage_view__mutmut_156` | `payload.get(None)` for the legs | same |
| `x_triage_view__mutmut_157` | `payload.get("XXlegsXX")` | same |
| `x_triage_view__mutmut_158` | `payload.get("LEGS")` | same |
| `x_weather_rows__mutmut_82` | ceiling `basis` default `None` | `test_a_ceiling_without_a_basis_and_a_plain_visibility_read_as_given` |
| `x_weather_rows__mutmut_84` | ceiling `basis` default dropped (`None`) | same |
| `x_weather_rows__mutmut_87` | `"XXnot in the reportXX"` | same |
| `x_weather_rows__mutmut_88` | `"NOT IN THE REPORT"` | same |
| `x_weather_rows__mutmut_102` | `' or more'` always | same |
| `x_weather_rows__mutmut_108` | `'XXXX'` for `''` when the visibility is not a floor | same |

The six `triage_view` survivors were a **test that could not fail**, not only a missing one.
`test_the_panel_passes_the_why_not_lines_through` fed `weather: None` and `legs: {}` and checked only that there were
three count rows: exactly what `weather_rows(None)`, `leg_rows(None)` and `count_rows(None)` return. It was fixed, not
deleted: it now passes a real METAR, a real leg and the shared `COUNTS`, and pins the rows each produces. The
`weather_rows` survivors were a branch never run: every test gave the ceiling a `basis` and the visibility an
`at_least: True`. Each of the twelve was applied to `board.py` by hand and run against the board tests: all twelve
pass the old tests and fail the new ones.

## The judgements

Every surviving mutant is listed below with a judgement, 106 rows. None is a missing test: those were written (docs/QUALITY.md, section 3). Each category below is a judgement made by reading the mutants in it, not a filter applied to them.

| Judgement | Survivors | Why it is not a missing test |
|---|---|---|
| `prose` | 58 | Wording of a message, hint or exception text: wrapped in `XX` or re-cased. Equivalent for every caller: code branches on `code`, and the tests assert that a message and a hint exist (dropping either is killed). Pinning the exact words would be a change detector. |
| `header-case` | 8 | Case of an HTTP header name. Equivalent: header names are case-insensitive (RFC 9110, section 5.1) and httpx normalises them before sending. |
| `logger-name` | 4 | `get_logger(None)` for `get_logger(__name__)`. Equivalent: the logger name is not rendered into the JSON record and no per-logger level is configured, so the output is byte-identical. |
| `absent-claim-default` | 6 | The default for a claim that is absent from the token (`iss`/`tid` in the OBO cache key). Equivalent: any constant partitions identities the same way; every token without the claim gets the same value either way. The claim itself is killed when it is dropped. |
| `key-separator` | 1 | The separator joining scope names in the OBO cache key. Equivalent: any fixed separator yields the same partition, because scope names never contain whitespace (they are split on it everywhere they are read). |
| `unreachable-400` | 2 | `>= 400` became `> 400` or `>= 401` after the branch that handles 400, 401 and 403. Equivalent: a 400 never reaches this line. |
| `fast-path` | 2 | The lock-free read of the OBO cache was removed or keyed wrongly. Equivalent in result: the locked path re-reads the cache and returns the same token. It costs a lock acquisition, which no test should assert. |
| `rstrip-set` | 1 | `rstrip("/")` became `rstrip("XX/XX")`. `rstrip` takes a set of characters, so this also strips trailing `X`. Equivalent for every audience this server is configured with (URLs and `api://` URIs, none ending in `X`); judged acceptable rather than tested. |
| `pyjwk-alg` | 2 | `PyJWK(entry, algorithm=None)`. Equivalent: the entry was already required to declare `alg` RS256 or none, and PyJWK infers RS256 for an RSA key; the result is the same key. |
| `more-body-default` | 2 | `message.get("more_body", None)` for `False`. Equivalent: both are falsy. (`True` is killed by a test.) |
| `unconfigured-args` | 4 | An argument to the unconfigured `ApprovalVerifier(None, ...)`. Equivalent: that verifier raises `approval_not_configured` before it reads its clock or age limit. |
| `unknown-label` | 3 | The special case for the label `Unknown` was disabled or never matched. Equivalent: `"unknown".capitalize()` is `Unknown`, the same word. |
| `same-instant` | 2 | `datetime.fromtimestamp(snapshot)` without `UTC`. Equivalent: the naive local time goes straight into `astimezone(UTC)`, which reads it as local, so the instant and the text are unchanged. |
| `under-a-minute-tie` | 1 | `max(age, 1)` for `max(age, 0)` when ranking reports. Equivalent: it only ties ages of 1 s or less, and all of them print `Last update under 1 min ago` with the state `live`. |
| `whole-seconds` | 1 | `int(abs(seconds) / 60)` for `int(abs(seconds) // 60)`. Equivalent: both tools round ages to whole seconds, and for those truncating the quotient is the floor. |
| `truthy-count` | 1 | `sum(2 ...)` for `sum(1 ...)` in the banner's count of dated reports. Equivalent: the count is only tested for being non-zero. |
| `unreachable-cap` | 1 | `min(2.0, ...)` for `min(1.0, ...)` in `fade`. Equivalent: the other argument is `1.0` minus a non-negative number, so it never exceeds 1.0. |
| `whole-cell-offset` | 2 | The cluster cell key offset by 180° or 360° the other way. Equivalent: a whole number of cells at every listed size, so the grouping and the order are the same for every reference airport (run 44688906150, above). |
| `unread-argument` | 1 | `None` for the unknown count in a call whose `show_unknown` is False. Equivalent: the `and` stops at the flag. |
| `falsy-flag` | 1 | `show_unknown=None` for `False`. Equivalent: only ever read as the left side of an `and`. |
| `angle-ceiling` | 1 | The view's reach capped at 181° for 180°. Equivalent: no angle exceeds 180.0. |
| `rounded-away` | 1 | The lift's floor at 1 m for 0 m. Equivalent: below 1 m the lift rounds to 0.0 at five decimals. |
| `root-clamp` | 1 | The haversine root clamped at 2.0 for 1.0. Equivalent on the evidence, not proven: no input is known whose root rounds above 1 (run 44688906150, above). |

## Every survivor

| Mutant | Judgement | Original | Mutated |
|---|---|---|---|
| `auth.approval.x_approval_declined__mutmut_11` | `prose` | `hint="Do not retry or rephrase the proposal to get a different answer. Report the "` | `hint="XXDo not retry or rephrase the proposal to get a different answer. Report the XX"` |
| `auth.approval.x_approval_declined__mutmut_12` | `prose` | `hint="Do not retry or rephrase the proposal to get a different answer. Report the "` | `hint="do not retry or rephrase the proposal to get a different answer. report the "` |
| `auth.approval.x_approval_declined__mutmut_13` | `prose` | `hint="Do not retry or rephrase the proposal to get a different answer. Report the "` | `hint="DO NOT RETRY OR REPHRASE THE PROPOSAL TO GET A DIFFERENT ANSWER. REPORT THE "` |
| `auth.approval.x_approval_declined__mutmut_14` | `prose` | `"outcome to the user.",` | `"XXoutcome to the user.XX",` |
| `auth.approval.x_approval_declined__mutmut_15` | `prose` | `"outcome to the user.",` | `"OUTCOME TO THE USER.",` |
| `auth.approval.x_approval_expired__mutmut_12` | `prose` | `hint="Ask the approver to approve the proposal again, then retry with the new grant.",` | `hint="XXAsk the approver to approve the proposal again, then retry with the new grant.XX",` |
| `auth.approval.x_approval_expired__mutmut_13` | `prose` | `hint="Ask the approver to approve the proposal again, then retry with the new grant.",` | `hint="ask the approver to approve the proposal again, then retry with the new grant.",` |
| `auth.approval.x_approval_expired__mutmut_14` | `prose` | `hint="Ask the approver to approve the proposal again, then retry with the new grant.",` | `hint="ASK THE APPROVER TO APPROVE THE PROPOSAL AGAIN, THEN RETRY WITH THE NEW GRANT.",` |
| `auth.approval.x_approval_invalid__mutmut_12` | `prose` | `hint="Not retryable with the same grant. A new approval must come from the approver; "` | `hint="XXNot retryable with the same grant. A new approval must come from the approver; XX"` |
| `auth.approval.x_approval_invalid__mutmut_13` | `prose` | `hint="Not retryable with the same grant. A new approval must come from the approver; "` | `hint="not retryable with the same grant. a new approval must come from the approver; "` |
| `auth.approval.x_approval_invalid__mutmut_14` | `prose` | `hint="Not retryable with the same grant. A new approval must come from the approver; "` | `hint="NOT RETRYABLE WITH THE SAME GRANT. A NEW APPROVAL MUST COME FROM THE APPROVER; "` |
| `auth.approval.x_approval_invalid__mutmut_15` | `prose` | `"it cannot be constructed.",` | `"XXit cannot be constructed.XX",` |
| `auth.approval.x_approval_invalid__mutmut_16` | `prose` | `"it cannot be constructed.",` | `"IT CANNOT BE CONSTRUCTED.",` |
| `auth.approval.x_approval_not_configured__mutmut_10` | `prose` | `message="This server cannot verify approvals, so it will not write.",` | `message="this server cannot verify approvals, so it will not write.",` |
| `auth.approval.x_approval_not_configured__mutmut_11` | `prose` | `message="This server cannot verify approvals, so it will not write.",` | `message="THIS SERVER CANNOT VERIFY APPROVALS, SO IT WILL NOT WRITE.",` |
| `auth.approval.x_approval_not_configured__mutmut_12` | `prose` | `hint="An operator must set FLIGHTOPS_AUTH_ISSUER and FLIGHTOPS_AUTH_JWKS_URL. "` | `hint="XXAn operator must set FLIGHTOPS_AUTH_ISSUER and FLIGHTOPS_AUTH_JWKS_URL. XX"` |
| `auth.approval.x_approval_not_configured__mutmut_13` | `prose` | `hint="An operator must set FLIGHTOPS_AUTH_ISSUER and FLIGHTOPS_AUTH_JWKS_URL. "` | `hint="an operator must set flightops_auth_issuer and flightops_auth_jwks_url. "` |
| `auth.approval.x_approval_not_configured__mutmut_14` | `prose` | `hint="An operator must set FLIGHTOPS_AUTH_ISSUER and FLIGHTOPS_AUTH_JWKS_URL. "` | `hint="AN OPERATOR MUST SET FLIGHTOPS_AUTH_ISSUER AND FLIGHTOPS_AUTH_JWKS_URL. "` |
| `auth.approval.x_approval_not_configured__mutmut_15` | `prose` | `"Not retryable; the read tools are unaffected.",` | `"XXNot retryable; the read tools are unaffected.XX",` |
| `auth.approval.x_approval_not_configured__mutmut_16` | `prose` | `"Not retryable; the read tools are unaffected.",` | `"not retryable; the read tools are unaffected.",` |
| `auth.approval.x_approval_not_configured__mutmut_17` | `prose` | `"Not retryable; the read tools are unaffected.",` | `"NOT RETRYABLE; THE READ TOOLS ARE UNAFFECTED.",` |
| `auth.approval.x_approval_not_configured__mutmut_9` | `prose` | `message="This server cannot verify approvals, so it will not write.",` | `message="XXThis server cannot verify approvals, so it will not write.XX",` |
| `auth.approval.x_approval_payload_mismatch__mutmut_14` | `prose` | `hint="An approval covers exactly the payload the approver saw. Propose again and have "` | `hint="XXAn approval covers exactly the payload the approver saw. Propose again and have XX` |
| `auth.approval.x_approval_payload_mismatch__mutmut_15` | `prose` | `hint="An approval covers exactly the payload the approver saw. Propose again and have "` | `hint="an approval covers exactly the payload the approver saw. propose again and have "` |
| `auth.approval.x_approval_payload_mismatch__mutmut_16` | `prose` | `hint="An approval covers exactly the payload the approver saw. Propose again and have "` | `hint="AN APPROVAL COVERS EXACTLY THE PAYLOAD THE APPROVER SAW. PROPOSE AGAIN AND HAVE "` |
| `auth.approval.x_approval_payload_mismatch__mutmut_17` | `prose` | `"that proposal approved; do not reuse an approval across proposals.",` | `"XXthat proposal approved; do not reuse an approval across proposals.XX",` |
| `auth.approval.x_approval_payload_mismatch__mutmut_18` | `prose` | `"that proposal approved; do not reuse an approval across proposals.",` | `"THAT PROPOSAL APPROVED; DO NOT REUSE AN APPROVAL ACROSS PROPOSALS.",` |
| `auth.approval.x_approval_required__mutmut_10` | `prose` | `hint="You cannot approve this yourself. Tell the user that an approver must sign the "` | `hint="you cannot approve this yourself. tell the user that an approver must sign the "` |
| `auth.approval.x_approval_required__mutmut_12` | `prose` | `"proposal, and stop; do not retry without an approval.",` | `"XXproposal, and stop; do not retry without an approval.XX",` |
| `auth.approval.x_approval_required__mutmut_13` | `prose` | `"proposal, and stop; do not retry without an approval.",` | `"PROPOSAL, AND STOP; DO NOT RETRY WITHOUT AN APPROVAL.",` |
| `auth.approval.x_approval_required__mutmut_9` | `prose` | `hint="You cannot approve this yourself. Tell the user that an approver must sign the "` | `hint="XXYou cannot approve this yourself. Tell the user that an approver must sign the XX"` |
| `auth.approval.x_approver_is_proposer__mutmut_12` | `prose` | `hint="A second person must approve. Tell the user who needs to act; do not retry.",` | `hint="XXA second person must approve. Tell the user who needs to act; do not retry.XX",` |
| `auth.approval.x_approver_is_proposer__mutmut_13` | `prose` | `hint="A second person must approve. Tell the user who needs to act; do not retry.",` | `hint="a second person must approve. tell the user who needs to act; do not retry.",` |
| `auth.approval.x_approver_is_proposer__mutmut_14` | `prose` | `hint="A second person must approve. Tell the user who needs to act; do not retry.",` | `hint="A SECOND PERSON MUST APPROVE. TELL THE USER WHO NEEDS TO ACT; DO NOT RETRY.",` |
| `auth.approval.x_approver_not_authorised__mutmut_12` | `prose` | `"permission.",` | `"XXpermission.XX",` |
| `auth.approval.x_approver_not_authorised__mutmut_13` | `prose` | `"permission.",` | `"PERMISSION.",` |
| `auth.approval.x_approver_not_authorised__mutmut_14` | `prose` | `"Not retryable with the same approver.",` | `"XXNot retryable with the same approver.XX",` |
| `auth.approval.x_approver_not_authorised__mutmut_15` | `prose` | `"Not retryable with the same approver.",` | `"not retryable with the same approver.",` |
| `auth.approval.x_approver_not_authorised__mutmut_16` | `prose` | `"Not retryable with the same approver.",` | `"NOT RETRYABLE WITH THE SAME APPROVER.",` |
| `auth.challenge.x__read_body__mutmut_27` | `more-body-default` | `if not message.get("more_body", False):` | `if not message.get("more_body", None):` |
| `auth.challenge.x__read_body__mutmut_29` | `more-body-default` | `if not message.get("more_body", False):` | `if not message.get("more_body", ):` |
| `auth.challenge.xǁScopeChallengeMiddlewareǁ__init____mutmut_5` | `logger-name` | `self._log = get_logger(__name__)` | `self._log = get_logger(None)` |
| `auth.challenge.xǁScopeChallengingServerǁstreamable_http_app__mutmut_7` | `prose` | `raise RuntimeError("the scope challenge needs a resource server URL")` | `raise RuntimeError("XXthe scope challenge needs a resource server URLXX")` |
| `auth.jwks.x__parse_keys__mutmut_50` | `pyjwk-alg` | `key = jwt.PyJWK(entry, algorithm="RS256").key` | `key = jwt.PyJWK(entry, algorithm=None).key` |
| `auth.jwks.x__parse_keys__mutmut_52` | `pyjwk-alg` | `key = jwt.PyJWK(entry, algorithm="RS256").key` | `key = jwt.PyJWK(entry, ).key` |
| `auth.jwks.xǁJwksCacheǁ__init____mutmut_15` | `logger-name` | `self._log = get_logger(__name__)` | `self._log = get_logger(None)` |
| `auth.jwks.xǁJwksCacheǁ__init____mutmut_26` | `header-case` | `headers={"User-Agent": user_agent, "Accept": "application/json"},` | `headers={"user-agent": user_agent, "Accept": "application/json"},` |
| `auth.jwks.xǁJwksCacheǁ__init____mutmut_27` | `header-case` | `headers={"User-Agent": user_agent, "Accept": "application/json"},` | `headers={"USER-AGENT": user_agent, "Accept": "application/json"},` |
| `auth.jwks.xǁJwksCacheǁ__init____mutmut_29` | `header-case` | `headers={"User-Agent": user_agent, "Accept": "application/json"},` | `headers={"User-Agent": user_agent, "accept": "application/json"},` |
| `auth.jwks.xǁJwksCacheǁ__init____mutmut_30` | `header-case` | `headers={"User-Agent": user_agent, "Accept": "application/json"},` | `headers={"User-Agent": user_agent, "ACCEPT": "application/json"},` |
| `auth.obo.x__key__mutmut_10` | `absent-claim-default` | `str(claims.get("iss", "")),` | `str(claims.get("iss", "XXXX")),` |
| `auth.obo.x__key__mutmut_13` | `absent-claim-default` | `str(claims.get("tid", "")),` | `str(claims.get("tid", None)),` |
| `auth.obo.x__key__mutmut_15` | `absent-claim-default` | `str(claims.get("tid", "")),` | `str(claims.get("tid", )),` |
| `auth.obo.x__key__mutmut_18` | `absent-claim-default` | `str(claims.get("tid", "")),` | `str(claims.get("tid", "XXXX")),` |
| `auth.obo.x__key__mutmut_21` | `key-separator` | `" ".join(sorted(scopes)),` | `"XX XX".join(sorted(scopes)),` |
| `auth.obo.x__key__mutmut_5` | `absent-claim-default` | `str(claims.get("iss", "")),` | `str(claims.get("iss", None)),` |
| `auth.obo.x__key__mutmut_7` | `absent-claim-default` | `str(claims.get("iss", "")),` | `str(claims.get("iss", )),` |
| `auth.obo.x_obo_exchange_rejected__mutmut_11` | `prose` | `message="The identity provider refused to issue a downstream token for this user "` | `message="XXThe identity provider refused to issue a downstream token for this user XX"` |
| `auth.obo.x_obo_exchange_rejected__mutmut_12` | `prose` | `message="The identity provider refused to issue a downstream token for this user "` | `message="the identity provider refused to issue a downstream token for this user "` |
| `auth.obo.x_obo_exchange_rejected__mutmut_13` | `prose` | `message="The identity provider refused to issue a downstream token for this user "` | `message="THE IDENTITY PROVIDER REFUSED TO ISSUE A DOWNSTREAM TOKEN FOR THIS USER "` |
| `auth.obo.x_obo_exchange_rejected__mutmut_14` | `prose` | `hint="The user's token is expired, revoked, not a user token, or lacks the scope. "` | `hint="XXThe user's token is expired, revoked, not a user token, or lacks the scope. XX"` |
| `auth.obo.x_obo_exchange_rejected__mutmut_15` | `prose` | `hint="The user's token is expired, revoked, not a user token, or lacks the scope. "` | `hint="the user's token is expired, revoked, not a user token, or lacks the scope. "` |
| `auth.obo.x_obo_exchange_rejected__mutmut_16` | `prose` | `hint="The user's token is expired, revoked, not a user token, or lacks the scope. "` | `hint="THE USER'S TOKEN IS EXPIRED, REVOKED, NOT A USER TOKEN, OR LACKS THE SCOPE. "` |
| `auth.obo.x_obo_exchange_rejected__mutmut_17` | `prose` | `"Ask the user to sign in again; do not retry with the same token.",` | `"XXAsk the user to sign in again; do not retry with the same token.XX",` |
| `auth.obo.x_obo_exchange_rejected__mutmut_18` | `prose` | `"Ask the user to sign in again; do not retry with the same token.",` | `"ask the user to sign in again; do not retry with the same token.",` |
| `auth.obo.x_obo_not_configured__mutmut_10` | `prose` | `message="This server is not configured to call downstream APIs on behalf of a user.",` | `message="this server is not configured to call downstream apis on behalf of a user.",` |
| `auth.obo.x_obo_not_configured__mutmut_11` | `prose` | `message="This server is not configured to call downstream APIs on behalf of a user.",` | `message="THIS SERVER IS NOT CONFIGURED TO CALL DOWNSTREAM APIS ON BEHALF OF A USER.",` |
| `auth.obo.x_obo_not_configured__mutmut_12` | `prose` | `hint="An operator must set FLIGHTOPS_AUTH_TOKEN_URL, FLIGHTOPS_AUTH_CLIENT_ID and "` | `hint="XXAn operator must set FLIGHTOPS_AUTH_TOKEN_URL, FLIGHTOPS_AUTH_CLIENT_ID and XX"` |
| `auth.obo.x_obo_not_configured__mutmut_13` | `prose` | `hint="An operator must set FLIGHTOPS_AUTH_TOKEN_URL, FLIGHTOPS_AUTH_CLIENT_ID and "` | `hint="an operator must set flightops_auth_token_url, flightops_auth_client_id and "` |
| `auth.obo.x_obo_not_configured__mutmut_14` | `prose` | `hint="An operator must set FLIGHTOPS_AUTH_TOKEN_URL, FLIGHTOPS_AUTH_CLIENT_ID and "` | `hint="AN OPERATOR MUST SET FLIGHTOPS_AUTH_TOKEN_URL, FLIGHTOPS_AUTH_CLIENT_ID AND "` |
| `auth.obo.x_obo_not_configured__mutmut_15` | `prose` | `"FLIGHTOPS_AUTH_CLIENT_SECRET. Not retryable; the other tools are unaffected.",` | `"XXFLIGHTOPS_AUTH_CLIENT_SECRET. Not retryable; the other tools are unaffected.XX",` |
| `auth.obo.x_obo_not_configured__mutmut_16` | `prose` | `"FLIGHTOPS_AUTH_CLIENT_SECRET. Not retryable; the other tools are unaffected.",` | `"flightops_auth_client_secret. not retryable; the other tools are unaffected.",` |
| `auth.obo.x_obo_not_configured__mutmut_17` | `prose` | `"FLIGHTOPS_AUTH_CLIENT_SECRET. Not retryable; the other tools are unaffected.",` | `"FLIGHTOPS_AUTH_CLIENT_SECRET. NOT RETRYABLE; THE OTHER TOOLS ARE UNAFFECTED.",` |
| `auth.obo.x_obo_not_configured__mutmut_9` | `prose` | `message="This server is not configured to call downstream APIs on behalf of a user.",` | `message="XXThis server is not configured to call downstream APIs on behalf of a user.XX",` |
| `auth.obo.xǁOboExchangerǁ__init____mutmut_15` | `header-case` | `headers={"User-Agent": settings.user_agent, "Accept": "application/json"},` | `headers={"user-agent": settings.user_agent, "Accept": "application/json"},` |
| `auth.obo.xǁOboExchangerǁ__init____mutmut_16` | `header-case` | `headers={"User-Agent": settings.user_agent, "Accept": "application/json"},` | `headers={"USER-AGENT": settings.user_agent, "Accept": "application/json"},` |
| `auth.obo.xǁOboExchangerǁ__init____mutmut_18` | `header-case` | `headers={"User-Agent": settings.user_agent, "Accept": "application/json"},` | `headers={"User-Agent": settings.user_agent, "accept": "application/json"},` |
| `auth.obo.xǁOboExchangerǁ__init____mutmut_19` | `header-case` | `headers={"User-Agent": settings.user_agent, "Accept": "application/json"},` | `headers={"User-Agent": settings.user_agent, "ACCEPT": "application/json"},` |
| `auth.obo.xǁOboExchangerǁ__init____mutmut_6` | `logger-name` | `self._log = get_logger(__name__)` | `self._log = get_logger(None)` |
| `auth.obo.xǁOboExchangerǁ_exchange__mutmut_50` | `unreachable-400` | `if response.status_code >= 400:` | `if response.status_code > 400:` |
| `auth.obo.xǁOboExchangerǁ_exchange__mutmut_51` | `unreachable-400` | `if response.status_code >= 400:` | `if response.status_code >= 401:` |
| `auth.obo.xǁOboExchangerǁtoken_for__mutmut_10` | `fast-path` | `cached = self._cache.get(key)` | `cached = self._cache.get(None)` |
| `auth.obo.xǁOboExchangerǁtoken_for__mutmut_9` | `fast-path` | `cached = self._cache.get(key)` | `cached = None` |
| `auth.verifier.x__canonical__mutmut_3` | `rstrip-set` | `return audience.rstrip("/")` | `return audience.rstrip("XX/XX")` |
| `auth.verifier.xǁJwtVerifierǁ__init____mutmut_9` | `logger-name` | `self._log = get_logger(__name__)` | `self._log = get_logger(None)` |
| `auth.x_build_approval_verifier__mutmut_3` | `unconfigured-args` | `return ApprovalVerifier(None, clock=clock, max_age_s=settings.approval_max_age_s)` | `return ApprovalVerifier(None, clock=None, max_age_s=settings.approval_max_age_s)` |
| `auth.x_build_approval_verifier__mutmut_4` | `unconfigured-args` | `return ApprovalVerifier(None, clock=clock, max_age_s=settings.approval_max_age_s)` | `return ApprovalVerifier(None, clock=clock, max_age_s=None)` |
| `auth.x_build_approval_verifier__mutmut_6` | `unconfigured-args` | `return ApprovalVerifier(None, clock=clock, max_age_s=settings.approval_max_age_s)` | `return ApprovalVerifier(None, max_age_s=settings.approval_max_age_s)` |
| `auth.x_build_approval_verifier__mutmut_7` | `unconfigured-args` | `return ApprovalVerifier(None, clock=clock, max_age_s=settings.approval_max_age_s)` | `return ApprovalVerifier(None, clock=clock, )` |
| `credits.xǁCreditLedgerǁ__init____mutmut_4` | `prose` | `raise ValueError("budget_per_hour must not be negative")` | `raise ValueError("XXbudget_per_hour must not be negativeXX")` |
| `dashboard.board.x__traffic_line__mutmut_5` | `same-instant` | `datetime.fromtimestamp(snapshot, UTC)` | `datetime.fromtimestamp(snapshot, None)` |
| `dashboard.board.x__traffic_line__mutmut_7` | `same-instant` | `datetime.fromtimestamp(snapshot, UTC)` | `datetime.fromtimestamp(snapshot, )` |
| `dashboard.board.x__weather_line__mutmut_54` | `under-a-minute-tie` | `(age < -METAR_STALE_AFTER_S, max(age, 0))` | `(age < -METAR_STALE_AFTER_S, max(age, 1))` |
| `dashboard.board.x_age_text__mutmut_3` | `whole-seconds` | `minutes = int(abs(seconds) // 60)` | `minutes = int(abs(seconds) / 60)` |
| `dashboard.board.x_banner__mutmut_12` | `truthy-count` | `dated = sum(1 for view in weather if view["age_s"] is not None)` | `dated = sum(2 for view in weather if view["age_s"] is not None)` |
| `dashboard.board.x_fade__mutmut_14` | `unreachable-cap` | `min(1.0, 1.0 - max(0.0, age_s) / FADE_AFTER_S)` | `min(2.0, 1.0 - max(0.0, age_s) / FADE_AFTER_S)` |
| `dashboard.board.x_triage_view__mutmut_102` | `unknown-label` | `"Unknown" if severity == "unknown" else severity.capitalize()` | `"Unknown" if (severity == "unknown") and False else severity.capitalize()` |
| `dashboard.board.x_triage_view__mutmut_108` | `unknown-label` | `"Unknown" if severity == "unknown" else severity.capitalize()` | `"Unknown" if severity == "XXunknownXX" else severity.capitalize()` |
| `dashboard.board.x_triage_view__mutmut_109` | `unknown-label` | `"Unknown" if severity == "unknown" else severity.capitalize()` | `"Unknown" if severity == "UNKNOWN" else severity.capitalize()` |
| `dashboard.board.x_airports_in_view__mutmut_6` | `angle-ceiling` | `reach = min(180.0, radius_deg + VIEW_MARGIN_DEG)` | `reach = min(181.0, radius_deg + VIEW_MARGIN_DEG)` |
| `dashboard.board.x_angular_distance_deg__mutmut_32` | `root-clamp` | `math.asin(min(1.0, math.sqrt(h)))` | `math.asin(min(2.0, math.sqrt(h)))` |
| `dashboard.board.x_clusters__mutmut_5` | `whole-cell-offset` | `math.floor((airport["latitude"] + 90.0) / cell_deg)` | `math.floor((airport["latitude"] - 90.0) / cell_deg)` |
| `dashboard.board.x_clusters__mutmut_11` | `whole-cell-offset` | `math.floor((airport["longitude"] + 180.0) / cell_deg)` | `math.floor((airport["longitude"] - 180.0) / cell_deg)` |
| `dashboard.board.x_clusters__mutmut_112` | `unread-argument` | `"text": cluster_text(counts, unknown, show_unknown=False)` | `"text": cluster_text(counts, None, show_unknown=False)` |
| `dashboard.board.x_clusters__mutmut_113` | `falsy-flag` | `"text": cluster_text(counts, unknown, show_unknown=False)` | `"text": cluster_text(counts, unknown, show_unknown=None)` |
| `dashboard.board.x_globe_lift__mutmut_7` | `rounded-away` | `clamped = max(0.0, min(GLOBE_ALTITUDE_CEILING_M, altitude_m))` | `clamped = max(1.0, min(GLOBE_ALTITUDE_CEILING_M, altitude_m))` |
