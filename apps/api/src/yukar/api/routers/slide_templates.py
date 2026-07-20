"""Slide templates router — list / thumbnails / delete project deck designs.

Templates are project-level bundles saved by the Manager's
``pptx_save_template`` under ``<project>/docs/slide-templates/<name>/``.
The project Docs page lists them with their thumbnails; deletion is a
user-side cleanup action (mirroring epic screenshots — without it, pruning
a stale template would require filesystem surgery).  Bundles are mutable
(overwrite re-saves in place), so responses are served uncacheable.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from yukar.deps import WorkspaceRootDep
from yukar.storage import slide_templates_repo

router = APIRouter(tags=["slide-templates"])


class SlideTemplateMeta(BaseModel):
    name: str
    description: str
    slide_count: int
    size: str
    created_at: str
    previews: list[str]
    has_notes: bool


def _validated(name: str) -> str:
    try:
        return slide_templates_repo.validate_template_name(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get(
    "/api/projects/{project_id}/slide-templates",
    response_model=list[SlideTemplateMeta],
)
async def list_slide_templates(
    project_id: str, root: WorkspaceRootDep
) -> list[SlideTemplateMeta]:
    return [
        SlideTemplateMeta(
            name=m.name,
            description=m.description,
            slide_count=m.slide_count,
            size=m.size,
            created_at=m.created_at,
            previews=m.previews,
            has_notes=m.has_notes,
        )
        for m in slide_templates_repo.list_templates(root, project_id)
    ]


@router.get(
    "/api/projects/{project_id}/slide-templates/{name}/previews/{filename}",
    responses={200: {"content": {"image/jpeg": {}}}},
    response_class=Response,
)
async def get_slide_template_preview(
    project_id: str, name: str, filename: str, root: WorkspaceRootDep
) -> Response:
    _validated(name)
    try:
        data = slide_templates_repo.read_template_preview(root, project_id, name, filename)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@router.delete(
    "/api/projects/{project_id}/slide-templates/{name}",
    status_code=204,
)
async def delete_slide_template(project_id: str, name: str, root: WorkspaceRootDep) -> None:
    _validated(name)
    deleted = await slide_templates_repo.delete_template(root, project_id, name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Template not found: {name}")
