"""Search and index router — M3 implementation.

Endpoints
---------
POST /api/projects/{project_id}/search
    Semantic search over indexed repos.
POST /api/projects/{project_id}/index
    Trigger (re-)indexing; async 202 response.
GET  /api/projects/{project_id}/index/status
    Per-repo indexing status.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field

from yukar.api.routers import get_repo_or_404
from yukar.deps import IndexerServiceDep, WorkspaceRootDep
from yukar.indexer.service import DimensionMismatchError, IndexState

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects/{project_id}", tags=["search"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    query: str
    repo: str | None = None
    top_k: int = Field(default=8, ge=1, le=100)


class SearchResultItem(BaseModel):
    repo: str
    path: str
    snippet: str
    score: float = Field(
        description="Normalized similarity score in the range [0, 1]. "
        "1.0 = exact match (zero L2 distance); values closer to 1 are better. "
        "Derived from raw L2 distance d as: score = 1 / (1 + d)."
    )
    start_line: int
    end_line: int
    language: str


class SearchResponse(BaseModel):
    results: list[SearchResultItem]
    # Repos that were requested but not indexed yet (so callers can surface a hint).
    unindexed_repos: list[str] = Field(default_factory=list)


class IndexTriggerResponse(BaseModel):
    accepted: bool
    repos: list[str]  # repos that will be / are being reindexed


class RepoIndexStatus(BaseModel):
    repo_name: str
    state: IndexState
    files: int
    chunks: int
    last_indexed_at: str | None
    ts_files: int = 0
    fallback_files: int = 0
    last_error: str | None = None
    last_error_at: str | None = None


class IndexStatusResponse(BaseModel):
    statuses: list[RepoIndexStatus]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/search", response_model=SearchResponse)
async def search(
    project_id: str,
    body: SearchRequest,
    root: WorkspaceRootDep,
    indexer: IndexerServiceDep,
) -> SearchResponse:
    """Semantic search over indexed repos in a project.

    Args:
        project_id: Project identifier.
        body: Search parameters.

    Returns:
        Ranked search results with snippets. Unindexed repos appear in
        ``unindexed_repos`` so the caller can surface a hint.
    """
    # Delegate index-existence check and unindexed list to the service
    # (Minor review fix #6: router is kept thin).
    repo_names, unindexed = await indexer.resolve_search_repos(project_id, body.repo)

    if not repo_names:
        return SearchResponse(results=[], unindexed_repos=unindexed)

    try:
        raw_results = await indexer.search(
            project_id,
            body.query,
            repo_name=body.repo,
            top_k=body.top_k,
        )
    except DimensionMismatchError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc

    # Normalize raw L2 distances to a [0, 1] similarity score where 1 = best.
    # score = 1 / (1 + distance)  — monotonically decreasing in distance.
    # raw_results are already sorted by ascending distance (best first).
    items: list[SearchResultItem] = []
    for chunk, distance in raw_results:
        normalized_score = 1.0 / (1.0 + float(distance))
        items.append(
            SearchResultItem(
                repo=chunk.get("repo") or "",
                path=chunk.get("path") or "",
                snippet=chunk.get("text") or "",
                score=normalized_score,
                start_line=chunk.get("start_line") or 0,
                end_line=chunk.get("end_line") or 0,
                language=chunk.get("language") or "",
            )
        )

    return SearchResponse(results=items, unindexed_repos=unindexed)


@router.post("/index", response_model=IndexTriggerResponse, status_code=202)
async def trigger_index(
    project_id: str,
    root: WorkspaceRootDep,
    indexer: IndexerServiceDep,
    background_tasks: BackgroundTasks,
    repo: str | None = Query(
        default=None,
        description="Repo name; omit to reindex all enabled repos",
    ),
) -> IndexTriggerResponse:
    """Trigger (re-)indexing for one or all repos — returns 202 immediately.

    The actual reindex runs as a background task. Concurrent calls for the same
    (project, repo) are automatically serialised by the per-repo FAISS lock
    inside ``IndexerService.reindex_repo``.

    Args:
        project_id: Project identifier.
        repo: Repository name. If omitted, all repos with ``index.enabled=true``
            are reindexed.

    Returns:
        202 with the list of repos that will be reindexed.
    """
    from yukar.storage.project_repo import list_repos

    if repo is not None:
        repo_obj = await get_repo_or_404(root, project_id, repo)
        repos_to_index = [repo_obj]
    else:
        all_repos = await list_repos(root, project_id)
        repos_to_index = [r for r in all_repos if r.index.enabled]

    repo_names = [r.name for r in repos_to_index]

    async def _reindex_all() -> None:
        for r in repos_to_index:
            try:
                n = await indexer.reindex_repo(project_id, r.name, Path(r.path))
                logger.info("Background reindex %s/%s: %d chunks", project_id, r.name, n)
            except Exception:
                logger.exception("Background reindex %s/%s failed", project_id, r.name)

    background_tasks.add_task(_reindex_all)

    return IndexTriggerResponse(accepted=True, repos=repo_names)


@router.get("/index/status", response_model=IndexStatusResponse)
async def get_index_status(
    project_id: str,
    root: WorkspaceRootDep,
    indexer: IndexerServiceDep,
) -> IndexStatusResponse:
    """Return per-repo indexing status for a project.

    Args:
        project_id: Project identifier.

    Returns:
        Status snapshot for each repo that has (or had) an index.
    """
    statuses = await indexer.get_status(project_id)
    return IndexStatusResponse(
        statuses=[
            RepoIndexStatus(
                repo_name=s.repo_name,
                state=s.state,
                files=s.files,
                chunks=s.chunks,
                last_indexed_at=s.last_indexed_at,
                ts_files=s.ts_files,
                fallback_files=s.fallback_files,
                last_error=s.last_error,
                last_error_at=s.last_error_at,
            )
            for s in statuses
        ]
    )
