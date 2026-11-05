"""Connector contract for external providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from lifepilot.external.http import ExternalHttpClient
from lifepilot.external.models import (
    ExternalQuery,
    ProviderId,
    ProviderOutcome,
)


class ExternalConnector(ABC):
    """Read-only connector against a documented public interface."""

    provider_id: ClassVar[ProviderId]
    # Jurisdictions this connector can usefully serve (ISO-ish codes + EU).
    jurisdictions: ClassVar[frozenset[str]]
    capabilities: ClassVar[tuple[str, ...]]

    def __init__(self, http: ExternalHttpClient) -> None:
        self.http = http

    def supports(self, query: ExternalQuery) -> bool:
        if query.jurisdiction:
            return query.jurisdiction.upper() in self.jurisdictions
        return True

    @abstractmethod
    def search(self, query: ExternalQuery) -> ProviderOutcome:
        """Execute a bounded search/retrieval for the query."""
