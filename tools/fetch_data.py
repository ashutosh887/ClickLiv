"""Download the official dataset from the organiser's public repo, which stores it in Git LFS."""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = ("https://media.githubusercontent.com/media/sidagarwal04/click-a-thon-2026"
        "/main/SonyLiv/data")

FILES = {
    "ch-hackathon-content-data.csv": 1_181_455,
    "ch-hackathon-raw-data.csv": 232_827_255,
}


def download(name: str, expected: int, target: Path) -> bool:
    path = target / name
    if path.exists() and path.stat().st_size == expected:
        print(f"{name:<34}already present, {expected:,} bytes")
        return True
    try:
        with urllib.request.urlopen(f"{BASE}/{name}", timeout=120) as response:
            payload = response.read()
    except (urllib.error.URLError, OSError) as exc:
        print(f"{name:<34}FAILED, {exc}")
        return False
    path.write_bytes(payload)
    size = path.stat().st_size
    ok = size == expected
    print(f"{name:<34}{size:,} bytes{'' if ok else f'  UNEXPECTED, wanted {expected:,}'}")
    return ok


def main(argv: list[str]) -> int:
    target = Path(argv[0] if argv else "data")
    target.mkdir(parents=True, exist_ok=True)
    ok = all(download(name, size, target) for name, size in FILES.items())
    print("dataset ready" if ok else "dataset incomplete")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
