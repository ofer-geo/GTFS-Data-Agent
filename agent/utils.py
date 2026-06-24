from config import PROVIDER, GROQ_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY


def get_client():
    """Return the right LLM client based on the PROVIDER set in config."""
    if PROVIDER == "groq":
        from groq import Groq
        return Groq(api_key=GROQ_API_KEY)
    elif PROVIDER == "openai":
        from openai import OpenAI
        return OpenAI(api_key=OPENAI_API_KEY)
    elif PROVIDER == "anthropic":
        from anthropic import Anthropic
        return Anthropic(api_key=ANTHROPIC_API_KEY)
    elif PROVIDER == "google":
        if not GOOGLE_API_KEY:
            raise ValueError("GOOGLE_API_KEY is missing. Add it to your .env file.")
        from openai import OpenAI
        return OpenAI(
            api_key=GOOGLE_API_KEY,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            default_headers={"x-goog-api-key": GOOGLE_API_KEY},
        )
    else:
        raise ValueError(f"Unknown provider: '{PROVIDER}'. Choose groq, openai, anthropic, or google.")