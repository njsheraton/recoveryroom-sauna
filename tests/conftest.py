import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run(coro, timeout=20):
    """Run a coroutine to completion with an overall timeout (tests have no
    pytest-asyncio dependency)."""
    return asyncio.run(asyncio.wait_for(coro, timeout))
