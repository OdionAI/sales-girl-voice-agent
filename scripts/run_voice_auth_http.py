import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("VOICE_AUTH_EMBEDDER", "ecapa")
os.environ.setdefault("VOICE_AUTH_DIR", str(ROOT / ".local"))
os.environ.setdefault("VOICE_AUTH_HTTP_PORT", "8098")
os.environ.setdefault("VOICE_AUTH_COSINE_THRESHOLD", "0.40")
os.environ.pop("VOICE_AUTH_SIDECAR_URL", None)

from agent.voice_enroll_http import _serve


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(_serve())
