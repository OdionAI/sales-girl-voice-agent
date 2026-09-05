from collections.abc import Iterable


_LOOKUP_REQUIREMENTS = {
    "wema_get_balance": (
        "A request for the caller's balance requires wema_get_balance in the "
        "same turn, before giving any balance."
    ),
    "wema_get_transactions": (
        "A request for recent transactions requires wema_get_transactions "
        "before describing that history."
    ),
}


def with_wema_tool_requirements(instructions: str, enabled_tool_names: Iterable[str]) -> str:
    enabled = set(enabled_tool_names)
    if not any(name.startswith("wema_") for name in enabled):
        return instructions
    requirements = [text for name, text in _LOOKUP_REQUIREMENTS.items() if name in enabled]
    requirements.extend([
        'Actually invoke the function. Saying "let me check" is not a tool call and '
        "must not be your entire response to a supported lookup.",
        "The tool performs required voice authorization. Request the lookup through "
        "the tool; never simulate authorization or bypass a blocked result.",
        "Use only values returned by the successful tool result for this request. "
        "Never speak placeholders such as [amount], an example balance, or an invented transaction.",
        "If a result is blocked, failed or needs input, explain that outcome briefly "
        "instead of supplying an amount. Do not call a read request a completed payment.",
        "Use the saved caller account through the tool's defaults. Ask for an account "
        "only when the tool actually needs an account selection.",
        "Return normal spoken text without surrounding quotation marks. "
        "Speaking-style examples are not responses to copy.",
    ])
    return (
        f"{instructions.rstrip()}\n\n"
        "Wema live-data tool requirements (take precedence over speaking style):\n"
        + "\n".join(f"- {text}" for text in requirements)
        + "\n"
    )
