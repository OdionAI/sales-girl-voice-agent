from __future__ import annotations

from typing import Any, Callable, TypeVar

T = TypeVar("T")

try:
    from langfuse import get_client, observe
    from langfuse.decorators import langfuse_context

    _LANGFUSE_AVAILABLE = True
    _LANGFUSE_CLIENT = get_client()
except Exception:
    _LANGFUSE_AVAILABLE = False
    _LANGFUSE_CLIENT = None

    def observe(*_args: Any, **_kwargs: Any) -> Callable[[Callable[..., T]], Callable[..., T]]:
        def _decorator(fn: Callable[..., T]) -> Callable[..., T]:
            return fn

        return _decorator

    class _NoopContext:
        @staticmethod
        def update_current_trace(**_kwargs: Any) -> None:
            return

        @staticmethod
        def update_current_observation(**_kwargs: Any) -> None:
            return

    langfuse_context = _NoopContext()


def trace_tool(
    tool_name: str,
    metadata: dict[str, Any] | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
) -> None:
    if not _LANGFUSE_AVAILABLE:
        return

    trace_metadata = {
        "tool_name": tool_name,
        **(metadata or {}),
    }
    try:
        langfuse_context.update_current_trace(
            name=f"voice_agent.{tool_name}",
            user_id=user_id,
            session_id=session_id,
            metadata=trace_metadata,
        )
    except Exception:
        return


def update_observation(**kwargs: Any) -> None:
    if not _LANGFUSE_AVAILABLE:
        return
    try:
        langfuse_context.update_current_observation(**kwargs)
    except Exception:
        return


@observe(name="conversation.turn_event", as_type="span")
def trace_conversation_event(
    event_name: str,
    *,
    payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
) -> None:
    if not _LANGFUSE_AVAILABLE:
        return

    event_metadata = {
        "event_name": event_name,
        **(metadata or {}),
    }
    try:
        langfuse_context.update_current_trace(
            name="voice_agent.conversation",
            user_id=user_id,
            session_id=session_id,
            metadata=event_metadata,
        )
        langfuse_context.update_current_observation(
            name=f"conversation.{event_name}",
            input=payload or {},
            metadata=event_metadata,
        )
    except Exception:
        return


def flush_traces() -> None:
    if not _LANGFUSE_AVAILABLE or _LANGFUSE_CLIENT is None:
        return
    try:
        _LANGFUSE_CLIENT.flush()
    except Exception:
        return
