import streamlit as st
import pandas as pd
from agent.core import react_agent
import json

st.set_page_config(
    page_title="Israel Transit Agent",
    page_icon="🚌",
    layout="wide",
)


@st.cache_resource(show_spinner="Loading GTFS data (first run may take a minute)...")
def load_gtfs():
    from agent.gtfs_db import download_and_load
    return download_and_load()


# Always wire the cached connection into tools, even after a Streamlit rerun
from agent import tools as _tools
_tools.set_connection(load_gtfs())

# --- Sidebar ---
with st.sidebar:
    st.header("Israel Transit Agent 🚍")
    st.caption("Ask about Israeli public transport schedules (GTFS data).")

    st.markdown("### Example questions")
    EXAMPLES = [
        "What is the first stop of line 5?",
        "How many stops does line 189 have?",
        "Show the stops of line 18 on a map",
        "What is the last stop of line 480?",
        "What operators run line 5?",
    ]
    for ex in EXAMPLES:
        if st.button(ex, use_container_width=True):
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
    if st.button("New conversation", use_container_width=True):
        st.session_state["chat_history"] = []
        st.rerun()

# --- Chat state ---
if "chat_history" not in st.session_state:
    st.session_state["chat_history"] = []

st.title("Israel Transit Agent")

# --- Display past messages ---
for msg in st.session_state["chat_history"]:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("coords"):
            df = pd.DataFrame(msg["coords"])
            st.map(df[["lat", "lon"]])
            with st.expander("Map points"):
                st.dataframe(df, use_container_width=True)

# --- Input: example buttons or typed question ---
pending = st.session_state.pop("pending_question", None)
user_input = st.chat_input("Ask about stops, routes, schedules...") or pending

if user_input:
    st.session_state["chat_history"].append({"role": "user", "content": user_input})

    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        status_ph = st.empty()
        log_ph = st.empty()

        final_log, final_coords, final_answer = [], [], ""

        # Pass the full conversation history (user + assistant turns only)
        agent_history = [
            {"role": m["role"], "content": m["content"]}
            for m in st.session_state["chat_history"]
        ]

        for update in react_agent(agent_history):
            final_log = update.get("log", [])
            final_coords = update.get("coords", [])

            if update["status"] == "calling":
                status_ph.info(f"⚙️ Calling **{update['tool']}**...")
            elif update["status"] == "step":
                status_ph.info(f"✅ {len(final_log)} step(s) done, thinking...")
            elif update["status"] == "retry":
                status_ph.warning("⟳ Retrying...")

            with log_ph.container():
                with st.expander("Agent steps", expanded=False):
                    for i, step in enumerate(final_log, 1):
                        if step["type"] == "retry":
                            st.warning(f"{i}. ⟳ {step['text']}")
                        else:
                            st.markdown(f"**{i}. {step['tool']}**")
                            st.code(
                                json.dumps(step["args"], ensure_ascii=False),
                                language="json",
                            )
                            st.text(step["observation"])

        status_ph.empty()
        final_answer = update.get("answer", "")

        st.markdown(final_answer)

        if final_coords:
            df = pd.DataFrame(final_coords)
            st.map(df[["lat", "lon"]])
            with st.expander("Map points"):
                st.dataframe(df, use_container_width=True)

    # Store the assistant turn (with coords for re-rendering)
    st.session_state["chat_history"].append({
        "role": "assistant",
        "content": final_answer,
        "coords": final_coords,
    })
