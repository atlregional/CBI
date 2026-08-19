"""
Exports the multi-corridor dashboard datasets to Parquet and uploads them
to Azure Data Lake Storage Gen2 (or a Fabric Lakehouse's Files area, which
exposes the same ADLS Gen2-compatible API via OneLake — same code works
for either target, only the endpoint URL differs).

This is one route into Microsoft Fabric. It works today with just an
Azure Storage account / Fabric workspace and no on-premises data gateway.
The alternative route — a Fabric Dataflow Gen2 or Data Pipeline connecting
directly to PostgreSQL through an on-premises data gateway — avoids this
script entirely and refreshes on a Fabric-native schedule instead; see
FABRIC_DASHBOARD_GUIDE.md for the tradeoff.

Requires:
    pip install polars pyarrow azure-storage-file-datalake azure-identity

Reads Azure connection info from cbi_azure_credentials.ini (same folder),
same pattern as cbi_credentials.ini for PostgreSQL — copy
cbi_azure_credentials.example.ini and fill it in. Never commit the real
file (see .gitignore).
"""

from __future__ import annotations

import configparser
from pathlib import Path

import polars as pl
from azure.identity import ClientSecretCredential
from azure.storage.filedatalake import DataLakeServiceClient

import cbi_database

LOCAL_EXPORT_DIR = Path(r"C:\Users\Soheil\Desktop\CBI\outputs\fabric_export")

# One row per (view name, query). Every export is network-wide — not
# filtered by corridor — since the point of this export is a combined
# multi-corridor dashboard.
DATASETS: dict[str, str] = {
    "bottleneck_annual_dashboard": """
        SELECT * FROM "Year_2025".vw_bottleneck_dashboard_ranked
    """,
    "bottleneck_monthly_dashboard": """
        SELECT * FROM "Year_2025".vw_bottleneck_monthly_dashboard
    """,
    "bottleneck_weekday_dashboard": """
        SELECT * FROM "Year_2025".vw_bottleneck_weekday_dashboard
    """,
    "segment_recurring_bottlenecks": """
        SELECT * FROM "Year_2025".segment_recurring_bottlenecks
    """,
    "corridor_segments": """
        SELECT corridor_id, corridor_name, segment_order, tmc, road,
               direction, intersection, miles, start_latitude,
               start_longitude, end_latitude, end_longitude
        FROM "Year_2025".corridor_segments
    """,
    "congestion_events": """
        SELECT analysis_date, corridor, direction, event_id, start_time,
               end_time, duration_minutes, first_segment_order,
               last_segment_order, first_intersection, last_intersection,
               corridor_start_mile, corridor_end_mile,
               maximum_contiguous_extent_miles, event_area_mile_hours,
               average_speed_mph, average_speed_ratio
        FROM "Year_2025".congestion_events
    """,
}


def _load_azure_config(config_path: Path) -> dict:
    if not config_path.exists():
        raise RuntimeError(
            f"{config_path.name} not found. Copy "
            "cbi_azure_credentials.example.ini to cbi_azure_credentials.ini "
            "and fill it in first."
        )

    parser = configparser.ConfigParser()
    parser.read(config_path)
    section = parser["azure"]

    required = ["tenant_id", "client_id", "client_secret", "storage_account", "container"]
    missing = [key for key in required if not section.get(key)]
    if missing:
        raise RuntimeError(
            f"cbi_azure_credentials.ini is missing: {', '.join(missing)}"
        )

    return dict(section)


def _get_datalake_client(config: dict) -> DataLakeServiceClient:
    credential = ClientSecretCredential(
        tenant_id=config["tenant_id"],
        client_id=config["client_id"],
        client_secret=config["client_secret"],
    )

    account_url = f"https://{config['storage_account']}.dfs.core.windows.net"

    return DataLakeServiceClient(account_url=account_url, credential=credential)


def export_and_upload(config_path: Path | None = None) -> dict:
    config_path = config_path or Path(__file__).parent / "cbi_azure_credentials.ini"
    config = _load_azure_config(config_path)

    LOCAL_EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    service_client = _get_datalake_client(config)
    file_system_client = service_client.get_file_system_client(
        file_system=config["container"]
    )

    results = {}

    for dataset_name, query in DATASETS.items():
        print(f"Exporting {dataset_name}...")

        dataframe = pl.read_database_uri(
            query=query,
            uri=cbi_database.database_uri(),
            engine="connectorx",
        )

        local_path = LOCAL_EXPORT_DIR / f"{dataset_name}.parquet"
        dataframe.write_parquet(local_path)

        remote_path = f"cbi/{dataset_name}.parquet"
        file_client = file_system_client.get_file_client(remote_path)

        with open(local_path, "rb") as file_data:
            data = file_data.read()
            file_client.upload_data(data, overwrite=True)

        results[dataset_name] = {
            "rows": dataframe.height,
            "local_path": str(local_path),
            "remote_path": f"{config['container']}/{remote_path}",
        }

        print(f"  {dataframe.height:,} rows -> {results[dataset_name]['remote_path']}")

    return results


if __name__ == "__main__":
    export_and_upload()
