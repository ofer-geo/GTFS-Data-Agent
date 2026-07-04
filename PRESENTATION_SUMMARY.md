# GTFS Data Agent — Presentation Summary

This document is written as a slide-by-slide outline. Each `##` header is a suggested slide title; the bullets under it are the content for that slide. Feed this directly into a slide-generation tool.

---

## Title Slide
**GTFS Data Agent**
An LLM agent that answers questions about Israeli public transit — built as a study in where to trust a language model's judgment, and where to take that judgment away from it.

---

## The Problem
- Israel's public transit data (GTFS format) is large, relational, and genuinely useful to query in plain language — "how many stops does line 25 have?", "what's the timetable for line 125 on Thursday?", "which line has more stops, 125 of Dan or 947 of Egged?"
- A raw LLM can't answer these reliably on its own: it doesn't have the data, and if simply told "the answer is X stops," it will confidently invent numbers, stop names, and times that sound plausible but are wrong.
- The core design challenge: give the model enough freedom to be a good conversational assistant (deciding which tool to call, when to ask a clarifying question, how to phrase an answer) while removing its freedom to invent facts.

---

## Architecture at a Glance
- **Data layer**: the official Israeli MoT GTFS feed, loaded into DuckDB in memory (agency, routes, trips, stops, stop_times, calendar — ~7.4M stop_times rows). No route geometry (`shapes.txt`) is loaded, by design, to fit memory limits.
- **Agent loop**: a ReAct-style loop (`agent/core.py`) — the LLM sees a system prompt describing available tools and a fixed workflow, decides which tool to call, the tool executes deterministically against DuckDB, the result goes back to the model, repeat until it has enough to answer.
- **Model fallback chain**: Gemini 2.5 Flash → Groq (multiple open models) — if one is rate-limited or errors, the agent automatically advances to the next, so a single provider's quota exhaustion doesn't break the conversation.
- **UI**: Streamlit, two-column layout — chat on the left, a live map + chart panel on the right that updates as the conversation resolves a line.

---

## "Free Hand, Fixed Rails" — The Core Design Principle
- The model has genuine freedom over **orchestration**: which tool to call, in what order, how many clarifying questions to ask, how to phrase the final answer.
- The model has **zero** freedom over **facts**: every number, name, code, or time in an answer must come verbatim from a tool's return value — never estimated, rounded, or "filled in" from a similar-looking example.
- This split is enforced two ways:
  1. A very explicit system prompt (what the model is told).
  2. Deterministic, code-level guardrails that don't depend on the model reading the prompt correctly (what the code enforces regardless).
- The rest of this deck is largely about *why* rule #2 turned out to be necessary — the prompt alone wasn't enough.

---

## Agent Principles
The agent is built around a small, closed set of GTFS-specific capabilities, not a general-purpose reasoning engine:

- **A pre-defined toolkit for the popular queries** — each common question type has its own dedicated, deterministic tool:
  - Line & Route Resolution — turn a line number (+ optional operator) into the real route(s) behind it, resolving ambiguity when several operators or physical routes share a number.
  - Direction Resolution — list a line's directions and deterministically resolve a numbered reply to the correct one.
  - Stop Lookup & Ordering — full, ordered stop lists per direction, first/last stop, stop counts.
  - Departure Timetable Lookup — exact departure times for a specific day.
  - Frequency & Schedule Analysis — average departures per hour by day type, with an auto-generated chart.
  - Route Visualization — a map is plotted automatically the moment a line is identified, with no tool call needed.
- **A general database escape hatch** for anything the named tools don't cover — a constrained, read-only (SELECT-only) query tool, for open-ended questions like "how many agencies are there in total?"
- **Full GTFS schema knowledge** — the model is given a description of the entire underlying data model (tables, columns, relationships), not just the named tools, so it can reason about what's possible and construct the escape-hatch queries correctly.
- **A planning layer** — a small, additional step that recognizes when a question needs more than one line resolved (e.g. a comparison) and breaks it into sub-goals, before handing each sub-goal to the same ordinary single-line tools.
- **A verification layer** — a second pass that checks a draft answer both for clarity/completeness (an LLM-based polish step) and, separately, against the real data actually fetched (a deterministic, code-based check) before it reaches the user.
- **A reliability layer** — automatic fallback across multiple free-tier model providers, so a single provider running low on quota doesn't stop the conversation.
- **Conversational memory** — the identified line, its directions, and what's already been shown persist across turns, so a natural follow-up doesn't force the user to repeat context.

---

## Feature Spotlight: The Scoped Sequencer
- Problem: "which line has more stops, 125 of Dan or 947 of Egged?" needs *two* lines resolved and compared — the base architecture only ever tracked one line's context at a time.
- Solution: a small, additional planning step (`_build_plan`) — one cheap LLM call that decides *only* "is this a multi-line question, and if so, what are the sub-goals" from a closed, fixed vocabulary of goal types (stop count, agency, first stop, last stop).
- Everything after that is 100% deterministic: each sub-goal reuses the exact same single-line tools already described above — no new "reasoning" surface, no new way to hallucinate.
- Explicitly *not* a general planning layer — that idea was considered and rejected as unnecessary complexity for a system this size; the sequencer is scoped narrowly to the one real gap it fixes.
- Verdict computation (which line "wins") and comparison charts are built in plain Python from the real fetched numbers, not asked of the LLM.

---

## The Hallucination Problem — A Case Study
- Live example: asked for the full stop list of a 56-stop line, the model would correctly call the right tool, get the correct data back... and then still write out a completely fabricated list of stop names and codes in its own answer text.
- This happened in multiple surface forms across testing — a colon-separated list, a numbered list, a markdown table — each time evading whatever narrow pattern had been built to catch the previous one.
- Key realization: the model can be *correctly grounded* (it has the real data) and still fabricate, because writing out a long, repetitive, structured list in prose is exactly the kind of task LLMs are unreliable at — this isn't a prompt-wording problem, it's a task-design problem.

---

## Attempted Fix That Didn't Work (and why that's worth showing)
- Tried: give a second "verification" LLM pass the real tool data and ask it to fact-check the draft answer against it.
- Result: unreliable in two different ways — one model rubber-stamped a fabricated answer unchanged; another got confused about which data covered which line and incorrectly claimed it couldn't verify at all.
- Lesson generalized: LLMs are not reliable at precisely counting list items or exact-matching strings just because they're "given the real data." Any fix relying on an LLM to police another LLM's output has the same failure mode as the original problem.

---

## The Fix That Worked: Render, Don't Retype
- Instead of asking the model to transcribe a long list into prose (and then trying to catch it when it inevitably drifts), the real data is rendered **directly and deterministically** as a table in the UI — the same pattern already used for departure timetables.
- The model's job shrinks to one short sentence acknowledging the answer; the actual stop names, codes, and times the user sees come straight from the database, with zero LLM involvement in producing them.
- This eliminates the fabrication surface at its root, instead of reactively detecting it after the fact.

---

## Hallucination Security Layers
Six layers, applied outside-in — from before the model is even called, to right before an answer is accepted:

1. **Known-gap pre-check (zero LLM calls).** For questions about something structurally absent from the data (route distance, fares, real-time position) — answered instantly and honestly. The model never sees the question, so there is zero chance of it improvising an answer.
2. **Deterministic dispatch.** A numbered disambiguation reply (agency choice, route choice, direction choice) is resolved in code, guaranteeing the correct follow-up tool is actually called — never left to the model "remembering" what it was asking across a conversation turn.
3. **No silent truncation of real data.** Large, genuine tool results (a long stop list, a busy line's full timetable) are kept intact rather than cut off — a truncated tool result is exactly what once invited the model to "complete the pattern" with invented data.
4. **Render, don't retype.** Long structured data (stop lists, timetables) is shown to the user via a real table generated straight from the tool's output — the model is told, and structurally not needed, to transcribe it as prose.
5. **Deterministic post-check.** Right before an answer is accepted, plain Python (not another LLM) checks whether fact-shaped content that shouldn't be there — a stop-code listing, a specific departure time — still appears in the model's text. If so, the answer is rejected and a corrective retry is forced, bounded by a circuit breaker so it can never loop forever.
6. **System prompt, reinforced.** An explicit, high-salience rule stating that calling the right tool does not excuse writing something that doesn't match its output — defense in depth on top of the code-level checks, not a replacement for them.

---

## Token Economy
Running entirely on free-tier model quotas made token efficiency a first-class concern, not an afterthought:

- **A pool of free-tier models**, not one — Gemini plus several Groq-hosted open models. The whole system runs at zero API cost by spreading load across providers instead of paying for a single premium one.
- **Tool-result summarization between turns.** Once a step is no longer the current one, its full tool output is compressed to a one-line summary in the conversation history instead of being kept verbatim — full detail is only ever carried for as long as it's actually needed.
- **Selective, not blanket, truncation.** Most tool results are capped at a small size; only the few tool types that genuinely need the full data to answer correctly (long stop lists, busy timetables) are exempted from the cap — everything else stays lean by default.
- **Chart data never reaches the model as text.** A generated chart's underlying JSON (which can be tens of thousands of characters) is replaced with a short placeholder before being added to the conversation — the model only needs to know the chart was made, never its internal data.
- **Deterministic fast paths skip the LLM loop entirely** for recognized patterns (e.g. resuming an already-clarified schedule request) — a full reasoning turn isn't spent on something the code can already resolve on its own.
- **Small, single-purpose LLM calls** for narrow jobs (planning, verification, a frequency-chart summary) instead of routing every side-task through one large, expensive reasoning call.
- **Skip planning when it's clearly unnecessary** — a bare numeric reply to a pending clarification is never a new multi-line question, so the planning layer is skipped for it rather than spending a classification call on an already-known answer.

---

## Challenges
- **Free-tier-only constraint.** Quota exhaustion forces a fallback to smaller, weaker models mid-conversation — and weaker models follow instructions less reliably. Several of the hallucination incidents found during development happened exactly when the strongest model had run out of quota.
- **The tools-vs-free-hand boundary needs constant reinforcement.** Even with clear instructions, a model can call the correct tool, receive the correct data, and still fabricate while writing the surrounding sentence — prompting alone was never sufficient, which is the whole reason the deterministic layers above exist.
- **Latency.** Automatic model fallback and rate-limit retries add real wait time, especially when several providers are degraded at once.
- **Reactive fixing doesn't scale.** Early fabrication fixes were narrow detectors built around one exact phrasing — each got evaded by the model's next attempt at a slightly different format. The real progress came from redesigning the task (render, don't retype), not from writing a smarter detector.
- **Live LLM behavior is hard to reproduce.** The same question doesn't always fail the same way twice, and rate limits made it harder to get a clean, repeatable test signal while iterating.
- **Data limitations.** No `shapes.txt` (route geometry) is loaded, so genuinely useful questions like route distance are currently impossible to answer without a data-pipeline change.
- **Bilingual surface area.** Hebrew and English both need to be handled naturally (and GTFS names must always stay in their original Hebrew form) — doubling the phrasing patterns any check or instruction has to account for.

---

## Engineering Lessons (General, Not Just GTFS-Specific)
- **Deterministic checks beat LLM-based fact-checking.** Every reliable safety net built here was a plain Python comparison (does this number match a real count? does this code exist in the real data?) — never "ask another LLM to check."
- **Chasing surface format is a losing game.** A detector built around one exact phrasing gets evaded by the model's next attempt at a different phrasing. Anchor detection to the underlying signal (how many stop-code-shaped numbers are present at all) instead of a specific layout.
- **Fixing the root cause beats patching the symptom.** The best fix in this whole project wasn't a smarter fabrication detector — it was removing the reason the model needed to transcribe data at all.
- **Verify against real data before claiming a fix works.** Multiple "fixes" during development looked correct in isolation and still failed the first live re-test — the discipline of re-running the exact failing scenario against the real database caught this every time.

---

## Current Capabilities
**Good at**: stop lists & stop order, departure timetables & frequency charts, operators & agencies, comparing two lines.

**Not the best at yet**: departure time at a specific stop (only per-trip times by direction exist in the data), occasional slowdowns when a model provider is rate-limited.

---

## Future Work
- **Geospatial queries via OpenStreetMap integration** — bringing in route geometry (e.g. OSM data, since `shapes.txt` isn't loaded) to support route distance and nearest-stop-to-a-location questions.
- **Improved plots and mapping** — richer visualizations beyond the current map/chart panel.
- **Broader line comparison** — extending the scoped sequencer beyond two lines at a time.
- **Real-time data integration via SIRI** — Israel's real-time transit data standard, for live vehicle position and delay information.

---

## Closing / Takeaway
- An LLM agent over real-world structured data is only as trustworthy as its weakest unguarded surface — and that surface isn't always where you'd expect (a correctly-fetched, correctly-grounded answer can still fabricate at the transcription step).
- The architecture that held up: let the model be creative about *how* it gets an answer, and make it structurally impossible — not just instructed — to be creative about *what the answer is*.
