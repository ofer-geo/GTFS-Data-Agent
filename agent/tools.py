import json
import re
import duckdb
from collections import defaultdict

_conn: duckdb.DuckDBPyConnection = None
MAX_ROWS = 100

# Persists across turns so select_option can map numbers to Hebrew values
selection_state = {
    "pending_type": None,
    "line_number": None,
    "agencies": [],
    "grouped_lines": [],
    "options": [],
}


def set_connection(conn: duckdb.DuckDBPyConnection):
    global _conn
    _conn = conn


def get_schema() -> str:
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


def get_line_variants(line_number: str, agency_name: str = None) -> str:
    """
    Return route variants for a given line number.
    Stage 1 (no agency_name): if multiple agencies exist, ask for agency.
    Stage 2 (agency_name given): if multiple real lines in that agency, ask for route.
    Stage 3: uniquely identified — can_proceed = true.
    """
    if _conn is None:
        return "Error: GTFS database not loaded yet."
    try:
        params = [str(line_number)]
        where_clause = "WHERE r.route_short_name = ?"
        if agency_name:
            where_clause += " AND a.agency_name = ?"
            params.append(agency_name)

        rows = _conn.execute(f"""
            SELECT DISTINCT
                r.route_id, a.agency_name, r.route_long_name, r.route_desc
            FROM routes r
            JOIN agency a ON r.agency_id = a.agency_id
            {where_clause}
            ORDER BY a.agency_name, r.route_long_name, r.route_id
        """, params).fetchall()

        if not rows:
            msg = f"No routes found for line number '{line_number}'"
            if agency_name:
                msg += f" and agency '{agency_name}'"
            return json.dumps({
                "line_number": line_number,
                "agency_name": agency_name,
                "can_proceed": False,
                "clarification_needed": None,
                "reason": msg + ".",
                "routes": [],
            }, ensure_ascii=False, indent=2)

        routes = []
        for route_id, row_agency, route_long_name, route_desc in rows:
            match = re.search(r"\b\d{5}\b", route_desc or "")
            routes.append({
                "route_id": route_id,
                "agency_name": row_agency,
                "route_long_name": route_long_name,
                "route_desc": route_desc,
                "route_code_5_digits": match.group(0) if match else None,
            })

        agencies = sorted(set(r["agency_name"] for r in routes))

        # Stage 1: multiple agencies → ask which one
        if agency_name is None and len(agencies) > 1:
            selection_state.update({
                "pending_type": "agency",
                "line_number": line_number,
                "agencies": agencies,
                "grouped_lines": [],
                "options": [],
            })
            options = [{"option_number": i, "label": a} for i, a in enumerate(agencies, 1)]
            return json.dumps({
                "line_number": line_number,
                "can_proceed": False,
                "clarification_needed": "agency",
                "options_count": len(options),
                "options": options,
                "instruction": f"Show ONLY the options list above as a numbered list. Valid choices: 1 to {len(options)}. Ask the user to enter a number.",
            }, ensure_ascii=False, indent=2)

        # Stage 2: group real lines within this agency by 5-digit route code
        line_groups = defaultdict(list)
        for r in routes:
            key = r["route_code_5_digits"] or f"route_id:{r['route_id']}"
            line_groups[key].append(r)

        grouped_lines = []
        for route_code, group_routes in line_groups.items():
            grouped_lines.append({
                "agency_name": group_routes[0]["agency_name"],
                "route_code_5_digits": None if route_code.startswith("route_id:") else route_code,
                "variants_count": len(group_routes),
                "route_ids": [r["route_id"] for r in group_routes],
                "route_long_names": sorted(set(r["route_long_name"] for r in group_routes)),
                "route_descriptions": [r["route_desc"] for r in group_routes],
                "routes": group_routes,
            })

        if len(grouped_lines) > 1:
            options = [
                {
                    "option_number": i,
                    "label": g["route_long_names"][0] if g["route_long_names"] else str(route_code),
                    "route_code_5_digits": g["route_code_5_digits"],
                    "route_ids": g["route_ids"],
                }
                for i, g in enumerate(grouped_lines, 1)
            ]
            selection_state.update({
                "pending_type": "route",
                "line_number": line_number,
                "agencies": [],
                "grouped_lines": grouped_lines,
                "options": options,
            })
            return json.dumps({
                "line_number": line_number,
                "agency_name": agencies[0] if len(agencies) == 1 else agency_name,
                "can_proceed": False,
                "clarification_needed": "route",
                "reason": f"Line '{line_number}' has more than one route/area for this agency.",
                "options_count": len(options),
                "options": options,
                "instruction": f"Show ONLY the options list above as a numbered list. Valid choices: 1 to {len(options)}. Ask the user to enter a number.",
            }, ensure_ascii=False, indent=2)

        # Stage 3: uniquely identified
        selection_state.update({
            "pending_type": None,
            "agencies": [],
            "grouped_lines": [],
            "options": [],
        })
        selected_group = grouped_lines[0]
        return json.dumps({
            "line_number": line_number,
            "agency_name": selected_group["agency_name"],
            "can_proceed": True,
            "clarification_needed": None,
            "reason": f"Line '{line_number}' is uniquely identified.",
            "routes_count": len(routes),
            "selected_line": selected_group,
            "routes": routes,
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return f"Error: {e}"


def select_option(option_number: int) -> str:
    """
    Map the user's numbered choice to the stored Hebrew agency/route value.
    Call this whenever the user replies with a number after a numbered list.
    """
    option_number = int(option_number)
    idx = option_number - 1
    pending_type = selection_state.get("pending_type")

    if pending_type == "agency":
        agencies = selection_state.get("agencies", [])
        if idx < 0 or idx >= len(agencies):
            return json.dumps({"error": f"Invalid option {option_number}. Valid range: 1–{len(agencies)}"})
        agency_name = agencies[idx]
        line_number = selection_state["line_number"]
        return get_line_variants(line_number=line_number, agency_name=agency_name)

    if pending_type == "route":
        grouped_lines = selection_state.get("grouped_lines", [])
        if idx < 0 or idx >= len(grouped_lines):
            return json.dumps({"error": f"Invalid option {option_number}. Valid range: 1–{len(grouped_lines)}"})
        selected_line = grouped_lines[idx]
        selection_state.update({"pending_type": None, "grouped_lines": [], "options": []})
        return json.dumps({
            "can_proceed": True,
            "clarification_needed": None,
            "selected_line": selected_line,
            "reason": "Route option selected. The agent can proceed.",
        }, ensure_ascii=False, indent=2)

    return json.dumps({"error": "No pending selection. Ask the user a question first."})


def get_line_stops(route_ids: list) -> str:
    """
    Get ordered stops for each direction of a line.
    Pass the route_ids list from the selected_line result.
    Routes sharing the same 5-digit code are the same line in different directions.
    Returns stops grouped by direction, ordered by stop_sequence.
    """
    if _conn is None:
        return "Error: GTFS database not loaded yet."
    try:
        if not route_ids:
            return "Error: route_ids list is empty."

        directions = []
        for route_id in route_ids:
            rows = _conn.execute("""
                SELECT
                    t.direction_id,
                    t.trip_headsign,
                    s.stop_name,
                    s.stop_lat,
                    s.stop_lon,
                    st.stop_sequence,
                    s.stop_code
                FROM stop_times st
                JOIN stops s ON st.stop_id = s.stop_id
                JOIN trips t ON st.trip_id = t.trip_id
                WHERE t.route_id = ?
                  AND st.trip_id = (
                      SELECT trip_id FROM trips WHERE route_id = ? LIMIT 1
                  )
                ORDER BY st.stop_sequence
            """, [route_id, route_id]).fetchall()

            if not rows:
                continue

            stops = [
                {"sequence": r[5], "stop_name": r[2], "stop_code": r[6], "lat": r[3], "lon": r[4]}
                for r in rows
            ]
            directions.append({
                "route_id": route_id,
                "direction_id": rows[0][0],
                "headsign": rows[0][1],
                "stops_count": len(stops),
                "first_stop": stops[0]["stop_name"],
                "last_stop": stops[-1]["stop_name"],
                "stops": stops,
            })

        if not directions:
            return "No stops found for the given route_ids."

        return json.dumps(directions, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: {e}"


def run_sql(query: str) -> str:
    if _conn is None:
        return "Error: GTFS database not loaded yet."
    stripped = query.strip()
    if not stripped.upper().startswith("SELECT"):
        return "Error: Only SELECT queries are allowed."
    if "LIMIT" not in stripped.upper():
        stripped = stripped.rstrip(";") + f" LIMIT {MAX_ROWS}"
    try:
        rel = _conn.execute(stripped)
        cols = [desc[0] for desc in rel.description]
        rows = rel.fetchmany(MAX_ROWS)
        records = [dict(zip(cols, row)) for row in rows]
        if not records:
            return "Query returned no results."
        extra = f"\n[capped at {MAX_ROWS} rows]" if len(rows) == MAX_ROWS else ""
        return json.dumps(records, ensure_ascii=False, default=str) + extra
    except Exception as e:
        return f"SQL Error: {e}"


# ---- Tools map ----
tools_map = {
    "get_schema": get_schema,
    "get_line_variants": get_line_variants,
    "select_option": select_option,
    "get_line_stops": get_line_stops,  # kept for internal use
    "run_sql": run_sql,
}

# ---- Tools schema ----
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_schema",
            "description": "Return column names and types for every GTFS table. Use only for technical/database questions.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_line_variants",
            "description": (
                "Call this whenever the user asks about a specific line number. "
                "First call with only line_number. "
                "If clarification_needed='agency', ask the user to pick an agency, then call again with agency_name. "
                "If clarification_needed='route', ask the user to pick a route. "
                "If can_proceed=true, the line is uniquely identified."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "line_number": {
                        "type": "string",
                        "description": "The line number, e.g. '5' or '480'. Pass only the number.",
                    },
                    "agency_name": {
                        "type": "string",
                        "description": "Optional agency name chosen by the user, e.g. 'דן', 'אגד'.",
                    },
                },
                "required": ["line_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_option",
            "description": (
                "Call this when the user replies with a number after a numbered list of agencies or routes. "
                "Do not interpret the number yourself — this tool maps it to the correct stored value."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "option_number": {
                        "type": "integer",
                        "description": "The number the user selected from the list.",
                    }
                },
                "required": ["option_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "Execute a SELECT query on the GTFS database. "
                "Use this after the line is identified (can_proceed=true) to answer any question about stops, schedules, routes, agencies, etc. "
                "Can also be called directly for general database questions that don't require line disambiguation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A valid SQL SELECT statement. Only SELECT is allowed.",
                    }
                },
                "required": ["query"],
            },
        },
    },
]
