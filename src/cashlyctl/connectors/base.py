from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Connector(ABC):
    @abstractmethod
    def health(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def actions(self) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def capabilities(self) -> dict[str, bool]:
        raise NotImplementedError

