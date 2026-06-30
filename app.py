import streamlit as st
import pandas as pd
import pydeck as pdk
import plotly.io as pio
import plotly.express as px
import threading
import queue
import time
import json
import os
import sys
import importlib

st.set_page_config(
    page_title="Israel Transit Agent",
    page_icon="🚌",
    layout="wide",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

h1 { font-weight: 800 !important; letter-spacing: -0.02em; }

[data-testid="stChatMessage"] {
    padding: 14px 18px;
    border-radius: 14px;
    margin-bottom: 12px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    border: 1px solid #ececf1;
}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
    background: #eef4ff;
    border-color: #dce7fb;
}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) {
    background: #ffffff;
}

[data-testid="stSidebar"] .stButton button {
    border-radius: 9px;
    border: 1px solid #e3e6ea;
    transition: all .12s ease;
    text-align: left;
}
[data-testid="stSidebar"] .stButton button:hover {
    border-color: #3b82f6;
    color: #3b82f6;
    background: #f0f6ff;
}

[data-testid="stChatInput"] {
    border-radius: 12px;
    border: 1.5px solid #e3e6ea;
}
[data-testid="stChatInput"]:focus-within {
    border-color: #3b82f6;
    box-shadow: 0 0 0 3px #e8f0fe;
}

[data-testid="stSidebar"] h3 {
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #6b7280;
}

[data-testid="stChatMessage"] pre {
    border-radius: 8px;
    font-size: 12px;
}

/* Viz panel section headers */
.viz-section-header {
    font-size: 13px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #6b7280;
    margin-bottom: 8px;
}

/* Empty placeholder cards */
.viz-placeholder {
    display: flex;
    align-items: center;
    justify-content: center;
    border: 1.5px dashed #d1d5db;
    border-radius: 12px;
    color: #9ca3af;
    font-size: 13px;
    text-align: center;
    padding: 24px 16px;
}
</style>
""", unsafe_allow_html=True)


@st.cache_resource(show_spinner="Loading GTFS data (first run may take a minute)...")
def load_gtfs():
    from agent.gtfs_db import download_and_load
    return download_and_load()


from agent import tools as _tools
_tools.set_connection(load_gtfs())


@st.cache_data(show_spinner=False)
def _agency_lines_chart_json():
    conn = load_gtfs()
    rows = conn.execute("""
        SELECT a.agency_name,
               COUNT(DISTINCT regexp_extract(r.route_desc, '[0-9]{5}')) AS line_count
        FROM routes r
        JOIN agency a ON r.agency_id = a.agency_id
        WHERE r.route_desc IS NOT NULL
          AND regexp_extract(r.route_desc, '[0-9]{5}') != ''
        GROUP BY a.agency_name
        ORDER BY line_count DESC
        LIMIT 10
    """).fetchall()
    df = pd.DataFrame(rows, columns=["Agency", "Lines"])
    fig = px.bar(
        df, x="Agency", y="Lines",
        title="Top 10 agencies by number of lines",
        color_discrete_sequence=["#3b82f6"],
    )
    fig.update_layout(
        height=300,
        margin=dict(l=40, r=20, t=50, b=120),
        showlegend=False,
        xaxis_title="",
        yaxis_title="Number of lines",
    )
    fig.update_xaxes(tickangle=40)
    return fig.to_json()


def render_israel_overview_map():
    st.pydeck_chart(pdk.Deck(
        layers=[],
        initial_view_state=pdk.ViewState(latitude=31.5, longitude=34.85, zoom=7, pitch=0),
        map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
    ), height=320, use_container_width=True)


# --- Session state defaults ---
for key, default in {
    "chat_history": [],
    "agent_running": False,
    "agent_queue": None,
    "agent_log": [],
    "agent_coords": [],
    "agent_map_data": None,
    "agent_chart_data": None,
    "agent_timetable_data": None,
    "agent_answer": None,
    "stop_event": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


def _step_label(tool: str, args: dict, obs: str) -> str:
    line = args.get("line_number", "")
    line_tag = f" {line}" if line else ""
    try:
        data = json.loads(obs)
    except Exception:
        data = {}

    if tool == "get_line_variants":
        if isinstance(data, dict):
            if data.get("clarification_needed") == "agency":
                return f"🔍 Found line{line_tag} — operated by multiple agencies, asking user to choose"
            if data.get("clarification_needed") == "route":
                return f"🔍 Found line{line_tag} — multiple routes exist, asking user to choose"
            if data.get("can_proceed"):
                agency = data.get("agency_name", "")
                return f"✅ Line{line_tag} identified" + (f" ({agency})" if agency else "")
        return f"🔍 Looking up line{line_tag}..."

    if tool == "select_option":
        return f"✅ Option {args.get('option_number', '')} selected"

    if tool == "get_line_directions":
        n = len(data.get("directions", [])) if isinstance(data, dict) else 0
        return f"📋 Found {n} direction(s), asking user to choose"

    if tool == "get_line_stops":
        if isinstance(data, list):
            total = sum(d.get("stops_count", 0) for d in data)
            return f"🚏 Loaded {total} stops across {len(data)} direction(s)"
        return "🚏 Loading stop list..."

    if tool == "show_map":
        return "🗺️ Building the route map..."

    if tool == "get_departure_timetable":
        day = args.get("specific_day", "")
        if isinstance(data, dict) and data.get("timetable_type"):
            dirs = data.get("directions", {})
            total = sum(len(v.get("departures", [])) for v in dirs.values())
            return f"🕐 Timetable loaded — {total} departures across {len(dirs)} direction(s) on {day}"
        return f"🕐 Loading timetable" + (f" for {day}" if day else "") + "..."

    if tool == "get_departure_schedule":
        return "📊 Calculating average departures per hour..."

    if tool == "plot_departure_schedule":
        return "📈 Generating the departure chart..."

    if tool == "run_sql":
        n = len(data) if isinstance(data, list) else "?"
        return f"🔎 Database query returned {n} result(s)"

    if tool == "get_schema":
        return "📂 Reading database schema"

    return f"🔧 {tool}"


def render_timetable(timetable_data: dict):
    directions = timetable_data.get("directions", {})
    day = timetable_data.get("day", "")
    if not directions:
        return
    cols = {}
    for info in directions.values():
        headsign = info.get("headsign", "?")
        cols[headsign] = info.get("departures", [])
    max_len = max(len(v) for v in cols.values()) if cols else 0
    padded = {k: v + [""] * (max_len - len(v)) for k, v in cols.items()}
    df = pd.DataFrame(padded)
    if day:
        st.caption(f"Timetable — {day.capitalize()}")
    st.dataframe(df, use_container_width=True, hide_index=True)


def render_route_map(map_data: dict):
    routes = map_data.get("routes", [])
    if not routes:
        return

    title = routes[0]["route_long_name"] if len(routes) == 1 else "מסלול הקו"
    st.markdown(f"<div style='font-weight:600;font-size:14px;margin-bottom:6px'>{title}</div>",
                unsafe_allow_html=True)

    scatter_data, text_data = [], []
    for route in routes:
        color = route.get("color", [220, 38, 38])
        for stop in route["stops"]:
            scatter_data.append({
                "lat": stop["lat"], "lon": stop["lon"],
                "color": color,
                "stop_name": stop["stop_name"],
                "stop_code": stop.get("stop_code", ""),
                "sequence": stop["sequence"],
            })
            text_data.append({
                "lat": stop["lat"], "lon": stop["lon"],
                "label": str(stop["sequence"]),
            })

    if not scatter_data:
        return

    mid_lat = sum(s["lat"] for s in scatter_data) / len(scatter_data)
    mid_lon = sum(s["lon"] for s in scatter_data) / len(scatter_data)

    scatter = pdk.Layer(
        "ScatterplotLayer",
        data=scatter_data,
        get_position="[lon, lat]",
        get_fill_color="color",
        get_line_color=[255, 255, 255],
        get_radius=60,
        radius_min_pixels=6,
        radius_max_pixels=14,
        stroked=True,
        filled=True,
        line_width_min_pixels=2,
        pickable=True,
    )

    text = pdk.Layer(
        "TextLayer",
        data=text_data,
        get_position="[lon, lat]",
        get_text="label",
        get_size=11,
        get_color=[40, 40, 40],
        get_background_color=[255, 255, 255, 200],
        background=True,
        background_padding=[2, 1, 2, 1],
        get_pixel_offset=[12, -12],
        pickable=False,
    )

    st.pydeck_chart(pdk.Deck(
        layers=[scatter, text],
        initial_view_state=pdk.ViewState(latitude=mid_lat, longitude=mid_lon, zoom=13),
        map_style="light",
        tooltip={"text": "{stop_name}\nקוד: {stop_code}\nעצירה מס׳: {sequence}"},
    ), height=400, use_container_width=True)

    if len(routes) > 1:
        parts = []
        for route in routes:
            c = route.get("color", [220, 38, 38])
            hex_c = "#{:02x}{:02x}{:02x}".format(*c)
            parts.append(f'<span style="color:{hex_c};font-size:15px">●</span> {route["route_long_name"]}')
        st.markdown(" &nbsp;&nbsp; ".join(parts), unsafe_allow_html=True)


def _agent_thread(question, context, result_queue, stop_event, provider):
    import os, sys, importlib
    os.environ["PROVIDER"] = provider
    for mod_name in ["config", "agent.utils", "agent.core"]:
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
    from agent.core import react_agent
    try:
        for update in react_agent(question, context=context, stop_event=stop_event):
            result_queue.put(update)
    except Exception as e:
        result_queue.put({
            "status": "done", "log": [], "coords": [],
            "answer": f"Error: {e}",
        })
    result_queue.put(None)


PROVIDERS = {
    "groq":   "Groq (llama-3.3-70b-versatile)",
    "google": "Google (gemini-2.5-flash)",
}

# --- Sidebar ---
with st.sidebar:
    default_provider = "google"
    provider_keys = list(PROVIDERS.keys())
    selected_provider = st.selectbox(
        "LLM Provider",
        provider_keys,
        index=provider_keys.index(st.session_state.get("provider", default_provider)),
        format_func=lambda k: PROVIDERS[k],
        disabled=st.session_state.agent_running,
    )
    st.caption("Free tier models — may experience rate limits or token shortages.")
    if selected_provider != st.session_state.get("provider", default_provider):
        st.session_state["provider"] = selected_provider
        st.rerun()

    st.divider()

    st.markdown("### What I can help with")
    st.markdown("""
**Line information**
- Stops & stop order
- Departure schedule & timetable
- Operators & agencies

**Coming soon**
- Mapping & geospatial queries
- Comparison between lines
""")

    st.divider()

    st.markdown("### Questions for example")

    EXAMPLES = [
        "What is the first stop of line 189 of Dan?",
        "How many operators run line 5?",
        "What is the timetable of line 125 of Dan on Thursday?",
        "How many trips has line 13 of Metropolin?",
    ]
    for ex in EXAMPLES:
        if st.button(ex, use_container_width=True, disabled=st.session_state.agent_running):
            st.session_state["pending_question"] = ex

    st.divider()
    if st.button("New conversation", use_container_width=True,
                 disabled=st.session_state.agent_running):
        st.session_state["chat_history"] = []
        st.session_state["agent_map_data"] = None
        st.rerun()


# ── Pull page content up ──
st.markdown("<style>div.block-container{padding-top:1.5rem;}</style>", unsafe_allow_html=True)

# ── Two-column layout with separator ──
col_chat, col_sep, col_viz = st.columns([1, 0.02, 1], gap="small")

with col_sep:
    st.markdown(
        "<div style='border-left:2px solid #e3e6ea; height:700px; margin:0 auto;'></div>",
        unsafe_allow_html=True,
    )

# ── Right column: visualization panel ──
with col_viz:
    st.markdown("#### 🗺️ Map")

    display_map = st.session_state.agent_map_data
    if not display_map:
        for msg in reversed(st.session_state.chat_history):
            if msg.get("map_data"):
                display_map = msg["map_data"]
                break

    if display_map:
        render_route_map(display_map)
    else:
        render_israel_overview_map()

    st.markdown("<div style='margin-top:24px'></div>", unsafe_allow_html=True)
    st.markdown("#### 📊 Charts")

    display_chart = st.session_state.agent_chart_data
    if not display_chart:
        for msg in reversed(st.session_state.chat_history):
            if msg.get("chart_data"):
                display_chart = msg["chart_data"]
                break

    if display_chart:
        try:
            fig = pio.from_json(display_chart["figure_json"])
            st.plotly_chart(fig, use_container_width=True)
        except Exception as e:
            st.error(f"Chart render error: {e}")
    else:
        try:
            default_fig = pio.from_json(_agency_lines_chart_json())
            st.plotly_chart(default_fig, use_container_width=True)
        except Exception:
            st.markdown(
                "<div class='viz-placeholder' style='height:180px'>"
                "Chart will appear here based on your question"
                "</div>",
                unsafe_allow_html=True,
            )


# ── Vertical separator between columns ──
st.markdown("""
<style>
[data-testid="column"]:first-child {
    border-right: 2px solid #e3e6ea;
    padding-right: 2rem;
}
[data-testid="column"]:last-child {
    padding-left: 2rem;
}
</style>
""", unsafe_allow_html=True)

# ── Left column: chat pane ──
with col_chat:
    st.title("ISRAEL TRANSIT AGENT 🚍")

    # Fixed-height scrollable message window
    chat_window = st.container(height=580, border=False)

    with chat_window:
        if not st.session_state.get("chat_history"):
            st.markdown(
                "<p style='font-size:20px; font-weight:600; color:#1f2937; margin-top:8px; margin-bottom:6px;'>"
                "What do you want to know..?"
                "</p>",
                unsafe_allow_html=True,
            )
            st.caption("Tip: include the operator and line number for faster, more accurate results — e.g. \"What is the first stop of line 125 of Dan?\"")


        # Chat history
        for msg in st.session_state["chat_history"]:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
                if msg.get("timetable_data"):
                    render_timetable(msg["timetable_data"])

        # ── Agent running: poll and show live progress ──
        if st.session_state.agent_running:
            q = st.session_state.agent_queue

            while True:
                try:
                    update = q.get_nowait()
                except queue.Empty:
                    break

                if update is None:
                    st.session_state.agent_running = False
                    st.session_state["chat_history"].append({
                        "role": "assistant",
                        "content": st.session_state.agent_answer or "",
                        "map_data": st.session_state.agent_map_data,
                        "chart_data": st.session_state.agent_chart_data,
                        "timetable_data": st.session_state.agent_timetable_data,
                    })
                    st.rerun()

                st.session_state.agent_log = update.get("log", st.session_state.agent_log)
                st.session_state.agent_coords = update.get("coords", st.session_state.agent_coords)
                if update.get("map_data"):
                    st.session_state.agent_map_data = update["map_data"]
                if update.get("chart_data"):
                    st.session_state.agent_chart_data = update["chart_data"]
                if update.get("timetable_data"):
                    st.session_state.agent_timetable_data = update["timetable_data"]
                if update.get("answer"):
                    st.session_state.agent_answer = update["answer"]

            with st.chat_message("assistant"):
                if st.button("⏹ Stop", type="secondary"):
                    st.session_state.stop_event.set()

                with st.expander("🤔 Agent steps", expanded=True):
                    for step in st.session_state.agent_log:
                        if step["type"] == "retry":
                            text = step.get("text", "")
                            if "rate limit" in text or "waiting" in text:
                                import re as _re
                                secs = _re.search(r"(\d+)s", text)
                                wait = f" ({secs.group(1)}s)" if secs else ""
                                label = f"⏳ Rate limit hit, waiting{wait}..."
                            elif "tool call as text" in text:
                                label = "⟳ Model response malformed, retrying..."
                            elif "without calling any tool" in text:
                                label = "⟳ No tool called, nudging model..."
                            elif "tool_use_failed" in text:
                                label = "⟳ Tool call failed, retrying..."
                            else:
                                label = f"⟳ Retrying..."
                            st.caption(label)
                        else:
                            label = _step_label(
                                step["tool"], step["args"], step.get("observation", "")
                            )
                            st.caption(label)

                if st.session_state.agent_answer:
                    st.markdown(st.session_state.agent_answer)
                if st.session_state.agent_timetable_data:
                    render_timetable(st.session_state.agent_timetable_data)

    # ── Input below the scrollable window ──
    if st.session_state.agent_running:
        time.sleep(0.5)
        st.rerun()
    else:
        pending = st.session_state.pop("pending_question", None)
        user_input = st.chat_input("Ask about stops, routes, schedules...") or pending

        if user_input:
            st.session_state["chat_history"].append({"role": "user", "content": user_input})

            question = user_input
            context = [
                {"role": m["role"], "content": m["content"]}
                for m in st.session_state["chat_history"][:-1]
            ]

            st.session_state.agent_log = []
            st.session_state.agent_coords = []
            st.session_state.agent_map_data = None
            st.session_state.agent_chart_data = None
            st.session_state.agent_timetable_data = None
            st.session_state.agent_answer = None
            st.session_state.stop_event = threading.Event()
            st.session_state.agent_queue = queue.Queue()
            st.session_state.agent_running = True

            active_provider = st.session_state.get("provider", os.environ.get("PROVIDER", "google"))
            t = threading.Thread(
                target=_agent_thread,
                args=(question, context, st.session_state.agent_queue,
                      st.session_state.stop_event, active_provider),
                daemon=True,
            )
            t.start()

            st.rerun()
