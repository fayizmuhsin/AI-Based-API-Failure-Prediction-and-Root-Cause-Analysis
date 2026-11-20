"""Read-only API for persisted root-cause investigations."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.rca.repository import RcaStorageError
from app.rca.runtime import rca_repository

router = APIRouter(prefix="/rca", tags=["root-cause analysis"])


class RcaResponse(BaseModel):
    model_config = ConfigDict(extra="allow")


class InvestigationSummary(RcaResponse):
    investigation_id: str
    created_at: str
    episode_start: str
    episode_end: str
    status: str
    trigger_count: int
    candidate_count: int


class InvestigationListResponse(BaseModel):
    count: int
    items: list[InvestigationSummary]


class CandidateResponse(RcaResponse):
    service_id: str
    service_name: str
    category: str
    rank: int
    rank_score: float = Field(ge=0, le=100)
    score_semantics: str
    evidence_strength: str
    selected_as_probable_root_cause: bool
    supporting_signal_count: int
    supporting_signals: list[str]
    affected_services: list[str]
    affected_anomalous_services: list[str]
    evidence: list[dict[str, Any]]
    explanation: str


class InvestigationDetailResponse(RcaResponse):
    investigation_id: str
    created_at: str
    episode_start: str
    episode_end: str
    status: str
    conclusion: str
    triggering_anomaly_ids: list[int]
    triggering_anomalies: list[dict[str, Any]]
    root_causes: list[CandidateResponse]
    candidates: list[CandidateResponse]
    affected_services: list[str]
    evidence_summary: dict[str, Any]
    methodology: dict[str, Any]


class InvestigationSummaryStats(BaseModel):
    total_investigations: int
    completed: int
    inconclusive: int
    pending_anomalies: int
    retrying_anomalies: int
    root_causes_by_service: list[dict[str, Any]]


@router.get("/recent", response_model=InvestigationListResponse, summary="List recent RCA investigations")
def recent_investigations(limit: int = Query(default=50, ge=1, le=500)) -> dict:
    try:
        items = rca_repository.recent_investigations(limit)
        return {"count": len(items), "items": items}
    except RcaStorageError as exc:
        raise HTTPException(status_code=503, detail="Investigation storage is unavailable") from exc


@router.get("/summary", response_model=InvestigationSummaryStats, summary="Summarize RCA investigations")
def investigation_summary() -> dict:
    try:
        return rca_repository.investigation_summary()
    except RcaStorageError as exc:
        raise HTTPException(status_code=503, detail="Investigation storage is unavailable") from exc


@router.get("/{investigation_id}/candidates", response_model=list[CandidateResponse], summary="List ranked RCA candidates")
def investigation_candidates(investigation_id: UUID = Path()) -> list[dict]:
    try:
        candidates = rca_repository.candidates(str(investigation_id))
    except RcaStorageError as exc:
        raise HTTPException(status_code=503, detail="Investigation storage is unavailable") from exc
    if candidates is None:
        raise HTTPException(status_code=404, detail="Investigation was not found")
    return candidates


@router.get("/{investigation_id}", response_model=InvestigationDetailResponse, summary="Get an RCA investigation")
def investigation_detail(investigation_id: UUID = Path()) -> dict:
    try:
        result = rca_repository.investigation_detail(str(investigation_id))
    except RcaStorageError as exc:
        raise HTTPException(status_code=503, detail="Investigation storage is unavailable") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Investigation was not found")
    return result
