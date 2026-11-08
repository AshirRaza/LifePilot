"""Offline retrieval-quality evaluation helpers (Phase 8F).

Gold labels come from the coverage matrix document associations and passage
excerpts — never from the retriever's own output.
"""

from __future__ import annotations

import csv
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lifepilot.db.models import Document, DocumentChunk
from lifepilot.rag.retrieval import (
    InternalKnowledgeRetriever,
    InternalQuery,
    RetrievalMode,
    RetrievalStatus,
)

RelevanceLevel = Literal["document", "passage"]


@dataclass(frozen=True)
class GoldCase:
    case_id: str
    query: str
    jurisdiction: str
    pathway_id: str | None
    requirement: str | None
    expected_document_ids: frozenset[int]
    expected_urls: frozenset[str]
    passage_needles: tuple[str, ...]
    scored: bool
    notes: str = ""
    category: str = "core"


@dataclass
class ModeMetrics:
    mode: RetrievalMode
    n_scored: int = 0
    n_passage_scored: int = 0
    recall_at_3_doc: float = 0.0
    recall_at_5_doc: float = 0.0
    recall_at_10_doc: float = 0.0
    recall_at_3_pass: float | None = None
    recall_at_5_pass: float | None = None
    recall_at_10_pass: float | None = None
    mrr_at_10_doc: float = 0.0
    mrr_at_10_pass: float | None = None
    precision_at_5_doc: float | None = None
    citation_accuracy: float = 0.0
    jurisdiction_leak_rate: float = 0.0
    current_version_leak_rate: float = 0.0
    duplicate_chunk_rate: float = 0.0
    duplicate_document_rate: float = 0.0
    empty_result_rate: float = 0.0
    split_metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    details: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "n_scored": self.n_scored,
            "n_passage_scored": self.n_passage_scored,
            "recall_at_3_doc": round(self.recall_at_3_doc, 4),
            "recall_at_5_doc": round(self.recall_at_5_doc, 4),
            "recall_at_10_doc": round(self.recall_at_10_doc, 4),
            "recall_at_3_pass": None
            if self.recall_at_3_pass is None
            else round(self.recall_at_3_pass, 4),
            "recall_at_5_pass": None
            if self.recall_at_5_pass is None
            else round(self.recall_at_5_pass, 4),
            "recall_at_10_pass": None
            if self.recall_at_10_pass is None
            else round(self.recall_at_10_pass, 4),
            "mrr_at_10_doc": round(self.mrr_at_10_doc, 4),
            "mrr_at_10_pass": None
            if self.mrr_at_10_pass is None
            else round(self.mrr_at_10_pass, 4),
            "precision_at_5_doc": None
            if self.precision_at_5_doc is None
            else round(self.precision_at_5_doc, 4),
            "citation_accuracy": round(self.citation_accuracy, 4),
            "jurisdiction_leak_rate": round(self.jurisdiction_leak_rate, 4),
            "current_version_leak_rate": round(self.current_version_leak_rate, 4),
            "duplicate_chunk_rate": round(self.duplicate_chunk_rate, 4),
            "duplicate_document_rate": round(self.duplicate_document_rate, 4),
            "empty_result_rate": round(self.empty_result_rate, 4),
            "split_metrics": self.split_metrics,
            "details": self.details,
        }


_STOPWORDS = frozenset(
    """
    a an the and or of to for in on with by from that this is are was were be
    been being as at it its you your we our they their must can may will shall
    should would could need needs needed only have has had do does did not no
    yes if then than also more most such other into over after before under
    about between through during without within against again further once
    here there when where why how all each few both same so too very just
    """.split()
)


def _norm(text_value: str) -> str:
    return re.sub(r"\s+", " ", (text_value or "").lower()).strip()


def _content_tokens(text_value: str) -> list[str]:
    return [
        t
        for t in re.findall(r"[a-z0-9']+", _norm(text_value))
        if t not in _STOPWORDS and len(t) > 2
    ]


def _needle_from_excerpt(excerpt: str, *, min_len: int = 28) -> str | None:
    cleaned = _norm(excerpt)
    if len(cleaned) < min_len:
        return None
    # Prefer a stable mid-length phrase from the independently stored excerpt.
    return cleaned[: min(90, len(cleaned))]


def _needles_from_excerpt(excerpt: str) -> list[str]:
    """Build longest-first independent needles from a matrix passage excerpt."""
    cleaned = _norm(excerpt)
    if not cleaned:
        return []
    needles: list[str] = []
    if len(cleaned) >= 28:
        needles.append(cleaned[:120])
    for part in re.split(r"[—.;:/|\n]+", cleaned):
        part = part.strip()
        if len(part) >= 28:
            needles.append(part[:120])
    for win_len in (70, 55, 40):
        if len(cleaned) < win_len:
            continue
        step = max(15, win_len // 3)
        for start in range(0, len(cleaned) - win_len + 1, step):
            needles.append(cleaned[start : start + win_len])
    seen: set[str] = set()
    uniq: list[str] = []
    for needle in needles:
        if needle not in seen:
            seen.add(needle)
            uniq.append(needle)
    return sorted(uniq, key=len, reverse=True)


def load_coverage_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def resolve_passage_references(
    session: Session,
    *,
    document_ids: Iterable[int],
    excerpts: Sequence[str],
) -> tuple[frozenset[int], str]:
    """Independently map matrix excerpts onto current chunks.

    Matching uses excerpt-derived needles / token overlap against stored chunk
    text only — never retriever rankings. Adjacent chunks are accepted when a
    needle spans a chunk boundary. Returns ``(chunk_ids, method)`` where method
    is one of ``substring``, ``adjacent``, ``overlap``, or ``unresolved``.
    """
    doc_ids = [int(x) for x in document_ids]
    clean_excerpts = [e for e in excerpts if (e or "").strip()]
    if not doc_ids or not clean_excerpts:
        return frozenset(), "unresolved"

    rows = session.execute(
        select(
            DocumentChunk.id,
            DocumentChunk.document_id,
            DocumentChunk.chunk_index,
            DocumentChunk.content_text,
        )
        .where(
            DocumentChunk.is_current_version.is_(True),
            DocumentChunk.document_id.in_(doc_ids),
        )
        .order_by(DocumentChunk.document_id.asc(), DocumentChunk.chunk_index.asc())
    ).all()
    if not rows:
        return frozenset(), "unresolved"

    by_doc: dict[int, list[tuple[int, int, str]]] = {}
    for cid, doc_id, idx, content in rows:
        by_doc.setdefault(int(doc_id), []).append(
            (int(cid), int(idx), _norm(str(content)))
        )

    all_hits: set[int] = set()
    methods: set[str] = set()
    for excerpt in clean_excerpts:
        needles = _needles_from_excerpt(excerpt)
        excerpt_hits: set[int] = set()
        method = "unresolved"
        for doc_id in doc_ids:
            bodies = by_doc.get(doc_id, [])
            if not bodies:
                continue
            best_len = 0
            local: set[int] = set()
            for cid, _idx, body in bodies:
                for needle in needles:
                    if len(needle) < best_len:
                        break
                    if needle in body:
                        if len(needle) > best_len:
                            best_len = len(needle)
                            local = {cid}
                        else:
                            local.add(cid)
                        break
            if local and best_len >= 28:
                excerpt_hits |= local
                method = "substring"
                continue

            for i in range(len(bodies) - 1):
                combo = bodies[i][2] + " " + bodies[i + 1][2]
                for needle in needles:
                    if len(needle) >= 40 and needle in combo:
                        excerpt_hits.add(bodies[i][0])
                        excerpt_hits.add(bodies[i + 1][0])
                        method = "adjacent"
                        break
                if method == "adjacent":
                    break
            if method == "adjacent":
                continue

            et = _content_tokens(excerpt)
            if len(et) < 4:
                continue
            et_set = set(et)
            scored: list[tuple[float, int]] = []
            for cid, _idx, body in bodies:
                inter = et_set & set(_content_tokens(body))
                distinctive = [t for t in inter if len(t) >= 6]
                score = len(inter) / len(et_set)
                if score >= 0.55 and len(inter) >= 3 and len(distinctive) >= 2:
                    scored.append((score, cid))
            if scored:
                scored.sort(reverse=True)
                best = scored[0][0]
                excerpt_hits |= {cid for s, cid in scored if s >= best - 0.05}
                method = "overlap"

        if excerpt_hits:
            all_hits |= excerpt_hits
            methods.add(method)

    if not all_hits:
        return frozenset(), "unresolved"
    if "substring" in methods:
        return frozenset(all_hits), "substring"
    if "adjacent" in methods:
        return frozenset(all_hits), "adjacent"
    return frozenset(all_hits), "overlap"


def resolve_gold_chunk_ids(
    session: Session,
    *,
    document_ids: Iterable[int],
    needles: Sequence[str],
) -> frozenset[int]:
    """Backward-compatible wrapper around independent passage resolution."""
    chunk_ids, _method = resolve_passage_references(
        session,
        document_ids=document_ids,
        excerpts=list(needles),
    )
    return chunk_ids


def build_evaluation_cases(
    session: Session,
    coverage_path: Path,
) -> tuple[list[GoldCase], list[dict[str, Any]]]:
    """Build scored + diagnostic cases from the coverage matrix."""
    rows = load_coverage_rows(coverage_path)
    by_pid: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_pid.setdefault(row["pathway_id"], []).append(row)

    # Hand-authored queries mapped to matrix pathways (gold from matrix only).
    query_specs: list[dict[str, Any]] = [
        {
            "case_id": "gb_sw_eligibility",
            "query": "Skilled Worker visa eligibility requirements licensed sponsor",
            "jurisdiction": "GB",
            "pathway_id": "gb_skilled_worker",
            "requirement": "eligibility",
            "category": "core",
        },
        {
            "case_id": "gb_sw_salary_docs",
            "query": "Skilled Worker visa documents certificate of sponsorship English proof",
            "jurisdiction": "GB",
            "pathway_id": "gb_skilled_worker",
            "requirement": "required_documents",
            "category": "core",
        },
        {
            "case_id": "gb_sw_apply",
            "query": "How to apply for a Skilled Worker visa online",
            "jurisdiction": "GB",
            "pathway_id": "gb_skilled_worker",
            "requirement": "application_process",
            "category": "core",
        },
        {
            "case_id": "gb_student_money",
            "query": "Student visa financial requirement money to support yourself",
            "jurisdiction": "GB",
            "pathway_id": "gb_student",
            "requirement": "financial_or_sponsorship",
            "category": "core",
        },
        {
            "case_id": "gb_student_eligibility",
            "query": "Student visa eligibility age and confirmation of acceptance",
            "jurisdiction": "GB",
            "pathway_id": "gb_student",
            "requirement": "eligibility",
            "category": "core",
        },
        {
            "case_id": "ie_csep_eligibility",
            "query": "Critical Skills Employment Permit eligibility criteria job offer",
            "jurisdiction": "IE",
            "pathway_id": "ie_critical_skills_employment_permit",
            "requirement": "eligibility",
            "category": "core",
        },
        {
            "case_id": "ie_csep_checklist",
            "query": "Critical Skills Employment Permit required documents checklist",
            "jurisdiction": "IE",
            "pathway_id": "ie_critical_skills_employment_permit",
            "requirement": "required_documents",
            "category": "core",
        },
        {
            "case_id": "ie_gep_eligibility",
            "query": "General Employment Permit eligibility criteria remuneration",
            "jurisdiction": "IE",
            "pathway_id": "ie_general_employment_permit",
            "requirement": "eligibility",
            "category": "core",
        },
        {
            "case_id": "nl_hsm_requirements",
            "query": "Highly skilled migrant residence permit recognised sponsor requirements",
            "jurisdiction": "NL",
            "pathway_id": "nl_highly_skilled_migrant",
            "requirement": "eligibility",
            "category": "core",
        },
        {
            "case_id": "nl_hsm_documents",
            "query": "Highly skilled migrant means of evidence documents form 7511",
            "jurisdiction": "NL",
            "pathway_id": "nl_highly_skilled_migrant",
            "requirement": "required_documents",
            "category": "core",
        },
        {
            "case_id": "sg_ep_eligibility",
            "query": "Employment Pass eligibility qualifying salary COMPASS",
            "jurisdiction": "SG",
            "pathway_id": "sg_employment_pass",
            "requirement": "eligibility",
            "category": "core",
        },
        {
            "case_id": "sg_ep_documents",
            "query": "Employment Pass documents required passport ACRA qualifications",
            "jurisdiction": "SG",
            "pathway_id": "sg_employment_pass",
            "requirement": "required_documents",
            "category": "core",
        },
        {
            "case_id": "ca_fsw_provenance",
            "query": "Federal Skilled Worker Program Express Entry overview",
            "jurisdiction": "CA",
            "pathway_id": "ca_federal_skilled_worker",
            "requirement": "official_provenance",
            "category": "core",
            "notes": "No ca_express_entry pathway row; FSW COMPLETE provenance used as Express Entry family proxy.",
        },
        {
            "case_id": "ca_fsw_funds_partial",
            "query": "Federal Skilled Worker proof of funds CAD requirements",
            "jurisdiction": "CA",
            "pathway_id": "ca_federal_skilled_worker",
            "requirement": "financial_or_sponsorship",
            "category": "partial_gold",
            "notes": "PARTIAL matrix cell — document-level gold only; passage scoring optional.",
        },
        # Held-out / generalization cases (not used to design Phase 8H retrieval).
        {
            "case_id": "heldout_gb_global_talent_prize",
            "query": "Global Talent visa prestigious prize endorsement exemption",
            "jurisdiction": "GB",
            "pathway_id": "gb_global_talent",
            "requirement": "eligibility",
            "category": "heldout",
            "notes": "Held-out COMPLETE cell; exact-code/prize eligibility phrasing.",
        },
        {
            "case_id": "heldout_gb_graduate_eligibility",
            "query": "Graduate visa eligibility requirements current Student visa",
            "jurisdiction": "GB",
            "pathway_id": "gb_graduate",
            "requirement": "eligibility",
            "category": "heldout",
        },
        {
            "case_id": "heldout_au_student_funds",
            "query": "Student visa 500 evidence enough money AUD87856 parent income",
            "jurisdiction": "AU",
            "pathway_id": "au_student_500",
            "requirement": "financial_or_sponsorship",
            "category": "heldout",
            "notes": "Held-out; includes numeric legal/financial reference AUD87,856.",
        },
        {
            "case_id": "heldout_ca_cec_overview",
            "query": "Canadian Experience Class skilled workers permanent residence overview",
            "jurisdiction": "CA",
            "pathway_id": "ca_canadian_experience_class",
            "requirement": "official_provenance",
            "category": "heldout",
        },
        {
            "case_id": "heldout_nz_citizenship_apply",
            "query": "How to apply for New Zealand citizenship by grant steps",
            "jurisdiction": "NZ",
            "pathway_id": "nz_citizenship_by_grant",
            "requirement": "application_process",
            "category": "heldout",
        },
        {
            "case_id": "heldout_sg_pr_checklist",
            "query": "Singapore permanent residence document checklist passport photo",
            "jurisdiction": "SG",
            "pathway_id": "sg_permanent_resident",
            "requirement": "required_documents",
            "category": "heldout",
        },
    ]

    cases: list[GoldCase] = []
    exclusions: list[dict[str, Any]] = []

    for spec in query_specs:
        pid = spec["pathway_id"]
        req = spec["requirement"]
        matches = [
            r
            for r in by_pid.get(pid, [])
            if r["requirement"] == req and r.get("document_id")
        ]
        if not matches:
            exclusions.append(
                {
                    "case_id": spec["case_id"],
                    "reason": "no_matrix_document_association",
                    "pathway_id": pid,
                    "requirement": req,
                }
            )
            continue
        # Prefer COMPLETE, else PARTIAL for document-level only.
        complete = [r for r in matches if r["classification"] == "COMPLETE"]
        partial = [r for r in matches if r["classification"] == "PARTIAL"]
        chosen = complete or partial
        if not chosen:
            exclusions.append(
                {
                    "case_id": spec["case_id"],
                    "reason": "no_complete_or_partial_with_document",
                    "pathway_id": pid,
                    "requirement": req,
                }
            )
            continue

        doc_ids: set[int] = set()
        urls: set[str] = set()
        needles: list[str] = []
        for row in chosen:
            try:
                doc_ids.add(int(row["document_id"]))
            except (TypeError, ValueError):
                continue
            if row.get("source_url"):
                urls.add(row["source_url"].rstrip("/"))
            # Keep full matrix excerpts for independent passage resolution.
            excerpt = (row.get("passage_excerpt") or "").strip()
            if excerpt:
                needles.append(excerpt)

        # Verify documents exist.
        existing = set(
            session.scalars(
                select(Document.id).where(Document.id.in_(list(doc_ids)))
            ).all()
        )
        missing_docs = sorted(doc_ids - existing)
        if missing_docs:
            exclusions.append(
                {
                    "case_id": spec["case_id"],
                    "reason": "matrix_document_missing_from_db",
                    "missing_document_ids": missing_docs,
                }
            )
            doc_ids = existing
        if not doc_ids:
            continue

        scored = bool(complete)  # PARTIAL-only cases still evaluated but flagged
        cases.append(
            GoldCase(
                case_id=spec["case_id"],
                query=spec["query"],
                jurisdiction=spec["jurisdiction"],
                pathway_id=pid,
                requirement=req,
                expected_document_ids=frozenset(int(x) for x in doc_ids),
                expected_urls=frozenset(urls),
                passage_needles=tuple(needles),
                scored=scored,
                notes=spec.get("notes", ""),
                category=spec.get("category", "core"),
            )
        )

    # Diagnostic / adversarial cases (not all scored for recall).
    diagnostic_specs = [
        GoldCase(
            case_id="wrong_subtype_gb_student_vs_sw",
            query="Student visa Confirmation of Acceptance for Studies CAS",
            jurisdiction="GB",
            pathway_id="gb_skilled_worker",
            requirement="eligibility",
            expected_document_ids=frozenset(),
            expected_urls=frozenset(),
            passage_needles=(),
            scored=False,
            notes="Wrong-pathway probe: student query under SW pathway expectation context.",
            category="wrong_pathway",
        ),
        GoldCase(
            case_id="cross_jurisdiction_nl_topic_in_gb",
            query="Highly skilled migrant recognised sponsor IND salary",
            jurisdiction="GB",
            pathway_id=None,
            requirement=None,
            expected_document_ids=frozenset(),
            expected_urls=frozenset(),
            passage_needles=(),
            scored=False,
            notes="Must not leak NL documents into GB results.",
            category="cross_jurisdiction",
        ),
        GoldCase(
            case_id="eu_directive_diagnostic",
            query="Directive right of citizens move and reside freely",
            jurisdiction="EU",
            pathway_id=None,
            requirement=None,
            expected_document_ids=frozenset(),
            expected_urls=frozenset(),
            passage_needles=(),
            scored=False,
            notes="No COMPLETE EU pathway cells in coverage matrix — diagnostic only.",
            category="eu_unscored",
        ),
        GoldCase(
            case_id="sparse_jp_student",
            query="Japan student residence status eligibility documents",
            jurisdiction="JP",
            pathway_id="jp_student",
            requirement="eligibility",
            expected_document_ids=frozenset(),
            expected_urls=frozenset(),
            passage_needles=(),
            scored=False,
            notes="JP student cells UNEVALUATED/MISSING — expect weak/empty evidence.",
            category="sparse",
        ),
        GoldCase(
            case_id="sparse_ae_partial",
            query="UAE residence visa for family members sponsorship requirements",
            jurisdiction="AE",
            pathway_id="ae_family",
            requirement="financial_or_sponsorship",
            expected_document_ids=frozenset(),
            expected_urls=frozenset(),
            passage_needles=(),
            scored=False,
            notes="AE family financial cell is PARTIAL only — diagnostic.",
            category="sparse",
        ),
    ]

    # Attach document gold for AE partial if present.
    ae_rows = [
        r
        for r in by_pid.get("ae_family", [])
        if r["requirement"] == "financial_or_sponsorship" and r.get("document_id")
    ]
    if ae_rows:
        try:
            ae_doc = int(ae_rows[0]["document_id"])
            diagnostic_specs[4] = GoldCase(
                case_id=diagnostic_specs[4].case_id,
                query=diagnostic_specs[4].query,
                jurisdiction="AE",
                pathway_id="ae_family",
                requirement="financial_or_sponsorship",
                expected_document_ids=frozenset({ae_doc}),
                expected_urls=frozenset(
                    {ae_rows[0]["source_url"].rstrip("/")}
                    if ae_rows[0].get("source_url")
                    else set()
                ),
                passage_needles=tuple(
                    n
                    for n in [_needle_from_excerpt(ae_rows[0].get("passage_excerpt") or "")]
                    if n
                ),
                scored=False,
                notes=diagnostic_specs[4].notes,
                category="sparse",
            )
        except ValueError:
            pass

    # Attach SW eligibility docs for wrong-pathway probe comparison.
    sw_elig = [
        r
        for r in by_pid.get("gb_skilled_worker", [])
        if r["requirement"] == "eligibility" and r.get("document_id")
    ]
    if sw_elig:
        try:
            sw_doc = int(sw_elig[0]["document_id"])
            diagnostic_specs[0] = GoldCase(
                case_id=diagnostic_specs[0].case_id,
                query=diagnostic_specs[0].query,
                jurisdiction="GB",
                pathway_id="gb_skilled_worker",
                requirement="eligibility",
                expected_document_ids=frozenset({sw_doc}),
                expected_urls=frozenset(),
                passage_needles=(),
                scored=False,
                notes=diagnostic_specs[0].notes,
                category="wrong_pathway",
            )
        except ValueError:
            pass

    cases.extend(diagnostic_specs)
    return cases, exclusions


def _first_relevant_rank(ids: Sequence[int], relevant: set[int]) -> int | None:
    for i, item in enumerate(ids, start=1):
        if item in relevant:
            return i
    return None


def _recall_at(k: int, retrieved: Sequence[int], relevant: set[int]) -> float:
    if not relevant:
        return 0.0
    hit = any(x in relevant for x in retrieved[:k])
    return 1.0 if hit else 0.0


def evaluate_mode(
    session: Session,
    retriever: InternalKnowledgeRetriever,
    cases: Sequence[GoldCase],
    *,
    mode: RetrievalMode,
    top_k: int = 10,
) -> ModeMetrics:
    scored_cases = [c for c in cases if c.scored and c.expected_document_ids]
    metrics = ModeMetrics(mode=mode, n_scored=len(scored_cases))
    if not scored_cases and not cases:
        return metrics

    recall3_doc: list[float] = []
    recall5_doc: list[float] = []
    recall10_doc: list[float] = []
    recall3_pass: list[float] = []
    recall5_pass: list[float] = []
    recall10_pass: list[float] = []
    mrr_doc: list[float] = []
    mrr_pass: list[float] = []
    prec5: list[float] = []
    cite_ok: list[float] = []
    jur_leak: list[float] = []
    cur_leak: list[float] = []
    dupes: list[float] = []
    dup_docs: list[float] = []
    empty: list[float] = []
    passage_scored_n = 0
    # Per-split accumulators for development (core) vs held-out reporting.
    split_buckets: dict[str, dict[str, list[float]]] = {
        "core": {
            "r3d": [],
            "r5d": [],
            "r10d": [],
            "mrrd": [],
            "r3p": [],
            "r5p": [],
            "r10p": [],
            "mrrp": [],
            "cite": [],
        },
        "heldout": {
            "r3d": [],
            "r5d": [],
            "r10d": [],
            "mrrd": [],
            "r3p": [],
            "r5p": [],
            "r10p": [],
            "mrrp": [],
            "cite": [],
        },
    }

    for case in cases:
        gold_chunks, passage_method = resolve_passage_references(
            session,
            document_ids=case.expected_document_ids,
            excerpts=case.passage_needles,
        )
        result = retriever.retrieve(
            InternalQuery(
                query=case.query,
                jurisdiction=case.jurisdiction,
                mode=mode,
                top_k=top_k,
            )
        )
        passages = result.passages
        retrieved_docs = [p.document_id for p in passages]
        retrieved_chunks = [p.chunk_id for p in passages]
        retrieved_urls = [p.official_url.rstrip("/") for p in passages]

        leak = any(p.jurisdiction_code != case.jurisdiction for p in passages)
        # Current-version leakage: hydrate check already filters; verify via DB.
        noncurrent = 0
        if retrieved_chunks:
            noncurrent = session.scalar(
                select(DocumentChunk.id)
                .where(
                    DocumentChunk.id.in_(retrieved_chunks),
                    DocumentChunk.is_current_version.is_(False),
                )
                .limit(1)
            )
            noncurrent = 1 if noncurrent is not None else 0
        has_dupe = len(retrieved_chunks) != len(set(retrieved_chunks))
        has_dup_doc = len(retrieved_docs) != len(set(retrieved_docs))
        is_empty = result.status == RetrievalStatus.NO_RESULTS or not passages

        detail: dict[str, Any] = {
            "case_id": case.case_id,
            "category": case.category,
            "scored": case.scored,
            "status": result.status.value,
            "latency_ms": round(result.latency_ms, 2),
            "retrieved_document_ids": retrieved_docs,
            "retrieved_chunk_ids": retrieved_chunks,
            "gold_document_ids": sorted(case.expected_document_ids),
            "gold_chunk_ids": sorted(gold_chunks),
            "passage_gold_resolved": bool(gold_chunks),
            "passage_resolution_method": passage_method,
            "jurisdiction_leak": leak,
            "noncurrent_hits": int(noncurrent),
            "duplicate_chunk_ids": has_dupe,
            "duplicate_document_ids": has_dup_doc,
            "empty": is_empty,
            "notes": case.notes,
        }

        if case.scored and case.expected_document_ids:
            rel_docs = set(case.expected_document_ids)
            r3d = _recall_at(3, retrieved_docs, rel_docs)
            r5d = _recall_at(5, retrieved_docs, rel_docs)
            r10d = _recall_at(10, retrieved_docs, rel_docs)
            recall3_doc.append(r3d)
            recall5_doc.append(r5d)
            recall10_doc.append(r10d)
            rank_doc = _first_relevant_rank(retrieved_docs, rel_docs)
            mrr_d = 0.0 if rank_doc is None else 1.0 / rank_doc
            mrr_doc.append(mrr_d)
            top5_docs = retrieved_docs[:5]
            if top5_docs:
                prec5.append(sum(1 for d in top5_docs if d in rel_docs) / len(top5_docs))
            # Citation accuracy: retrieved official URL belongs to a gold document.
            if passages:
                gold_urls = set(case.expected_urls)
                if gold_urls:
                    cite_val = (
                        1.0
                        if any(u in gold_urls for u in retrieved_urls[:5])
                        or any(d in rel_docs for d in retrieved_docs[:5])
                        else 0.0
                    )
                else:
                    cite_val = (
                        1.0 if any(d in rel_docs for d in retrieved_docs[:5]) else 0.0
                    )
            else:
                cite_val = 0.0
            cite_ok.append(cite_val)

            split_key = "heldout" if case.category == "heldout" else "core"
            bucket = split_buckets[split_key]
            bucket["r3d"].append(r3d)
            bucket["r5d"].append(r5d)
            bucket["r10d"].append(r10d)
            bucket["mrrd"].append(mrr_d)
            bucket["cite"].append(cite_val)

            if gold_chunks:
                passage_scored_n += 1
                rel_chunks = set(gold_chunks)
                r3p = _recall_at(3, retrieved_chunks, rel_chunks)
                r5p = _recall_at(5, retrieved_chunks, rel_chunks)
                r10p = _recall_at(10, retrieved_chunks, rel_chunks)
                recall3_pass.append(r3p)
                recall5_pass.append(r5p)
                recall10_pass.append(r10p)
                rank_pass = _first_relevant_rank(retrieved_chunks, rel_chunks)
                mrr_p = 0.0 if rank_pass is None else 1.0 / rank_pass
                mrr_pass.append(mrr_p)
                bucket["r3p"].append(r3p)
                bucket["r5p"].append(r5p)
                bucket["r10p"].append(r10p)
                bucket["mrrp"].append(mrr_p)
                detail["doc_hit_at_10"] = r10d
                detail["pass_hit_at_10"] = r10p
            else:
                detail["doc_hit_at_10"] = r10d
                detail["pass_hit_at_10"] = None
                detail["passage_scoring"] = "excluded_unresolved_excerpt"

        # Safety rates measured on all cases with results attempted.
        jur_leak.append(1.0 if leak else 0.0)
        cur_leak.append(1.0 if noncurrent else 0.0)
        dupes.append(1.0 if has_dupe else 0.0)
        dup_docs.append(1.0 if has_dup_doc else 0.0)
        empty.append(1.0 if is_empty else 0.0)

        # Wrong-pathway: share of top-5 docs that match SW gold when query is student.
        if case.category == "wrong_pathway" and case.expected_document_ids:
            top5 = retrieved_docs[:5]
            detail["wrong_pathway_sw_doc_in_top5"] = any(
                d in case.expected_document_ids for d in top5
            )

        metrics.details.append(detail)

    def _avg(vals: list[float]) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    metrics.n_passage_scored = passage_scored_n
    metrics.recall_at_3_doc = _avg(recall3_doc)
    metrics.recall_at_5_doc = _avg(recall5_doc)
    metrics.recall_at_10_doc = _avg(recall10_doc)
    metrics.mrr_at_10_doc = _avg(mrr_doc)
    metrics.precision_at_5_doc = _avg(prec5) if prec5 else None
    metrics.citation_accuracy = _avg(cite_ok) if cite_ok else 0.0
    metrics.jurisdiction_leak_rate = _avg(jur_leak)
    metrics.current_version_leak_rate = _avg(cur_leak)
    metrics.duplicate_chunk_rate = _avg(dupes)
    metrics.duplicate_document_rate = _avg(dup_docs)
    metrics.empty_result_rate = _avg(empty)
    if recall3_pass:
        metrics.recall_at_3_pass = _avg(recall3_pass)
        metrics.recall_at_5_pass = _avg(recall5_pass)
        metrics.recall_at_10_pass = _avg(recall10_pass)
        metrics.mrr_at_10_pass = _avg(mrr_pass)

    for split_name, bucket in split_buckets.items():
        if not bucket["r10d"]:
            continue
        metrics.split_metrics[split_name] = {
            "n_scored": len(bucket["r10d"]),
            "n_passage_scored": len(bucket["r10p"]),
            "recall_at_3_doc": round(_avg(bucket["r3d"]), 4),
            "recall_at_5_doc": round(_avg(bucket["r5d"]), 4),
            "recall_at_10_doc": round(_avg(bucket["r10d"]), 4),
            "mrr_at_10_doc": round(_avg(bucket["mrrd"]), 4),
            "citation_accuracy": round(_avg(bucket["cite"]), 4),
            "recall_at_3_pass": (
                None if not bucket["r3p"] else round(_avg(bucket["r3p"]), 4)
            ),
            "recall_at_5_pass": (
                None if not bucket["r5p"] else round(_avg(bucket["r5p"]), 4)
            ),
            "recall_at_10_pass": (
                None if not bucket["r10p"] else round(_avg(bucket["r10p"]), 4)
            ),
            "mrr_at_10_pass": (
                None if not bucket["mrrp"] else round(_avg(bucket["mrrp"]), 4)
            ),
        }
    return metrics


def pathway_association_readiness(
    session: Session, coverage_path: Path
) -> dict[str, Any]:
    rows = load_coverage_rows(coverage_path)
    complete = [r for r in rows if r["classification"] == "COMPLETE" and r.get("document_id")]
    partial = [r for r in rows if r["classification"] == "PARTIAL" and r.get("document_id")]
    pathways_complete = sorted({r["pathway_id"] for r in complete})
    pathways_any = sorted(
        {
            r["pathway_id"]
            for r in rows
            if r["classification"] in ("COMPLETE", "PARTIAL") and r.get("document_id")
        }
    )
    tagged_chunks = session.execute(
        text(
            """
            SELECT COUNT(*) FILTER (WHERE pathway_id IS NOT NULL) AS with_pathway,
                   COUNT(*) FILTER (WHERE requirement_category IS NOT NULL) AS with_req,
                   COUNT(*) AS total
            FROM document_chunks WHERE is_current_version
            """
        )
    ).mappings().one()
    dj = session.execute(text("SELECT COUNT(*) FROM document_jurisdictions")).scalar_one()

    # Ambiguity: same document linked to multiple pathways.
    doc_to_pathways: dict[str, set[str]] = {}
    for r in complete + partial:
        doc_to_pathways.setdefault(r["document_id"], set()).add(r["pathway_id"])
    ambiguous_docs = {
        d: sorted(pids) for d, pids in doc_to_pathways.items() if len(pids) > 1
    }

    return {
        "complete_cells_with_document": len(complete),
        "partial_cells_with_document": len(partial),
        "pathways_with_complete_document_link": len(pathways_complete),
        "pathways_with_complete_or_partial_document_link": len(pathways_any),
        "pathways_complete_list": pathways_complete,
        "chunk_pathway_tags": dict(tagged_chunks),
        "document_jurisdictions_rows": int(dj),
        "ambiguous_document_ids_multi_pathway": {
            k: v for k, v in list(ambiguous_docs.items())[:20]
        },
        "ambiguous_document_count": len(ambiguous_docs),
        "safe_chunk_filter_ready": False,
        "recommendation": (
            "Do not auto-tag chunks. Optionally enable document-level pathway "
            "soft-boost for the limited COMPLETE document associations only, "
            "while keeping hard pathway filters unsupported until curated "
            "chunk-level associations exist."
        ),
    }


def measure_performance(
    session: Session, retriever: InternalKnowledgeRetriever
) -> dict[str, Any]:
    # Cold already paid by caller if model loaded; measure component timings.
    q = "Skilled Worker visa salary requirements"
    jur = "GB"

    t0 = time.perf_counter()
    emb = retriever.embedder.embed_queries([q])
    embed_ms = (time.perf_counter() - t0) * 1000

    t1 = time.perf_counter()
    _ = retriever._keyword_search(q, jur, limit=10)
    keyword_ms = (time.perf_counter() - t1) * 1000

    t2 = time.perf_counter()
    _ = retriever._semantic_search(q, jur, limit=10)
    semantic_db_ms = (time.perf_counter() - t2) * 1000

    warm = {}
    for mode in ("keyword", "semantic", "hybrid"):
        t = time.perf_counter()
        result = retriever.retrieve(
            InternalQuery(query=q, jurisdiction=jur, mode=mode, top_k=5)
        )
        warm[mode] = {
            "latency_ms": round((time.perf_counter() - t) * 1000, 2),
            "reported_latency_ms": round(result.latency_ms, 2),
            "hits": len(result.passages),
        }

    mem_mb = None
    try:
        import os

        import psutil  # type: ignore

        mem_mb = round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 1)
    except Exception:
        mem_mb = None

    return {
        "query_embed_ms": round(embed_ms, 2),
        "keyword_db_ms": round(keyword_ms, 2),
        "semantic_db_plus_embed_ms": round(semantic_db_ms, 2),
        "vector_shape": list(emb.vectors.shape),
        "warm_end_to_end_ms": warm,
        "process_rss_mb": mem_mb,
        "note": "semantic_db_plus_embed_ms includes query encoding for that call path.",
    }
