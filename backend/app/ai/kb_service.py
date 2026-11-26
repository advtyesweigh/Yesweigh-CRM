"""FAQ Knowledge Base — CRUD, FTS search, admin approval. Separate from ai_knowledge glossary."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KbArticle, KbArticleVersion, User, WhatsAppConversation

ORG = "yesweigh"

STATUSES = frozenset({"suggested", "under_review", "approved", "rejected", "archived"})
CATEGORIES = frozenset(
    {
        "greeting",
        "product_info",
        "catalogue",
        "warranty",
        "dealer_registration",
        "troubleshooting",
        "company_info",
        "working_hours",
        "software",
        "sales",
        "complaint",
        "other",
    }
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_status(s: str | None) -> str:
    v = (s or "suggested").strip().lower().replace(" ", "_")
    return v if v in STATUSES else "suggested"


def _norm_category(s: str | None) -> str:
    v = (s or "other").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "product": "product_info",
        "products": "product_info",
        "specs": "product_info",
        "dealer": "dealer_registration",
        "hours": "working_hours",
        "company": "company_info",
        "faq": "other",
        "support": "troubleshooting",
    }
    v = aliases.get(v, v)
    return v if v in CATEGORIES else "other"


def article_to_dict(row: KbArticle, *, include_sources: bool = True) -> dict[str, Any]:
    src_msgs: list[str] = []
    try:
        raw = json.loads(row.source_message_ids or "[]")
        if isinstance(raw, list):
            src_msgs = [str(x) for x in raw][:40]
    except Exception:  # noqa: BLE001
        src_msgs = []
    out: dict[str, Any] = {
        "id": row.public_id,
        "db_id": row.id,
        "title": row.title or "",
        "question": row.question or "",
        "answer": row.answer or "",
        "category": row.category or "other",
        "product": row.product or "",
        "keywords": row.keywords or "",
        "language": row.language or "en",
        "status": row.status or "suggested",
        "version": int(row.version or 1),
        "needs_review": bool(row.needs_review),
        "usage_count": int(row.usage_count or 0),
        "helpful_count": int(row.helpful_count or 0),
        "incorrect_count": int(row.incorrect_count or 0),
        "similar_to_id": row.similar_to_id,
        "contradiction_of_id": row.contradiction_of_id,
        "review_notes": row.review_notes or "",
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if include_sources:
        out["source_employee_id"] = row.source_employee_id
        out["source_conversation_id"] = row.source_conversation_id
        out["source_message_ids"] = src_msgs
    return out


async def stats(db: AsyncSession, *, organization_id: str = ORG) -> dict[str, int]:
    rows = (
        await db.execute(
            select(KbArticle.status, func.count())
            .where(KbArticle.organization_id == organization_id)
            .group_by(KbArticle.status)
        )
    ).all()
    counts = {str(s): int(c or 0) for s, c in rows}
    needs = (
        await db.execute(
            select(func.count()).where(
                KbArticle.organization_id == organization_id,
                KbArticle.needs_review.is_(True),
                KbArticle.status.in_(("suggested", "under_review", "approved")),
            )
        )
    ).scalar_one()
    return {
        "approved": counts.get("approved", 0),
        "suggested": counts.get("suggested", 0),
        "under_review": counts.get("under_review", 0),
        "rejected": counts.get("rejected", 0),
        "archived": counts.get("archived", 0),
        "needs_review": int(needs or 0),
        "total": sum(counts.values()),
    }


async def list_articles(
    db: AsyncSession,
    *,
    organization_id: str = ORG,
    status: str | None = None,
    category: str | None = None,
    language: str | None = None,
    q: str | None = None,
    needs_review: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[KbArticle], int]:
    lim = max(1, min(int(limit or 50), 200))
    off = max(0, int(offset or 0))
    base = select(KbArticle).where(KbArticle.organization_id == organization_id)
    count_q = select(func.count()).select_from(KbArticle).where(KbArticle.organization_id == organization_id)
    if status:
        st = _norm_status(status)
        base = base.where(KbArticle.status == st)
        count_q = count_q.where(KbArticle.status == st)
    if category:
        cat = _norm_category(category)
        base = base.where(KbArticle.category == cat)
        count_q = count_q.where(KbArticle.category == cat)
    if language:
        lang = (language or "").strip().lower()[:20]
        base = base.where(KbArticle.language == lang)
        count_q = count_q.where(KbArticle.language == lang)
    if needs_review is not None:
        base = base.where(KbArticle.needs_review.is_(bool(needs_review)))
        count_q = count_q.where(KbArticle.needs_review.is_(bool(needs_review)))
    qtext = (q or "").strip()
    if qtext:
        # Prefer Postgres FTS; fall back to ILIKE if index/query fails.
        try:
            fts = await _fts_ids(db, qtext, organization_id=organization_id, limit=lim + off)
            if fts:
                base = base.where(KbArticle.id.in_(fts))
                count_q = count_q.where(KbArticle.id.in_(fts))
            else:
                like = f"%{qtext}%"
                filt = or_(
                    KbArticle.title.ilike(like),
                    KbArticle.question.ilike(like),
                    KbArticle.answer.ilike(like),
                    KbArticle.keywords.ilike(like),
                    KbArticle.product.ilike(like),
                )
                base = base.where(filt)
                count_q = count_q.where(filt)
        except Exception:  # noqa: BLE001
            like = f"%{qtext}%"
            filt = or_(
                KbArticle.title.ilike(like),
                KbArticle.question.ilike(like),
                KbArticle.answer.ilike(like),
                KbArticle.keywords.ilike(like),
            )
            base = base.where(filt)
            count_q = count_q.where(filt)

    total = int((await db.execute(count_q)).scalar_one() or 0)
    rows = (
        await db.execute(
            base.order_by(KbArticle.updated_at.desc(), KbArticle.id.desc()).offset(off).limit(lim)
        )
    ).scalars().all()
    return list(rows), total


async def _fts_ids(
    db: AsyncSession,
    query: str,
    *,
    organization_id: str = ORG,
    limit: int = 50,
    approved_only: bool = False,
) -> list[int]:
    q = re.sub(r"[^\w\s]", " ", query or "", flags=re.UNICODE)
    q = re.sub(r"\s+", " ", q).strip()
    if not q:
        return []
    # plainto_tsquery is safer than raw user tokens
    status_clause = "AND status = 'approved'" if approved_only else ""
    sql = text(
        f"""
        SELECT id FROM kb_articles
        WHERE organization_id = :org
          {status_clause}
          AND to_tsvector(
                'simple',
                coalesce(title,'') || ' ' || coalesce(question,'') || ' ' ||
                coalesce(answer,'') || ' ' || coalesce(keywords,'') || ' ' || coalesce(product,'')
              ) @@ plainto_tsquery('simple', :q)
        ORDER BY ts_rank(
            to_tsvector(
              'simple',
              coalesce(title,'') || ' ' || coalesce(question,'') || ' ' ||
              coalesce(answer,'') || ' ' || coalesce(keywords,'') || ' ' || coalesce(product,'')
            ),
            plainto_tsquery('simple', :q)
        ) DESC
        LIMIT :lim
        """
    )
    rows = (await db.execute(sql, {"org": organization_id, "q": q, "lim": limit})).all()
    return [int(r[0]) for r in rows]


async def search_approved(
    db: AsyncSession,
    query: str,
    *,
    organization_id: str = ORG,
    category: str | None = None,
    language: str | None = None,
    limit: int = 8,
) -> list[KbArticle]:
    lim = max(1, min(int(limit or 8), 20))
    ids = await _fts_ids(db, query, organization_id=organization_id, limit=lim * 3, approved_only=True)
    if not ids:
        like = f"%{(query or '').strip()}%"
        stmt = (
            select(KbArticle)
            .where(
                KbArticle.organization_id == organization_id,
                KbArticle.status == "approved",
                or_(
                    KbArticle.title.ilike(like),
                    KbArticle.question.ilike(like),
                    KbArticle.keywords.ilike(like),
                    KbArticle.answer.ilike(like),
                ),
            )
            .order_by(KbArticle.usage_count.desc(), KbArticle.updated_at.desc())
            .limit(lim)
        )
        if category:
            stmt = stmt.where(KbArticle.category == _norm_category(category))
        if language:
            stmt = stmt.where(KbArticle.language == (language or "").strip().lower()[:20])
        return list((await db.execute(stmt)).scalars().all())

    rows = (
        await db.execute(select(KbArticle).where(KbArticle.id.in_(ids), KbArticle.status == "approved"))
    ).scalars().all()
    by_id = {r.id: r for r in rows}
    ordered = [by_id[i] for i in ids if i in by_id]
    if category:
        cat = _norm_category(category)
        ordered = [r for r in ordered if r.category == cat] or ordered
    if language:
        lang = (language or "").strip().lower()[:20]
        lang_pref = [r for r in ordered if r.language == lang]
        ordered = lang_pref or ordered
    return ordered[:lim]


async def get_by_public_id(db: AsyncSession, public_id: str) -> KbArticle | None:
    return (
        await db.execute(
            select(KbArticle).where(KbArticle.public_id == public_id, KbArticle.organization_id == ORG)
        )
    ).scalar_one_or_none()


async def create_article(
    db: AsyncSession,
    user: User | None,
    data: dict[str, Any],
    *,
    status: str = "suggested",
) -> KbArticle:
    row = KbArticle(
        public_id=str(uuid.uuid4()),
        organization_id=ORG,
        title=(data.get("title") or "").strip()[:240],
        question=(data.get("question") or "").strip()[:4000],
        answer=(data.get("answer") or "").strip()[:8000],
        category=_norm_category(data.get("category")),
        product=(data.get("product") or "").strip()[:160],
        keywords=(data.get("keywords") or "").strip()[:2000],
        language=(data.get("language") or "en").strip().lower()[:20] or "en",
        status=_norm_status(status),
        version=1,
        source_employee_id=data.get("source_employee_id"),
        source_conversation_id=data.get("source_conversation_id"),
        source_message_ids=json.dumps(data.get("source_message_ids") or []),
        similar_to_id=data.get("similar_to_id"),
        contradiction_of_id=data.get("contradiction_of_id"),
        needs_review=bool(data.get("needs_review") or data.get("contradiction_of_id")),
        created_by_user_id=user.id if user else None,
    )
    if not row.title and row.question:
        row.title = row.question[:120]
    db.add(row)
    await db.flush()
    db.add(
        KbArticleVersion(
            article_id=row.id,
            version=1,
            title=row.title,
            question=row.question,
            answer=row.answer,
            category=row.category,
            changed_by_user_id=user.id if user else None,
            change_reason="created",
        )
    )
    await db.flush()
    return row


async def update_article(
    db: AsyncSession,
    row: KbArticle,
    user: User | None,
    data: dict[str, Any],
    *,
    reason: str = "edit",
) -> KbArticle:
    changed = False
    for field, cast in (
        ("title", lambda v: str(v or "").strip()[:240]),
        ("question", lambda v: str(v or "").strip()[:4000]),
        ("answer", lambda v: str(v or "").strip()[:8000]),
        ("product", lambda v: str(v or "").strip()[:160]),
        ("keywords", lambda v: str(v or "").strip()[:2000]),
        ("language", lambda v: str(v or "en").strip().lower()[:20] or "en"),
        ("review_notes", lambda v: str(v or "").strip()[:2000]),
    ):
        if field in data:
            new_v = cast(data[field])
            if getattr(row, field) != new_v:
                setattr(row, field, new_v)
                changed = True
    if "category" in data:
        cat = _norm_category(data.get("category"))
        if row.category != cat:
            row.category = cat
            changed = True
    if changed:
        row.version = int(row.version or 1) + 1
        db.add(
            KbArticleVersion(
                article_id=row.id,
                version=row.version,
                title=row.title,
                question=row.question,
                answer=row.answer,
                category=row.category,
                changed_by_user_id=user.id if user else None,
                change_reason=(reason or "edit")[:255],
            )
        )
    await db.flush()
    return row


async def set_status(
    db: AsyncSession,
    row: KbArticle,
    user: User,
    status: str,
    *,
    notes: str = "",
) -> KbArticle:
    st = _norm_status(status)
    row.status = st
    row.reviewed_by_user_id = user.id
    row.reviewed_at = _now()
    if notes:
        row.review_notes = notes.strip()[:2000]
    if st == "approved":
        row.needs_review = False
    await db.flush()
    return row


async def find_similar_approved(
    db: AsyncSession,
    question: str,
    *,
    organization_id: str = ORG,
    limit: int = 5,
) -> list[KbArticle]:
    return await search_approved(db, question, organization_id=organization_id, limit=limit)


async def detect_contradiction(
    db: AsyncSession,
    candidate_answer: str,
    similar: list[KbArticle],
) -> KbArticle | None:
    """Heuristic: if answers share little overlap but questions matched, flag first approved."""
    cand = re.sub(r"\s+", " ", (candidate_answer or "").lower()).strip()
    if not cand or not similar:
        return None
    cand_tokens = set(re.findall(r"[a-zA-Z\u0d00-\u0d7f]{3,}", cand))
    for art in similar:
        if art.status != "approved":
            continue
        other = re.sub(r"\s+", " ", (art.answer or "").lower()).strip()
        other_tokens = set(re.findall(r"[a-zA-Z\u0d00-\u0d7f]{3,}", other))
        if not cand_tokens or not other_tokens:
            continue
        overlap = len(cand_tokens & other_tokens) / max(1, len(cand_tokens | other_tokens))
        # Low lexical overlap on similar question → possible contradiction
        if overlap < 0.15 and len(cand) > 40 and len(other) > 40:
            return art
    return None


async def source_preview(db: AsyncSession, article: KbArticle) -> dict[str, Any]:
    conv = None
    if article.source_conversation_id:
        conv = await db.get(WhatsAppConversation, article.source_conversation_id)
    return {
        "conversation_id": conv.public_id if conv else None,
        "customer_name": (conv.customer_name if conv else "") or "",
        "customer_phone": (conv.customer_phone if conv else "") or "",
        "source_employee_id": article.source_employee_id,
        "source_message_ids": json.loads(article.source_message_ids or "[]"),
    }


async def bump_usage(db: AsyncSession, article_ids: list[int]) -> None:
    if not article_ids:
        return
    rows = (
        await db.execute(select(KbArticle).where(KbArticle.id.in_(article_ids)))
    ).scalars().all()
    for r in rows:
        r.usage_count = int(r.usage_count or 0) + 1
    await db.flush()
