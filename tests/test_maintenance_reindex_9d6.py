"""Ensure maintenance reindex embeds chunks after extract/chunk."""

from __future__ import annotations

from types import SimpleNamespace

from lifepilot.maintenance.pipeline import default_reindex


def test_default_reindex_runs_embedding(monkeypatch):
    calls: list[str] = []

    class FakeSession:
        def get(self, _model, _id):
            return SimpleNamespace(id=7)

        def scalars(self, _stmt):
            return SimpleNamespace(all=lambda: [101, 102])

    def fake_extract(*_a, **_k):
        calls.append("extract")

    def fake_chunk(*_a, **_k):
        calls.append("chunk")

    def fake_embed(*_a, **_k):
        calls.append("embed")
        return SimpleNamespace(failed_batches=0)

    import lifepilot.extract.runner as extract_runner
    import lifepilot.maintenance.pipeline as maint
    import lifepilot.rag.embed_pipeline as embed_pipeline
    import lifepilot.rag.pipeline as rag_pipeline

    monkeypatch.setattr(maint, "get_current_version", lambda *_a, **_k: SimpleNamespace(id=1))
    monkeypatch.setattr(extract_runner, "extract_document_version", fake_extract)
    monkeypatch.setattr(rag_pipeline, "run_chunking", fake_chunk)
    monkeypatch.setattr(embed_pipeline, "run_embedding", fake_embed)

    ok = default_reindex(FakeSession(), 7)  # type: ignore[arg-type]
    assert ok is True
    assert calls == ["extract", "chunk", "embed"]


def test_default_reindex_raises_on_embed_failure(monkeypatch):
    import lifepilot.extract.runner as extract_runner
    import lifepilot.maintenance.pipeline as maint
    import lifepilot.rag.embed_pipeline as embed_pipeline
    import lifepilot.rag.pipeline as rag_pipeline

    class FakeSession:
        def get(self, _model, _id):
            return SimpleNamespace(id=7)

        def scalars(self, _stmt):
            return SimpleNamespace(all=lambda: [101])

    monkeypatch.setattr(maint, "get_current_version", lambda *_a, **_k: SimpleNamespace(id=1))
    monkeypatch.setattr(extract_runner, "extract_document_version", lambda *_a, **_k: None)
    monkeypatch.setattr(rag_pipeline, "run_chunking", lambda *_a, **_k: None)
    monkeypatch.setattr(
        embed_pipeline,
        "run_embedding",
        lambda *_a, **_k: SimpleNamespace(failed_batches=1),
    )

    try:
        default_reindex(FakeSession(), 7)  # type: ignore[arg-type]
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "embedding failed" in str(exc)
    assert raised
