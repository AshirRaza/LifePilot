"""Curated official government seed definitions (registry metadata only)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class JurisdictionSeed:
    code: str
    name: str
    jurisdiction_type: str
    country_code: str | None = None


@dataclass(frozen=True)
class AuthoritySeed:
    key: str
    jurisdiction_code: str
    name: str
    short_name: str | None = None
    website_url: str | None = None


@dataclass(frozen=True)
class SourceSeed:
    """One curated seed URL. Topics are registry metadata stored in notes."""

    key: str
    jurisdiction_code: str
    authority_key: str
    name: str
    seed_url: str
    language: str
    topics: tuple[str, ...]
    expected_hosts: tuple[str, ...]
    extra_notes: str = ""
    # Optional extra path prefixes beyond the seed URL itself.
    extra_allowed_patterns: tuple[str, ...] = field(default_factory=tuple)


JURISDICTIONS: tuple[JurisdictionSeed, ...] = (
    JurisdictionSeed("GB", "United Kingdom", "country", "GB"),
    JurisdictionSeed("DE", "Germany", "country", "DE"),
    JurisdictionSeed("NL", "Netherlands", "country", "NL"),
    JurisdictionSeed("CA", "Canada", "country", "CA"),
    JurisdictionSeed("EU", "European Union", "supranational", None),
)

AUTHORITIES: tuple[AuthoritySeed, ...] = (
    AuthoritySeed(
        key="uk_home_office",
        jurisdiction_code="GB",
        name="UK Home Office",
        short_name="HO",
        website_url="https://www.gov.uk/",
    ),
    AuthoritySeed(
        key="make_it_in_germany",
        jurisdiction_code="DE",
        name="Make it in Germany",
        short_name="MIIG",
        website_url="https://www.make-it-in-germany.com/",
    ),
    AuthoritySeed(
        key="nl_ind",
        jurisdiction_code="NL",
        name="Immigration and Naturalisation Service (IND)",
        short_name="IND",
        website_url="https://ind.nl/",
    ),
    AuthoritySeed(
        key="ca_ircc",
        jurisdiction_code="CA",
        name="Immigration, Refugees and Citizenship Canada (IRCC)",
        short_name="IRCC",
        website_url="https://www.canada.ca/",
    ),
    AuthoritySeed(
        key="eu_your_europe",
        jurisdiction_code="EU",
        name="European Commission — Your Europe",
        short_name="Your Europe",
        website_url="https://europa.eu/youreurope/",
    ),
)

SOURCES: tuple[SourceSeed, ...] = (
    SourceSeed(
        key="gb_skilled_worker_visa",
        jurisdiction_code="GB",
        authority_key="uk_home_office",
        name="UK Skilled Worker visa guidance",
        seed_url="https://www.gov.uk/skilled-worker-visa",
        language="en-GB",
        topics=("immigration", "employment"),
        expected_hosts=("www.gov.uk", "gov.uk"),
    ),
    SourceSeed(
        key="de_visa_residence_types",
        jurisdiction_code="DE",
        authority_key="make_it_in_germany",
        name="Make it in Germany — visa / residence types",
        seed_url="https://www.make-it-in-germany.com/en/visa-residence/types",
        language="en",
        topics=("immigration", "employment"),
        expected_hosts=("www.make-it-in-germany.com", "make-it-in-germany.com"),
        extra_notes=(
            "Official German federal skilled-worker portal. "
            "Crawl permission (robots.txt / terms / rate limits) must be checked "
            "separately before any automated crawling."
        ),
    ),
    SourceSeed(
        key="nl_ind_work_permits",
        jurisdiction_code="NL",
        authority_key="nl_ind",
        name="IND — work residence permits",
        seed_url="https://ind.nl/en/residence-permits/work",
        language="en",
        topics=("immigration", "employment"),
        expected_hosts=("ind.nl", "www.ind.nl"),
    ),
    SourceSeed(
        key="nl_ind_highly_skilled_migrant",
        jurisdiction_code="NL",
        authority_key="nl_ind",
        name="IND — highly skilled migrant",
        seed_url="https://ind.nl/en/residence-permits/work/highly-skilled-migrant",
        language="en",
        topics=("immigration", "employment"),
        expected_hosts=("ind.nl", "www.ind.nl"),
    ),
    SourceSeed(
        key="ca_ircc_work_canada",
        jurisdiction_code="CA",
        authority_key="ca_ircc",
        name="IRCC — work in Canada",
        seed_url=(
            "https://www.canada.ca/en/immigration-refugees-citizenship/"
            "services/work-canada.html"
        ),
        language="en",
        topics=("immigration", "employment"),
        expected_hosts=("www.canada.ca", "canada.ca"),
    ),
    SourceSeed(
        key="eu_your_europe_residence",
        jurisdiction_code="EU",
        authority_key="eu_your_europe",
        name="Your Europe — residence",
        seed_url="https://europa.eu/youreurope/citizens/residence/index_en.htm",
        language="en",
        topics=("residence", "mobility"),
        expected_hosts=("europa.eu", "www.europa.eu"),
    ),
    SourceSeed(
        key="de_miig_downloads",
        jurisdiction_code="DE",
        authority_key="make_it_in_germany",
        name="Make it in Germany — official downloads",
        seed_url=(
            "https://www.make-it-in-germany.com/en/"
            "looking-for-foreign-professionals/support/downloads"
        ),
        language="en",
        topics=("immigration", "employment", "official downloadable documents"),
        expected_hosts=("www.make-it-in-germany.com", "make-it-in-germany.com"),
        extra_notes=(
            "Document-discovery test source (PDFs/docs linked from this page). "
            "Automated clients may encounter bot-protection interstitials; "
            "robots.txt / terms / rate limits must be checked before crawling."
        ),
    ),
)
