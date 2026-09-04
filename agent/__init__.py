from typing import Any

__all__ = ["SalonAgent"]


def __getattr__(name: str) -> Any:
    if name != "SalonAgent":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from .salon_agent import SalonAgent

    return SalonAgent
