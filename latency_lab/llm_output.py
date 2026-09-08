"""Incremental speech filtering; tool execution only uses structured tool_calls."""

import re


_NAMES = ("think", "thinking", "reasoning", "tool_call", "function", "parameter")
_PREFIXES = tuple(f"<{slash}{name}" for name in _NAMES for slash in ("", "/"))
_TAG = re.compile(r"<(/?)(think|thinking|reasoning|tool_call|function|parameter)(?:[=\s/][^<>]*)?>", re.I)


class SpokenTextFilter:
    def __init__(self):
        self.buffer = ""
        self.hidden = []

    def _possible_tag(self):
        text = self.buffer.lower()
        return any(
            prefix.startswith(text) or (
                text.startswith(prefix) and text[len(prefix):len(prefix) + 1] in " =/>\t\r\n"
            )
            for prefix in _PREFIXES
        )

    def push(self, text: str) -> str:
        self.buffer += text
        output = []
        while self.buffer:
            start = self.buffer.find("<")
            if start < 0:
                if not self.hidden:
                    output.append(self.buffer)
                self.buffer = ""
                break
            if start:
                if not self.hidden:
                    output.append(self.buffer[:start])
                self.buffer = self.buffer[start:]
            match = _TAG.match(self.buffer)
            if match:
                closing, name = match.group(1), match.group(2).lower()
                if closing:
                    if name in self.hidden:
                        index = len(self.hidden) - 1 - self.hidden[::-1].index(name)
                        self.hidden = self.hidden[:index]
                elif not match.group().endswith("/>"):
                    self.hidden.append(name)
                self.buffer = self.buffer[match.end():]
            elif self._possible_tag():
                # Hold only a possible internal tag, not the rest of the answer.
                break
            else:
                if not self.hidden:
                    output.append("<")
                self.buffer = self.buffer[1:]
        return "".join(output)
