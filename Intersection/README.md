# Arterial Intersection Congestion Severity — Standalone Extension

Implements the 6-step methodology for ranking the top 20 arterial
intersections by congestion severity, using an hour-of-day free-flow
baseline (fixing the self-suppressed-baseline problem documented in the
main technical report's Known Limitations section) instead of the main
pipeline's per-segment historical-percentile `reference_speed`.

## Setup

1. Run `sql/001_named_arterial_registry.sql` once. It creates and seeds the
   named-arterial registry (Buford Hwy, Roswell Rd, Floyd Rd, MLK, Howell
   Mill Rd, Piedmont Rd, East-West Connector, Atlanta Rd).

   **Before trusting the grouping**, run the "candidate road name
   discovery" queries at the bottom of that file for each arterial and
   check what `tmc_metadata.road` actually contains in your database — the
   seeded `road_pattern` values are best-guess starting points, not
   verified against your real data. Add any missing patterns you find.

2. Drop `scripts/cbi_arterial_intersection_severity.py` and
   `scripts/cbi_generate_arterial_severity_page.py` into the same folder
   as your existing `cbi_database.py`.

3. Confirm `ARTERIAL_F_SYSTEM = (3, 4)` at the top of
   `cbi_arterial_intersection_severity.py` matches how you want "Arterial"
   defined (FHWA functional system 3 = Principal Arterial, 4 = Minor
   Arterial). Adjust if your project uses a different classification field.

## Running it

```
python cbi_arterial_intersection_severity.py       # steps 1-5, writes results to DB
python cbi_generate_arterial_severity_page.py       # step 6, standalone HTML page
```

The first script prints its free-flow hour(s) and top-20 table to the
console, and writes the full ranked result to a new table,
`"Year_2025".arterial_intersection_severity`. The second script reads that
table and produces a self-contained HTML page (KPI cards, scatter plot,
ranked table) styled to match the rest of this project's output.

## What each config constant controls

All at the top of `cbi_arterial_intersection_severity.py`:

| Constant | Default | Controls |
|---|---|---|
| `ARTERIAL_F_SYSTEM` | `(3, 4)` | Which `tmc_metadata.f_system` values count as "Arterial" |
| `CONGESTION_RATIO` | `0.70` | Same cutoff as the rest of the pipeline — used for occurrence |
| `FREE_FLOW_TOP_N_HOURS` | `1` | How many top-speed hours get averaged into the free-flow baseline; widen to 3 for a more stable reference |
| `AM_PEAK_HOURS` / `PM_PEAK_HOURS` | 7-9 / 16-18 | Hours checked for peak-hour speed drop |
| `KMEANS_K` | `6` | Number of clusters; not tuned via elbow/silhouette method, worth checking on real data |
| `TOP_N` | `20` | Final ranked list size |
| `MIN_ANALYZED_DAYS` | `30` | Segments with less data than this are excluded from occurrence calculation |

## Design notes and known open questions

- **Why the free-flow hour is identified system-wide, not per-segment**:
  a chronically congested segment could otherwise define its own
  artificially-low "free flow" hour and never register as abnormal — this
  was the confirmed root-cause pattern behind the US-23/GA-42 (Griffin
  St/Macon St, McDonough) false-positive documented in the main technical
  report. This design deliberately avoids repeating that mistake.
- **K-means finds a group, not a ranking** — the script always adds a
  ranking step on top of clustering (by standardized composite score
  within the worst cluster), since "top 20" isn't something clustering
  produces on its own.
- **AADT rollup for grouped arterials uses MAX, not SUM**, across a named
  arterial's matched segments at one intersection — AADT is a traffic
  count at a location, not a cumulative flow that should be added across
  segments representing the same real-world intersection.
- **Occurrence is computed at the hourly-aggregate level**, not the
  5-minute-interval level the main pipeline uses (Appendix A.4) — same
  concept (percentage of days a location is congested), applied at a
  coarser time resolution appropriate to this specific analysis. Worth
  being explicit about this difference if the two occurrence numbers are
  ever compared side by side.

## Integrating into the main CBI dashboard

The existing regional dashboard (`regional_congestion_report_*.html`) uses
a tabbed layout (Metadata, Regional Overview, Region Map, Corridors, Watch
Segments). The cleanest integration path is adding one more tab that reads
from the same `arterial_intersection_severity` table and reuses the
KPI-card / table / scatter-chart pattern already built here, rather than
linking out to a separate file.
