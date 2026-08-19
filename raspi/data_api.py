"""Authenticated, read-only access to archived CSV reading files."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse


def _eligible_csv(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".csv") or name.endswith(".csv.gz")


def _inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _modified_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_data_api_router(readings_root: Path, app_config: Dict[str, Any]) -> APIRouter:
    """Create the limited data router using the server's loaded configuration."""
    config = app_config.get("data_api", {})
    if not isinstance(config, dict):
        config = {}
    token = str(config.get("token") or "")
    root = readings_root.resolve()
    router = APIRouter(prefix="/api/data", tags=["data-files"])

    def require_token(authorization: Optional[str]) -> None:
        if not token:
            raise HTTPException(status_code=503, detail="data API is not configured")
        scheme, separator, supplied = (authorization or "").partition(" ")
        authenticated = (
            separator == " "
            and scheme.lower() == "bearer"
            and bool(supplied)
            and secrets.compare_digest(supplied.encode("utf-8"), token.encode("utf-8"))
        )
        if not authenticated:
            raise HTTPException(
                status_code=401,
                detail="valid bearer token required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def resolve_file(requested_path: str) -> Path:
        if not requested_path or Path(requested_path).is_absolute():
            raise HTTPException(status_code=400, detail="invalid data file path")
        try:
            candidate = (root / requested_path).resolve(strict=True)
        except (OSError, RuntimeError):
            raise HTTPException(status_code=404, detail="data file not found") from None
        if not _inside(candidate, root):
            raise HTTPException(status_code=403, detail="data file path is outside the readings directory")
        if not candidate.is_file():
            raise HTTPException(status_code=400, detail="requested path is not a file")
        if not _eligible_csv(candidate):
            raise HTTPException(status_code=400, detail="only CSV data files are available")
        return candidate

    @router.get("/files")
    def list_data_files(authorization: Optional[str] = Header(default=None)):
        require_token(authorization)
        files = []
        if not root.is_dir():
            return JSONResponse({"files": files}, headers={"Cache-Control": "no-store"})
        for path in root.rglob("*"):
            if not _eligible_csv(path):
                continue
            try:
                resolved = path.resolve(strict=True)
                if not _inside(resolved, root) or not resolved.is_file() or not _eligible_csv(resolved):
                    continue
                stat = resolved.stat()
            except (OSError, RuntimeError):
                continue
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size_bytes": stat.st_size,
                    "modified_at": _modified_timestamp(stat.st_mtime),
                }
            )
        files.sort(key=lambda item: item["path"])
        return JSONResponse({"files": files}, headers={"Cache-Control": "no-store"})

    @router.get("/files/{file_path:path}")
    def download_data_file(file_path: str, authorization: Optional[str] = Header(default=None)):
        require_token(authorization)
        path = resolve_file(file_path)
        media_type = "application/gzip" if path.name.lower().endswith(".csv.gz") else "text/csv"
        return FileResponse(
            path,
            media_type=media_type,
            filename=path.name,
            headers={"Cache-Control": "no-store"},
        )

    return router
