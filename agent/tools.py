import json
import requests
from config import OPEN_BUS_BASE_URL, HEADERS


# --- Tool functions ---
# These are the actual functions the agent can call.
# Each one returns a plain string — the LLM reads that string as the tool result.

def get_open_bus_endpoints(filter_keyword: str = "") -> str:
    """Discover available Open Bus API endpoints and their query parameters."""
    url = OPEN_BUS_BASE_URL + "/openapi.json"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=60)
        resp.raise_for_status()
        paths = resp.json().get("paths", {})
        lines = []
        for path, methods in sorted(paths.items()):
            if filter_keyword and filter_keyword.lower() not in path.lower():
                continue
            get = methods.get("get")
            if not get:
                continue
            params = [p.get("name") for p in get.get("parameters", [])]
            lines.append(f"{path}\n    params: {', '.join(params) if params else '(none)'}")
        return "\n".join(lines) if lines else f"No endpoints matched '{filter_keyword}'."
    except Exception as e:
        return f"Error fetching API spec: {e}"


def query_open_bus_api(endpoint: str, params_json: str = "{}") -> str:
    """Query an Open Bus STRIDE API list endpoint."""
    try:
        params = json.loads(params_json) if params_json else {}
    except json.JSONDecodeError:
        return f"Invalid params_json (must be a JSON object string): {params_json!r}"
    try:
        resp = requests.get(OPEN_BUS_BASE_URL + endpoint, params=params, headers=HEADERS, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            # Only return first 5 records to keep the response small for the LLM
            preview = data[:5]
            return (f"Returned {len(data)} record(s) (showing first {len(preview)}).\n"
                    + json.dumps(preview, ensure_ascii=False, default=str))
        return json.dumps(data, ensure_ascii=False, default=str)
    except requests.exceptions.HTTPError as e:
        body = resp.text[:500] if "resp" in dir() else ""
        return f"HTTP error: {e}. Response body: {body}"
    except requests.exceptions.RequestException as e:
        return f"Request error: {e}"
    except Exception as e:
        return f"Unexpected error: {e}"


# --- Private helpers ---

def _get_routes_for_line(line_number: str, operator: str = "", cluster: str = ""):
    """
    Fetch route records for a line, with optional operator/cluster filters.
    Returns (routes_list, error_string). error_string is "" on success.
    """
    params = {"route_short_name": line_number, "limit": 100}
    if operator:
        params["agency__name__icontains"] = operator
    if cluster:
        params["cluster__name__icontains"] = cluster
    try:
        r = requests.get(
            OPEN_BUS_BASE_URL + "/gtfs_routes/list",
            params=params,
            headers=HEADERS,
            timeout=60,
        )
        r.raise_for_status()
        return r.json(), ""
    except requests.exceptions.HTTPError as e:
        return [], f"HTTP error: {e}"
    except Exception as e:
        return [], f"Error: {e}"


def _disambiguate(routes, line_number: str):
    """
    Group routes by (operator, cluster). If more than one distinct combination
    exists, return a message listing the options so the agent can ask the user.
    Returns None when there is no ambiguity.
    """
    combos = {}
    for route in routes:
        operator = route.get("agency__name", route.get("agency_id", "?"))
        cluster  = route.get("cluster__name", route.get("cluster_id", "?"))
        key = (operator, cluster)
        combos[key] = True

    if len(combos) <= 1:
        return None

    options = [
        {"operator": op, "cluster": cl}
        for op, cl in sorted(combos.keys())
    ]
    return (
        f"Line {line_number} matches {len(options)} different (operator, cluster) combinations. "
        f"Please tell the user and ask them to choose one:\n"
        + json.dumps(options, ensure_ascii=False)
        + "\nThen call this tool again with the 'operator' and 'cluster' parameters filled in."
    )


# --- Tool functions ---

def get_line_stops(line_number: str, date: str = "2024-01-01",
                   operator: str = "", cluster: str = "") -> str:
    """Get all stops for one trip of a bus line on a given date, ordered by stop sequence."""
    try:
        # Step 1: resolve routes and check for operator/cluster ambiguity
        routes, err = _get_routes_for_line(line_number, operator, cluster)
        if err:
            return err
        if not routes:
            suffix = f" (operator={operator!r}, cluster={cluster!r})" if (operator or cluster) else ""
            return f"No routes found for line {line_number}{suffix}."

        msg = _disambiguate(routes, line_number)
        if msg:
            return msg

        # Step 2: get one ride for the first route on this date
        # (all remaining routes share the same operator+cluster, so any route_id is fine)
        route_id = routes[0]["id"]
        operator_name = routes[0].get("agency__name", "")
        cluster_name  = routes[0].get("cluster__name", "")

        r1 = requests.get(
            OPEN_BUS_BASE_URL + "/gtfs_rides/list",
            params={
                "gtfs_route_id": route_id,
                "start_time_from": f"{date}T00:00:00Z",
                "start_time_to":   f"{date}T23:59:59Z",
                "limit": 1,
            },
            headers=HEADERS,
            timeout=60,
        )
        r1.raise_for_status()
        rides = r1.json()
        if not rides:
            return f"No rides found for line {line_number} on {date}. Try a different date."
        ride_id = rides[0]["id"]

        # Step 3: get all stops for that single ride (one direction, no mixing)
        r2 = requests.get(
            OPEN_BUS_BASE_URL + "/gtfs_ride_stops/list",
            params={"gtfs_ride_id": ride_id, "order_by": "stop_sequence asc", "limit": 200},
            headers=HEADERS,
            timeout=60,
        )
        r2.raise_for_status()
        rows = r2.json()
        stops = [
            {
                "stop_sequence": row.get("stop_sequence"),
                "name": row.get("gtfs_stop__name", ""),
                "lat": row.get("gtfs_stop__lat"),
                "lon": row.get("gtfs_stop__lon"),
            }
            for row in rows
        ]
        return (
            f"Line {line_number} | operator: {operator_name} | cluster: {cluster_name} | "
            f"{date}: {len(stops)} stop(s).\n"
            + json.dumps(stops, ensure_ascii=False)
        )
    except requests.exceptions.HTTPError as e:
        return f"HTTP error: {e}"
    except Exception as e:
        return f"Error: {e}"


def count_rides_by_direction(line_number: str, date_from: str, date_to: str,
                             operator: str = "", cluster: str = "") -> str:
    """Count rides for a bus line broken down by direction, over a given date range."""
    try:
        # Step 1: resolve routes and check for operator/cluster ambiguity
        routes, err = _get_routes_for_line(line_number, operator, cluster)
        if err:
            return err
        if not routes:
            suffix = f" (operator={operator!r}, cluster={cluster!r})" if (operator or cluster) else ""
            return f"No routes found for line {line_number}{suffix}."

        msg = _disambiguate(routes, line_number)
        if msg:
            return msg

        # Step 2: count rides per route (each direction is its own route record)
        results = []
        for route in routes:
            route_id   = route["id"]
            direction  = route.get("route_direction", "?")
            long_name  = route.get("route_long_name", "")
            agency     = route.get("agency__name", route.get("agency_id", ""))
            cluster_nm = route.get("cluster__name", route.get("cluster_id", ""))

            r2 = requests.get(
                OPEN_BUS_BASE_URL + "/gtfs_rides/list",
                params={
                    "gtfs_route_id":   route_id,
                    "start_time_from": f"{date_from}T00:00:00Z",
                    "start_time_to":   f"{date_to}T23:59:59Z",
                    "get_count":       "true",
                },
                headers=HEADERS,
                timeout=60,
            )
            r2.raise_for_status()
            data = r2.json()
            count = data if isinstance(data, int) else data.get("num_results", data.get("count", "?"))

            if count == 0:
                continue

            results.append({
                "route_id":   route_id,
                "direction":  direction,
                "route_name": long_name,
                "operator":   agency,
                "cluster":    cluster_nm,
                "ride_count": count,
            })

        if not results:
            return f"No rides found for line {line_number} between {date_from} and {date_to}."

        return (
            f"Line {line_number} rides from {date_from} to {date_to}:\n"
            + json.dumps(results, ensure_ascii=False)
        )
    except requests.exceptions.HTTPError as e:
        return f"HTTP error: {e}"
    except Exception as e:
        return f"Error: {e}"


# --- Tools map ---
# Used by core.py to call the right function by name when the LLM requests a tool call.

tools_map = {
    "get_open_bus_endpoints": get_open_bus_endpoints,
    "query_open_bus_api": query_open_bus_api,
    "get_line_stops": get_line_stops,
    "count_rides_by_direction": count_rides_by_direction,
}


# --- Tools schema ---
# Sent to the LLM on every request so it knows what tools exist and how to use them.
# params_json is always a JSON string (not a dict) — that's how function-calling works.

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_open_bus_endpoints",
            "description": (
                "Discover the available Open Bus STRIDE API endpoints and their exact query "
                "parameter names by reading the live OpenAPI spec. Use this FIRST whenever you "
                "are unsure which endpoint or parameter to use, instead of guessing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filter_keyword": {
                        "type": "string",
                        "description": (
                            "Optional substring to filter endpoints by path, "
                            "e.g. 'siri', 'gtfs', 'vehicle', 'ride'. Leave empty to list all."
                        ),
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_open_bus_api",
            "description": (
                "Query an Open Bus STRIDE API list endpoint (Israeli public transport: "
                "real-time SIRI data and planned GTFS data). Returns JSON records. "
                "Use for bus stops, ride counts, live vehicle locations, and speed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "endpoint": {
                        "type": "string",
                        "description": (
                            "The API path to call, e.g. '/siri_vehicle_locations/list', "
                            "'/gtfs_ride_stops/list', '/gtfs_rides/list'."
                        ),
                    },
                    "params_json": {
                        "type": "string",
                        "description": (
                            "Query parameters as a JSON OBJECT STRING (not a nested object). "
                            "Example: '{\"gtfs_route__route_short_name\": \"189\", \"limit\": 5}'. "
                            "Use '{}' for no parameters."
                        ),
                    },
                },
                "required": ["endpoint"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_line_stops",
            "description": (
                "Get all stops for a bus line on a given date, ordered by stop sequence. "
                "Returns stop name, coordinates (lat/lon), and sequence number for one trip. "
                "Prefer this over query_open_bus_api for any question about stops of a line. "
                "If the tool returns an ambiguity message, ask the user to choose operator and cluster, "
                "then call again with those values filled in."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "line_number": {
                        "type": "string",
                        "description": "The bus line number, e.g. '189', '480', '19'.",
                    },
                    "date": {
                        "type": "string",
                        "description": (
                            "Date in YYYY-MM-DD format, e.g. '2024-01-01'. "
                            "Defaults to 2024-01-01 if not specified."
                        ),
                    },
                    "operator": {
                        "type": "string",
                        "description": (
                            "Optional. Operator (agency) name to filter by, e.g. 'דן', 'אגד'. "
                            "Use when the tool returns an ambiguity message listing multiple operators."
                        ),
                    },
                    "cluster": {
                        "type": "string",
                        "description": (
                            "Optional. Cluster (city/area) name to filter by, e.g. 'תל אביב', 'בני ברק'. "
                            "Use when the tool returns an ambiguity message listing multiple clusters."
                        ),
                    },
                },
                "required": ["line_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "count_rides_by_direction",
            "description": (
                "Count how many rides a bus line has in a date range, broken down by direction. "
                "Each direction is a separate route in GTFS (same line number, different route_direction). "
                "Returns ride count, direction, route name (origin→destination), operator, and cluster per direction. "
                "If the tool returns an ambiguity message, ask the user to choose operator and cluster, "
                "then call again with those values filled in. "
                "Use this instead of query_open_bus_api for ride-count questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "line_number": {
                        "type": "string",
                        "description": "The bus line number, e.g. '189', '480', '19'.",
                    },
                    "date_from": {
                        "type": "string",
                        "description": "Start date in YYYY-MM-DD format, e.g. '2023-01-01'.",
                    },
                    "date_to": {
                        "type": "string",
                        "description": (
                            "End date in YYYY-MM-DD format. Use the same value as date_from "
                            "for a single day, e.g. '2023-01-01'."
                        ),
                    },
                    "operator": {
                        "type": "string",
                        "description": (
                            "Optional. Operator (agency) name to filter by, e.g. 'דן', 'אגד'. "
                            "Use when the tool returns an ambiguity message listing multiple operators."
                        ),
                    },
                    "cluster": {
                        "type": "string",
                        "description": (
                            "Optional. Cluster (city/area) name to filter by, e.g. 'תל אביב', 'בני ברק'. "
                            "Use when the tool returns an ambiguity message listing multiple clusters."
                        ),
                    },
                },
                "required": ["line_number", "date_from", "date_to"],
            },
        },
    },
]