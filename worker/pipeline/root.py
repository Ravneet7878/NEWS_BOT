"""ADK entry point — module-level root_agent required by ADK runner."""

from google.adk.agents import SequentialAgent  # type: ignore[import-untyped]

from worker.pipeline.curator import curator_agent
from worker.pipeline.summariser import summariser_agent

root_agent = SequentialAgent(
    name="news_digest_pipeline",
    description="Curates and summarises pre-fetched news for a single user.",
    sub_agents=[curator_agent, summariser_agent],
)
