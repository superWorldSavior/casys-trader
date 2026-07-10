"""LLM adapter for the macro/news analyst."""

from trader.agent.news_macro.analyzer import (
    DEFAULT_NEWS_MACRO_ANALYST_MODEL,
    DEFAULT_NEWS_MACRO_ANALYST_SESSION_LABEL,
    DEFAULT_NEWS_MACRO_ANALYST_TIMEOUT_S,
    LlmNewsMacroAnalyst,
    NewsMacroAnalystError,
    build_news_macro_router_from_env,
)
from trader.agent.news_macro.prompt import build_news_macro_prompt, parse_news_macro_completion

__all__ = [
    "DEFAULT_NEWS_MACRO_ANALYST_MODEL",
    "DEFAULT_NEWS_MACRO_ANALYST_SESSION_LABEL",
    "DEFAULT_NEWS_MACRO_ANALYST_TIMEOUT_S",
    "LlmNewsMacroAnalyst",
    "NewsMacroAnalystError",
    "build_news_macro_prompt",
    "build_news_macro_router_from_env",
    "parse_news_macro_completion",
]
