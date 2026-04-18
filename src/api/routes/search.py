"""
Semantic search endpoint.

POST /api/search         — search across all meeting transcripts
POST /api/search/reindex — re-index all existing meetings
"""

import json
import logging
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.utils.temporal import parse_temporal

logger = logging.getLogger("meetingmind.api.search")

router = APIRouter()

_repo = None
_embedder = None
_last_reindex: float = 0.0


def init(repo, embedder) -> None:
    global _repo, _embedder
    _repo = repo
    _embedder = embedder


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(ge=1, le=100, default=10)
    mode: str = Field(default="hybrid", pattern="^(hybrid|semantic|keyword)$")
    date_from: float | None = None
    date_to: float | None = None


class SearchResult(BaseModel):
    meeting_id: str
    segment_index: int
    text: str
    speaker: str
    start_time: float
    score: float
    meeting_title: str | None = None
    meeting_started_at: float | None = None


class SearchResponse(BaseModel):
    results: list[SearchResult]
    query: str


class ReindexResponse(BaseModel):
    status: str
    meetings_indexed: int
    segments_indexed: int


@router.post("/api/search", response_model=SearchResponse)
async def search_transcripts(body: SearchRequest):
    if not _repo or not _embedder:
        raise HTTPException(status_code=503, detail="Search not available")

    if not body.query.strip():
        return SearchResponse(results=[], query=body.query)

    # Parse temporal references from query.
    cleaned_query, parsed_from, parsed_to = parse_temporal(body.query)
    date_from = body.date_from if body.date_from is not None else parsed_from
    date_to = body.date_to if body.date_to is not None else parsed_to

    if body.mode == "keyword":
        # FTS5 only.
        meetings = await _repo.search_meetings(query=cleaned_query, limit=body.limit)
        results = [
            SearchResult(
                meeting_id=m.id,
                segment_index=0,
                text=m.title,
                speaker="",
                start_time=0.0,
                score=1.0,
                meeting_title=m.title,
                meeting_started_at=m.started_at,
            )
            for m in meetings
        ]
        return SearchResponse(results=results, query=body.query)

    # Embed the query for semantic/hybrid modes.
    query_embedding = _embedder.embed_single(cleaned_query)

    if body.mode == "semantic":
        raw_results = await _repo.search_embeddings(
            query_embedding,
            limit=body.limit,
            date_from=date_from,
            date_to=date_to,
        )
    else:  # hybrid
        raw_results = await _repo.search_hybrid(
            cleaned_query,
            query_embedding,
            limit=body.limit,
            date_from=date_from,
            date_to=date_to,
        )

    # Batch-fetch meeting titles.
    meeting_ids = list({r["meeting_id"] for r in raw_results})
    meetings_map: dict = {}
    for mid in meeting_ids:
        m = await _repo.get_meeting(mid)
        if m:
            meetings_map[mid] = m

    results = []
    for r in raw_results:
        meeting = meetings_map.get(r["meeting_id"])
        score = r.get("score", 1.0 - r.get("distance", 0.0))
        results.append(
            SearchResult(
                meeting_id=r["meeting_id"],
                segment_index=r.get("segment_index", 0),
                text=r["text"],
                speaker=r.get("speaker", ""),
                start_time=r.get("start_time", 0.0),
                score=round(score, 4),
                meeting_title=meeting.title if meeting else None,
                meeting_started_at=meeting.started_at if meeting else None,
            )
        )

    return SearchResponse(results=results, query=body.query)


@router.post("/api/search/reindex", response_model=ReindexResponse)
async def reindex_all():
    """Re-index all existing meetings for semantic search."""
    global _last_reindex

    if not _repo or not _embedder:
        raise HTTPException(status_code=503, detail="Search not available")

    if time.time() - _last_reindex < 300:
        raise HTTPException(
            status_code=429,
            detail="Reindex already ran recently. Try again in a few minutes.",
        )
    _last_reindex = time.time()

    meetings = await _repo.list_meetings(limit=10000)
    total_meetings = 0
    total_segments = 0

    for meeting in meetings:
        if not meeting.transcript_json:
            continue

        try:
            data = json.loads(meeting.transcript_json)
            segments = data.get("segments", [])
            if not segments:
                continue

            texts = [s.get("text", "") for s in segments]
            texts = [t for t in texts if t.strip()]
            if not texts:
                continue

            vectors = _embedder.embed(texts)

            emb_records = []
            for i, (seg, vec) in enumerate(zip(segments, vectors)):
                emb_records.append(
                    {
                        "segment_index": i,
                        "embedding": vec,
                        "text": seg.get("text", ""),
                        "speaker": seg.get("speaker", ""),
                        "start_time": seg.get("start", 0.0),
                    }
                )

            await _repo.store_embeddings(meeting.id, emb_records)
            total_meetings += 1
            total_segments += len(emb_records)
        except Exception as e:
            logger.warning("Failed to index meeting %s: %s", meeting.id, e)

    logger.info("Reindex complete: %d meetings, %d segments", total_meetings, total_segments)
    return ReindexResponse(
        status="complete",
        meetings_indexed=total_meetings,
        segments_indexed=total_segments,
    )
