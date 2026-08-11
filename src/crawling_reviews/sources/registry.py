"""Source registry."""
from __future__ import annotations

from urllib.parse import urlsplit

from ..core.errors import UnsupportedSourceError
from .base import SourceAdapter
from .g2.adapter import G2Adapter

ADAPTERS: list[type[SourceAdapter]] = [
    G2Adapter,
    # ZocdocAdapter,   <- one line, once sources/zocdoc/ exists
]

_BY_NAME = {cls.source_name.lower(): cls for cls in ADAPTERS}


def supported_sources() -> list[str]:
    return sorted(_BY_NAME)


def resolve_adapter(source_name: str) -> type[SourceAdapter]:
    cls = _BY_NAME.get((source_name or "").lower())
    if cls is None:
        raise UnsupportedSourceError(
            f'unsupported source "{source_name}". Supported: {", ".join(supported_sources())}'
        )
    return cls


def infer_source(url: str) -> str | None:
    """Guess the source from the URL host, so source_name can be optional."""
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return None
    for cls in ADAPTERS:
        if any(host.endswith(pattern) for pattern in cls.host_patterns):
            return cls.source_name
    return None
