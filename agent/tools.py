import json
import duckdb

# Set by app.py via set_connection() after GTFS is loaded
_conn: duckdb.DuckDBPyConnection = None

MAX_ROWS = 100  # cap rows returned to LLM to avoid context overflow


def set_connection(conn: duckdb.DuckDBPyConnection):
    global _conn
    _conn = conn


def get_schema() -> str:
    """Return the column names and types for every GTFS table in the database."""
    if _conn is None:
        return "Error: GTFS database not loaded yet."
    try:
        tables = [row[0] for row in _conn.execute("SHOW TABLES").fetchall()]
        lines = []
        for table in tables:
            cols = _conn.execute(f"DESCRIBE {table}").fetchall()
            col_str = ", ".join(f"{c[0]} ({c[1]})" for c in cols)
            lines.append(f"{table}: {col_str}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_sql(query: str) -> str:
    """
    Execute a SQL SELECT query against the GTFS database and return results as JSON.
    Always use LIMIT in your queries. Maximum rows returned: 100.
    """
    if _conn is None:
        return "Error: GTFS database not loaded yet."
    try:
        rel = _conn.execute(query)
        cols = [desc[0] for desc in rel.description]
        rows = rel.fetchmany(MAX_ROWS)
        extra = ""
        if len(rows) == MAX_ROWS:
            extra = f"\n[results capped at {MAX_ROWS} rows]"
        records = [dict(zip(cols, row)) for row in rows]
        if not records:
            return "Query returned no results."
        return json.dumps(records, ensure_ascii=False, default=str) + extra
    except Exception as e:
        return f"SQL Error: {e}"


# ---- Tools map ----
tools_map = {
    "get_schema": get_schema,
    "run_sql": run_sql,
}

# ---- Tools schema (sent to LLM on every request) ----
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_schema",
            "description": (
                "Return the column names and data types for every table in the GTFS database. "
                "Call this first if you are unsure which columns or tables to use."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "Execute a SQL SELECT query against the local GTFS database and return results as JSON. "
                "Tables: agency, stops, routes, trips, stop_times, calendar, calendar_dates. "
                "Always include a LIMIT clause. Max rows returned is 100."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "A valid SQL SELECT statement. Example: "
                            "\"SELECT stop_name, stop_lat, stop_lon FROM stops LIMIT 5\""
                        ),
                    }
                },
                "required": ["query"],
            },
        },
    },
]
