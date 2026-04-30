"""ADK LlmAgent that deduplicates and scores raw articles."""

from google.adk.agents import LlmAgent  # type: ignore[import-untyped]

curator_agent = LlmAgent(
    name="news_curator",
    model="gemini-2.0-flash",
    instruction="""
You are a news curator. Read the "raw_articles" array from session state.

Tasks:
1. Remove duplicate stories (same event, multiple sources — keep the most reputable source).
2. Score each remaining article 0.0–1.0 for importance, recency, and relevance.
3. Discard articles with score < 0.5.
4. Sort remaining articles by score descending.
5. Store results in session state under the key "curated_articles".
   Each item: {"title": str, "url": str, "summary": str, "topic": str,
               "relevance_score": float, "source": str}

Return ONLY valid JSON. No explanation text outside the JSON array.
""",
    tools=[],
    input_key="raw_articles",
    output_key="curated_articles",
)
