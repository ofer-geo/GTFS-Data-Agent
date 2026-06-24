import os
from dotenv import load_dotenv

load_dotenv()

PROVIDER = os.environ.get("PROVIDER", "groq")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")

MODELS = {
    "groq": "llama-3.3-70b-versatile",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-20250514",
    "google": "gemini-2.0-flash",
}
MODEL = MODELS[PROVIDER]

OPEN_BUS_BASE_URL = "https://open-bus-stride-api.hasadna.org.il"
HEADERS = {
    "User-Agent": "AgenticAI-Course/1.0 (Educational; Bar-Ilan University)",
    "Accept": "application/json",
}