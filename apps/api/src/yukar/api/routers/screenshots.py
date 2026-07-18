"""Screenshots router — list / fetch / delete epic browser-verification captures.

Screenshots are the binary siblings of docs: an agent saves one under
``epic/docs/screenshots/`` by passing ``save=True`` to ``browser_screenshot``.
The Docs page lists them (metadata) and renders each thumbnail by fetching the
raw-bytes endpoint directly in an ``<img>`` — it is not JSON, so it is served
as an image response rather than through ``apiFetch``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from yukar.api.routers import get_epic_or_404, get_project_or_404
from yukar.deps import WorkspaceRootDep
from yukar.storage import screenshots_repo

router = APIRouter(tags=["screenshots"])


class ScreenshotMeta(BaseModel):
    filename: str
    size_bytes: int
    captured_at: str


@router.get(
    "/api/projects/{project_id}/epics/{epic_id}/screenshots",
    response_model=list[ScreenshotMeta],
)
async def list_epic_screenshots(
    project_id: str, epic_id: str, root: WorkspaceRootDep
) -> list[ScreenshotMeta]:
    return [
        ScreenshotMeta(
            filename=m.filename,
            size_bytes=m.size_bytes,
            captured_at=m.captured_at,
        )
        for m in screenshots_repo.list_epic_screenshots(root, project_id, epic_id)
    ]


@router.get(
    "/api/projects/{project_id}/epics/{epic_id}/screenshots/{filename}",
    responses={200: {"content": {"image/jpeg": {}}}},
    response_class=Response,
)
async def get_epic_screenshot(
    project_id: str, epic_id: str, filename: str, root: WorkspaceRootDep
) -> Response:
    try:
        data = screenshots_repo.read_epic_screenshot(root, project_id, epic_id, filename)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return Response(
        content=data,
        media_type=screenshots_repo.media_type_for(filename),
        # Immutable: a saved screenshot's filename embeds its capture time and
        # is never rewritten, so the browser may cache it aggressively.
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.delete(
    "/api/projects/{project_id}/epics/{epic_id}/screenshots/{filename}",
    status_code=204,
)
async def delete_epic_screenshot(
    project_id: str, epic_id: str, filename: str, root: WorkspaceRootDep
) -> Response:
    # Confirm the epic exists so a delete against a typo'd path is a clean 404
    # rather than a silent no-op that looks like success.
    await get_project_or_404(root, project_id)
    await get_epic_or_404(root, project_id, epic_id)
    try:
        deleted = screenshots_repo.delete_epic_screenshot(root, project_id, epic_id, filename)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Screenshot not found: {filename}")
    return Response(status_code=204)
