# CBI Dashboard on Microsoft Fabric + Power BI + Copilot

This is the setup guide for turning the PostgreSQL pipeline's output into
an interactive, natural-language-queryable dashboard (map + charts +
table + descriptive text) using Microsoft Fabric, Power BI, and Copilot.

**Read this before starting**: most of what's below happens in the Fabric
and Power BI portals (point-and-click configuration), not in code. I can't
provision Azure/Fabric resources for you or verify these steps against
your actual tenant — I don't have access to it. Treat this as a detailed
starting checklist, not a guarantee every click matches your tenant's UI
exactly (Microsoft changes these portals fairly often).

This also supersedes / significantly extends "Part V" of the CBI
technical report, which sketched a more generic Azure OpenAI + Functions +
Static Web Apps architecture. If you want, I can rewrite Part V around
Fabric + Power BI + Copilot instead so the report matches this plan —
just ask.

---

## 1. Getting your data into Fabric

You have two real options. Pick one; don't build both.

### Option A — On-premises Data Gateway (recommended if available)

Fabric/Power BI connects **directly** to your local PostgreSQL instance on
a schedule, no export script needed.

1. Install the **On-premises Data Gateway** on the same Windows machine
   the pipeline runs on (or any machine that can reach PostgreSQL).
2. Register the gateway to your Fabric/Power BI tenant (requires a Fabric
   or Power BI admin account).
3. In Fabric, create a **Dataflow Gen2** (or a **Data Pipeline** with a
   Copy Activity) using the PostgreSQL connector, routed through the
   gateway.
4. Point it at the same views the report uses:
   `vw_bottleneck_dashboard_ranked`, `vw_bottleneck_monthly_dashboard`,
   `vw_bottleneck_weekday_dashboard`, `segment_recurring_bottlenecks`,
   `corridor_segments`, `congestion_events`.
5. Land the output in a **Fabric Lakehouse** as Delta tables.
6. Set a refresh schedule (e.g. after `run_cbi_pipeline.bat` finishes —
   see Section 5).

**Pros**: native, incremental refresh, no separate script to maintain.
**Cons**: requires gateway install/admin approval, which may be a longer
lead time than the script below if you want to see a dashboard today.

### Option B — Parquet export via `cbi_export_to_fabric.py` (already built)

The script added to this package exports the same six datasets to Parquet
and uploads them to Azure Data Lake Storage Gen2 — which is also how
Fabric's **OneLake** exposes a Lakehouse's `Files` area, so the exact same
upload code works for either a plain storage account or directly into a
Lakehouse, depending on which URL you point it at.

1. `pip install azure-storage-file-datalake azure-identity`
2. Create an **Azure AD App Registration** (a Fabric/Azure admin does
   this) with **Storage Blob Data Contributor** on the target
   storage account or Fabric workspace.
3. Copy `cbi_azure_credentials.example.ini` → `cbi_azure_credentials.ini`,
   fill in the tenant/client id, secret, storage account, and container
   (or Lakehouse) name. **Never commit this file** — same rule as
   `cbi_credentials.ini`.
4. Run it standalone (`python cbi_export_to_fabric.py`) or pass
   `-ExportFabric` to `run_cbi_pipeline.ps1` to run it automatically after
   every pipeline run.
5. In Fabric, over the uploaded `Files`, use **"Load to Tables"** (in the
   Lakehouse UI) to materialize them as Delta tables — or point a small
   Dataflow Gen2 at the OneLake files to do light transformation first.

**Pros**: works today, no gateway approval needed.
**Cons**: a script you maintain instead of a native connector; refresh is
whenever you run it, not Fabric-scheduled.

---

## 2. Semantic model (Power BI, inside Fabric)

Once the six tables exist in the Lakehouse (either option above), build a
Power BI semantic model on top of them:

- **Relationships**: `corridor_segments.corridor_id` → other tables'
  `corridor_id`/`corridor` + `direction`; `segment_recurring_bottlenecks.
  bottleneck_id` → `bottleneck_daily_metrics.bottleneck_id` (already a
  clean 1-to-many after the multi-corridor migration's identity-column
  fix).
- **Date table**: mark `congestion_events.analysis_date` (or a dedicated
  calendar table) as the model's date table for time intelligence.
- **Key measures (DAX)**: total annual mile-hours, average severity
  index, bottleneck count by corridor, occurrence % trend — mirror the
  columns already defined in Appendix A of the technical report so the
  dashboard and the report agree on definitions.

---

## 3. The report: map, charts, table, descriptive text

All four of your requested elements map onto standard Power BI visuals —
nothing here needs a custom visual or code:

| What you asked for | Power BI visual | Notes |
|---|---|---|
| Interactive map | **Azure Maps visual** (built into Power BI) or ArcGIS Maps | Plot `corridor_segments` (start/end lat/long) colored by congestion severity; overlay `segment_recurring_bottlenecks` as sized markers (size = `severity_index`) |
| Graphs / charts | Bar chart (severity ranking), line chart (`vw_bottleneck_monthly_dashboard` trend), heatmap matrix (`vw_bottleneck_weekday_dashboard` by weekday × bottleneck) | Standard visuals, driven directly by the semantic model |
| Table | Table/matrix visual on `vw_bottleneck_dashboard_ranked` | Same columns as the technical report's Section 11 ranking table |
| Descriptive text | **Smart Narrative visual** | Auto-generates a natural-language summary of whatever's on the page (e.g. "Jodeco Road has the highest severity index at 9,573.9, driven by a 98.1% occurrence rate...") and updates live as filters change |

---

## 4. Natural-language Q&A — three options, different cost/control tradeoffs

This is the "users type in what they'd like to know" part. Three ways to
get there, in order of increasing cost and control:

1. **Power BI Q&A visual** — free, built into any Power BI report, no
   Premium/Fabric capacity required. Users type a question, it's answered
   directly from the semantic model with an auto-picked visual. Quality
   depends on clean, well-named columns and measures — worth spending
   time on the semantic model's field names for this reason.

2. **Copilot in Power BI** — richer natural-language Q&A, can generate
   DAX, summarize a report, and answer more open-ended questions than the
   Q&A visual. **Requires Fabric capacity F64 or higher, or Power BI
   Premium P1+** — this is a real licensing cost, not a checkbox, and is
   worth confirming with whoever owns your Azure/Fabric budget before
   committing to it as the primary interface.

3. **Custom Azure OpenAI integration** — a small web app (or Power BI
   custom visual) that takes a typed question, uses Azure OpenAI to
   translate it into a DAX or SQL query against the Fabric SQL endpoint,
   runs it, and returns a formatted answer. More engineering effort than
   options 1–2, but full control over prompt design, guardrails, and cost
   — this is essentially the "AI layer" originally sketched in Part V of
   the technical report, and is still relevant if Copilot's black-box
   behavior isn't precise enough for how bottleneck data specifically
   needs to be reasoned about (e.g. respecting the distinction between
   `annual_mile_hours` at the peak segment vs. `annual_queue_mile_hours`
   at the zone level — Appendix A.4 vs. A.7 of the technical report).

**Recommendation**: start with the free Q&A visual to validate the
concept, add Copilot once capacity/licensing is approved if its answers
are noticeably better, and only build the custom Azure OpenAI path if
Copilot's answers don't respect the report's specific metric definitions
closely enough for your users' needs.

---

## 5. Tying the schedule together

If you go with Option B (the export script), the natural order is:

1. `run_cbi_pipeline.bat` — refreshes PostgreSQL data for all corridors
   (as today).
2. `run_cbi_pipeline.ps1 -ExportFabric` — same run, plus pushes the six
   datasets to OneLake at the end.
3. In Fabric, schedule the Lakehouse's "Load to Tables" step (or a
   Dataflow Gen2 refresh) shortly after step 2 would typically finish, or
   trigger it via a Fabric pipeline that watches for new files.
4. The Power BI report's semantic model refreshes on its own schedule (or
   "on data refresh" if using DirectLake mode against the Lakehouse,
   which needs no separate refresh step at all).

If you go with Option A (gateway), the Dataflow Gen2 can be scheduled
independently of the PowerShell pipeline entirely, since it talks to
PostgreSQL directly.

---

## Open questions to resolve before building this for real

- Do you already have a Fabric workspace / capacity, or is this a new
  procurement? (Determines how soon Option A is realistic, and whether
  Copilot's licensing cost is already covered.)
- Is the dashboard for internal agency use (share the Power BI
  workspace/app directly) or does it need to be embedded in a public-
  facing site (Power BI Embedded, more engineering effort)?
- Should I rewrite Part V of the technical report around this plan, so
  the report and the actual architecture stay in sync?
