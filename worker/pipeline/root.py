"""ADK entry point — module-level root_agent required by ADK runner."""

from google.adk.agents import SequentialAgent  # type: ignore[import-untyped]

from worker.pipeline.curator import curator_agent
from worker.pipeline.fetcher import fetcher_agent
from worker.pipeline.summariser import summariser_agent

# ADK requires a module-level variable named exactly `root_agent`
root_agent = SequentialAgent(
    name="news_digest_pipeline",
    description="Fetches, curates, and summarises personalised news for a single user.",
    sub_agents=[fetcher_agent, curator_agent, summariser_agent],
)
