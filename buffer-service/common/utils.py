import base64, random, json, logging, os
from typing import Dict

loglevel = os.getenv("LOGLEVEL", "INFO").upper()
logging.basicConfig(level=loglevel,
                    format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger("carbonshift")

def b64enc(data: bytes) -> str:
    return base64.b64encode(data).decode()

def b64dec(data: str | bytes) -> bytes:
    if isinstance(data, bytes):
        return base64.b64decode(data)
    return base64.b64decode(data.encode())

def weighted_choice(weights: Dict[str, int]) -> str:
    ks, vs = zip(*weights.items())
    return random.choices(ks, weights=vs, k=1)[0]

# Fallback schedule
DEFAULT_SCHEDULE = {
    "deadlines": {"high-power": 30, "mid-power": 120, "low-power": 600},
    "directWeight": 80,
    "queueWeight": 20,
    "flavourWeights": {"high": 10, "mid": 20, "low": 70},
    "consumption_enabled": 1,
    "validUntil": "2099-12-31T23:59:59Z",
}

# Debug mode flag and function
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

def debug(msg: str) -> None:
    """Print debug message if DEBUG env var is true."""
    if DEBUG:
        print(f"[DEBUG] {msg}")