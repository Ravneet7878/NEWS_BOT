"""ADK LlmAgent that fetches raw news articles via Google Search."""

from google.adk.agents import LlmAgent  # type: ignore[import-untyped]
from google.adk.tools import google_search  # type: ignore[import-untyped]

from worker.tools.firestore_tools import get_user_preferences

fetcher_agent = LlmAgent(
    name="news_fetcher",
    model="gemini-2.0-flash",
    instruction="""
You are a news fetcher. You will be given a telegram_id.

Steps:
1. Call get_user_preferences(telegram_id) to retrieve the user's topics and weights.
2. Sort topics by weight descending.
3. For each topic, call google_search to find the top 5 most important news stories
   published in the last 24 hours. Search query format: "{topic} news last 24 hours".
4. For each story extract: title, url, 2-sentence factual summary, topic name.
5. Store the complete list in session state under the key "raw_articles" as a JSON array.
   Each item: {"title": str, "url": str, "summary": str, "topic": str, "source": str}

Use factual, neutral language. Do not include opinion pieces or press releases.
""",
    tools=[google_search, get_user_preferences],
    output_key="raw_articles",
)
