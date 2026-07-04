import json
import time
import re

from config import MODEL_PRIORITY
from agent.tools import (
    tools_map, TOOLS_SCHEMA, selection_state, plot_route_map,
    get_line_variants, get_line_stops, select_option, get_agency_names,
    plot_comparison_chart, plot_multi_line_map,
)
from agent.prompts import SYSTEM_PROMPT
from agent.utils import get_client

# Convert OpenAI-style tools schema to Anthropic format
ANTHROPIC_TOOLS = [
    {
        "name": t["function"]["name"],
        "description": t["function"]["description"],
        "input_schema": t["function"]["parameters"],
    }
    for t in TOOLS_SCHEMA
]

# Keywords that count as the user explicitly picking timetable vs frequency chart.
# A bare "schedule"/"מה הלוח זמנים" question matches neither, so it stays ambiguous.
_TIMETABLE_KEYWORDS = {
    "timetable", "departure time", "departure times", "exact time",
    "לוח זמנים", "זמני יציאה", "מתי יוצא", "מתי יוצאת", "מתי היציאות",
}
_FREQUENCY_KEYWORDS = {
    "frequency", "how often", "average departure", "per hour",
    "תדירות", "כל כמה", "בממוצע",
}
# Broader net: is this conversation about departures/schedule at all (regardless
# of whether timetable vs frequency has been specified yet)?
_SCHEDULE_KEYWORDS = _TIMETABLE_KEYWORDS | _FREQUENCY_KEYWORDS | {
    "schedule", "departure", "departures", "how many trips", "trips per",
    "לוח", "יציאות", "נסיעות",
}


def get_messages(history, provider, line_context: dict = None) -> list:
    if isinstance(history, str):
        history = [{"role": "user", "content": history}]

    pending = selection_state.get("pending_type")

    system = SYSTEM_PROMPT

    # Persistent memory of the line resolved earlier in this conversation -
    # independent of `pending` (which only covers the immediate next reply).
    # Without this the model has to re-infer the line from prior chat text,
    # which weaker fallback models do unreliably (they just re-ask instead).
    if line_context and line_context.get("route_ids"):
        given = line_context.get("given", {})
        given_bits = []
        if given.get("stops"):
            given_bits.append("stop list already shown")
        if given.get("timetable_days"):
            given_bits.append(f"timetable already shown for: {', '.join(sorted(given['timetable_days']))}")
        if given.get("frequency_chart"):
            given_bits.append("frequency chart already shown")
        directions = line_context.get("directions") or []
        dir_text = (
            "; ".join(f"{d.get('headsign', '?')} (route_id={d.get('route_id')})" for d in directions)
            if directions else "not yet listed"
        )
        system += (
            f"\n\n⚠️ CONVERSATION CONTEXT: Line {line_context.get('line_number')} operated by "
            f"{line_context.get('agency')} was already identified earlier in this conversation "
            f"(route_ids={line_context.get('route_ids')}). Directions: {dir_text}. "
            + (f"Already given this conversation: {'; '.join(given_bits)}. " if given_bits else "")
            + "If the user's new question is about this same line and does not name a different "
              "line number, reuse these route_ids directly — do NOT call get_line_variants again "
              "and do NOT ask the user for the line number or agency again."
        )

    if pending == "direction":
        directions = selection_state.get("directions", [])
        all_route_ids = selection_state.get("all_route_ids", [])
        dir_text = "\n".join(
            f"{d['option_number']}. {d['headsign']} (route_id={d['route_id']})"
            for d in directions
        )
        all_num = len(directions) + 1
        system += (
            f"\n\n⚠️ CURRENT STATE: You showed the user {len(directions)} directions and are waiting for their choice."
            f"\nDirections:\n{dir_text}"
            f"\n{all_num}. כל הכיוונים — route_ids={all_route_ids}"
            f"\nBased on the user's response, call get_line_stops with:"
            f"\n- A specific direction: get_line_stops(route_ids=[<that direction's route_id>])"
            f"\n- All directions: get_line_stops(route_ids={all_route_ids})"
            f"\nDo NOT call get_line_variants or get_line_directions again."
        )
    elif pending == "schedule_choice":
        route_ids = selection_state.get("schedule_route_ids", [])
        agency = selection_state.get("schedule_agency", "")
        line_num = selection_state.get("schedule_line_number", "")
        system += (
            f"\n\n⚠️ CURRENT STATE: Line {line_num} of {agency} is already identified - "
            f"route_ids={route_ids}. You already asked the user to choose between timetable "
            f"and frequency chart; their latest message is that choice.\n"
            f"- Timetable → call get_departure_timetable(route_ids={route_ids}, specific_day=...). "
            f"If they didn't name a day, ask which day first instead of calling the tool.\n"
            f"- Frequency chart → call get_departure_schedule(route_ids={route_ids}), then "
            f"plot_departure_schedule(route_ids={route_ids}).\n"
            f"Do NOT call get_line_variants again - the line is already resolved."
        )
    elif pending:
        options = selection_state.get("agencies", []) if pending == "agency" else [
            g["route_long_names"][0] if g.get("route_long_names") else ""
            for g in selection_state.get("grouped_lines", [])
        ]
        options_text = "\n".join(f"{i+1}. {o}" for i, o in enumerate(options))
        system += (
            f"\n\n⚠️ CURRENT STATE: You showed a numbered list and are waiting for the user to choose."
            f"\nThe list was:\n{options_text}"
            f"\nThe user's latest message is either a number or a name from this list."
            f"\nFind the matching number and call select_option(option_number)."
            f"\nDo NOT call get_line_variants. Do NOT pass agency names as arguments."
        )

    last_user_msg = next(
        (m["content"] for m in reversed(history) if m.get("role") == "user" and isinstance(m.get("content"), str)),
        "",
    )
    reply_language = "Hebrew" if re.search(r"[֐-׿]", last_user_msg) else "English"
    system += (
        f"\n\n⚠️ LANGUAGE: The user's last message is in {reply_language}. "
        f"Write your entire reply — including any explanatory sentences around a numbered "
        f"list — in {reply_language}. Only GTFS names (stops, agencies, headsigns, route "
        f"names) stay in their original Hebrew form."
    )

    if provider == "anthropic":
        return [m for m in history if m["role"] != "system"]
    return [{"role": "system", "content": system}] + list(history)


def extract_coords(text: str) -> list:
    coords = []
    for block in re.findall(r'\{[^{}]+\}', text):
        clean = block.replace('\\"', '"').replace("\\", "")
        lat_m = re.search(r'"?lat"?\s*:\s*(-?\d{1,2}\.\d+)', clean)
        lon_m = re.search(r'"?lon"?\s*:\s*(-?\d{1,3}\.\d+)', clean)
        if not (lat_m and lon_m):
            continue
        lat, lon = float(lat_m.group(1)), float(lon_m.group(1))
        if not (29 < lat < 34 and 34 < lon < 36):
            continue
        label_m = re.search(r'"?label"?\s*:\s*"?([^"}]+)', clean)
        label = label_m.group(1).strip().strip('"') if label_m else ""
        coords.append({"lat": lat, "lon": lon, "label": label})
    seen, out = set(), []
    for c in coords:
        key = (round(c["lat"], 5), round(c["lon"], 5))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _reasoning_kwargs(provider: str, model: str) -> dict:
    """
    Extra request kwargs to keep chain-of-thought out of message.content.
    Each provider/model family controls this differently:
    - Groq gpt-oss models: only accept include_reasoning (reasoning_format
      isn't supported for them).
    - Groq qwen3: accepts reasoning_format, and defaults to "raw" (reasoning
      inline in content via <think> tags) if omitted - a different knob
      entirely from gpt-oss's, and the one that was leaking here.
    - Google: reasoning_effort="none" disables Gemini's "thinking" pass,
      which also cuts hidden thinking-token cost.
    """
    if provider == "groq":
        if model.startswith("qwen"):
            return {"reasoning_format": "hidden"}
        return {"include_reasoning": False}
    if provider == "google":
        return {"reasoning_effort": "none"}
    return {}


def _call_llm(messages, provider, model, client, tool_choice="auto"):
    if provider == "anthropic":
        return client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=ANTHROPIC_TOOLS,
        )
    else:
        kwargs = dict(model=model, messages=messages, tools=TOOLS_SCHEMA, tool_choice=tool_choice)
        if provider != "google":
            kwargs["parallel_tool_calls"] = False
        kwargs.update(_reasoning_kwargs(provider, model))
        return client.chat.completions.create(**kwargs)


def _parse_response(response, provider):
    """
    Return (content_text, tool_calls_list) normalized across providers.
    tool_calls_list items have: .id, .function.name, .function.arguments (JSON string)
    """
    if provider == "anthropic":
        text = ""
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                text += block.text
            elif block.type == "tool_use":
                class _TC:
                    pass
                class _Fn:
                    pass
                tc = _TC()
                fn = _Fn()
                fn.name = block.name
                fn.arguments = json.dumps(block.input)
                tc.id = block.id
                tc.function = fn
                tool_calls.append(tc)
        return text, tool_calls
    else:
        msg = response.choices[0].message
        return msg.content or "", msg.tool_calls or []


def _append_tool_result(messages, tool_call_id, func_name, result, provider, anthropic_raw_response=None):
    """Add the assistant tool-call + tool result to message history."""
    if provider == "anthropic":
        # For Anthropic, the assistant turn must include the original content blocks
        if anthropic_raw_response and not any(
            isinstance(m.get("content"), list) for m in messages if m["role"] == "assistant"
        ):
            messages.append({"role": "assistant", "content": anthropic_raw_response.content})
        # Tool results go as a user message with tool_result blocks
        # Group multiple results under one user message
        last = messages[-1] if messages else {}
        if last.get("role") == "user" and isinstance(last.get("content"), list):
            last["content"].append({"type": "tool_result", "tool_use_id": tool_call_id, "content": result})
        else:
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_call_id, "content": result}
            ]})
    else:
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": result})


def extract_map_data(result: str) -> dict | None:
    try:
        data = json.loads(result)
        if isinstance(data, dict) and data.get("chart_type") == "route_map":
            return data
    except Exception:
        pass
    return None


def extract_chart_data(result: str) -> dict | None:
    try:
        data = json.loads(result)
        if isinstance(data, dict) and data.get("chart_type") == "departure_schedule":
            return data
    except Exception:
        pass
    return None


def extract_timetable_data(result: str) -> dict | None:
    try:
        data = json.loads(result)
        if isinstance(data, dict) and data.get("timetable_type") == "departure_timetable":
            return data
    except Exception:
        pass
    return None


def _summarize_tool_result(func_name: str, content: str) -> str:
    """Condense a tool result to a one-line summary for message history trimming."""
    if len(content) <= 300:
        return content
    try:
        data = json.loads(content)
        if func_name == "get_line_stops":
            dirs = data if isinstance(data, list) else [data]
            parts = [f"{d.get('headsign', '?')} ({d.get('stops_count', '?')} stops)" for d in dirs]
            return f"[get_line_stops: {len(dirs)} direction(s) — {'; '.join(parts)}]"
        elif func_name == "get_line_variants":
            return (f"[get_line_variants: line={data.get('line_number')}, "
                    f"agency={data.get('agency_name')}, "
                    f"can_proceed={data.get('can_proceed')}, "
                    f"clarification_needed={data.get('clarification_needed')!r}]")
        elif func_name == "select_option":
            return f"[select_option: can_proceed={data.get('can_proceed')}, agency={data.get('agency_name')}]"
        elif func_name == "run_sql":
            rows = data if isinstance(data, list) else []
            return f"[run_sql: {len(rows)} row(s) returned]"
        elif func_name == "get_departure_timetable":
            dirs = data.get("directions", {}) if isinstance(data, dict) else {}
            total = sum(len(v.get("departures", [])) for v in dirs.values())
            return f"[get_departure_timetable: {len(dirs)} direction(s), {total} departures on {data.get('day', '?')}]"
        elif func_name == "get_departure_schedule":
            routes = list(data.keys()) if isinstance(data, dict) else []
            day_types = list(list(data.values())[0].keys()) if routes else []
            return f"[get_departure_schedule: {len(routes)} route(s), day types: {', '.join(str(d) for d in day_types)}]"
        elif func_name == "plot_departure_schedule":
            return "[plot_departure_schedule: chart generated and sent to UI]"
        else:
            return f"[{func_name}: result summarized ({len(content)} chars)]"
    except (json.JSONDecodeError, TypeError):
        return f"[{func_name}: {content[:150]}...]"


def _trim_tool_results(messages: list, tool_call_names: dict) -> None:
    """Replace tool result content in-place with short summaries."""
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "tool":
            func_name = tool_call_names.get(m.get("tool_call_id", ""), "unknown")
            content = m.get("content", "")
            if isinstance(content, str):
                m["content"] = _summarize_tool_result(func_name, content)
        elif m.get("role") == "user" and isinstance(m.get("content"), list):
            for block in m["content"]:
                if block.get("type") == "tool_result":
                    func_name = tool_call_names.get(block.get("tool_use_id", ""), "unknown")
                    content = block.get("content", "")
                    if isinstance(content, str):
                        block["content"] = _summarize_tool_result(func_name, content)


def _detect_limit_type(error_text: str) -> str:
    """
    Best-effort classification of a rate-limit error's period, from its message text.
    Providers phrase this differently - e.g. Google's quotaId comes back as
    camelCase like "GenerateRequestsPerDayPerProjectPerModel-FreeTier" (no
    space around "Per"/"Day"), so match with an optional separator instead of
    a literal "per day" substring.
    """
    t = error_text.lower()
    if re.search(r"per.?day|rpd|tpd|daily", t):
        return "daily"
    if re.search(r"per.?hour|rph|hourly", t):
        return "hourly"
    if re.search(r"per.?minute|rpm|tpm", t):
        return "per-minute"
    if re.search(r"per.?second|rps|tps", t):
        return "per-second"
    if "high demand" in t or "unavailable" in t or "overloaded" in t:
        return "availability"
    return "rate"


def _is_hebrew(text: str) -> bool:
    return bool(re.search(r"[֐-׿]", text))


_GENERAL_DB_KEYWORDS = {
    "how many", "most", "least", "highest", "lowest", "longest", "shortest", "average", "total",
    "כמה", "הכי", "בממוצע", "בסך הכל", "הארוך ביותר", "הקצר ביותר", "הרב ביותר", "המעט ביותר",
}


def _looks_like_general_db_question(text: str) -> bool:
    """
    Keyword check for "how many X / which Y has the most Z" style
    open-ended database questions - the ones the workflow tells the model
    to answer via run_sql() directly, with no specific line/route_ids
    involved at all. Checks the QUESTION'S intent rather than pattern-
    matching the ANSWER'S numbers: an answer to a general-database
    question could state almost any number in almost any phrasing, so
    there's no reliable content signature to check after the fact (unlike
    stop lists or departure times, which the system prompt mandates a
    fixed format for) - classifying the question instead sidesteps that
    entirely.
    """
    t = text.lower()
    return any(kw in t for kw in _GENERAL_DB_KEYWORDS)


def _with_closing_question(answer: str, hebrew: bool) -> str:
    """
    Appended to every genuinely finished answer (not a clarifying question,
    not an error) so a conversation never just stops - the user always gets
    an explicit invitation to confirm or continue.
    """
    closing = ("האם זו התשובה שחיפשת? יש עוד משהו שאוכל לעזור בו?" if hebrew
               else "Is this what you were looking for? Anything else I can help with?")
    return f"{answer}\n\n{closing}"


def _schedule_type_question(history: list) -> str:
    """Deterministic, code-built timetable-vs-frequency question, in the user's language."""
    last_user_msg = next(
        (m["content"] for m in reversed(history) if m.get("role") == "user" and isinstance(m.get("content"), str)),
        "",
    )
    if _is_hebrew(last_user_msg):
        return (
            "אילו נתונים תרצה על הקו?\n"
            "1. לוח זמנים — זמני יציאה מדויקים ליום מסוים\n"
            "2. תרשים תדירות — ממוצע יציאות לשעה לפי סוג יום"
        )
    return (
        "Which would you like?\n"
        "1. Timetable — exact departure times for a specific day\n"
        "2. Frequency chart — average departures per hour by day type"
    )


_DAY_ALIASES = {
    "sunday": "sunday", "ראשון": "sunday",
    "monday": "monday", "שני": "monday",
    "tuesday": "tuesday", "שלישי": "tuesday",
    "wednesday": "wednesday", "רביעי": "wednesday",
    "thursday": "thursday", "חמישי": "thursday",
    "friday": "friday", "שישי": "friday",
    "saturday": "saturday", "שבת": "saturday",
}


def _extract_day(text: str):
    t = text.lower()
    for alias, day in _DAY_ALIASES.items():
        if alias in t:
            return day
    return None


def _summarize_frequency(question, line_num, agency, schedule_json, provider, model, client) -> str:
    """
    Small LLM pass over the REAL per-hour departure data, producing a short
    2-3 sentence summary (peak hours, general pattern) - never a table, since
    the chart already shows the numbers. Feeding it real data (rather than
    letting the main model answer from a stale/trimmed context) is what
    prevents the fabricated-looking frequency ranges seen before.
    """
    system = (
        "You are given real departure-frequency data (JSON: route_id -> day_type -> hour -> "
        "average departures) for a bus line. Write a SHORT 2-3 sentence summary, in the same "
        "language as the user's question, mentioning peak hours and the general pattern. "
        "Do NOT restate the data as a table or list. Do NOT invent numbers not in the data."
    )
    user = f"User's question: {question}\nLine {line_num} ({agency})\nData:\n{schedule_json}"
    try:
        if provider == "anthropic":
            resp = client.messages.create(
                model=model, max_tokens=400, system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
        else:
            kwargs = dict(model=model, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
            kwargs.update(_reasoning_kwargs(provider, model))
            resp = client.chat.completions.create(**kwargs)
            text = resp.choices[0].message.content or ""
        return text.strip() or "Here is the frequency chart."
    except Exception:
        return "Here is the frequency chart."


def _is_retryable_error(error_text: str, status) -> bool:
    """
    True for rate-limit/quota errors AND transient server-side unavailability
    (e.g. Gemini's 503 "This model is currently experiencing high demand") -
    both are cases where falling through to the next model in MODEL_PRIORITY
    (or waiting and retrying) is the right move, rather than surfacing a raw
    error. A real 503 with this exact wording is what slipped through the
    original rate-limit-only check and crashed a whole request (status=503
    isn't 429, and "high demand" doesn't contain any rate-limit keyword).
    """
    t = error_text.lower()
    return (
        status in (413, 429, 503) or "rate_limit" in t or "too large" in t
        or "overloaded" in t or "quota" in t or "resource_exhausted" in t
        or "high demand" in t or "unavailable" in t
    )


def _verify_answer(question: str, draft: str, provider: str, model: str, client) -> str:
    """
    One cheap extra LLM pass: check the draft answer actually satisfies the
    user's question and is presented clearly, and revise only if needed.
    Deliberately bypasses _call_llm/TOOLS_SCHEMA (no tools needed here) to
    keep this pass small - it's a token cost added on top of every answer,
    so it should stay minimal.

    An earlier version of this tried passing this turn's real tool results
    in and asking the model to fact-check the draft against them directly.
    Tested against a deliberately fabricated draft (wrong stop count + a
    fake stop name) and it failed two different ways depending on the
    model: one rubber-stamped the fabrication unchanged; another got
    confused about which data covered which line and incorrectly claimed
    the data didn't cover it at all. LLMs aren't reliable at precisely
    counting array entries or exact-matching strings in a data blob just
    because it's "given real data" - so that approach was reverted.
    Deterministic checks (see _verify_stop_counts) cover specific
    high-risk fact types instead, without depending on an LLM to catch its
    own arithmetic.
    """
    system = (
        "You are reviewing a draft answer to a user's public-transport question. "
        "Check: (1) does it fully answer what was asked, (2) is it clear and "
        "user-friendly (lists for multiple items, no raw SQL/JSON). "
        "STRICT RULE: you may only reformat, reorganize, or clarify wording that is "
        "already in the draft below. You have no access to the real database, so you "
        "MUST NOT add, complete, or 'fill in' any number, time, stop name, stop code, "
        "or statistic that is not already written in the draft - not even a plausible-"
        "looking one. If the draft is missing a detail, leave it missing; do not invent "
        "it for the sake of looking complete. "
        "If the draft already satisfies both checks, output it completely unchanged. "
        "Otherwise, output a corrected version that only rearranges/clarifies existing "
        "content. Output ONLY the final answer text - no meta-commentary about the review."
    )
    user = f"User's question:\n{question}\n\nDraft answer:\n{draft}"
    try:
        if provider == "anthropic":
            resp = client.messages.create(
                model=model, max_tokens=1024, system=system,
                messages=[{"role": "user", "content": user}],
            )
            verified = "".join(b.text for b in resp.content if b.type == "text")
        else:
            kwargs = dict(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
            kwargs.update(_reasoning_kwargs(provider, model))
            resp = client.chat.completions.create(**kwargs)
            verified = resp.choices[0].message.content or ""
        return verified.strip() or draft
    except Exception:
        return draft


# Registry of known "detailed factual content" shapes this app can produce,
# each paired with the line_context["given"] flag that's only set True once
# a real tool has actually returned that kind of data somewhere in this
# conversation (see the main loop's line_context tracking). Generalizes the
# check across every fact type the app knows how to fetch, instead of one
# bespoke check for stops specifically - adding a new fact-producing tool
# later means adding one line here, not writing a new function.
_FACT_PATTERNS = [
    ("stop list", re.compile(r"(תחנה\s*[:：]|stop\s*[:：]\s*\S)", re.IGNORECASE), "stops"),
    ("departure time", re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b"), "timetable_days"),
]


def _claims_facts_without_fetching(answer: str, line_context: dict):
    """
    Deterministic red flag, checked BEFORE the answer is accepted (not
    after, like _verify_stop_counts): does the answer contain a shape of
    content that only a specific real tool call could have produced (see
    _FACT_PATTERNS), while line_context shows that tool has never actually
    returned data in this conversation? Returns the matched fact-type
    label if so, else None.

    This catches what _verify_stop_counts structurally can't: a fabricated
    stop list can have a correct-looking overall COUNT (stops_count was
    visible even while individual stops were invented), which passes a
    count-only check, but no legitimate answer contains per-stop detail if
    that data was never fetched at all.

    Deliberately NOT "any final answer with zero tool calls this turn is
    suspect" - that blanket version was considered and rejected: it would
    break the sequencer's already-validated "so which one has more?"
    follow-up (line_context["given"]["comparison_done"]), which
    legitimately reuses an earlier turn's real data without a redundant
    fresh tool call, exactly as the system prompt allows. Checking per
    fact TYPE whether the conversation has EVER actually fetched that kind
    of data (not just this turn) handles cross-turn reuse correctly while
    still catching content with no backing tool call anywhere.

    Known limits: doesn't track which LINE a fact belongs to (a multi-line
    conversation could miss a second line's fabrication once the first
    line's data is real), and only covers fact types with a pattern
    registered above - a genuinely new fact type still needs one added.
    Full generality would mean fact-checking arbitrary prose against
    arbitrary data, which was tried with an LLM and proved unreliable (see
    _verify_answer's docstring) - this trades some coverage for actually
    being reliable on the fact types it does check.
    """
    given = line_context.get("given", {})
    for label, pattern, given_key in _FACT_PATTERNS:
        if pattern.search(answer) and not given.get(given_key):
            return label
    return None


def _stop_counts_from_tool_results(turn_tool_results: list) -> list:
    """[(label, count), ...] from this turn's get_line_stops-shaped result(s)
    in the main loop's raw (func_name, json_str) list - ground truth for
    _verify_stop_counts. Detected by shape (a list of dicts with
    stops_count), not by func_name, so it doesn't matter whether
    get_line_stops was called directly or dispatched via select_option
    (direction selection) - any caller returning this same data is covered
    without needing its name added here by hand."""
    counts = []
    for _func_name, result in turn_tool_results:
        try:
            directions = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(directions, list):
            continue
        for d in directions:
            if isinstance(d, dict) and isinstance(d.get("stops_count"), int):
                counts.append((d.get("headsign", "?"), d["stops_count"]))
    return counts


def _stop_counts_from_sequencer_results(results: dict) -> list:
    """Same shape as _stop_counts_from_tool_results, built from the
    sequencer's already-parsed results dict instead (target -> per-direction
    dict for count_stops, or a plain value for other goal_types - only
    integers are ever real stop counts, so strings/agency names are
    naturally skipped)."""
    counts = []
    for target, value in results.items():
        if isinstance(value, dict):
            for headsign, v in value.items():
                if isinstance(v, int):
                    counts.append((f"line {target} ({headsign})", v))
        elif isinstance(value, int):
            counts.append((f"line {target}", value))
    return counts


def _verify_stop_counts(answer: str, counts: list, hebrew: bool) -> str:
    """
    Deterministic stop-count check. An LLM-based "fact-check the draft
    against real data" pass was tried and reverted (see _verify_answer's
    docstring) after it proved unreliable - it either rubber-stamped a
    fabricated count, or got confused about which data covered which line.
    A plain Python comparison has neither failure mode, for this specific
    fact type that's exactly what fabricated in the incident that prompted
    this whole sweep.

    Doesn't edit the answer's prose in place - regex-splicing a corrected
    number into a sentence risks producing broken grammar, especially
    across Hebrew/English phrasing. Instead, if any number that looks like
    a stop-count claim (immediately followed by "stop(s)"/"תחנה/תחנות")
    doesn't match a real per-direction or total count, appends the
    verified real numbers as a clear addendum rather than silently
    trusting - or trying to rewrite - the model's prose.
    """
    if not counts:
        return answer  # get_line_stops wasn't used this turn - nothing to check

    real_values = {c for _, c in counts}
    real_values.add(sum(c for _, c in counts))

    claimed = {int(n) for n in re.findall(r"\b(\d+)\b(?=\s*(?:stops?\b|תחנות\b|תחנה\b))", answer)}
    if not claimed or claimed <= real_values:
        return answer

    detail = ", ".join(f"{label}: {c}" for label, c in counts)
    if hebrew:
        return answer + f"\n\n(מספרי תחנות מאומתים: {detail})"
    return answer + f"\n\n(Verified stop counts: {detail})"


# ---------------------------------------------------------------------------
# Scoped sequencer: an additive, closed-vocabulary planner for questions that
# need a metric resolved across multiple lines (e.g. "which has more stops,
# line 5 or line 4?", "how many stops have lines 125 and 25?"). Deliberately
# NOT a general planner: it only ever sequences existing tools for a small,
# fixed set of goal_types, using zero LLM calls for the mechanical parts
# (line resolution reuses get_line_variants exactly as the main loop does;
# picking which tool to call per goal_type is a plain dict dispatch, not a
# decision). Whether a question needs this at all is also decided by the
# same LLM call that builds the plan - not a keyword/regex pre-filter, since
# that proved too brittle (missed plural "lines", missed phrasings with no
# explicit comparison word like "more"/"less"). If it decides no plan is
# needed, execution falls through to the exact same single-line loop below,
# unchanged.
# ---------------------------------------------------------------------------

_PLAN_GOAL_TYPES = {"count_stops", "get_agency", "get_first_stop", "get_last_stop"}


def _call_with_fallback(model_idx: int, call_fn):
    """
    Tries MODEL_PRIORITY starting at model_idx, advancing on any retryable
    error (rate limit/quota/overload) - same policy _call_llm's caller uses
    in the main loop, factored out so a small side call like _build_plan
    isn't stuck on whichever single model happened to be exhausted first.
    call_fn: (provider, model, client) -> result (raises on failure)
    Returns (result, model_idx_used) on success, or (None, model_idx) if the
    whole remaining chain fails - deliberately does NOT do the main loop's
    long wait-and-retry-same-model fallback, since this is meant to stay
    cheap: better to skip the sequencer for this turn than block on it.
    """
    idx = model_idx
    while idx < len(MODEL_PRIORITY):
        provider, model = MODEL_PRIORITY[idx]
        client = get_client(provider)
        try:
            return call_fn(provider, model, client), idx
        except Exception as e:
            # Try the next model regardless of the error's exact shape -
            # matching a growing allowlist of "retryable" error strings is
            # exactly the kind of fix that breaks on the next new error
            # shape (this codebase has already hit that twice: a 503 "high
            # demand" case, then a 403 "access denied" case, both slipping
            # past _is_retryable_error's keyword list and crashing instead
            # of trying another provider). Always advancing removes that
            # whole class of bug - trying another provider costs little.
            if idx + 1 < len(MODEL_PRIORITY):
                print(f"[Agent] {MODEL_PRIORITY[idx]} failed ({type(e).__name__}) "
                      f"building the plan - trying {MODEL_PRIORITY[idx + 1]}")
                idx += 1
                continue
            print(f"[Agent] Plan build failed on {provider}/{model}: {e}")
            return None, idx
    return None, idx


def _build_plan(question: str, model_idx: int):
    """
    One small LLM call - same pattern as _verify_answer and
    _summarize_frequency: its own narrow system prompt, kept separate from
    the main reasoning call. Decides BOTH whether this question needs
    multiple lines resolved, and if so, breaks it into sub-goals from a
    closed goal_type enum mapped directly onto existing tools - so it can
    misjudge which lines are involved, but can never invent a new tool or
    approach. Tries the model fallback chain via _call_with_fallback rather
    than giving up on the first provider. Returns (plan_or_None, model_idx) -
    plan is None whenever a single-line answer is more appropriate (including
    when the whole chain fails) - the normal loop handles those, continuing
    from the returned model_idx so a known-exhausted model isn't retried again.
    """
    known_agencies = get_agency_names()

    # Ask for an INDEX into this list rather than a copied string - the model
    # is reliably good at picking a number, but unreliable at reproducing a
    # Hebrew string exactly (it kept returning the user's own English spelling,
    # e.g. "Dan" instead of "דן", which is an exact-match SQL filter and
    # silently failed). An index can't be mistranslated or misspelled.
    agency_list_text = "\n".join(f"{i}: {name}" for i, name in enumerate(known_agencies))

    system = (
        "Decide whether this question needs information about MORE THAN ONE "
        "public transport line to answer (e.g. comparing two lines, or asking "
        "for a fact about each of several lines). Most questions are about a "
        "single line - only treat it as multi-line if the question genuinely "
        "names 2 or more distinct line numbers that each need to be looked up.\n\n"
        "If it's a single-line question, output exactly: {\"subgoals\": []}\n\n"
        "If it's genuinely multi-line, output ONLY compact JSON, no prose, no "
        "markdown fences, in exactly this shape:\n"
        '{"subgoals": [{"target": "<line number as a string>", '
        '"goal_type": "<one of: ' + ", ".join(sorted(_PLAN_GOAL_TYPES)) + '>", '
        '"agency_index": <the number from the operator list below if the user '
        "mentioned an operator for this line, in ANY language/spelling (e.g. "
        '\'Dan\' or \'of Dan\' means the row for "דן"), else null>}], '
        '"compare": "<short phrase for what to do with the results, e.g. '
        "'which line has more stops' or 'list the stop count for each'>\"}\n"
        "Do not invent goal_types outside that list. Do not add extra keys.\n\n"
        "Operators (pick by number, do not copy or translate the text yourself):\n"
        + agency_list_text
    )
    def _call(provider, model, client):
        if provider == "anthropic":
            resp = client.messages.create(
                model=model, max_tokens=400, system=system,
                messages=[{"role": "user", "content": question}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
        else:
            kwargs = dict(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": question}],
            )
            kwargs.update(_reasoning_kwargs(provider, model))
            resp = client.chat.completions.create(**kwargs)
            text = resp.choices[0].message.content or ""

        cleaned = text.strip().strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
        return json.loads(cleaned)

    data, model_idx = _call_with_fallback(model_idx, _call)
    if not data:
        return None, model_idx

    subgoals = []
    for s in data.get("subgoals", []):
        if not (isinstance(s, dict) and s.get("goal_type") in _PLAN_GOAL_TYPES and s.get("target")):
            continue
        idx = s.get("agency_index")
        agency_name = known_agencies[idx] if isinstance(idx, int) and 0 <= idx < len(known_agencies) else None
        subgoals.append({"target": s["target"], "goal_type": s["goal_type"], "agency_name": agency_name})
    if len(subgoals) < 2:
        return None, model_idx

    return {
        "subgoals": subgoals,
        "results": {},
        "current_index": 0,
        "compare": data.get("compare", ""),
        "original_question": question,
    }, model_idx


def _dispatch_goal(subgoal: dict, resolved: dict):
    """
    Deterministic goal_type -> tool dispatch, once a subgoal's line is
    uniquely resolved (resolved = a can_proceed=true get_line_variants
    result). No LLM involved - goal_type is a closed enum, so which tool to
    call is already known, not decided.

    For goal_types that vary by direction (count_stops, get_first_stop,
    get_last_stop), the value is a per-direction dict {headsign: value}
    rather than a single number - a line's two directions can have
    different stop counts, and silently summing (or picking direction 0
    arbitrarily) produced a misleading answer (verified: "114 stops" was
    actually 57+57 across two separate directions, not one meaningful
    total). get_agency isn't direction-dependent, so it stays a single value.
    """
    selected = resolved.get("selected_line", {})
    route_ids = selected.get("route_ids", [])
    goal_type = subgoal["goal_type"]

    if goal_type == "get_agency":
        return selected.get("agency_name", ""), route_ids

    directions = json.loads(get_line_stops(route_ids))
    if not isinstance(directions, list) or not directions:
        return None, route_ids
    if goal_type == "count_stops":
        return {d.get("headsign", "?"): d.get("stops_count", 0) for d in directions}, route_ids
    if goal_type == "get_first_stop":
        return {d.get("headsign", "?"): d.get("first_stop") for d in directions}, route_ids
    if goal_type == "get_last_stop":
        return {d.get("headsign", "?"): d.get("last_stop") for d in directions}, route_ids
    return None, route_ids


def _format_clarification(subgoal: dict, data: dict, hebrew: bool) -> str:
    """
    Builds the clarification text from a get_line_variants-shaped result.
    Always prefer data["agency_name"] over subgoal["agency_name"] when both
    could apply - data reflects what was JUST resolved (e.g. the agency the
    user picked a moment ago), while the subgoal's own value can be a stale
    guess from the original plan. If data omits agency_name (get_line_variants'
    "a selection is already pending" short-circuit doesn't return one), the
    caller is responsible for having already updated subgoal["agency_name"]
    from the last real resolution, so falling back to it here is still safe.
    """
    options = data.get("options", [])
    formatted = "\n".join(f"{o['option_number']}. {o['label']}" for o in options)
    if data.get("clarification_needed") == "agency":
        intro = (f"קו {subgoal['target']} מופעל על ידי יותר ממפעיל אחד — לאיזה מהם התכוונת?" if hebrew
                  else f"Line {subgoal['target']} is operated by more than one agency — which one did you mean?")
    else:
        # "route" here means 2+ unrelated line groups happen to share this
        # display number under the same agency (e.g. a Tel Aviv service
        # and a Beer Sheva service both labeled "126") - NOT directions of
        # one line, which is a separate concept (get_line_directions).
        # Saying "route" reads as ambiguous with "direction" to a user who
        # doesn't know the internal terminology, so spell out "line" instead.
        agency = data.get("agency_name") or subgoal.get("agency_name") or ""
        if hebrew:
            intro = (f"יש יותר מקו אחד עם המספר {subgoal['target']}"
                      + (f" של {agency}" if agency else "") + " — לאיזה מהם התכוונת?")
        else:
            intro = (f"There's more than one line numbered {subgoal['target']}"
                      + (f" operated by {agency}" if agency else "") + " — which one did you mean?")
    prompt = "אנא הזן מספר." if hebrew else "Please enter a number."
    # \n\n (not a single \n) so Markdown breaks out of the numbered list
    # into a new paragraph, instead of gluing this onto the last item.
    return f"{intro}\n\n{formatted}\n\n{prompt}"


def _resolve_current_subgoal(plan_state: dict):
    """
    Deterministically drives the current sub-goal to completion or to a
    pending disambiguation - reuses get_line_variants exactly as the main
    loop does. Returns a tuple:
      ("done", value, route_ids)
      ("clarification", message_to_show)
      ("error", message)
    """
    subgoal = plan_state["subgoals"][plan_state["current_index"]]
    agency_guess = subgoal.get("agency_name")
    raw = get_line_variants(line_number=subgoal["target"], agency_name=agency_guess)
    data = json.loads(raw)

    # A wrong/mismatched agency guess (e.g. the planner copying the user's
    # own English spelling instead of the DB's Hebrew name) makes the exact
    # SQL match come back empty rather than ambiguous - that's a dead end,
    # not a real "line not found". Retry once without the guess so the user
    # gets a normal agency disambiguation instead of a false error.
    if agency_guess and not data.get("can_proceed") and not data.get("clarification_needed"):
        raw = get_line_variants(line_number=subgoal["target"], agency_name=None)
        data = json.loads(raw)

    if data.get("clarification_needed"):
        # Keep the subgoal's own agency_name in sync with whatever was just
        # resolved, so a later stale/duplicate query (get_line_variants'
        # "already pending" short-circuit doesn't return agency_name at all)
        # still has a correct fallback instead of the original plan's guess.
        if data.get("agency_name"):
            subgoal["agency_name"] = data["agency_name"]
        hebrew = _is_hebrew(plan_state["original_question"])
        return ("clarification", _format_clarification(subgoal, data, hebrew))

    if not data.get("can_proceed"):
        return ("error", data.get("reason") or f"Couldn't resolve line {subgoal['target']}.")

    value, route_ids = _dispatch_goal(subgoal, data)
    return ("done", value, route_ids)


def _apply_subgoal_result(plan_state: dict, value, route_ids: list) -> None:
    """Store one resolved sub-goal's result and route_ids, then advance the cursor."""
    subgoal = plan_state["subgoals"][plan_state["current_index"]]
    plan_state["results"][subgoal["target"]] = value
    plan_state.setdefault("route_ids", {})[subgoal["target"]] = route_ids
    plan_state["current_index"] += 1


def _sequencer_update(line_context: dict, plan_state, answer: str, chart_data=None, map_data=None) -> dict:
    """Shared yield-dict shape for every sequencer outcome (clarification, error, or finished)."""
    return {
        "status": "done",
        "log": [{"type": "action", "tool": "sequencer", "args": {}, "observation": answer[:500]}],
        "coords": [], "map_data": map_data, "chart_data": chart_data, "timetable_data": None,
        "line_context": line_context, "plan_state": plan_state, "answer": answer,
    }


def _finish_sequencer(plan_state: dict, line_context: dict, provider: str, model: str, client) -> dict:
    """
    Every sub-goal is resolved - compute the verdict deterministically
    (arithmetic on numbers already fetched, not something the LLM should be
    trusted to state on its own) so the draft handed to _verify_answer
    already contains the answer, not just raw numbers - _verify_answer is
    forbidden from adding a fact that isn't already in the draft. Also
    builds a comparison chart and the winning line's route map, so the
    conversation ends with something to look at, not just text, and marks
    line_context as grounded so a short follow-up doesn't force a fresh,
    redundant tool call.
    """
    results = plan_state["results"]
    hebrew = _is_hebrew(plan_state["original_question"])

    # Per-direction results (count_stops/get_first_stop/get_last_stop) are
    # reported in full in the text - never silently summed or collapsed to
    # one direction - but ranking/charting still needs one representative
    # number per line, so numeric per-direction values use the higher of
    # the two directions.
    numeric = {}
    detail_parts = []
    for target, value in results.items():
        if isinstance(value, dict):
            per_direction = ", ".join(f"{headsign}: {v}" for headsign, v in value.items())
            detail_parts.append(f"line {target} ({per_direction})")
            direction_values = [v for v in value.values() if isinstance(v, (int, float))]
            if direction_values and len(direction_values) == len(value):
                numeric[target] = max(direction_values)
        else:
            detail_parts.append(f"line {target} has {value}")
            if isinstance(value, (int, float)):
                numeric[target] = value
    detail = "; ".join(detail_parts)

    chart_data, map_data, winner = None, None, None
    if len(numeric) == len(results) and len(numeric) >= 2:
        winner = max(numeric, key=numeric.get)
        runner_up = min(numeric, key=numeric.get)
        draft = (f"{detail}. Line {winner} has the highest value (using its higher-count direction); "
                 f"line {runner_up} has the lowest.")

        try:
            cd = json.loads(plot_comparison_chart(numeric, title=plan_state.get("compare", "")))
            if isinstance(cd, dict) and "chart_type" in cd:
                chart_data = cd
        except Exception as e:
            print(f"[Agent] Sequencer comparison chart failed: {e}")

        # One map with every compared line (each its own color), not just
        # the winner's - a "which line has more stops" question is inherently
        # about more than one line, so the visual should show all of them.
        lines_for_map = [
            {"route_ids": plan_state.get("route_ids", {}).get(sg["target"]),
             "line_num": sg["target"], "agency": sg.get("agency_name")}
            for sg in plan_state["subgoals"]
            if plan_state.get("route_ids", {}).get(sg["target"])
        ]
        if lines_for_map:
            try:
                map_data = extract_map_data(plot_multi_line_map(lines_for_map))
            except Exception as e:
                print(f"[Agent] Sequencer route map failed: {e}")
    else:
        draft = f"Comparing {plan_state.get('compare', '')} - {detail}."

    final = _verify_answer(plan_state["original_question"], draft, provider, model, client)
    final = _verify_stop_counts(final, _stop_counts_from_sequencer_results(results), hebrew)
    if chart_data and map_data:
        final += (" הצגתי גם תרשים השוואה ומפה עם המסלולים של כל הקווים." if hebrew
                  else " I've also shown a comparison chart and a map with all the compared lines.")
    elif chart_data:
        final += " הצגתי גם תרשים השוואה." if hebrew else " I've also shown a comparison chart."
    elif map_data:
        final += (" הצגתי גם מפה עם המסלולים של כל הקווים." if hebrew
                  else " I've also shown a map with all the compared lines.")
    final = _with_closing_question(final, hebrew)

    line_context.setdefault("given", {})["comparison_done"] = True
    return _sequencer_update(line_context, None, final, chart_data=chart_data, map_data=map_data)


def _run_sequencer_turn(question: str, plan_state: dict, line_context: dict, model_idx: int):
    """
    Handles one turn of the scoped sequencer end to end: resuming a pending
    sub-goal, starting a new plan, or resolving through to a finished
    comparison. Returns (update, new_model_idx) where update is a
    ready-to-yield dict if the sequencer fully handled this turn, or
    (None, new_model_idx) if it has nothing to do and the normal
    single-line loop below should run instead.
    """
    provider, model = MODEL_PRIORITY[model_idx]
    client = get_client(provider)

    if plan_state and selection_state.get("pending_type") in ("agency", "route"):
        m = re.search(r"\b(\d+)\b", question.strip())
        if m:
            sel = json.loads(select_option(int(m.group(1))))
            subgoal = plan_state["subgoals"][plan_state["current_index"]]
            if sel.get("can_proceed"):
                value, route_ids = _dispatch_goal(subgoal, sel)
                _apply_subgoal_result(plan_state, value, route_ids)
            elif sel.get("clarification_needed"):
                # select_option progressed to a NEW disambiguation stage
                # (e.g. agency -> route) - answer directly from `sel`, which
                # already reflects what was just picked, instead of letting
                # the while loop below re-derive it via a stale/duplicate
                # get_line_variants call that won't carry the right agency.
                if sel.get("agency_name"):
                    subgoal["agency_name"] = sel["agency_name"]
                hebrew = _is_hebrew(plan_state["original_question"])
                msg = _format_clarification(subgoal, sel, hebrew)
                return _sequencer_update(line_context, plan_state, msg), model_idx
            # else: bad reply / a real error - plan_state untouched, the
            # while loop below re-surfaces the same clarification

    elif plan_state is None:
        candidate, model_idx = _build_plan(question, model_idx)
        provider, model = MODEL_PRIORITY[model_idx]
        client = get_client(provider)
        if candidate:
            plan_state = candidate
            print(f"[Agent] Scoped sequencer engaged: {len(candidate['subgoals'])} sub-goal(s), "
                  f"using {provider}/{model}")

    if not plan_state:
        return None, model_idx

    while plan_state["current_index"] < len(plan_state["subgoals"]):
        outcome = _resolve_current_subgoal(plan_state)

        if outcome[0] == "clarification":
            return _sequencer_update(line_context, plan_state, outcome[1]), model_idx

        if outcome[0] == "error":
            print(f"[Agent] Sequencer sub-goal failed: {outcome[1]}")
            answer = f"I couldn't complete that comparison: {outcome[1]}"
            return _sequencer_update(line_context, None, answer), model_idx

        _, value, route_ids = outcome
        _apply_subgoal_result(plan_state, value, route_ids)

    return _finish_sequencer(plan_state, line_context, provider, model, client), model_idx


def react_agent(question: str, context: list = None, max_steps: int = 15, stop_event=None,
                 line_context: dict = None, plan_state: dict = None):
    """
    question: the current user message (string)
    context:  previous conversation turns as [{"role": ..., "content": ...}, ...]
              pass None or [] for a fresh conversation
    line_context: persisted summary of the line resolved earlier in this
              conversation (line_number/agency/route_ids/directions/given),
              as last yielded by a prior call - pass None for a fresh
              conversation. Threaded into get_messages() so the model doesn't
              have to re-derive an already-known line from raw chat text, and
              updated here as the conversation progresses.
    plan_state: persisted state for the scoped sequencer above - pass None
              for a fresh conversation, and re-pass whatever was last
              yielded otherwise. _build_plan() decides per-question whether
              it's needed; single-line questions fall through unaffected.

    Model selection is automatic: starts at MODEL_PRIORITY[0] and, on a
    rate-limit/quota error, silently advances to the next entry (logged as a
    "switch" step) rather than asking the user to pick a provider.
    """
    model_idx = 0
    provider, model = MODEL_PRIORITY[model_idx]
    client = get_client(provider)

    line_context = dict(line_context) if line_context else {
        "line_number": None, "agency": None, "route_ids": [], "directions": None, "given": {},
    }
    line_context.setdefault("given", {})
    plan_state = dict(plan_state) if plan_state else None

    history = list(context or []) + [{"role": "user", "content": question}]
    print(f"[Agent] New query -> provider={provider!r} model={model!r}")

    # --- Scoped sequencer: resume a pending sub-goal, start a new plan, resolve
    # through to a finished comparison, or (returns None) fall through below ---
    sequencer_result, model_idx = _run_sequencer_turn(question, plan_state, line_context, model_idx)
    if sequencer_result is not None:
        yield sequencer_result
        return
    provider, model = MODEL_PRIORITY[model_idx]
    client = get_client(provider)

    messages = get_messages(history, provider, line_context=line_context)

    # If nothing in this conversation has named timetable/frequency yet, the model
    # must ask before jumping straight to one - enforced below rather than left to
    # prompt-following alone, since that's proven unreliable on weaker fallback
    # models (see get_line_variants' pending-selection guard for the same idea).
    # Checks both roles: either the user named it explicitly, or the assistant
    # already asked the 1/2 clarifying question earlier (its own phrasing includes
    # "timetable"/"frequency chart"), meaning a later bare "1"/"2" reply is answering it.
    all_text = " ".join(
        m.get("content", "") for m in history if isinstance(m.get("content"), str)
    ).lower()
    must_ask_schedule_type = not any(k in all_text for k in _TIMETABLE_KEYWORDS | _FREQUENCY_KEYWORDS)
    is_schedule_question = any(k in all_text for k in _SCHEDULE_KEYWORDS)

    # --- Deterministic resolution of a pending timetable-vs-frequency choice ---
    # Bypasses the LLM's tool-call decision entirely when the reply is
    # unambiguous, so a rate-limited/weaker fallback model can't mishandle it
    # (and it's faster - one fewer LLM round-trip for the common case).
    if selection_state.get("pending_type") == "schedule_choice":
        sched_route_ids = selection_state.get("schedule_route_ids", [])
        sched_agency = selection_state.get("schedule_agency", "")
        sched_line_num = selection_state.get("schedule_line_number", "")
        q_lower = question.lower().strip()
        # Match "1"/"2" as a standalone token anywhere in the reply, not just an
        # exact match - natural replies like "option 1, sunday please" don't
        # equal "1" but clearly mean option 1. Safe to be lenient here since this
        # only runs right after we asked the user to pick 1 or 2.
        wants_frequency = bool(re.search(r"\b2\b", q_lower)) or any(k in q_lower for k in _FREQUENCY_KEYWORDS)
        wants_timetable = bool(re.search(r"\b1\b", q_lower)) or any(k in q_lower for k in _TIMETABLE_KEYWORDS)

        if wants_frequency and sched_route_ids:
            selection_state.update({"pending_type": None, "schedule_route_ids": [], "schedule_agency": None, "schedule_line_number": None})
            line_context = {
                "line_number": sched_line_num, "agency": sched_agency, "route_ids": sched_route_ids,
                "directions": line_context.get("directions"),
                "given": {**line_context.get("given", {}), "frequency_chart": True},
            }
            sched_result = tools_map["get_departure_schedule"](route_ids=sched_route_ids)
            plot_result = tools_map["plot_departure_schedule"](route_ids=sched_route_ids)
            fast_log = [
                {"type": "action", "tool": "get_departure_schedule", "args": {"route_ids": sched_route_ids}, "observation": sched_result[:500]},
                {"type": "action", "tool": "plot_departure_schedule", "args": {"route_ids": sched_route_ids}, "observation": plot_result[:500]},
            ]
            cd = extract_chart_data(plot_result)
            yield {"status": "step", "log": list(fast_log), "coords": [], "map_data": None, "chart_data": cd, "timetable_data": None, "line_context": line_context, "answer": None}
            summary = _summarize_frequency(question, sched_line_num, sched_agency, sched_result, provider, model, client)
            summary = _with_closing_question(summary, _is_hebrew(question))
            yield {"status": "done", "log": list(fast_log), "coords": [], "map_data": None, "chart_data": cd, "timetable_data": None, "line_context": line_context, "answer": summary}
            return

        if wants_timetable and sched_route_ids:
            day = _extract_day(q_lower)
            if day:
                selection_state.update({"pending_type": None, "schedule_route_ids": [], "schedule_agency": None, "schedule_line_number": None})
                days = sorted(set(line_context.get("given", {}).get("timetable_days", [])) | {day})
                line_context = {
                    "line_number": sched_line_num, "agency": sched_agency, "route_ids": sched_route_ids,
                    "directions": line_context.get("directions"),
                    "given": {**line_context.get("given", {}), "timetable_days": days},
                }
                tt_result = tools_map["get_departure_timetable"](route_ids=sched_route_ids, specific_day=day)
                fast_log = [{"type": "action", "tool": "get_departure_timetable", "args": {"route_ids": sched_route_ids, "specific_day": day}, "observation": tt_result[:500]}]
                td = extract_timetable_data(tt_result)
                yield {"status": "step", "log": list(fast_log), "coords": [], "map_data": None, "chart_data": None, "timetable_data": td, "line_context": line_context, "answer": None}
                if _is_hebrew(question):
                    answer = f"הנה לוח הזמנים לקו {sched_line_num} ({sched_agency}) ליום {day}:"
                else:
                    answer = f"Here is the timetable for line {sched_line_num} ({sched_agency}) on {day.capitalize()}:"
                answer = _with_closing_question(answer, _is_hebrew(question))
                yield {"status": "done", "log": list(fast_log), "coords": [], "map_data": None, "chart_data": None, "timetable_data": td, "line_context": line_context, "answer": answer}
                return
            # no day mentioned yet - fall through to the normal LLM flow, which is
            # now informed via get_messages()'s schedule_choice branch and will ask for it

    log, coords = [], []
    map_data = None
    chart_data = None
    timetable_data = None
    tool_calls_made = 0
    MAX_OBS_CHARS = 2000
    current_response = None
    tool_call_names = {}  # call_id → func_name, used for trimming
    turn_tool_results = []  # [(func_name, raw_result), ...] this turn - see _stop_counts_from_tool_results
    pending_plot_args = None  # set after get_departure_schedule until plot_departure_schedule runs
    data_tool_used = False  # set once a real answer-producing tool (stops/map/sql/schedule) has run

    _retry_reason = None
    _retry_count = 0

    def _bail_if_stuck(reason_key: str) -> bool:
        """
        Generic circuit breaker for the deterministic "model didn't do what
        we told it to" nudges below (tool_use_failed, tool-call-as-text,
        missing plot_departure_schedule call, no tool called at all). If the
        exact same corrective nudge fires twice in a row with no successful
        tool call in between, the model can't escape the loop on its own -
        stop burning the max_steps budget on slow round-trips and surface a
        clear message instead of silently grinding to "Max steps reached"
        after minutes of retries (see: a resolved line + a legitimate
        clarifying question like "which day?" being mistaken for a fresh,
        ungrounded answer, over and over).
        """
        nonlocal _retry_reason, _retry_count
        if _retry_reason == reason_key:
            _retry_count += 1
        else:
            _retry_reason = reason_key
            _retry_count = 1
        return _retry_count >= 2

    def _stuck_answer() -> str:
        if _is_hebrew(question):
            return "מצטער, נתקלתי בקושי לעבד את הבקשה הזו — תוכל לנסח אותה מחדש?"
        return "I'm having trouble processing this — could you rephrase your question?"

    for step in range(max_steps):

        # --- Trim previous tool results to summaries before next LLM call ---
        if step > 0:
            _trim_tool_results(messages, tool_call_names)

        # --- Check stop request ---
        if stop_event and stop_event.is_set():
            yield {"status": "done", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "line_context": line_context, "answer": "Stopped by user."}
            return

        # --- Call the LLM ---
        try:
            current_response = _call_llm(messages, provider, model, client)
        except Exception as e:
            es = str(e)
            status = getattr(e, "status_code", None)
            print(f"[Agent] LLM error - type={type(e).__name__!r} status={status!r} msg={es[:300]!r}")

            if "tool_use_failed" in es or (status == 400 and "tool" in es.lower()):
                if _bail_if_stuck("tool_use_failed"):
                    yield {"status": "done", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "line_context": line_context, "answer": _stuck_answer()}
                    return
                log.append({"type": "retry", "text": "tool_use_failed - retrying"})
                yield {"status": "retry", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}
                messages.append({"role": "user", "content":
                    "Your previous tool call was malformed. Use the structured "
                    "function-calling format. Try again."})
                continue

            # Try the next model regardless of the error's exact shape - not
            # just a known "retryable" allowlist. Keyword/status-code
            # matching is exactly the kind of fix that breaks on the next
            # new error shape: this codebase has already hit that twice (a
            # 503 "high demand" case, then a 403 "access denied" case, both
            # slipping past the old check and crashing the whole request
            # instead of trying another provider). Always advancing removes
            # that whole class of bug - trying another provider costs little.
            if model_idx + 1 < len(MODEL_PRIORITY):
                old_model = model
                model_idx += 1
                provider, model = MODEL_PRIORITY[model_idx]
                client = get_client(provider)
                log.append({
                    "type": "switch",
                    "text": f"{type(e).__name__} on {old_model} - switching to {model}",
                    "from_model": old_model,
                    "to_model": model,
                    "limit_type": _detect_limit_type(es),
                })
                yield {"status": "retry", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}
                continue

            # --- Whole chain exhausted ---
            if _is_retryable_error(es, status):
                # A known rate-limit/overload shape - waiting can plausibly
                # help, so retry the same (last) model after a pause.
                limit_type = _detect_limit_type(es)
                wait_s = 60 if provider == "google" else 20
                log.append({
                    "type": "retry",
                    "text": f"rate limit ({limit_type}) for {model} - waiting {wait_s}s",
                    "model": model,
                    "limit_type": limit_type,
                    "wait_s": wait_s,
                })
                yield {"status": "retry", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}
                for _ in range(wait_s * 2):  # 0.5s steps, interruptible
                    if stop_event and stop_event.is_set():
                        break
                    time.sleep(0.5)
                continue

            # An unrecognized error, and every model in the chain has now
            # failed on it - waiting wouldn't obviously help, so surface a
            # clear message instead of crashing with a raw exception.
            yield {"status": "done", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "line_context": line_context, "answer": _stuck_answer()}
            return

        content, tool_calls = _parse_response(current_response, provider)
        if tool_calls:
            # Real progress - the model produced a valid tool call, so the
            # circuit breaker's "same nudge fired twice in a row" tracking
            # no longer applies to whatever it was stuck on before.
            _retry_reason = None

        # --- No tool call: final answer ---
        if not tool_calls:
            if '"type": "function"' in content or ('"name":' in content and '"arguments":' in content):
                if _bail_if_stuck("tool_call_as_text"):
                    yield {"status": "done", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "line_context": line_context, "answer": _stuck_answer()}
                    return
                log.append({"type": "retry", "text": "model emitted tool call as text - retrying"})
                yield {"status": "retry", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}
                messages.append({"role": "user", "content":
                    "You wrote a tool call as plain text. Use the real function-calling "
                    "mechanism, or give your final answer in plain language."})
                continue

            if must_ask_schedule_type and is_schedule_question and not data_tool_used:
                # The model gave a final answer (real or fabricated) without ever
                # calling a schedule tool and without the timetable/frequency choice
                # being resolved - override it with the deterministic question rather
                # than risk shipping a hallucinated answer built from no real data.
                answer = _schedule_type_question(history)
                yield {"status": "done", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "answer": answer}
                return

            if pending_plot_args is not None:
                if _bail_if_stuck("missing_plot_call"):
                    yield {"status": "done", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "line_context": line_context, "answer": _stuck_answer()}
                    return
                log.append({"type": "retry", "text": "must call plot_departure_schedule before finishing - retrying"})
                yield {"status": "retry", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}
                messages.append({"role": "user", "content":
                    f"You called get_departure_schedule but haven't called plot_departure_schedule yet. "
                    f"Call plot_departure_schedule(route_ids={pending_plot_args['route_ids']}, "
                    f"specific_day={pending_plot_args['specific_day']!r}) now, then give your final answer."})
                continue

            unfetched_fact = _claims_facts_without_fetching(content, line_context)
            if unfetched_fact:
                if _bail_if_stuck(f"facts_not_fetched:{unfetched_fact}"):
                    yield {"status": "done", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "line_context": line_context, "answer": _stuck_answer()}
                    return
                log.append({"type": "retry", "text": f"answer states a {unfetched_fact} that was never actually fetched - retrying"})
                yield {"status": "retry", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}
                messages.append({"role": "user", "content":
                    f"Your answer contains what looks like a real {unfetched_fact}, but the tool that would "
                    "provide that has not actually returned data in this conversation. Every fact you state "
                    "must come from a tool's real returned data - call the right tool now, then answer using "
                    "only what it returns."})
                continue

            # Closes the same gap as unfetched_fact above, but for run_sql-
            # style general database questions ("how many agencies are
            # there", "which line has the most trips") instead of stops/
            # timetables: an EARLIER, unrelated grounding (e.g. a specific
            # line resolved several turns ago) makes already_grounded below
            # permanently true, which would otherwise let a brand new,
            # totally unrelated general-database claim through this turn
            # with zero backing, since nothing forces a fresh run_sql call
            # once anything at all has ever been grounded.
            if (tool_calls_made == 0 and not line_context.get("given", {}).get("run_sql_used")
                    and _looks_like_general_db_question(question)):
                if _bail_if_stuck("run_sql_not_used"):
                    yield {"status": "done", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "line_context": line_context, "answer": _stuck_answer()}
                    return
                log.append({"type": "retry", "text": "general database question answered without calling run_sql - retrying"})
                yield {"status": "retry", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}
                messages.append({"role": "user", "content":
                    "This looks like a general database question. Call run_sql() to get the real answer "
                    "before responding - do not answer from memory, even if you discussed something else "
                    "earlier in this conversation."})
                continue

            already_grounded = (
                bool(line_context.get("route_ids"))
                or data_tool_used
                or selection_state.get("pending_type") is not None
                or line_context.get("given", {}).get("comparison_done")
            )
            if tool_calls_made == 0 and not already_grounded:
                # Only force a tool call if NOTHING in this conversation has
                # touched real data yet (no resolved line, no tool call this
                # turn, no in-progress multi-turn flow) - i.e. a genuinely
                # fresh, ungrounded transport question the model might
                # otherwise answer from memory. Once anything is grounded, a
                # plain-text answer (including a legitimate clarifying
                # question, e.g. "which day?" mid schedule-choice) is allowed
                # through instead of being wrongly forced back into a tool call.
                transport_keywords = {"line", "stop", "route", "bus", "operator", "agency", "קו", "תחנה", "מפעיל"}
                first_user_msg = next(
                    (m["content"] for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)),
                    ""
                )
                is_transport = any(kw in first_user_msg.lower() for kw in transport_keywords)
                if is_transport:
                    if _bail_if_stuck("no_tool_called"):
                        yield {"status": "done", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "line_context": line_context, "answer": _stuck_answer()}
                        return
                    log.append({"type": "retry", "text": "model answered without calling any tool - retrying"})
                    yield {"status": "retry", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}
                    messages.append({"role": "user", "content":
                        "You have NOT called any tool yet. "
                        "For questions about a specific line number, call get_line_variants() first. "
                        "For general database questions, call run_sql() directly."})
                    continue

            coords += extract_coords(content)
            log.append({"type": "verify", "text": "checking the answer against the question"})
            yield {"status": "step", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}
            content = _verify_answer(question, content, provider, model, client)
            content = _verify_stop_counts(content, _stop_counts_from_tool_results(turn_tool_results), _is_hebrew(question))
            content = _with_closing_question(content, _is_hebrew(question))
            yield {"status": "done", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "line_context": line_context, "answer": content}
            return

        # --- Tool calls: execute and feed results back ---
        # For OpenAI/Groq, append the assistant message now
        if provider != "anthropic":
            messages.append(current_response.choices[0].message)

        stop_after_tool = False
        can_proceed = False
        last_parsed = {}

        for tool_call in tool_calls:
            func_name = tool_call.function.name
            tool_call_names[tool_call.id] = func_name
            try:
                args = json.loads(tool_call.function.arguments) or {}
            except json.JSONDecodeError as e:
                # Weaker fallback models (e.g. Groq's gpt-oss-20b/qwen3, several
                # rungs down MODEL_PRIORITY) are far more prone to malformed
                # tool-call JSON than the primary model. Left uncaught, this used
                # to crash the whole request right after a model switch. Feed it
                # back as a tool result (keeps the tool_call/tool_result pairing
                # valid for OpenAI/Groq) so the model can just retry.
                print(f"[Agent] Malformed tool call args from {model} for {func_name}: {tool_call.function.arguments[:200]!r}")
                result = f"Error: tool call arguments were not valid JSON ({e}). Retry with valid JSON arguments."
                log.append({"type": "action", "tool": func_name, "args": {}, "observation": result})
                yield {"status": "step", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}
                _append_tool_result(messages, tool_call.id, func_name, result, provider, current_response)
                continue

            if func_name == "get_line_variants":
                args = {k: v for k, v in args.items() if k in {"line_number", "agency_name"}}

            if must_ask_schedule_type and func_name in ("get_departure_timetable", "get_departure_schedule"):
                # Code-built, deterministic clarifying question instead of feeding back
                # a corrective tool error and trusting the model to phrase a short,
                # on-template reply - that round trip has drifted into long, unrelated
                # "what do you mean?" answers on some fallback models.
                answer = _schedule_type_question(history)
                yield {"status": "done", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "line_context": line_context, "answer": answer}
                return

            yield {"status": "calling", "tool": func_name, "args": args,
                   "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}

            if func_name not in tools_map:
                result = f"Error: tool '{func_name}' does not exist."
            else:
                try:
                    result = tools_map[func_name](**args)
                except Exception as e:
                    # A weak fallback model can pass wrong/extra argument names,
                    # raising before the tool's own internal error handling runs
                    # (e.g. select_option's int(option_number) on a non-numeric
                    # value). Surface it as a tool error instead of crashing the
                    # whole request.
                    print(f"[Agent] Tool call failed - {func_name}({args}): {type(e).__name__}: {e}")
                    result = f"Error calling {func_name}({args}): {e}"
                else:
                    # Raw, untruncated result - kept for the deterministic
                    # stop-count check (_stop_counts_from_tool_results) to
                    # read ground truth from, separate from `trimmed` below
                    # which is what actually goes into ongoing message
                    # history and can be capped/replaced.
                    turn_tool_results.append((func_name, result))
                tool_calls_made += 1

            try:
                last_parsed = json.loads(result)
                if last_parsed.get("clarification_needed"):
                    stop_after_tool = True
                elif last_parsed.get("can_proceed"):
                    can_proceed = True
            except (json.JSONDecodeError, AttributeError):
                pass

            if func_name in ("get_line_stops", "run_sql", "get_departure_timetable",
                              "get_departure_schedule", "plot_departure_schedule", "select_option"):
                # select_option is included because it can now dispatch straight
                # to get_line_stops (direction selection) - it's a real data
                # answer too, not just a disambiguation step.
                # Some real data tool answered the question - the schedule-type
                # guard below should only fire when NOTHING has answered it yet.
                data_tool_used = True

            if func_name == "get_departure_schedule":
                pending_plot_args = {"route_ids": args.get("route_ids"), "specific_day": args.get("specific_day")}

            if func_name == "plot_departure_schedule":
                pending_plot_args = None
                cd = extract_chart_data(result)
                if cd:
                    chart_data = cd

            if func_name == "get_departure_timetable":
                td = extract_timetable_data(result)
                if td:
                    timetable_data = td

            # --- Keep the persistent line-context summary current ---
            # (see get_messages()'s CONVERSATION CONTEXT block) so a later turn
            # doesn't have to re-derive the line, its directions, or what's
            # already been shown from raw chat text.
            try:
                result_obj = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                result_obj = None

            if func_name == "run_sql" and isinstance(result_obj, list):
                # Tracked outside the route_ids gate below - a general
                # database question (run_sql's own use case) often has no
                # specific line/route_ids at all, unlike stops/timetables.
                line_context["given"]["run_sql_used"] = True

            if line_context.get("route_ids"):
                if func_name == "get_line_directions" and isinstance(result_obj, dict) and result_obj.get("directions"):
                    line_context["directions"] = [
                        {"headsign": d.get("headsign"), "route_id": d.get("route_id")}
                        for d in result_obj["directions"]
                    ]
                elif func_name in ("get_line_stops", "select_option") and isinstance(result_obj, list):
                    line_context["given"]["stops"] = True
                    if not line_context.get("directions"):
                        line_context["directions"] = [
                            {"headsign": d.get("headsign"), "route_id": d.get("route_id")}
                            for d in result_obj
                        ]
                elif func_name == "get_departure_timetable" and isinstance(result_obj, dict) and result_obj.get("day"):
                    days = set(line_context["given"].get("timetable_days", []))
                    days.add(result_obj["day"])
                    line_context["given"]["timetable_days"] = sorted(days)
                elif func_name == "plot_departure_schedule":
                    line_context["given"]["frequency_chart"] = True

            log.append({"type": "action", "tool": func_name, "args": args, "observation": result[:500]})
            coords += extract_coords(result)
            yield {"status": "step", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "line_context": line_context, "answer": None}

            # A get_line_stops-shaped result (a list of per-direction dicts
            # with stops_count) is exempt from the generic cap regardless of
            # which tool produced it - get_line_stops directly, or
            # select_option dispatching a direction choice to it. Checking
            # the shape instead of naming both callers means any future
            # dispatcher that ends up returning this same data is
            # automatically covered too, instead of needing its name added
            # to a list by hand. get_departure_timetable gets the same
            # generous limit for the analogous reason. Both can genuinely
            # exceed the generic cap for a busy/long line (verified: 57-stop
            # line -> ~13,000 char stop list; busiest route's Sunday
            # timetable -> ~9,900 chars - both well past MAX_OBS_CHARS=2000),
            # and a follow-up question can reasonably ask about a specific
            # stop/departure from that same data later in the conversation.
            # Truncating either doesn't just lose detail - it silently
            # invites fabrication: verified once already for get_line_stops,
            # where the model correctly echoed the first ~15 real stops,
            # then invented the rest (including outright fake entries) to
            # match the stops_count it could still see in the truncated
            # JSON.
            #
            # plot_departure_schedule's result is replaced outright, not
            # just capped - the model is explicitly told to give a short
            # summary and never restate the chart's own figures, so it never
            # needs this JSON (~11,000 chars for a busy route) at all; the
            # UI already extracted chart_data from the untruncated `result`
            # above, so this only affects what the model itself sees.
            is_stop_list = isinstance(result_obj, list) and result_obj and all(
                isinstance(d, dict) and "stops_count" in d for d in result_obj
            )
            if func_name == "plot_departure_schedule":
                trimmed = "Chart generated and sent to the UI."
            else:
                obs_limit = 40000 if (is_stop_list or func_name == "get_departure_timetable") else MAX_OBS_CHARS
                trimmed = result if len(result) <= obs_limit else result[:obs_limit] + "\n...[truncated]"
            _append_tool_result(messages, tool_call.id, func_name, trimmed, provider, current_response)

            if stop_after_tool or can_proceed:
                break

        # --- Line identified: inject route_ids and let loop continue ---
        if can_proceed:
            selected = last_parsed.get("selected_line", {})
            route_ids = selected.get("route_ids", [])
            agency = last_parsed.get("agency_name") or selected.get("agency_name", "")
            line_num = last_parsed.get("line_number", "")
            ids_str = ", ".join(str(r) for r in route_ids)

            # Fresh resolution - replaces whatever line was previously tracked
            # (same line asked again, or a genuinely different one either way
            # this is now the authoritative current line for the conversation).
            line_context = {
                "line_number": line_num, "agency": agency, "route_ids": route_ids,
                "directions": None, "given": {},
            }

            # Automatically plot the route map the moment a line is resolved -
            # deterministic, not something the model decides to call, so a map
            # reliably accompanies every answer about a specific line regardless
            # of what the model does next for the text answer.
            map_result = plot_route_map(route_ids, line_num=line_num, agency=agency)
            md = extract_map_data(map_result)
            if md:
                map_data = md
                log.append({"type": "action", "tool": "plot_route_map", "args": {"route_ids": route_ids}, "observation": map_result[:500]})
                yield {"status": "step", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "line_context": line_context, "answer": None}

            # Auto-build the frequency chart (working days / Friday / Saturday
            # breakdown) for the resolved line, same pattern as the map above
            # (route_ids exist here).
            try:
                from agent.tools import plot_departure_schedule
                chart_result = plot_departure_schedule(route_ids)
                cd = extract_chart_data(chart_result)
                if cd:
                    chart_data = cd
                    line_context["given"]["frequency_chart"] = True
                    log.append({"type": "action", "tool": "plot_departure_schedule",
                                "args": {"route_ids": route_ids},
                                "observation": chart_result[:500]})
                    yield {"status": "step", "log": list(log), "coords": list(coords),
                           "map_data": map_data, "chart_data": chart_data,
                           "timetable_data": timetable_data, "line_context": line_context, "answer": None}
            except Exception as e:
                print(f"[Agent] Auto-chart failed: {e}")

            if must_ask_schedule_type and is_schedule_question:
                # Persist the resolved line so the NEXT turn (the user's timetable-vs-
                # frequency reply) doesn't have to re-derive it - chat history across
                # turns only keeps rendered text, not these route_ids, which is exactly
                # what caused the model to re-run get_line_variants from scratch before.
                selection_state.update({
                    "pending_type": "schedule_choice",
                    "schedule_route_ids": route_ids,
                    "schedule_agency": agency,
                    "schedule_line_number": line_num,
                })

            messages.append({
                "role": "user",
                "content": (
                    f"Line {line_num} of {agency} is now uniquely identified. "
                    f"route_ids = {route_ids}. "
                    f"These route_ids are the same line in different directions — always include all of them.\n"
                    f"A route map (stops numbered, one color per direction) has already been displayed to the "
                    f"user automatically — do NOT call any map tool, it doesn't exist. You may mention the map "
                    f"briefly in your answer if relevant.\n"
                    f"Based on the user's original question, decide what to do next:\n"
                    f"• Stop questions → call get_line_directions(route_ids={route_ids})\n"
                    f"• Schedule / departure / timetable questions → DO NOT call any tool yet. First ask the user to choose: (1) Timetable — exact times for a specific day, or (2) Frequency chart — average departures per hour by day type. Wait for their answer before proceeding.\n"
                    f"• Other questions → use run_sql() with WHERE route_id IN ({ids_str})"
                ),
            })
            can_proceed = False
            continue

        # --- Clarification needed: Python builds the numbered list, LLM writes the response ---
        if stop_after_tool:
            formatted_list = ""
            try:
                options = last_parsed.get("options", [])
                n = len(options)
                clarification_type = last_parsed.get("clarification_needed", "")
                formatted_list = "\n".join(f"{opt['option_number']}. {opt['label']}" for opt in options)

                if clarification_type == "direction":
                    after_list = (
                        f"Ask the user to enter a number from 1 to {n-1} for a specific direction, "
                        f"or {n} for all directions."
                    )
                elif clarification_type == "agency":
                    after_list = (
                        f"If the question is purely about who operates the line, this list IS the answer. "
                        f"Otherwise ask the user to enter a number from 1 to {n}."
                    )
                else:
                    after_list = f"Ask the user to enter a number from 1 to {n}."

                messages.append({
                    "role": "user",
                    "content": (
                        f"Include this numbered list verbatim in your response:\n\n"
                        f"{formatted_list}\n\n"
                        f"Do not reformat, renumber, or remove any item. "
                        f"{after_list}"
                    ),
                })
            except Exception:
                pass

            # Same rate-limit/model-fallback handling as the main loop above -
            # this call has no tool_calls step afterwards to retry from, so a
            # crash here used to skip straight past every log entry collected
            # so far and surface a raw provider error as the final answer.
            content = formatted_list or "Sorry, I couldn't reach any model right now — please try again in a moment."
            while True:
                try:
                    llm_resp = _call_llm(messages, provider, model, client, tool_choice="none")
                    content, _ = _parse_response(llm_resp, provider)
                    break
                except Exception as e:
                    es = str(e)
                    status = getattr(e, "status_code", None)
                    print(f"[Agent] LLM error (finalizing clarification) - type={type(e).__name__!r} status={status!r} msg={es[:300]!r}")
                    # Advance to the next model regardless of the error's
                    # exact shape (see the main loop's LLM-error handling for
                    # why this shouldn't be gated on a "retryable" keyword
                    # allowlist). content already has a safe fallback set
                    # above, so exhausting the chain here just uses that.
                    if model_idx + 1 >= len(MODEL_PRIORITY):
                        break
                    old_model = model
                    model_idx += 1
                    provider, model = MODEL_PRIORITY[model_idx]
                    client = get_client(provider)
                    log.append({
                        "type": "switch",
                        "text": f"{type(e).__name__} on {old_model} - switching to {model}",
                        "from_model": old_model,
                        "to_model": model,
                        "limit_type": _detect_limit_type(es),
                    })
                    yield {"status": "retry", "log": list(log), "coords": list(coords), "chart_data": chart_data, "timetable_data": timetable_data, "answer": None}

            yield {"status": "done", "log": list(log), "coords": list(coords), "map_data": map_data, "chart_data": chart_data, "timetable_data": timetable_data, "line_context": line_context, "answer": content}
            return

    yield {"status": "done", "log": list(log), "coords": list(coords), "chart_data": chart_data, "line_context": line_context, "answer": "Max steps reached"}
