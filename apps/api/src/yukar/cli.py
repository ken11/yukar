"""yukar CLI — `yukar serve` and `yukar openapi`."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _find_standalone() -> Path | None:
    """Locate the Next.js standalone server directory by walking up to the monorepo root.

    The monorepo root is identified by the presence of ``pnpm-workspace.yaml``.

    Search order for ``server.js``:
    1. Monorepo-nested path: ``apps/web/.next/standalone/apps/web/server.js``
       (produced when ``apps/web`` is built inside a monorepo with output: "standalone").
    2. Flat path: ``apps/web/.next/standalone/server.js`` (legacy / simple layout).

    Returns the directory that *contains* ``server.js`` (i.e. the cwd for ``node
    server.js``), or ``None`` with a warning printed to stderr if the monorepo root
    cannot be found within 8 levels of the package directory.
    """
    marker = "pnpm-workspace.yaml"
    candidate = Path(__file__).parent
    for _ in range(8):
        if (candidate / marker).exists():
            # Monorepo-nested layout (pnpm workspaces + output: "standalone")
            nested = candidate / "apps" / "web" / ".next" / "standalone" / "apps" / "web"
            if (nested / "server.js").exists():
                return nested
            # Flat layout fallback
            flat = candidate / "apps" / "web" / ".next" / "standalone"
            if (flat / "server.js").exists():
                return flat
            # Root found but neither layout has server.js yet — return nested
            # path so the caller can emit the correct "run pnpm build" message.
            return nested
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    print(
        "Warning: could not locate monorepo root (pnpm-workspace.yaml not found) — "
        "starting API only.",
        file=sys.stderr,
    )
    return None


def _prepare_static_assets(monorepo_root: Path, server_dir: Path) -> None:
    """Copy static assets into the standalone server directory.

    Next.js standalone does not bundle static files.  We must copy them next to
    ``server.js`` before starting the process:

    - ``apps/web/.next/static``  →  ``<server_dir>/.next/static``
    - ``apps/web/public``        →  ``<server_dir>/public``   (only when present)
    """
    web_dir = monorepo_root / "apps" / "web"

    static_src = web_dir / ".next" / "static"
    static_dst = server_dir / ".next" / "static"
    if static_src.is_dir():
        if static_dst.exists():
            shutil.rmtree(static_dst)
        static_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(static_src), str(static_dst))

    public_src = web_dir / "public"
    public_dst = server_dir / "public"
    if public_src.is_dir():
        if public_dst.exists():
            shutil.rmtree(public_dst)
        shutil.copytree(str(public_src), str(public_dst))


def _find_monorepo_root() -> Path | None:
    """Return the monorepo root directory or None."""
    marker = "pnpm-workspace.yaml"
    candidate = Path(__file__).parent
    for _ in range(8):
        if (candidate / marker).exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return None


def _serve(
    host: str,
    port: int,
    reload: bool,
    web_host: str = "127.0.0.1",
    web_port: int = 3000,
) -> None:
    """Start the yukar server (uvicorn always runs with workers=1)."""
    import uvicorn

    # Optionally spawn Next.js server if standalone build exists.
    next_proc: subprocess.Popen[bytes] | None = None
    server_dir = _find_standalone()
    if server_dir is not None and (server_dir / "server.js").exists():
        monorepo_root = _find_monorepo_root()
        if monorepo_root is not None:
            _prepare_static_assets(monorepo_root, server_dir)
        # Frontend bind address (default 127.0.0.1:3000; override via
        # `yukar serve --web-host/--web-port`).
        print(
            f"Starting Next.js standalone server from {server_dir} "
            f"on http://{web_host}:{web_port} ..."
        )
        next_proc = subprocess.Popen(
            ["node", "server.js"],
            cwd=str(server_dir),
            env={**os.environ, "PORT": str(web_port), "HOSTNAME": web_host},
        )
    elif server_dir is not None:
        # Root found but standalone build absent
        print(
            "Warning: Next.js standalone server.js not found — starting API only. "
            "Run `pnpm build` in apps/web for the full experience.",
            file=sys.stderr,
        )

    try:
        uvicorn.run(
            "yukar.app:create_app",
            factory=True,
            host=host,
            port=port,
            workers=1,
            reload=reload,
            log_level="info",
        )
    finally:
        if next_proc is not None:
            next_proc.terminate()
            try:
                next_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                next_proc.kill()
                next_proc.wait()


def _openapi() -> None:
    """Print OpenAPI JSON to stdout (used by pnpm gen:types)."""
    import json as _json

    from yukar.app import create_app

    app = create_app()
    schema = app.openapi()
    print(_json.dumps(schema, indent=2))


def _run_serve(args: object) -> None:
    import argparse

    assert isinstance(args, argparse.Namespace)
    _serve(
        host=args.host,
        port=args.port,
        reload=args.reload,
        web_host=args.web_host,
        web_port=args.web_port,
    )


def _run_openapi(args: object) -> None:  # noqa: ARG001
    _openapi()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="yukar", description="yukar CLI")
    sub = parser.add_subparsers()

    serve_p = sub.add_parser("serve", help="Start the yukar server (always workers=1)")
    # Backend (FastAPI / uvicorn) bind address.
    serve_p.add_argument("--host", default="127.0.0.1", help="Backend (API) host")
    serve_p.add_argument("--port", type=int, default=8000, help="Backend (API) port")
    # Frontend (Next.js standalone) bind address.
    serve_p.add_argument("--web-host", default="127.0.0.1", help="Frontend host")
    serve_p.add_argument("--web-port", type=int, default=3000, help="Frontend port")
    serve_p.add_argument("--reload", action="store_true", default=False)
    serve_p.set_defaults(func=_run_serve)

    openapi_p = sub.add_parser("openapi", help="Print OpenAPI JSON to stdout")
    openapi_p.set_defaults(func=_run_openapi)

    args = parser.parse_args()
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        sys.exit(1)
    func(args)


if __name__ == "__main__":
    main()
