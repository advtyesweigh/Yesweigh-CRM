"""Stable campaign media aliases must reuse one wa-media file (not UUID-per-send)."""
from __future__ import annotations

from pathlib import Path

from app import media_store


def test_ensure_stable_from_path_reuses_same_stem(tmp_path: Path, monkeypatch):
    root = tmp_path / "wa-media"
    monkeypatch.setattr(media_store, "MEDIA_ROOT", root)
    src = tmp_path / "library.mp4"
    payload = b"GATC-RAM-BYTES" * 1000
    src.write_bytes(payload)

    url1, name1 = media_store.ensure_stable_from_path(
        src, stem="campaign_uid_1000", media_type="video", filename="gatc.mp4"
    )
    url2, name2 = media_store.ensure_stable_from_path(
        src, stem="campaign_uid_1000", media_type="video", filename="gatc.mp4"
    )

    assert name1 == name2 == "campaign_uid_1000.mp4"
    assert url1 == url2 == f"{media_store.PUBLIC_PREFIX}/campaign_uid_1000.mp4"
    assert (root / name1).read_bytes() == payload
    # Only one stable file — no UUID copies.
    assert list(root.glob("*.mp4")) == [root / "campaign_uid_1000.mp4"]


def test_ensure_stable_bytes_skips_rewrite_when_size_matches(tmp_path: Path, monkeypatch):
    root = tmp_path / "wa-media"
    monkeypatch.setattr(media_store, "MEDIA_ROOT", root)
    content = b"abc123"
    url1, name1 = media_store.ensure_stable_bytes(
        content, stem="campaign_media_1", media_type="video", filename="x.mp4"
    )
    mtime1 = (root / name1).stat().st_mtime_ns
    url2, name2 = media_store.ensure_stable_bytes(
        content, stem="campaign_media_1", media_type="video", filename="x.mp4"
    )
    assert (url1, name1) == (url2, name2)
    assert (root / name1).stat().st_mtime_ns == mtime1
