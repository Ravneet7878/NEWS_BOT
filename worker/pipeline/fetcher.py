"""ADK LlmAgent that fetches raw news articles via Google Search."""

from google.adk.agents import LlmAgent  # type: ignore[import-untyped]
from google.adk.tools import google_search  # type: ignore[import-untyped]

fetcher_agent = LlmAgent(
    name="news_fetcher",
    model="gemini-2.5-flash",
    instruction="""
You are a news fetcher. User preferences are already in session state under "user_preferences"
as {"topics": [...], "topic_weights": {...}}.

Steps:
1. Read "user_preferences" from session state to get the user's topics and weights.
2. Sort topics by weight descending.
3. For each topic, call google_search twice to find the top 5 most important stories:
   - First call: "<topic_name> latest news" — picks up the most recent stories regardless of age.
   - If fewer than 3 usable results come back, make a second call: "<topic_name> trending news".
   - Never restrict searches to "last 24 hours" — prefer recency but always return the most
     significant available stories even if they are a few days old.
4. For each story:
   - Extract: title, url, 2-sentence factual summary, topic name, source (publication name).
   - CRITICAL URL RULES — follow exactly:
       a. Copy the url EXACTLY as returned by google_search. Do NOT modify, shorten,
          reconstruct, or invent any URL from titles or domain names.
       b. Skip any article where google_search did not return a url field.
       c. Skip any article whose url starts with "https://news.google.com" or contains "/amp/".
       d. Prefer direct publisher domains (reuters.com, bbc.com, apnews.com, bloomberg.com,
          techcrunch.com, theguardian.com, ft.com) over aggregator or redirect links.
5. Store the complete list in session state under the key "raw_articles" as a JSON array.
   Each item has these fields: title (str), url (str), summary (str), topic (str), source (str).

Use factual, neutral language. Do not include opinion pieces or press releases.
Return ONLY valid JSON when storing raw_articles. No explanation outside the JSON array.
""",
    tools=[google_search],
    output_key="raw_articles",
)
