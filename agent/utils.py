from config import GROQ_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY

_clients = {}  # provider -> client instance, cached per-process


def get_client(provider: str):
    """Return the LLM client for the given provider, creating and caching it on first use.

    Takes provider explicitly (rather than reading a global) so concurrent
    Streamlit sessions using different providers can't clobber each other.
    """
    if provider in _clients:
        return _clients[provider]

    if provider == "groq":
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)
    elif provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
    elif provider == "anthropic":
        from anthropic import Anthropic
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
    elif provider == "google":
        if not GOOGLE_API_KEY:
            raise ValueError("GOOGLE_API_KEY is missing. Add it to your .env file.")
        from openai import OpenAI
        client = OpenAI(
            api_key=GOOGLE_API_KEY,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            default_headers={"x-goog-api-key": GOOGLE_API_KEY},
        )
    else:
        raise ValueError(f"Unknown provider: '{provider}'. Choose groq, openai, anthropic, or google.")

    _clients[provider] = client
    return client