"""LLM factory - Gemini on Vertex AI through LangChain."""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from backend.config import Settings


def build_llm(settings: Settings) -> BaseChatModel:
    if not settings.gcp_project:
        raise RuntimeError(
            "GOOGLE_CLOUD_PROJECT is not set. Add it to .env and authenticate with "
            "`gcloud auth application-default login` (or set GOOGLE_APPLICATION_CREDENTIALS)."
        )
    from langchain_google_vertexai import ChatVertexAI

    return ChatVertexAI(
        model_name=settings.gemini_model,
        project=settings.gcp_project,
        location=settings.gcp_location,
        temperature=settings.temperature,
        max_output_tokens=2048,
        max_retries=3,
    )
