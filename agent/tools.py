import json
import re
import duckdb
import plotly.graph_objects as go
from collections import defaultdict

_conn: duckdb.DuckDBPyConnection = None
MAX_ROWS = 30

# Persists across turns so select_option can map numbers to Hebrew values
selection_state = {
    "pending_type": None,   # "agency", "route", "direction", "schedule_choice", or None
    "line_number": None,
    "agencies": [],
    "grouped_lines": [],
    "options": [],
    "directions": [],       # for direction selection
    "all_route_ids": [],    # for direction selection
    "schedule_route_ids": [],   # for schedule_choice: route_ids of the already-identified line
    "schedule_agency": None,    # for schedule_choice
    "schedule_line_number": None,  # for schedule_choice
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


def get_line_variants(line_number: str, agency_name: str = None, _internal: bool = False) -> str:
    """
    Return route variants for a given line number.
    Stage 1 (no agency_name): if multiple agencies exist, ask for agency.
    Stage 2 (agency_name given): if multiple real lines in that agency, ask for route.
    Stage 3: uniquely identified — can_proceed = true.

    _internal is only ever set True by select_option's own follow-up call (never
    reachable from a model tool-call — core.py strips unknown args before dispatch).
    It lets that legitimate re-entry through while still blocking the model from
    restarting an already-pending disambiguation instead of calling select_option.
    """
    if _conn is None:
        return "Error: GTFS database not loaded yet."

    pending = selection_state.get("pending_type")
    if not _internal and pending in ("agency", "route") and str(selection_state.get("line_number")) == str(line_number):
        return json.dumps({
            "error": "A selection is already pending for this line number.",
            "clarification_needed": pending,
            "options": selection_state.get("options", []),
            "instruction": (
                "Do NOT call get_line_variants again for this line. "
                "Call select_option(option_number) using the number from the user's last message."
            ),
        }, ensure_ascii=False)

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
            }, ensure_ascii=False)

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
            }, ensure_ascii=False)

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
            }, ensure_ascii=False)

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
        }, ensure_ascii=False)

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
        return get_line_variants(line_number=line_number, agency_name=agency_name, _internal=True)

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
        }, ensure_ascii=False)

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
        seen = set()  # deduplicate by (direction_id, headsign)
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

            key = (rows[0][0], rows[0][1])  # (direction_id, headsign)
            if key in seen:
                continue
            seen.add(key)

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

        return json.dumps(directions, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


def get_line_directions(route_ids: list) -> str:
    """
    Get unique directions for an identified line.
    Called after can_proceed=True to let the user choose a specific direction or all.
    """
    if _conn is None:
        return "Error: GTFS database not loaded yet."
    try:
        if not route_ids:
            return "Error: route_ids list is empty."

        directions = []
        seen = set()

        for route_id in route_ids:
            row = _conn.execute("""
                SELECT t.direction_id, t.trip_headsign, r.route_long_name
                FROM trips t
                JOIN routes r ON t.route_id = r.route_id
                WHERE t.route_id = ?
                LIMIT 1
            """, [route_id]).fetchone()

            if not row:
                continue

            direction_id, headsign, route_long_name = row
            key = (direction_id, headsign)
            if key in seen:
                continue
            seen.add(key)

            directions.append({
                "option_number": len(directions) + 1,
                "route_id": route_id,
                "direction_id": direction_id,
                "headsign": headsign,
                "route_long_name": route_long_name,
                "label": headsign,
            })

        all_option_num = len(directions) + 1
        options = [{"option_number": d["option_number"], "label": d["label"]} for d in directions]
        options.append({"option_number": all_option_num, "label": "כל הכיוונים"})

        selection_state.update({
            "pending_type": "direction",
            "directions": directions,
            "all_route_ids": list(route_ids),
        })

        return json.dumps({
            "clarification_needed": "direction",
            "directions_count": len(directions),
            "options": options,
            "directions": directions,
            "all_route_ids": list(route_ids),
        }, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


# Distinct colors per direction, reused across the map's line + stop-marker traces
_MAP_COLORS = ["#dc2626", "#2563eb", "#10b981", "#f59e0b", "#8b5cf6"]


def plot_route_map(route_ids: list, line_num: str = None, agency: str = None) -> str:
    """
    Interactive map: numbered stop markers per direction, each direction its
    own color, with a dropdown to isolate a single direction (mirrors
    plot_departure_schedule's route/day dropdowns). Reuses get_line_stops for
    the underlying stop data - there's no separate "geometry" query needed
    since shapes.txt isn't loaded (see gtfs_db.py). Points only, no connecting
    line between stops.

    line_num/agency are passed in from the caller (already known when a line
    is resolved) purely for the title - avoids a redundant DB lookup.

    This is not exposed to the LLM as a callable tool - agent/core.py calls
    it directly the moment a line is resolved, so a map always accompanies
    any answer about a specific line without depending on the model to
    remember to ask for one.
    """
    try:
        raw = get_line_stops(route_ids)
        directions = json.loads(raw)
        if not isinstance(directions, list) or not directions:
            return json.dumps({"error": "No stop data found for the given route_ids."}, ensure_ascii=False)

        fig = go.Figure()
        trace_for_direction = {}  # direction index -> trace index
        all_lats, all_lons = [], []

        def label_for(direction):
            headsign = direction.get("headsign") or str(direction.get("route_id", ""))
            return headsign[:20] + ("…" if len(headsign) > 20 else "")

        for i, direction in enumerate(directions):
            color = _MAP_COLORS[i % len(_MAP_COLORS)]
            stops = direction["stops"]
            lats = [s["lat"] for s in stops]
            lons = [s["lon"] for s in stops]
            all_lats += lats
            all_lons += lons

            fig.add_trace(go.Scattermapbox(
                lat=lats, lon=lons, mode="markers+text",
                marker=dict(size=20, color=color),
                text=[str(s["sequence"]) for s in stops],
                textfont=dict(size=10, color="white"),
                hovertext=[f"{s['stop_name']} — {s['stop_code']}" for s in stops],
                hoverinfo="text",
                name=label_for(direction), showlegend=True,
            ))
            trace_for_direction[i] = len(fig.data) - 1

        total_traces = len(fig.data)

        def visible_for(selected):
            visible = [False] * total_traces
            for idx in selected:
                visible[trace_for_direction[idx]] = True
            return visible

        buttons = [dict(
            label="All directions", method="update",
            args=[{"visible": visible_for(list(trace_for_direction.keys()))}],
        )]
        for i, direction in enumerate(directions):
            buttons.append(dict(
                label=label_for(direction), method="update",
                args=[{"visible": visible_for([i])}],
            ))

        mid_lat = sum(all_lats) / len(all_lats)
        mid_lon = sum(all_lons) / len(all_lons)

        title_text = "Route map"
        if line_num or agency:
            title_text += f" — Line {line_num or ''} | {agency or ''}"

        fig.update_layout(
            title=dict(text=title_text, y=0.99, yanchor="top"),
            mapbox=dict(style="open-street-map", center=dict(lat=mid_lat, lon=mid_lon), zoom=12),
            margin=dict(l=0, r=0, t=80, b=0),
            height=420,
            updatemenus=[dict(
                buttons=buttons, direction="down", x=0, y=0.99,
                xanchor="left", yanchor="top", showactive=True,
                pad=dict(t=25),
            )],
            legend=dict(title="Direction", x=0, y=0),
        )

        return json.dumps({"chart_type": "route_map", "figure_json": fig.to_json()}, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


_VALID_DAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
_DAY_COLS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def avg_departures_by_hour_by_day_type(route_ids, specific_day=None):
    """
    Queries the DuckDB tables already held in memory (loaded once at startup)
    instead of re-parsing stop_times.txt (~7.4M rows, ~400MB) from disk on
    every call - that re-parse was the main cause of slow responses.
    """
    route_ids = [int(r) for r in route_ids]
    placeholders = ",".join("?" * len(route_ids))

    rows = _conn.execute(f"""
        WITH filtered_trips AS (
            SELECT trip_id, route_id, service_id
            FROM trips
            WHERE route_id IN ({placeholders})
        ),
        first_stop AS (
            SELECT trip_id, departure_time,
                   ROW_NUMBER() OVER (PARTITION BY trip_id ORDER BY stop_sequence) AS rn
            FROM stop_times
            WHERE trip_id IN (SELECT trip_id FROM filtered_trips)
        )
        SELECT ft.route_id, ft.service_id, fs.departure_time
        FROM filtered_trips ft
        JOIN first_stop fs ON fs.trip_id = ft.trip_id AND fs.rn = 1
    """, route_ids).fetchall()

    if not rows:
        return {}

    service_ids = sorted(set(r[1] for r in rows))
    cal_placeholders = ",".join("?" * len(service_ids))
    cal_rows = _conn.execute(
        f"SELECT service_id, {', '.join(_DAY_COLS)} FROM calendar WHERE service_id IN ({cal_placeholders})",
        service_ids,
    ).fetchall()
    cal_map = {r[0]: dict(zip(_DAY_COLS, r[1:])) for r in cal_rows}

    if specific_day is not None:
        specific_day = specific_day.lower()
        if specific_day not in _VALID_DAYS:
            raise ValueError(f"specific_day must be one of: {_VALID_DAYS}")
        day_groups = {specific_day: [specific_day]}
    else:
        day_groups = {
            "working_days": ["sunday", "monday", "tuesday", "wednesday", "thursday"],
            "friday": ["friday"],
            "saturday": ["saturday"],
        }

    buckets = defaultdict(list)  # (route_id, hour) -> [service_id, ...]
    for route_id, service_id, departure_time in rows:
        hour = int(departure_time.split(":")[0]) % 24
        buckets[(route_id, hour)].append(service_id)

    result = {}
    for route_id in sorted(set(r[0] for r in rows)):
        result[route_id] = {group_name: {} for group_name in day_groups}
        hours = sorted(h for (rid, h) in buckets if rid == route_id)
        for hour in hours:
            sids = buckets[(route_id, hour)]
            for group_name, days in day_groups.items():
                daily_counts = [
                    sum(1 for sid in sids if cal_map.get(sid, {}).get(day) == 1)
                    for day in days
                ]
                result[route_id][group_name][hour] = round(sum(daily_counts) / len(daily_counts), 1)
    return result


def _build_departure_chart(route_departures_dict, specific_day=None):
    route_ids = list(route_departures_dict.keys())
    placeholders = ",".join("?" * len(route_ids))
    meta_rows = _conn.execute(f"""
        SELECT r.route_id, r.route_short_name, r.route_long_name, a.agency_name
        FROM routes r
        LEFT JOIN agency a ON r.agency_id = a.agency_id
        WHERE r.route_id IN ({placeholders})
    """, route_ids).fetchall()
    route_info_by_id = {
        r[0]: {"route_short_name": r[1] or "", "route_long_name": r[2] or str(r[0]), "agency_name": r[3] or ""}
        for r in meta_rows
    }

    if specific_day is not None:
        specific_day = specific_day.lower()
        day_types = [specific_day]
        day_labels = {specific_day: specific_day.capitalize()}
    else:
        day_types = ["working_days", "friday", "saturday"]
        day_labels = {"working_days": "Working days", "friday": "Friday", "saturday": "Saturday"}

    fig = go.Figure()
    route_meta = {}
    trace_map = {}
    trace_index = 0

    for route_id in route_ids:
        route_info = route_info_by_id.get(route_id, {})
        route_meta[route_id] = {
            "route_short_name": route_info.get("route_short_name", ""),
            "route_long_name": route_info.get("route_long_name", route_id),
            "agency_name": route_info.get("agency_name", ""),
        }
        data = route_departures_dict[route_id]
        all_hours = sorted({
            hour
            for day_type in day_types
            for hour in data.get(day_type, {}).keys()
        })
        trace_map[route_id] = {}
        for day_type in day_types:
            fig.add_trace(go.Bar(
                x=all_hours,
                y=[data.get(day_type, {}).get(hour, 0) for hour in all_hours],
                name=day_labels[day_type],
                visible=(route_id == route_ids[0] and day_type == day_types[0]),
                legendgroup=day_type,
            ))
            trace_map[route_id][day_type] = trace_index
            trace_index += 1

    total_traces = len(fig.data)

    def visibility_for(selected_route_id, selected_day_types):
        visible = [False] * total_traces
        for day_type in selected_day_types:
            visible[trace_map[selected_route_id][day_type]] = True
        return visible

    def title_for(route_id):
        meta = route_meta[route_id]
        return (
            "Departures by line and hour"
            f"<br><sup>Line {meta['route_short_name']} | {meta['agency_name']}</sup>"
        )

    direction_buttons = [
        dict(
            label=route_meta[route_id]["route_long_name"],
            method="update",
            args=[
                {"visible": visibility_for(route_id, [day_types[0]])},
                {"title.text": title_for(route_id), "legend.title.text": "Type of day"},
            ],
        )
        for route_id in route_ids
    ]

    if specific_day is not None:
        day_options = {specific_day.capitalize(): [specific_day]}
    else:
        day_options = {
            "Working days": ["working_days"],
            "Friday": ["friday"],
            "Saturday": ["saturday"],
            "All": day_types,
        }

    day_buttons = [
        dict(
            label=label,
            method="update",
            args=[
                {"visible": visibility_for(route_ids[0], selected_days)},
                {"title.text": title_for(route_ids[0]), "legend.title.text": "Type of day"},
            ],
        )
        for label, selected_days in day_options.items()
    ]

    fig.update_layout(
        title=dict(
            text=title_for(route_ids[0]),
            y=0.98, x=0.5, xanchor="center", yanchor="top",
            font=dict(size=24, family="Arial", color="black"),
            pad=dict(t=15),
        ),
        height=650,
        xaxis_title="Hour",
        yaxis_title="Average departures",
        barmode="group",
        legend_title="Type of day",
        margin=dict(l=55, r=35, t=190, b=60),
        updatemenus=[
            dict(buttons=direction_buttons, direction="down", x=0, y=1.27,
                 xanchor="left", yanchor="top", showactive=True),
            dict(buttons=day_buttons, direction="down", x=0, y=1.13,
                 xanchor="left", yanchor="top", showactive=True),
        ],
        annotations=[
            dict(text="<b>Direction:</b>", x=0.00, y=1.32, xref="paper", yref="paper",
                 showarrow=False, xanchor="left", yanchor="top"),
            dict(text="<b>Type of day:</b>", x=0.00, y=1.18, xref="paper", yref="paper",
                 showarrow=False, xanchor="left", yanchor="top"),
        ],
    )
    fig.update_xaxes(tickmode="array", tickvals=list(range(24)))
    return fig.to_json()


def _get_departure_timetable_raw(route_ids, specific_day):
    specific_day = specific_day.lower()
    if specific_day not in _VALID_DAYS:
        raise ValueError(f"specific_day must be one of: {_VALID_DAYS}")
    route_ids = [int(r) for r in route_ids]
    placeholders = ",".join("?" * len(route_ids))

    rows = _conn.execute(f"""
        WITH filtered_trips AS (
            SELECT t.trip_id, t.route_id, t.service_id, r.route_long_name
            FROM trips t
            JOIN routes r ON r.route_id = t.route_id
            WHERE t.route_id IN ({placeholders})
        ),
        active_trips AS (
            SELECT ft.trip_id, ft.route_id, ft.route_long_name
            FROM filtered_trips ft
            JOIN calendar c ON c.service_id = ft.service_id
            WHERE c.{specific_day} = 1
        ),
        first_stop AS (
            SELECT trip_id, departure_time,
                   ROW_NUMBER() OVER (PARTITION BY trip_id ORDER BY stop_sequence) AS rn
            FROM stop_times
            WHERE trip_id IN (SELECT trip_id FROM active_trips)
        )
        SELECT act.route_id, act.route_long_name, fs.departure_time
        FROM active_trips act
        JOIN first_stop fs ON fs.trip_id = act.trip_id AND fs.rn = 1
        ORDER BY act.route_id, fs.departure_time
    """, route_ids).fetchall()

    result = {}
    for route_id, route_long_name, departure_time in rows:
        entry = result.setdefault(route_id, {"headsign": route_long_name or str(route_id), "departures": []})
        entry["departures"].append(departure_time[:5])
    return result


def get_departure_timetable(route_ids: list, specific_day: str) -> str:
    """Get all departure times for a line on a specific day, grouped by direction."""
    try:
        directions = _get_departure_timetable_raw(route_ids, specific_day)
        return json.dumps({
            "timetable_type": "departure_timetable",
            "day": specific_day,
            "directions": directions,
        }, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


def get_departure_schedule(route_ids: list, specific_day: str = None) -> str:
    """Get average departures per hour by day type for a line."""
    try:
        result = avg_departures_by_hour_by_day_type(route_ids, specific_day)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


def plot_departure_schedule(route_ids: list, specific_day: str = None) -> str:
    """Generate an interactive departure schedule chart and return it for rendering."""
    try:
        data = avg_departures_by_hour_by_day_type(route_ids, specific_day)
        fig_json = _build_departure_chart(data, specific_day)
        return json.dumps(
            {"chart_type": "departure_schedule", "figure_json": fig_json},
            ensure_ascii=False,
        )
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
    "get_line_directions": get_line_directions,
    "get_line_stops": get_line_stops,
    "get_departure_timetable": get_departure_timetable,
    "get_departure_schedule": get_departure_schedule,
    "plot_departure_schedule": plot_departure_schedule,
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
            "name": "get_line_directions",
            "description": (
                "After a line is uniquely identified (can_proceed=true), call this BEFORE get_line_stops. "
                "Returns the available directions (headsigns) with option numbers. "
                "Present the list to the user and ask which direction they want, or all of them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "route_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "List of route_id integers for the identified line.",
                    }
                },
                "required": ["route_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_line_stops",
            "description": (
                "Get all stops for every direction of an identified line, ordered by stop_sequence. "
                "Returns stop_name, stop_code, lat, lon, and sequence number for each stop. "
                "Use this to answer any stop-related question: first stop, last stop, Nth stop, "
                "total stop count, full stop list, or map of stops. "
                "Always prefer this tool over run_sql for stop questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "route_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "List of route_id integers for the identified line (all directions).",
                    }
                },
                "required": ["route_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_departure_timetable",
            "description": (
                "Get the full list of departure times for a line on a specific day, grouped by direction. "
                "Use this when the user asks for a timetable or specific departure times. "
                "Only use AFTER the line is identified (can_proceed=true). One line at a time. "
                "Always ask the user which day they want if not specified."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "route_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "All route_id integers for the identified line (all directions).",
                    },
                    "specific_day": {
                        "type": "string",
                        "description": "Day of week in English lowercase: sunday, monday, tuesday, wednesday, thursday, friday, saturday.",
                    },
                },
                "required": ["route_ids", "specific_day"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_departure_schedule",
            "description": (
                "Get average departures per hour grouped by day type (working days, Friday, Saturday) "
                "for a line. Call this when the user asks about departure frequency, schedule, or how "
                "often a bus runs. Only use AFTER a line is uniquely identified (can_proceed=true). "
                "Only for one line at a time (same 5-digit route code). "
                "Always follow this with plot_departure_schedule."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "route_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "All route_id integers for the identified line (all directions from selected_line.route_ids).",
                    },
                    "specific_day": {
                        "type": "string",
                        "description": "Optional specific day (e.g. 'monday', 'friday'). Omit to get working_days/friday/saturday groups.",
                    },
                },
                "required": ["route_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plot_departure_schedule",
            "description": (
                "Generate an interactive chart of average departures per hour. "
                "Call this AFTER get_departure_schedule to render the visualization in the UI. "
                "Use the same route_ids and specific_day as get_departure_schedule."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "route_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "All route_id integers for the identified line (same as get_departure_schedule).",
                    },
                    "specific_day": {
                        "type": "string",
                        "description": "Optional specific day, same as passed to get_departure_schedule.",
                    },
                },
                "required": ["route_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "Execute a SELECT query on the GTFS database. "
                "Use this ONLY when the question cannot be answered by get_line_stops or a combination of available tools. "
                "Examples: schedule/timing questions, finding which lines serve a stop, cross-table queries. "
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
