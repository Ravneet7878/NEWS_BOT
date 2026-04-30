"""ADK LlmAgent that deduplicates and scores raw articles."""

from google.adk.agents import LlmAgent  # type: ignore[import-untyped]

curator_agent = LlmAgent(
    name="news_curator",
    model="gemini-2.5-flash",
    instruction="""
You are a news curator. Read the "raw_articles" value from session state.
It may be a JSON array or a string containing a JSON array (possibly wrapped in markdown code fences — strip them if present).

Tasks:
1. Filter out invalid articles first — remove any article that:
   - Has an empty, null, or missing url field.
   - Has a url that starts with "https://news.google.com" or contains "/amp/".
2. Deduplicate by exact URL — if the same url appears more than once, keep only the first occurrence.
3. Deduplicate by event — for articles covering the same story from different sources,
   keep only the one from the most reputable direct publisher. Do NOT merge their URLs.
4. Score each remaining article 0.0–1.0 for importance, recency, and relevance.
5. Discard articles with score < 0.5.
6. Sort remaining articles by score descending.
7. Store results in session state under the key "curated_articles".
   Each item has these fields: title (str), url (str), summary (str), topic (str),
   relevance_score (float), source (str).
   IMPORTANT: Copy the url field exactly from raw_articles — never modify or reconstruct URLs.

Return ONLY valid JSON. No explanation text outside the JSON array.
""",
    tools=[],
    output_key="curated_articles",
)
