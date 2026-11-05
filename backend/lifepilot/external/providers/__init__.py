"""External provider connectors."""

from lifepilot.external.providers.canada_open import CanadaOpenConnector
from lifepilot.external.providers.eu_immigration_portal import (
    EuImmigrationPortalConnector,
)
from lifepilot.external.providers.eu_opendata import EuOpenDataConnector
from lifepilot.external.providers.eurlex_cellar import EurlexCellarConnector
from lifepilot.external.providers.govuk import GovukContentConnector
from lifepilot.external.providers.us_federal_register import (
    UsFederalRegisterConnector,
)

ALL_CONNECTOR_CLASSES = (
    GovukContentConnector,
    CanadaOpenConnector,
    EuOpenDataConnector,
    EurlexCellarConnector,
    UsFederalRegisterConnector,
    EuImmigrationPortalConnector,
)

__all__ = [
    "ALL_CONNECTOR_CLASSES",
    "CanadaOpenConnector",
    "EuImmigrationPortalConnector",
    "EuOpenDataConnector",
    "EurlexCellarConnector",
    "GovukContentConnector",
    "UsFederalRegisterConnector",
]
