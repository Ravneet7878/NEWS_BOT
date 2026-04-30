"""ADK LlmAgent that writes prose summaries for curated articles."""

from google.adk.agents import LlmAgent  # type: ignore[import-untyped]

summariser_agent = LlmAgent(
    name="news_summariser",
    model="gemini-2.0-flash",
    instruction="""
You are a news summariser. Read the "curated_articles" array from session state.

For each article write a clear, factual 5–6 line summary in flowing prose.
No bullet points. Cover: what happened, why it matters, key context.

Store results in session state under the key "final_digest".
Each item: {"title": str, "url": str, "topic": str, "summary": str}

Return ONLY valid JSON. No explanation text outside the JSON array.
""",
    tools=[],
    input_key="curated_articles",
    output_key="final_digest",
)
