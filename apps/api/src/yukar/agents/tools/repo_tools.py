"""Repo search / summarize tools for Manager and Worker agents (spec §6.3).

``make_repo_tools(...)`` returns Strands tools that give agents access to the
FAISS-based code search and cached repo summary.

Design constraints (spec §6.3):
- ``repo_search`` and ``repo_summarize`` never trigger a new index build inside
  the tool.  If the index is missing, the tool returns a human-readable message
  so the agent can handle the situation gracefully.
- Worker tools are closed over ``repo_name`` (from ``AgentContext``) so a
  Worker can only search its assigned repo — the closure makes cross-repo access
  structurally impossible without bypassing Python.
- Manager tools operate across all repos in the project.

``IndexerService`` is accepted as a plain ``Any`` to avoid a circular import at
module load time (``service.py`` imports from ``config.paths``, not from
``agents``).
"""

from __future__ import annotations

from typing import Any


def make_repo_tools(
    project_id: str,
    indexer_service: Any,  # IndexerService — typed as Any to avoid circular import
    *,
    repo_name: str | None = None,
) -> list[Any]:
    """Return [repo_search, repo_summarize] Strands tools.

    Args:
        project_id: The project these tools are scoped to.
        indexer_service: The shared ``IndexerService`` instance.
        repo_name: If provided, tools are scoped to this single repo (Worker
            mode). If ``None``, tools search all indexed repos (Manager mode).

    Returns:
        A list of two Strands ``AgentTool`` objects.
    """
    from strands import tool

    # Capture in closure — repo_name=None means Manager (all repos),
    # repo_name=<str> means Worker (single repo, structurally enforced).

    @tool
    async def repo_search(query: str, top_k: int = 8) -> dict[str, Any]:
        """Search the indexed repository codebase for relevant code snippets.

        Uses FAISS vector search. Returns the top-k most semantically similar
        chunks to *query*.

        Args:
            query: Natural-language or code description of what to find.
            top_k: Maximum number of results (default 8).

        Returns:
            A dict with a ``results`` list. Each result has ``repo``, ``path``,
            ``snippet``, ``score``, ``start_line`` (1-indexed, inclusive),
            ``end_line`` (1-indexed, inclusive), ``language``.
            Line numbers are 1-indexed so they can be used directly with
            standard editor line references.
            Returns an empty list if the repo is not indexed yet.
        """
        try:
            raw = await indexer_service.search(
                project_id,
                query,
                repo_name=repo_name,
                top_k=top_k,
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "results": []}

        results = [
            {
                "repo": chunk.get("repo", ""),
                "path": chunk.get("path", ""),
                "snippet": chunk.get("text", ""),
                "score": float(score),
                # Convert from 0-indexed (internal) to 1-indexed (agent-facing).
                "start_line": chunk.get("start_line", 0) + 1,
                "end_line": chunk.get("end_line", 0) + 1,
                "language": chunk.get("language", ""),
            }
            for chunk, score in raw
        ]
        return {"results": results}

    @tool
    async def repo_summarize() -> dict[str, Any]:
        """Return the cached Markdown summary of the repository structure.

        The summary includes the file tree, language breakdown, and top-level
        symbols. It is generated during indexing and does NOT trigger a new
        index build here.

        Returns:
            A dict with ``summary`` (Markdown text) or ``message`` if the repo
            has not been indexed yet.
        """
        from yukar.config import paths as config_paths
        from yukar.indexer import faiss_store

        if repo_name is not None:
            # Worker mode: single repo.
            idx_dir = config_paths.index_dir(indexer_service.workspace_root, project_id, repo_name)
            if not faiss_store.index_exists(idx_dir):
                from yukar.indexer.stats import read_error

                err = read_error(idx_dir)
                if err is not None:
                    return {
                        "message": (
                            f"Repository '{repo_name}' has not been indexed yet — "
                            f"the last build attempt failed: {err.get('message', 'unknown error')} "
                            f"(error_type={err.get('error_type', '?')}, "
                            f"failed_at={err.get('failed_at', '?')}). "
                            "Fix the underlying issue (e.g. AWS credentials for Bedrock) "
                            "and trigger a re-index."
                        ),
                        "summary": None,
                    }
                return {
                    "message": f"Repository '{repo_name}' has not been indexed yet. "
                    "Run a sync to build the index.",
                    "summary": None,
                }
            summary_path = idx_dir / "summary.md"
            if not summary_path.exists():
                return {"message": "Summary not available.", "summary": None}
            try:
                text = summary_path.read_text(encoding="utf-8", errors="replace")
                return {"repo": repo_name, "summary": text}
            except OSError as exc:
                return {"error": str(exc), "summary": None}
        else:
            # Manager mode: collect summaries for all indexed repos.
            from yukar.storage.project_repo import list_repos

            repos = await list_repos(indexer_service.workspace_root, project_id)
            summaries: list[dict[str, Any]] = []
            for r in repos:
                if not r.index.enabled:
                    continue
                idx_dir = config_paths.index_dir(indexer_service.workspace_root, project_id, r.name)
                if not faiss_store.index_exists(idx_dir):
                    from yukar.indexer.stats import read_error

                    err = read_error(idx_dir)
                    if err is not None:
                        summaries.append(
                            {
                                "repo": r.name,
                                "message": (
                                    f"not indexed — last build failed: "
                                    f"{err.get('message', 'unknown error')}"
                                ),
                            }
                        )
                    else:
                        summaries.append({"repo": r.name, "message": "not indexed yet"})
                    continue
                summary_path = idx_dir / "summary.md"
                if not summary_path.exists():
                    summaries.append({"repo": r.name, "message": "summary not available"})
                    continue
                try:
                    text = summary_path.read_text(encoding="utf-8", errors="replace")
                    summaries.append({"repo": r.name, "summary": text})
                except OSError as exc:
                    summaries.append({"repo": r.name, "error": str(exc)})

            return {"summaries": summaries}

    return [repo_search, repo_summarize]
