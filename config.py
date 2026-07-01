import os
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")

# Automatic model fallback chain, tried in order for every new question.
# On a rate-limit/quota error the agent silently drops to the next entry
# (and reports the switch in the step log) instead of asking the user to pick.
#
# Note (2026-07-01): gemini-2.5-flash and gemini-2.5-flash-lite both have an
# announced retirement date of 2026-10-16 (per ai.google.dev/gemini-api/docs/deprecations) —
# fine for now, but re-check that page before then. gemini-2.0-flash was
# excluded here because Google's docs show it already past its 2026-06-01
# shutdown date; gpt-oss-20b fills that slot on the same GROQ_API_KEY instead.
MODEL_PRIORITY = [
    ("google", "gemini-2.5-flash"),
    ("groq", "openai/gpt-oss-120b"),
    ("google", "gemini-2.5-flash-lite"),
    ("groq", "openai/gpt-oss-20b"),
    ("groq", "qwen/qwen3-32b"),  # Groq preview model — can be pulled without notice, last resort only
]

OPEN_BUS_BASE_URL = "https://open-bus-stride-api.hasadna.org.il"
HEADERS = {
    "User-Agent": "AgenticAI-Course/1.0 (Educational; Bar-Ilan University)",
    "Accept": "application/json",
}