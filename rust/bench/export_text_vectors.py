import sys, json, random, string, difflib
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from latency_lab.pipeline import normalize, PhraseTokenizer
from latency_lab.providers import tts_token_budget, clean_transcript
from latency_lab.llm_output import SpokenTextFilter

rng = random.Random(190)
pairs = [
    ("hello there", "hello there."),
    ("my number is 123", "my number is 124"),
    ("a" * 210 + "b", "a" * 210 + "c"),
    ("", ""),
]
for _ in range(100):
    a = "".join(rng.choice("abc123 ") for _ in range(rng.randint(1, 250)))
    b = a[: len(a) // 2] + rng.choice("abc ") + a[len(a) // 2 + 1 :]
    pairs.append((a, b))
texts = [
    "Hello there. Next question? Yes!",
    "Please dial *966*911# now.",
    "one  two three " * 30,
    "Hello <think>private</think>there.",
    "A <function=foo>hidden</function> sentence.",
    "Pi is 3.14. Done.",
    "東京",
    "こんにちは",
    "﨑",
    "language English<asr_text>hello 日本",
    "language English<asr_text>Hello\nlanguage English<asr_text>there",
    "language French<asr_text>bonjour",
]
filters = []
for text in texts:
    for step in [1, 3, 9, 10000]:
        f = SpokenTextFilter()
        pieces = [text[i : i + step] for i in range(0, len(text), step)]
        out = "".join(f.push(p) for p in pieces)
        filters.append({"pieces": pieces, "expected": out})
phrases = []
for text in texts:
    t = PhraseTokenizer()
    out = []
    for c in text:
        out += t.push(c)
    out += t.finish()
    phrases.append({"text": text, "expected": out})
Path(sys.argv[2]).write_text(
    json.dumps(
        {
            "ratios": [
                {
                    "a": normalize(a),
                    "b": normalize(b),
                    "expected": difflib.SequenceMatcher(
                        None, normalize(a), normalize(b)
                    ).ratio(),
                }
                for a, b in pairs
            ],
            "filters": filters,
            "phrases": phrases,
            "tts": [{"text": t, "budget": tts_token_budget(t)} for t in texts],
            "cleanup": [{"text": t, "expected": clean_transcript(t)} for t in texts],
        },
        indent=2,
    )
    + "\n"
)
# Run the actual reference receiver on deterministic protocol events, without network I/O.
import asyncio, aiohttp, dataclasses
from latency_lab.providers import RSTTClient
from latency_lab.config import LabConfig


class WS:
    closed = False

    def __init__(self, parts):
        self.parts = parts

    async def send_json(self, _item):
        pass

    def __aiter__(self):
        async def items():
            for part in self.parts:
                yield aiohttp.WSMessage(
                    aiohttp.WSMsgType.TEXT,
                    json.dumps({"type": "transcription.delta", "delta": part}),
                    None,
                )

        return items()


async def deltas(parts):
    result = []

    async def partial(text, raw):
        result.append(text)

    async def final(text):
        raise AssertionError("partials must not authorize output")

    c = RSTTClient(LabConfig(), partial, final, lambda *_: None)
    ws = WS(parts)
    c.ws = ws
    await c._receive(ws)
    return result


async def export():
    vectors = []
    for parts in [
        ["language", "English", "<asr_text>", "Hello", ",", "world", ".", "Next"],
        ["language", "French", "bonjour", "language", "English", "Hello"],
        ["language English<asr_text>Call", " 123", "456", "!"],
        ["(", "Hello", ")", "there"],
        ["hello", " language", "English", "<asr_text>new", "phrase"],
    ]:
        vectors.append({"parts": parts, "expected": await deltas(parts)})
    p = Path(sys.argv[2])
    body = json.loads(p.read_text())
    body["deltas"] = vectors
    p.write_text(json.dumps(body, indent=2) + "\n")


asyncio.run(export())
