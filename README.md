# CBI Multi-Corridor Pipeline

Automates the I-75 NB pipeline (Parts I–II of the CBI technical report)
across every corridor in `corridor_definitions`, and generates one Word
report per corridor.

## Setup (one time)

1. **Run the migration** — either `setup_migration.ps1` (double-click-friendly,
   prompts for your DB password and applies it via `psql`), or run
   `sql/003_multicorridor_migration.sql` yourself in your usual SQL client.
   This fixes a real bug: `segment_recurring_bottlenecks.bottleneck_id`
   is currently a plain integer PK that a second corridor's writer would
   collide with (I-75 NB already used ids 1–6). The migration converts it
   to a database-assigned identity column starting after your existing
   rows, and adds the shared `segment_daily` / `segment_profile` tables
   plus a `corridor_pipeline_runs` log table. **Run this exactly once —
   re-running it will fail (safely) rather than silently re-applying.**

2. **Extend `corridor_definitions`** if you want corridors beyond the
   eight already seeded (I-75, I-575, I-675, Downtown Connector — each
   NB/SB). I-20 and I-85 are on your original rollout list but aren't in
   the table yet:

   ```sql
   INSERT INTO "Year_2025".corridor_definitions
       (road, direction, corridor_name, corridor_group)
   VALUES
       ('I-85', 'NORTHBOUND', 'I-85 Northbound', 'Interstate'),
       ('I-85', 'SOUTHBOUND', 'I-85 Southbound', 'Interstate'),
       ('I-20', 'EASTBOUND', 'I-20 Eastbound', 'Interstate'),
       ('I-20', 'WESTBOUND', 'I-20 Westbound', 'Interstate')
   ON CONFLICT (road, direction) DO NOTHING;
   ```

   Then rebuild `corridor_segments` (SQL master statement 028) so the new
   rows get their TMC lists.

3. **Install the one new dependency**: `pip install python-docx`
   (everything else — polars, psycopg, connectorx, scipy, numpy — you
   already have).

4. Drop the eight `.py` files from `scripts/` into the same folder as your
   existing `cbi_database.py`, `cbi_detector.py` (they import those two
   directly, unchanged).

## Running it — one command

Everything after setup is one script. No more running phase scripts one
by one.

- **Double-click `run_cbi_pipeline.bat`** — prompts for the DB password
  once, runs every active corridor through all four stages plus report
  generation, then opens the output folder when it's done.
- **Or from PowerShell**, for more control:

  ```powershell
  .\run_cbi_pipeline.ps1                              # every active corridor
  .\run_cbi_pipeline.ps1 -Only "I-85,NORTHBOUND"       # just one, for testing
  .\run_cbi_pipeline.ps1 -SkipReport                   # data stages only
  ```

If you'd rather call the Python orchestrator directly yourself (e.g. from
a different terminal, or a scheduled task that already has
`CBI_DB_PASSWORD` set), that still works exactly as before:

```
python cbi_run_all_corridors.py
python cbi_run_all_corridors.py --only "I-85,NORTHBOUND"
python cbi_run_all_corridors.py --skip-report
```

Each corridor runs four stages in order — events → profile → bottlenecks →
characterization — then generates its report. If a stage fails, that
corridor stops there (later stages depend on earlier output) but the
orchestrator moves on to the next corridor rather than aborting the whole
run. Everything is logged to `corridor_pipeline_runs` and to
`outputs/multi_corridor/pipeline_errors.txt`.

## What's a faithful port vs. a reconstruction

- `cbi_corridor_database.py`, `cbi_corridor_events.py`,
  `cbi_corridor_characterization.py` — direct parameterizations of your
  validated `cbi_database.py`, `cbi_annual_runner.py`, and
  `cbi_bottleneck_characterization.py`. Logic unchanged, only the
  corridor/direction values move from hardcoded constants to arguments.
- `cbi_corridor_bottlenecks.py` — **reconstructed** from the report's
  description of `cbi_segment_bottlenecks.py` (box-car smoothing,
  `scipy.find_peaks`, relative-threshold boundary expansion, valley
  splitting), not copied from your original file. The threshold constants
  at the top of the file are placeholders — compare them against your
  validated script before trusting results on a second corridor. Two
  fields (`start_mile`/`end_mile`, `average_congested_minutes`/
  `p95_congested_minutes`) are left `None` pending either a cumulative-mile
  column on `corridor_segments` or a join back through `segment_daily` —
  noted inline in the code.

## Scheduling

For a single Windows machine, a Task Scheduler action running
`powershell -ExecutionPolicy Bypass -File run_cbi_pipeline.ps1` on whatever
cadence matches your data refresh (e.g. monthly) is enough to start —
though for a fully unattended scheduled run, set `CBI_DB_PASSWORD` as a
permanent system/user environment variable first, since `run_cbi_pipeline.ps1`
only prompts interactively when that variable isn't already set. Part V of
the technical report sketches the Azure Data Factory / Functions
equivalent if this needs to run unattended in the cloud instead.

## File Reference

```
cbi_multicorridor_pipeline/
├── run_cbi_pipeline.bat        <- double-click this for a normal run
├── run_cbi_pipeline.ps1        <- what the .bat calls; supports -Only / -SkipReport / -ExportFabric
├── setup_migration.ps1         <- run once, before the first pipeline run
├── cbi_credentials.example.ini <- copy to cbi_credentials.ini and fill in (gitignored)
├── cbi_azure_credentials.example.ini <- copy to cbi_azure_credentials.ini for Fabric export (gitignored)
├── .gitignore                  <- blocks both credentials files from being committed
├── FABRIC_DASHBOARD_GUIDE.md   <- Fabric + Power BI + Copilot dashboard setup
├── README.md
├── sql/
│   └── 003_multicorridor_migration.sql
└── scripts/
    ├── cbi_corridor_registry.py
    ├── cbi_corridor_database.py
    ├── cbi_corridor_events.py
    ├── cbi_corridor_profile.py
    ├── cbi_corridor_bottlenecks.py
    ├── cbi_corridor_characterization.py
    ├── cbi_generate_corridor_report.py
    ├── cbi_export_to_fabric.py     <- pushes combined dashboard data to Fabric/ADLS
    └── cbi_run_all_corridors.py    <- the orchestrator the launcher calls
```

## Saved credentials — read this

`cbi_credentials.ini` holds your PostgreSQL password in plain text so the
launcher scripts don't have to prompt every run. Two things to actually
do, not just note:

1. **Copy the template, don't rename it in place**: `cbi_credentials.example.ini`
   → `cbi_credentials.ini`, then edit the copy. The example file has no
   real password in it and is safe to keep in git; the real one is not.
2. **Confirm `.gitignore` is doing its job** before you ever run `git add .`
   in this folder — run `git status` first and make sure `cbi_credentials.ini`
   and `cbi_azure_credentials.ini` do not show up as files to be committed.
   Your CBI repository on GitHub is public; a committed credentials file
   there is a real, not hypothetical, exposure.

If you want stronger protection than a gitignored plaintext file, Windows
Credential Manager (via the `CredentialManager` PowerShell module) or
Azure Key Vault are both better long-term homes for this password — the
`.ini` approach here is the simplest thing that works, not the most
secure thing possible.
