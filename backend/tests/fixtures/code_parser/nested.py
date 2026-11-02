import os
from pathlib import Path


class Cache:
    """A small cache fixture."""

    def fetch(self, key: str) -> str:
        def normalize(value: str) -> str:
            return value.strip()

        return normalize(key)


def replacement(value: str) -> str:
    return os.path.join(str(Path.cwd()), value)
