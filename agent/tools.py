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


def get_line_variants(line_number: str) -> str:
    """
    Return all route variants for a given line number (route_short_name),
    including operator (agency_name) and route area (route_long_name).
    Always call this first for any question about a specific line.
    """
    if _conn is None:
        return "Error: GTFS database not loaded yet."
    try:
        rows = _conn.execute("""
            SELECT DISTINCT r.route_id, a.agency_name, r.route_long_name
            FROM routes r
            JOIN agency a ON r.agency_id = a.agency_id
            WHERE r.route_short_name = ?
            ORDER BY a.agency_name, r.route_long_name
        """, [str(line_number)]).fetchall()
        if not rows:
            return f"No routes found for line number '{line_number}'."
        records = [{"route_id": r[0], "agency_name": r[1], "route_long_name": r[2]} for r in rows]
        return json.dumps(records, ensure_ascii=False)
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
    "get_line_variants": get_line_variants,
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
            "name": "get_line_variants",
            "description": (
                "ALWAYS call this first for any question about a specific line number. "
                "Returns all route variants for that line: operator (agency_name) and route area (route_long_name). "
                "If multiple variants exist, present them as a numbered list and ask the user to choose before proceeding."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "line_number": {
                        "type": "string",
                        "description": "The line number to look up, e.g. '5' or '480'.",
                    }
                },
                "required": ["line_number"],
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
