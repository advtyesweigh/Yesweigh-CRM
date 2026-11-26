"""Stage Move → knowledge base learning for Intelligence classification.

On each human Move-to, extract recent chat phrases and upsert into ``ai_stage_kb``.
Funnel / enquiry / stamping / closed-exclude paths consult these labels so similar
future chats score toward the human-chosen stage.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiStageKb, AiStageMove, WhatsAppConversation, WhatsAppMessage

ORG = "yesweigh"

_MSG_LIMIT = 24
_SNIPPET_MAX = 900
_PHRASE_MIN = 4
_PHRASE_MAX = 80
_MAX_PHRASES = 12
_MIN_WEIGHT_MATCH = 1
_MIN_WEIGHT_EXCLUDE = 2  # Closed/blast exclude needs a second confirmation

# Drop ultra-generic tokens that would pollute every stage.
_STOP_PHRASES = frozenset(
    {
        "okay",
        "ok",
        "ok sir",
        "okay sir",
        "yes",
        "yes sir",
        "no",
        "sir",
        "you sir",
        "hi sir",
        "hello",
        "hi",
        "hai",
        "thanks",
        "thank you",
        "thank you sir",
        "good morning",
        "good evening",
        "ശരി",
        "ശരി സർ",
        "ഓക്കേ",
        "ഹായ്",
        # Media placeholders / fillers — never stage evidence on their own.
        "document",
        "photo",
        "video",
        "voice message",
        "media",
        "sticker",
        "attachment",
        "📄 document",
        "📷 photo",
        "🎥 video",
        "🎤 voice message",
    }
)

# Light stage-hint regexes — used as signals + phrase seeds when they fire.
_STAGE_HINTS: dict[str, re.Pattern[str]] = {
    "enquiry": re.compile(
        r"(?i)("
        r"price|rate|quotation|quote|how\s*much|dealer\s*price|"
        r"വില|നിരക്ക്|ക്വട്ടേഷൻ|ആവശ്യകത|requirement|waiting\s+for\s+requirement"
        r")"
    ),
    "sales_order": re.compile(
        r"(?i)("
        r"sales\s*order|order\s*confirm|spare\s*parts?|add\s+freight|"
        r"സെയിൽസ്\s*ഓർഡർ|ഓർഡർ\s*ഉണ്ടാക്ക|എസ്\s*ഉണ്ടാക്ക"
        r")"
    ),
    "waiting": re.compile(
        r"(?i)("
        r"waiting\s+for\s+payment|payment\s+pending|awaiting\s+payment|"
        r"invoice\s+sent|so\s+sent|അടയ്ക്കാൻ|പേയ്മെന്റ്\s*കാത്ത|"
        r"waiting\s+for\s+requirement|nobody\s+is\s+call|no\s+body\s+is\s+call"
        r")"
    ),
    "paid": re.compile(
        r"(?i)("
        r"paid\s+using|payment\s+via\s+upi|phonepe|gpay|paytm|"
        r"പണം\s*അടച്ച|ഫോൺപേ|യു\.?\s*പി\.?\s*ഐ"
        r")"
    ),
    "stamping": re.compile(
        r"(?i)("
        r"\bov\b|ov\s*please\s*send|stamping|stamped|certificate\s+of\s+verification|"
        r"legal\s*metrology|\bgo\s*\d{1,3}/\d{1,3}\b|"
        r"\b(?:y|yo|yz)\s*0*\d{3,6}\b|ayachilla|അയച്ചില്ല|"
        r"ഓവി|സ്റ്റാമ്പ്|പരിശോധന|സീൽ"
        r")"
    ),
    "service": re.compile(
        r"(?i)("
        r"repair|not\s*working|complaint|technician|spare\s*part|calibration|"
        r"പ്രശ്നം|പരാതി|റിപ്പയർ"
        r")"
    ),
    "commitments": re.compile(
        r"(?i)("
        r"will\s+(?:call|send|come)|by\s+(?:tomorrow|friday|monday)|2\s*min|"
        r"വിളിക്കാം|അയക്കാം|നാളെ"
        r")"
    ),
    "closed": re.compile(
        r"(?i)("
        r"dear\s+customer|container\s+strike|resume\s+dispatch|broadcast|"
        r"not\s+interested|no\s+need|wrong\s+number|"
        r"പ്രിയ\s*ഉപഭോക്താവ്|കണ്ടൈനർ\s*സ്ട്രൈക്ക്|വേണ്ട"
        r")"
    ),
}

_SENTENCE_SPLIT = re.compile(r"[\n\r]+|(?<=[.!?।])\s+")
_WS = re.compile(r"\s+")
# Keep Latin + Malayalam word runs (and short connectors) as candidate phrases.
_PHRASE_CANDIDATE = re.compile(
    r"(?iu)(?:[\w\u0d00-\u0d7f]+(?:[\s\-'][\w\u0d00-\u0d7f]+){1,7})"
)
_MEDIA_PLACEHOLDER = re.compile(
    r"(?i)^(?:\[media\]|media|📷|🎥|🎤|📄|sticker|attachment)\b"
)


@dataclass
class MoveFeatures:
    snippet: str = ""
    phrases: list[str] = field(default_factory=list)
    inbound: list[str] = field(default_factory=list)
    outbound: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    media_hints: list[str] = field(default_factory=list)

    def to_signals_json(self) -> str:
        return json.dumps(
            {
                "phrases": self.phrases,
                "inbound": self.inbound[:8],
                "outbound": self.outbound[:8],
                "signals": self.signals,
                "media_hints": self.media_hints,
            },
            ensure_ascii=False,
        )


def _clean_line(raw: str) -> str:
    text = _WS.sub(" ", (raw or "").strip())
    if not text or _MEDIA_PLACEHOLDER.match(text):
        return ""
    return text[:240]


def _normalize_phrase(raw: str) -> str:
    text = _WS.sub(" ", (raw or "").strip().lower())
    text = text.strip(" .,:;!?\"'`…-—/")
    if len(text) < _PHRASE_MIN or len(text) > _PHRASE_MAX:
        return ""
    if text in _STOP_PHRASES:
        return ""
    # Single very short Malayalam/English token — too weak alone.
    if " " not in text and len(text) < 6:
        return ""
    return text[:_PHRASE_MAX]


def _phrases_from_text(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for chunk in _SENTENCE_SPLIT.split(text or ""):
        line = _clean_line(chunk)
        if not line:
            continue
        # Prefer multi-word spans; also keep strong single sentences ≤ 80 chars.
        candidates = _PHRASE_CANDIDATE.findall(line)
        if not candidates and 4 <= len(line) <= _PHRASE_MAX:
            candidates = [line]
        for cand in candidates:
            norm = _normalize_phrase(cand)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            out.append(norm)
            if len(out) >= _MAX_PHRASES:
                return out
    return out


def extract_move_features(
    messages: list[dict[str, Any]] | list[Any],
    *,
    to_stage: str = "",
) -> MoveFeatures:
    """Pull recent inbound/outbound text + candidate phrases from a message window."""
    feat = MoveFeatures()
    lines_for_snippet: list[str] = []
    all_text_parts: list[str] = []
    per_msg_phrases: list[str] = []
    seen_phrases: set[str] = set()

    def _push_phrase(raw: str) -> None:
        norm = _normalize_phrase(raw)
        if not norm or norm in seen_phrases:
            return
        seen_phrases.add(norm)
        per_msg_phrases.append(norm)

    for row in messages[-_MSG_LIMIT:]:
        if isinstance(row, dict):
            direction = (row.get("direction") or "").strip().lower()
            body = row.get("body") or row.get("text") or ""
            transcript = row.get("transcript") or ""
            media_name = row.get("media_name") or ""
            media_type = row.get("media_type") or ""
        else:
            direction = (getattr(row, "direction", "") or "").strip().lower()
            body = getattr(row, "body", "") or ""
            transcript = getattr(row, "transcript", "") or ""
            media_name = getattr(row, "media_name", "") or ""
            media_type = getattr(row, "media_type", "") or ""

        blob = _clean_line(f"{body} {transcript}".strip())
        if media_name:
            hint = _clean_line(media_name)
            if hint and hint not in feat.media_hints:
                feat.media_hints.append(hint[:80])
        if media_type and "image" in media_type.lower() and "image" not in feat.media_hints:
            feat.media_hints.append("image")

        if not blob:
            continue
        all_text_parts.append(blob)
        who = "in" if direction == "in" else "out" if direction == "out" else direction or "?"
        lines_for_snippet.append(f"{who}: {blob[:160]}")
        if direction == "in":
            feat.inbound.append(blob[:160])
        elif direction == "out":
            feat.outbound.append(blob[:160])

        # Extract per message so we do not glue adjacent chat lines into one phrase.
        for p in _phrases_from_text(blob):
            _push_phrase(p)
        # Whole short customer line is often the best training example.
        if direction == "in" and _PHRASE_MIN <= len(blob) <= _PHRASE_MAX:
            _push_phrase(blob)

    feat.snippet = "\n".join(lines_for_snippet)[-_SNIPPET_MAX:]
    combined = " ".join(all_text_parts)

    # Stage-hint signals + seed exact matched fragments as phrases.
    for stage, pattern in _STAGE_HINTS.items():
        for match in pattern.finditer(combined):
            feat.signals.append(f"hint:{stage}")
            _push_phrase(match.group(0))
            break  # one signal flag per stage is enough

    phrases = list(per_msg_phrases)
    dest = (to_stage or "").strip().lower()
    dest_pat = _STAGE_HINTS.get(dest)
    if dest_pat:
        boosted = [p for p in phrases if dest_pat.search(p)]
        rest = [p for p in phrases if p not in boosted]
        phrases = (boosted + rest)[:_MAX_PHRASES]
    if dest == "closed":
        for match in _STAGE_HINTS["closed"].finditer(combined):
            norm = _normalize_phrase(match.group(0))
            if norm and norm not in phrases:
                phrases.insert(0, norm)

    feat.phrases = phrases[:_MAX_PHRASES]
    # Dedupe signals while preserving order.
    seen_sig: set[str] = set()
    clean_signals: list[str] = []
    for s in feat.signals:
        if s not in seen_sig:
            seen_sig.add(s)
            clean_signals.append(s)
    feat.signals = clean_signals
    return feat


async def load_recent_messages(
    db: AsyncSession,
    conversation_id: int,
    *,
    limit: int = _MSG_LIMIT,
) -> list[WhatsAppMessage]:
    rows = (
        await db.execute(
            select(WhatsAppMessage)
            .where(WhatsAppMessage.conversation_id == conversation_id)
            .order_by(WhatsAppMessage.created_at.desc(), WhatsAppMessage.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(reversed(list(rows)))


async def learn_from_move(
    db: AsyncSession,
    *,
    move: AiStageMove,
    features: MoveFeatures,
    from_stage: str = "",
) -> dict[str, Any]:
    """Upsert KB rows for this move. Positive for to_stage; negative for from_stage; exclude if closed."""
    to_stage = (move.to_stage or "").strip().lower()
    src = (from_stage or move.from_stage or "").strip().lower()
    phrases = list(features.phrases)
    if not phrases and features.snippet:
        phrases = _phrases_from_text(features.snippet)[:6]

    learned: list[dict[str, str]] = []
    for phrase in phrases:
        polarities: list[tuple[str, str]] = [(to_stage, "positive")]
        if src and src != to_stage and src != "closed":
            polarities.append((src, "negative"))
        if to_stage == "closed":
            polarities = [("closed", "exclude"), ("closed", "positive")]

        for stage, polarity in polarities:
            row = await _upsert_phrase(
                db,
                stage=stage,
                phrase=phrase,
                polarity=polarity,
                snippet=features.snippet,
                source_move_id=int(move.id) if move.id else None,
                source_jid=(move.customer_jid or "")[:80],
            )
            learned.append({"stage": stage, "polarity": polarity, "phrase": row.phrase, "weight": str(row.weight)})

    await db.flush()
    return {
        "phrases": phrases,
        "kb_rows": len(learned),
        "learned": learned[:20],
    }


async def _upsert_phrase(
    db: AsyncSession,
    *,
    stage: str,
    phrase: str,
    polarity: str,
    snippet: str,
    source_move_id: int | None,
    source_jid: str,
) -> AiStageKb:
    existing = (
        await db.execute(
            select(AiStageKb).where(
                AiStageKb.organization_id == ORG,
                AiStageKb.stage == stage,
                AiStageKb.phrase == phrase,
                AiStageKb.polarity == polarity,
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.weight = int(existing.weight or 0) + 1
        existing.hit_count = int(existing.hit_count or 0) + 1
        if snippet:
            existing.example_snippet = snippet[:_SNIPPET_MAX]
        if source_move_id:
            existing.source_move_id = source_move_id
        if source_jid:
            existing.source_jid = source_jid[:80]
        return existing

    row = AiStageKb(
        organization_id=ORG,
        stage=stage,
        phrase=phrase,
        polarity=polarity,
        weight=1,
        hit_count=1,
        example_snippet=(snippet or "")[:_SNIPPET_MAX],
        source_move_id=source_move_id,
        source_jid=source_jid[:80],
    )
    db.add(row)
    await db.flush()
    return row


@dataclass(frozen=True)
class StageKbPatterns:
    """Loaded KB phrases grouped for classification."""

    positive: dict[str, list[tuple[str, int]]]  # stage -> [(phrase, weight), ...]
    negative: dict[str, list[tuple[str, int]]]
    exclude: list[tuple[str, int]]  # closed/blast exclude phrases

    def stages_with_phrases(self) -> list[str]:
        return sorted(self.positive.keys())


_kb_cache: tuple[float, StageKbPatterns] | None = None
_KB_CACHE_TTL_S = 30.0


def invalidate_stage_kb_cache() -> None:
    global _kb_cache
    _kb_cache = None


async def load_stage_kb_patterns(
    db: AsyncSession,
    *,
    use_cache: bool = True,
) -> StageKbPatterns:
    global _kb_cache
    import time as _time

    now = _time.monotonic()
    if use_cache and _kb_cache is not None and now - _kb_cache[0] < _KB_CACHE_TTL_S:
        return _kb_cache[1]

    rows = (
        await db.execute(
            select(AiStageKb).where(
                AiStageKb.organization_id == ORG,
                AiStageKb.weight >= _MIN_WEIGHT_MATCH,
            )
        )
    ).scalars().all()

    positive: dict[str, list[tuple[str, int]]] = {}
    negative: dict[str, list[tuple[str, int]]] = {}
    exclude: list[tuple[str, int]] = []
    for row in rows:
        phrase = (row.phrase or "").strip()
        if not phrase:
            continue
        weight = int(row.weight or 1)
        pol = (row.polarity or "positive").strip().lower()
        stage = (row.stage or "").strip().lower()
        if pol == "exclude":
            if weight >= _MIN_WEIGHT_EXCLUDE:
                exclude.append((phrase, weight))
            continue
        if not stage:
            continue
        bucket = positive if pol != "negative" else negative
        bucket.setdefault(stage, []).append((phrase, weight))

    # Strongest phrases first.
    for bucket in (positive, negative):
        for stage, items in bucket.items():
            items.sort(key=lambda x: (-x[1], x[0]))
            bucket[stage] = items[:80]
    exclude.sort(key=lambda x: (-x[1], x[0]))
    patterns = StageKbPatterns(positive=positive, negative=negative, exclude=exclude[:80])
    _kb_cache = (now, patterns)
    return patterns


def score_blob_against_kb(blob: str, patterns: StageKbPatterns) -> dict[str, float]:
    """Return stage → score from positive matches minus negatives. Exclude is separate."""
    text = (blob or "").lower()
    if not text:
        return {}
    scores: dict[str, float] = {}
    for stage, items in patterns.positive.items():
        for phrase, weight in items:
            if phrase in text:
                scores[stage] = scores.get(stage, 0.0) + float(weight)
    for stage, items in patterns.negative.items():
        for phrase, weight in items:
            if phrase in text:
                scores[stage] = scores.get(stage, 0.0) - float(weight) * 0.75
    return scores


def best_kb_stage(blob: str, patterns: StageKbPatterns, *, min_score: float = 2.0) -> str | None:
    scores = score_blob_against_kb(blob, patterns)
    if not scores:
        return None
    stage, score = max(scores.items(), key=lambda kv: kv[1])
    if score < min_score:
        return None
    return stage


def text_matches_exclude(blob: str, patterns: StageKbPatterns) -> bool:
    text = (blob or "").lower()
    if not text:
        return False
    for phrase, _weight in patterns.exclude:
        if phrase in text:
            return True
    return False


def _escape_like(phrase: str) -> str:
    return phrase.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def kb_phrase_conv_ids_sql(
    phrases: list[str],
    *,
    min_weight_phrases: list[tuple[str, int]] | None = None,
) -> Select[Any] | None:
    """Conversation IDs whose recent message text contains any learned phrase."""
    from app.intel.wa_stages import dm_scope_clauses, _msg_text_expr

    items = min_weight_phrases
    if items is None:
        items = [(p, 1) for p in phrases if p]
    usable = [(p, w) for p, w in items if p and len(p) >= _PHRASE_MIN]
    if not usable:
        return None

    blob = _msg_text_expr()
    # Cap OR width so Postgres plans stay sane.
    clauses = [blob.ilike(f"%{_escape_like(p)}%") for p, _w in usable[:40]]
    if not clauses:
        return None
    return (
        select(WhatsAppMessage.conversation_id)
        .join(WhatsAppConversation, WhatsAppConversation.id == WhatsAppMessage.conversation_id)
        .where(*dm_scope_clauses(), or_(*clauses))
        .group_by(WhatsAppMessage.conversation_id)
    )


async def load_kb_stage_conv_ids(
    db: AsyncSession,
    patterns: StageKbPatterns,
    *,
    stages: list[str] | None = None,
) -> dict[str, set[int]]:
    """Map stage → conversation ids matching positive KB phrases (minus strong negatives)."""
    wanted = stages or list(patterns.positive.keys())
    out: dict[str, set[int]] = {s: set() for s in wanted}
    for stage in wanted:
        items = patterns.positive.get(stage) or []
        # Prefer phrases reinforced at least twice, else allow weight==1 for sparse learning.
        strong = [(p, w) for p, w in items if w >= 2] or items[:12]
        sql = kb_phrase_conv_ids_sql([], min_weight_phrases=strong)
        if sql is None:
            continue
        ids = set((await db.execute(sql)).scalars().all())
        # Drop chats that also match a stronger negative for this stage.
        neg = patterns.negative.get(stage) or []
        if neg and ids:
            neg_sql = kb_phrase_conv_ids_sql([], min_weight_phrases=[(p, w) for p, w in neg if w >= 2][:20])
            if neg_sql is not None:
                neg_ids = set((await db.execute(neg_sql)).scalars().all())
                ids -= neg_ids
        out[stage] = {int(i) for i in ids}
    return out


async def load_kb_exclude_conv_ids(
    db: AsyncSession,
    patterns: StageKbPatterns,
) -> set[int]:
    if not patterns.exclude:
        return set()
    sql = kb_phrase_conv_ids_sql([], min_weight_phrases=patterns.exclude[:40])
    if sql is None:
        return set()
    return {int(i) for i in (await db.execute(sql)).scalars().all()}
