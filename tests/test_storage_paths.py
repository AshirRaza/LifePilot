"""Path containment tests for raw/extracted artifact reads."""

from __future__ import annotations

from pathlib import Path

import pytest

from lifepilot.crawl.storage import resolve_under_data_root, safe_read_bytes


def test_resolve_allows_relative_under_root(tmp_path: Path):
    root = tmp_path / "data"
    target = root / "raw" / "documents" / "1" / "v1.html"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"ok")
    resolved = resolve_under_data_root(root, "raw/documents/1/v1.html")
    assert resolved == target.resolve()
    assert safe_read_bytes(root, "raw/documents/1/v1.html") == b"ok"


def test_resolve_rejects_parent_traversal(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("leak", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes data root"):
        resolve_under_data_root(root, "../secret.txt")
    with pytest.raises(ValueError, match="escapes data root"):
        safe_read_bytes(root, "../secret.txt")


def test_resolve_rejects_prefix_collision(tmp_path: Path):
    """``.../data`` must not accept paths under ``.../database`` via startswith."""
    data = tmp_path / "data"
    database = tmp_path / "database"
    data.mkdir()
    database.mkdir()
    leak = database / "leak.txt"
    leak.write_text("nope", encoding="utf-8")

    # Demonstrate the old string-prefix hazard on this layout.
    assert str(leak.resolve()).startswith(str(data.resolve()))

    with pytest.raises(ValueError, match="escapes data root"):
        resolve_under_data_root(data, str(leak))
    with pytest.raises(ValueError, match="escapes data root"):
        safe_read_bytes(data, str(leak))


def test_resolve_rejects_absolute_outside_root(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "other" / "file.bin"
    outside.parent.mkdir()
    outside.write_bytes(b"x")
    with pytest.raises(ValueError, match="escapes data root"):
        resolve_under_data_root(root, str(outside.resolve()))


def test_resolve_rejects_symlink_escape(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.bin"
    target.write_bytes(b"secret")
    link = root / "escape_link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink not available: {exc}")

    with pytest.raises(ValueError, match="escapes data root"):
        resolve_under_data_root(root, "escape_link/secret.bin")


def test_safe_read_missing_file(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    with pytest.raises(FileNotFoundError):
        safe_read_bytes(root, "raw/missing.html")
