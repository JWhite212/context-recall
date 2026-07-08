"""
Cross-meeting speaker identification.

Given a diarised transcript, the merged meeting audio, and the enrolled
voice profiles, this module:

1. collects the segments whose speaker label is still *unresolved*
   (the energy diariser's remote label, or pyannote's ``SPEAKER_NN``),
2. embeds each long-enough segment window with the ECAPA model,
3. clusters those embeddings so multiple remote participants sharing
   one "Remote" label separate into voice-consistent groups, and
4. matches each cluster against the enrolled profiles, renaming the
   matched segments to the person's name.

The clustering/matching maths lives in pure functions over numpy
arrays so it is fully testable without speechbrain installed — the
embedder is injected.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

logger = logging.getLogger("contextrecall.voice")

_PYANNOTE_LABEL_RE = re.compile(r"^SPEAKER_\d+$")


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom < 1e-10:
        return 0.0
    return float(np.dot(a, b) / denom)


def is_unresolved_label(label: str, remote_label: str) -> bool:
    """True for labels that name a *position*, not a person."""
    return label == remote_label or bool(_PYANNOTE_LABEL_RE.match(label))


def cluster_embeddings(embeddings: list[np.ndarray], threshold: float) -> list[list[int]]:
    """Greedy centroid clustering by cosine similarity.

    Deterministic and dependency-free: each embedding joins the first
    existing cluster whose centroid it matches at ≥ *threshold*, else
    starts a new cluster. Adequate for the handful of speakers a
    meeting produces; not a general-purpose clusterer.
    """
    clusters: list[list[int]] = []
    centroids: list[np.ndarray] = []
    sums: list[np.ndarray] = []
    for i, emb in enumerate(embeddings):
        best_idx = -1
        best_sim = threshold
        for ci, centroid in enumerate(centroids):
            sim = _cosine(emb, centroid)
            if sim >= best_sim:
                best_idx, best_sim = ci, sim
        if best_idx == -1:
            clusters.append([i])
            sums.append(emb.astype(np.float64).copy())
            centroids.append(emb.copy())
        else:
            clusters[best_idx].append(i)
            sums[best_idx] += emb
            centroid = sums[best_idx] / len(clusters[best_idx])
            norm = np.linalg.norm(centroid)
            centroids[best_idx] = centroid / norm if norm > 1e-10 else centroid
    return clusters


def match_profile(
    centroid: np.ndarray, profiles: list[dict], threshold: float
) -> tuple[dict | None, float]:
    """Best enrolled person for *centroid*: max cosine over their samples."""
    best_person: dict | None = None
    best_sim = threshold
    by_person: dict[str, dict] = {}
    for profile in profiles:
        emb = np.asarray(profile["embedding"], dtype=np.float32)
        sim = _cosine(centroid, emb)
        person_id = profile["person_id"]
        entry = by_person.setdefault(
            person_id, {"person_id": person_id, "name": profile["name"], "sim": -1.0}
        )
        entry["sim"] = max(entry["sim"], sim)
    for entry in by_person.values():
        if entry["sim"] >= best_sim:
            best_person, best_sim = entry, entry["sim"]
    return best_person, (best_person["sim"] if best_person else 0.0)


@dataclass
class VoiceMatch:
    """One identification decision over a cluster of segments."""

    original_label: str
    new_label: str
    person_id: str | None
    confidence: float
    segment_indices: list[int] = field(default_factory=list)


class VoiceRecogniser:
    """Applies voice identification to a transcript in place."""

    def __init__(self, embedder, config) -> None:
        self._embedder = embedder
        self._config = config

    def identify(self, transcript, audio_path: Path, profiles: list[dict]) -> list[VoiceMatch]:
        """Rename voice-matched segments; returns the decisions made.

        Mutates *transcript* the same way the diariser does. Segments in
        a cluster that matches no enrolled profile keep their label
        (optionally split into "Remote 2", … when configured).
        """
        remote_label = getattr(self._config, "remote_label", None) or "Remote"
        min_seconds = self._config.min_segment_seconds

        # Segment indices per unresolved label, long enough to embed.
        by_label: dict[str, list[int]] = {}
        for i, seg in enumerate(transcript.segments):
            if not seg.speaker or not is_unresolved_label(seg.speaker, remote_label):
                continue
            if (seg.end - seg.start) >= min_seconds:
                by_label.setdefault(seg.speaker, []).append(i)
        if not by_label:
            return []

        all_indices = [i for indices in by_label.values() for i in indices]
        windows = [(transcript.segments[i].start, transcript.segments[i].end) for i in all_indices]
        embedded = self._embedder.embed_windows(audio_path, windows)
        emb_by_index = {idx: emb for idx, emb in zip(all_indices, embedded) if emb is not None}

        matches: list[VoiceMatch] = []
        for label, indices in by_label.items():
            usable = [i for i in indices if i in emb_by_index]
            if not usable:
                continue
            embeddings = [emb_by_index[i] for i in usable]
            clusters = cluster_embeddings(embeddings, self._config.cluster_threshold)
            # Largest cluster first: it keeps the original label if unmatched.
            clusters.sort(key=len, reverse=True)
            unmatched_seen = 0
            for cluster in clusters:
                cluster_indices = [usable[j] for j in cluster]
                centroid = np.mean([emb_by_index[i] for i in cluster_indices], axis=0)
                person, sim = match_profile(centroid, profiles, self._config.match_threshold)
                if person is not None:
                    match = VoiceMatch(
                        original_label=label,
                        new_label=person["name"],
                        person_id=person["person_id"],
                        confidence=round(sim, 4),
                        segment_indices=cluster_indices,
                    )
                else:
                    # First unmatched cluster keeps the original label;
                    # later ones become "Remote 2", "Remote 3", ... when
                    # splitting is enabled, else stay untouched.
                    unmatched_seen += 1
                    if not self._config.split_unmatched_speakers or unmatched_seen == 1:
                        continue
                    match = VoiceMatch(
                        original_label=label,
                        new_label=f"{label} {unmatched_seen}",
                        person_id=None,
                        confidence=0.0,
                        segment_indices=cluster_indices,
                    )
                for i in match.segment_indices:
                    transcript.segments[i].speaker = match.new_label
                matches.append(match)

        for match in matches:
            logger.info(
                "Voice ID: '%s' → '%s' (%d segments, confidence %.2f)",
                match.original_label,
                match.new_label,
                len(match.segment_indices),
                match.confidence,
            )
        return matches
