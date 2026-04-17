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


class SearchResult(BaseModel):
    meeting_id: str
    segment_index: int
    text: str
    speaker: str
    start_time: float
    score: float
    meeting_title: str | None = None


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

    # Get all embeddings from DB.
    all_embeddings = await _repo.get_all_embeddings()
    if not all_embeddings:
        return SearchResponse(results=[], query=body.query)

    # Build (id, vector) pairs for search.
    id_vec_pairs = [(emb["id"], emb["embedding"]) for emb in all_embeddings]

    # Search.
    ranked = _embedder.search(body.query, id_vec_pairs, limit=body.limit)

    # Build result with metadata.
    emb_by_id = {emb["id"]: emb for emb in all_embeddings}

    # Batch-fetch meetings to avoid N+1 queries.
    ranked_embs = [emb_by_id[emb_id] for emb_id, _score in ranked]
    meeting_ids = list({emb["meeting_id"] for emb in ranked_embs})
    meetings_map: dict = {}
    for mid in meeting_ids:
        m = await _repo.get_meeting(mid)
        if m:
            meetings_map[mid] = m

    results = []
    for emb_id, score in ranked:
        emb = emb_by_id[emb_id]
        meeting = meetings_map.get(emb["meeting_id"])
        results.append(
            SearchResult(
                meeting_id=emb["meeting_id"],
                segment_index=emb["segment_index"],
                text=emb["text"],
                speaker=emb["speaker"],
                start_time=emb["start_time"],
                score=round(score, 4),
                meeting_title=meeting.title if meeting else None,
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
