import streamlit as st
import pandas as pd
import pydeck as pdk
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

/* Page title a touch bigger and tighter */
h1 { font-weight: 800 !important; letter-spacing: -0.02em; }

/* Chat message bubbles: more breathing room + soft cards */
[data-testid="stChatMessage"] {
    padding: 14px 18px;
    border-radius: 14px;
    margin-bottom: 12px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    border: 1px solid #ececf1;
}

/* User messages: subtle blue tint so you can tell who's who */
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
    background: #eef4ff;
    border-color: #dce7fb;
}

/* Assistant messages: clean white */
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) {
    background: #ffffff;
}

/* Sidebar example buttons: clickable hover feel */
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

/* Chat input: rounder, softer */
[data-testid="stChatInput"] {
    border-radius: 12px;
    border: 1.5px solid #e3e6ea;
}
[data-testid="stChatInput"]:focus-within {
    border-color: #3b82f6;
    box-shadow: 0 0 0 3px #e8f0fe;
}

/* Section headers in sidebar: small caps, muted */
[data-testid="stSidebar"] h3 {
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #6b7280;
}

/* Code blocks in the agent log: subtle background */
[data-testid="stChatMessage"] pre {
    border-radius: 8px;
    font-size: 12px;
}
</style>
""", unsafe_allow_html=True)


@st.cache_resource(show_spinner="Loading GTFS data (first run may take a minute)...")
def load_gtfs():
    from agent.gtfs_db import download_and_load
    return download_and_load()


from agent import tools as _tools
_tools.set_connection(load_gtfs())


# --- Session state defaults ---
for key, default in {
    "chat_history": [],
    "agent_running": False,
    "agent_queue": None,
    "agent_log": [],
    "agent_coords": [],
    "agent_answer": None,
    "stop_event": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


def _agent_thread(question, context, result_queue, stop_event, provider):
    """Run the agent in a background thread, pushing updates into a queue."""
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
    result_queue.put(None)  # sentinel: agent finished


PROVIDERS = {
    "groq":   "Groq (llama-3.3-70b-versatile)",
    "google": "Google (gemini-2.5-flash)",
    "openai": "OpenAI (gpt-4o-mini)",
}

# --- Sidebar ---
with st.sidebar:
    st.header("Israel Transit Agent 🚍")
    st.caption("Ask about Israeli public transport schedules (GTFS data).")

    default_provider = os.environ.get("PROVIDER", "google")
    provider_keys = list(PROVIDERS.keys())
    selected_provider = st.selectbox(
        "LLM Provider",
        provider_keys,
        index=provider_keys.index(st.session_state.get("provider", default_provider)),
        format_func=lambda k: PROVIDERS[k],
        disabled=st.session_state.agent_running,
    )
    if selected_provider != st.session_state.get("provider", default_provider):
        st.session_state["provider"] = selected_provider
        st.rerun()

    st.divider()

    st.markdown("### Example questions")
    EXAMPLES = [
        "What is the first stop of line 5?",
        "How many stops does line 189 have?",
        "Show the stops of line 18 on a map",
        "What is the last stop of line 480?",
        "What operators run line 5?",
    ]
    for ex in EXAMPLES:
        if st.button(ex, use_container_width=True, disabled=st.session_state.agent_running):
            st.session_state["pending_question"] = ex

    st.divider()
    st.markdown("### Out of scope")
    for item in [
        "Real-time vehicle locations — not in GTFS",
        "Actual delays — requires live SIRI feed",
        "Passenger counts — not in GTFS",
    ]:
        st.caption(f"• {item}")

    st.divider()
    if st.button("New conversation", use_container_width=True,
                 disabled=st.session_state.agent_running):
        st.session_state["chat_history"] = []
        st.rerun()

st.title("Israel Transit Agent 🚍")

# --- Welcome subtitle (homepage only) ---
if not st.session_state.get("chat_history"):
    st.markdown(
        "<p style='font-size:18px; color:#4b5563; margin-top:-8px; margin-bottom:18px;'>"
        "👋 Ask me about bus stops, routes, or operators across Israel — try an example on the left."
        "</p>",
        unsafe_allow_html=True,
    )

# --- Persistent map (collapsible, always available) ---
with st.expander("🗺️ Map", expanded=True):
    latest_coords = st.session_state.get("agent_coords", [])

    if latest_coords:
        df = pd.DataFrame(latest_coords)
        mid_lat = df["lat"].mean()
        mid_lon = df["lon"].mean()

        scatter = pdk.Layer(
            "ScatterplotLayer",
            data=df,
            get_position="[lon, lat]",
            get_fill_color="[59, 130, 246, 200]",   # blue stops
            get_radius=120,
            pickable=True,
        )

        path_data = [{"path": df[["lon", "lat"]].values.tolist()}]
        path = pdk.Layer(
            "PathLayer",
            data=path_data,
            get_path="path",
            get_color="[239, 68, 68]",              # red route line
            get_width=40,
            width_min_pixels=3,
        )

        st.pydeck_chart(pdk.Deck(
            layers=[path, scatter],
            initial_view_state=pdk.ViewState(latitude=mid_lat, longitude=mid_lon, zoom=12),
            map_style="light",
            tooltip={"text": "{label}"},
        ), height=480)
    else:
        # Default homepage view: Israel, no route yet
        st.pydeck_chart(pdk.Deck(
            initial_view_state=pdk.ViewState(latitude=31.7, longitude=35.0, zoom=7.5),
            map_style="light",
        ), height=480)

# --- Display past messages ---
for msg in st.session_state["chat_history"]:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("coords"):
            df = pd.DataFrame(msg["coords"])
            st.map(df[["lat", "lon"]])
            with st.expander("Map points"):
                st.dataframe(df, use_container_width=True)

# ── If agent is running, poll for updates and show a Stop button ──
if st.session_state.agent_running:
    q = st.session_state.agent_queue

    # Drain whatever the background thread has produced so far
    while True:
        try:
            update = q.get_nowait()
        except queue.Empty:
            break

        if update is None:  # sentinel: agent finished
            st.session_state.agent_running = False

            # Store the completed turn in chat history
            st.session_state["chat_history"].append({
                "role": "assistant",
                "content": st.session_state.agent_answer or "",
                "coords": st.session_state.agent_coords,
            })
            st.rerun()

        # Accumulate updates
        st.session_state.agent_log = update.get("log", st.session_state.agent_log)
        st.session_state.agent_coords = update.get("coords", st.session_state.agent_coords)
        if update.get("answer"):
            st.session_state.agent_answer = update["answer"]

    # Render live progress
    with st.chat_message("assistant"):
        if st.button("⏹ Stop", type="secondary"):
            st.session_state.stop_event.set()

        with st.expander("Agent steps", expanded=True):
            for i, step in enumerate(st.session_state.agent_log, 1):
                if step["type"] == "retry":
                    st.warning(f"{i}. ⟳ {step['text']}")
                else:
                    st.markdown(f"**{i}. {step['tool']}**")
                    st.code(json.dumps(step["args"], ensure_ascii=False), language="json")
                    st.text(step["observation"])

        if st.session_state.agent_answer:
            st.markdown(st.session_state.agent_answer)

    # Poll again after a short pause
    time.sleep(0.5)
    st.rerun()

# ── Input (only shown when agent is idle) ──
else:
    pending = st.session_state.pop("pending_question", None)
    user_input = st.chat_input("Ask about stops, routes, schedules...") or pending

    if user_input:
        # Show user message
        st.session_state["chat_history"].append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)

        # Build context (all turns except the current one) and extract question
        question = user_input
        context = [
            {"role": m["role"], "content": m["content"]}
            for m in st.session_state["chat_history"][:-1]
        ]

        # Reset per-run state
        st.session_state.agent_log = []
        st.session_state.agent_coords = []
        st.session_state.agent_answer = None
        st.session_state.stop_event = threading.Event()
        st.session_state.agent_queue = queue.Queue()
        st.session_state.agent_running = True

        # Launch background thread
        active_provider = st.session_state.get("provider", os.environ.get("PROVIDER", "groq"))
        t = threading.Thread(
            target=_agent_thread,
            args=(question, context, st.session_state.agent_queue, st.session_state.stop_event, active_provider),
            daemon=True,
        )
        t.start()

        st.rerun()