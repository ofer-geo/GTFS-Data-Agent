import os
import zipfile
import tempfile
import requests
import duckdb

GTFS_URL = "https://gtfs.mot.gov.il/gtfsfiles/israel-public-transportation.zip"

TABLES = [
    "agency",
    "stops",
    "routes",
    "trips",
    "stop_times",
    "calendar",
    "calendar_dates",
    "shapes",
    "translations",
    "fare_rules",
]


def download_and_load(gtfs_dir: str = None) -> duckdb.DuckDBPyConnection:
    """
    Download the Israeli MOT GTFS zip, extract it, and load all tables into
    an in-memory DuckDB connection. Returns the connection.

    If the txt files already exist in gtfs_dir, skips the download so that
    local development doesn't re-fetch on every restart.
    """
    if gtfs_dir is None:
        gtfs_dir = os.path.join(tempfile.gettempdir(), "gtfs_israel")

    os.makedirs(gtfs_dir, exist_ok=True)
    agency_file = os.path.join(gtfs_dir, "agency.txt")

    if not os.path.exists(agency_file):
        zip_path = os.path.join(gtfs_dir, "google_transit.zip")
        print(f"Downloading GTFS from {GTFS_URL} ...")
        resp = requests.get(GTFS_URL, stream=True, timeout=180)
        resp.raise_for_status()
        with open(zip_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
        print("Extracting GTFS files...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(gtfs_dir)
        os.remove(zip_path)

    print("Loading GTFS into DuckDB...")
    conn = duckdb.connect()
    for table in TABLES:
        csv_path = os.path.join(gtfs_dir, f"{table}.txt")
        if os.path.exists(csv_path):
            conn.execute(
                f"CREATE TABLE {table} AS "
                f"SELECT * FROM read_csv_auto('{csv_path}', header=true)"
            )
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table}: {count:,} rows")
        else:
            print(f"  {table}: not found, skipping")

    print("GTFS ready.")
    return conn
