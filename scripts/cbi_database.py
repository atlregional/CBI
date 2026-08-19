from __future__ import annotations

import os
from urllib.parse import quote_plus


def connection_kwargs() -> dict[str, str | int]:
    """
    PostgreSQL connection settings read from environment variables.

    run_cbi_pipeline.ps1 loads cbi_credentials.ini and sets these variables
    before starting Python.
    """
    password = os.getenv("CBI_DB_PASSWORD")

    if not password:
        raise RuntimeError(
            "CBI_DB_PASSWORD is not set. Run the PowerShell launcher or set "
            "the database environment variables before starting the pipeline."
        )

    return {
        "host": os.getenv("CBI_DB_HOST", "localhost"),
        "port": int(os.getenv("CBI_DB_PORT", "5432")),
        "dbname": os.getenv("CBI_DB_NAME", "CBI"),
        "user": os.getenv("CBI_DB_USER", "postgres"),
        "password": password,
    }


def database_uri() -> str:
    """
    ConnectorX/Polars-compatible PostgreSQL URI.
    """
    cfg = connection_kwargs()

    return (
        f"postgresql://{quote_plus(str(cfg['user']))}:"
        f"{quote_plus(str(cfg['password']))}"
        f"@{cfg['host']}:{cfg['port']}/{cfg['dbname']}"
    )
