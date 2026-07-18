"""Decks router — list / download Manager-rendered .pptx files + previews.

Decks are the binary siblings of docs, like screenshots: the Manager's
``pptx_render`` writes ``<name>.pptx`` (plus a ``<name>.previews/`` slide
gallery) under ``epic/docs/``.  The Docs page lists them, shows the slide
previews inline, and offers the .pptx as a download.

Deck paths may contain subdirectories, so they travel as a ``path`` query
parameter (validated against the docs directory in decks_repo) rather than
a path segment.  Deck files are mutable — re-rendering replaces them in
place — so responses are served uncacheable.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from yukar.deps import WorkspaceRootDep
from yukar.storage import decks_repo

router = APIRouter(tags=["decks"])


class DeckMeta(BaseModel):
    path: str
    size_bytes: int
    updated_at: str
    previews: list[str]


@router.get(
    "/api/projects/{project_id}/epics/{epic_id}/decks",
    response_model=list[DeckMeta],
)
async def list_epic_decks(
    project_id: str, epic_id: str, root: WorkspaceRootDep
) -> list[DeckMeta]:
    return [
        DeckMeta(
            path=m.path,
            size_bytes=m.size_bytes,
            updated_at=m.updated_at,
            previews=m.previews,
        )
        for m in decks_repo.list_epic_decks(root, project_id, epic_id)
    ]


@router.get(
    "/api/projects/{project_id}/epics/{epic_id}/decks/content",
    responses={200: {"content": {decks_repo.PPTX_MEDIA_TYPE: {}}}},
    response_class=Response,
)
async def get_epic_deck(
    project_id: str, epic_id: str, path: str, root: WorkspaceRootDep
) -> Response:
    try:
        data = decks_repo.read_epic_deck(root, project_id, epic_id, path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    filename = path.rsplit("/", 1)[-1]
    return Response(
        content=data,
        media_type=decks_repo.PPTX_MEDIA_TYPE,
        headers={
            # RFC 5987 filename* carries non-ASCII deck names safely.
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "no-store",
        },
    )


@router.get(
    "/api/projects/{project_id}/epics/{epic_id}/decks/preview",
    responses={200: {"content": {"image/jpeg": {}}}},
    response_class=Response,
)
async def get_epic_deck_preview(
    project_id: str, epic_id: str, path: str, name: str, root: WorkspaceRootDep
) -> Response:
    try:
        data = decks_repo.read_deck_preview(root, project_id, epic_id, path, name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )
