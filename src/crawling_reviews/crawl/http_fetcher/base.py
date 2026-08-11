"""Fetcher interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..session.models import Session


@dataclass
class FetchResponse:
    url: str
    status: int
    body: str
    elapsed_ms: int
    headers: dict[str, str] = field(default_factory=dict)

    def __len__(self) -> int:      # convenience for logging
        return len(self.body)


class Fetcher(ABC):
    """A transport. Implementations must translate every failure into a typed AppError."""

    name: str = "fetcher"

    @abstractmethod
    def fetch(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        session: "Session | None" = None,
        timeout_s: float | None = None,
    ) -> FetchResponse:
        """Retrieve one URL."""

    def close(self) -> None:
        """Release any long-lived resources. Default: nothing to do."""
