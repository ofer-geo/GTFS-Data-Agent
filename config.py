import os
from dotenv import load_dotenv

load_dotenv()

PROVIDER = os.environ.get("PROVIDER", "groq")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MODELS = {
    "groq": "llama-3.1-8b-instant",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-20250514",
}
MODEL = MODELS[PROVIDER]

OPEN_BUS_BASE_URL = "https://open-bus-stride-api.hasadna.org.il"
HEADERS = {
    "User-Agent": "AgenticAI-Course/1.0 (Educational; Bar-Ilan University)",
    "Accept": "application/json",
}