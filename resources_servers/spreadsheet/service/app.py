"""Standalone Linux HTTP service. Does not import NeMo Gym or verl."""

import argparse
import asyncio
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from resources_servers.spreadsheet.schemas import TOOL_MODELS

from .catalog import Catalog
from .chroot_runner import ChrootPython
from .engines import BubblewrapPython, LibreOfficePool
from .errors import ConflictError, EnvironmentError, ToolError
from .state import Store


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = "0.0.0.0"
    port: int = Field(default=8088, ge=1, le=65535)
    manifest: Path
    data_root: Path
    state_root: Path
    api_key_file: Path
    uno_python: str = "/usr/bin/python3"
    soffice: str = "/usr/bin/soffice"
    code_python: str = "/usr/bin/python3"
    runtime_paths: list[str] = Field(default_factory=list)
    libreoffice_workers: int = Field(default=2, ge=1, le=16)
    job_workers: int = Field(default=8, ge=1, le=64)
    max_pending: int = Field(default=4000, ge=1, le=10000)
    recalc_timeout: int = Field(default=120, ge=1, le=600)
    python_timeout: int = Field(default=60, ge=1, le=300)
    python_memory_mb: int = Field(default=1024, ge=128, le=16384)
    session_ttl_seconds: int = Field(default=86400, ge=60)
    allow_unqualified_tasks: bool = False
    python_isolation: str = "chroot_uid_seccomp"
    python_rootfs: Path | None = None


class SeedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rollout_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=512)


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(min_length=1, max_length=256)
    action: str = Field(min_length=1, max_length=64)
    arguments: dict = Field(default_factory=dict)


class ExecutionRequest(BaseModel):
    execution_id: str = Field(min_length=1, max_length=128)


class BodyLimit:
    def __init__(self, app, maximum=160 * 1024):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        messages, size = [], 0
        while True:
            message = await receive()
            size += len(message.get("body", b""))
            if size > self.maximum:
                response = JSONResponse({"detail": "Request body too large"}, status_code=413)
                return await response(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break

        async def replay():
            return messages.pop(0) if messages else await receive()

        return await self.app(scope, replay, send)


def create_app(config: ServiceConfig, *, store=None):
    key = config.api_key_file.read_text().strip()
    if len(key) < 32:
        raise ValueError("Service key must have at least 32 characters")
    # Read-only runtime mounts must not expose data, service credentials, or state.
    mounts = [Path(p).resolve() for p in ("/usr", "/bin", "/lib", "/lib64", *config.runtime_paths)]
    if config.python_rootfs:
        mounts.append(config.python_rootfs.resolve())
    for private in (config.data_root, config.state_root, config.api_key_file, config.manifest):
        if any(private.resolve().is_relative_to(mount) for mount in mounts):
            raise ValueError("Private service files cannot be placed under Python runtime mounts")
    if store is None:
        catalog = Catalog(config.manifest, config.data_root, config.allow_unqualified_tasks)
        engine = LibreOfficePool(
            config.state_root / "office",
            config.uno_python,
            config.soffice,
            config.libreoffice_workers,
            config.recalc_timeout,
        )
        if config.python_isolation == "chroot_uid_seccomp":
            if config.python_rootfs is None:
                raise ValueError("python_rootfs is required for chroot isolation")
            runner = ChrootPython(
                config.python_rootfs,
                workers=config.job_workers,
                timeout=config.python_timeout,
                memory_mb=config.python_memory_mb,
            )
        elif config.python_isolation == "bubblewrap":
            runner = BubblewrapPython(
                config.code_python,
                timeout=config.python_timeout,
                memory_mb=config.python_memory_mb,
                runtime_paths=config.runtime_paths,
            )
        else:
            raise ValueError("Unknown Python isolation backend; unsafe execution is not supported")
        store = Store(config.state_root / "sessions", catalog, engine, runner, config.job_workers, config.max_pending)

    async def sweep():
        while True:
            await asyncio.sleep(min(60, config.session_ttl_seconds))
            await asyncio.to_thread(store.cleanup, config.session_ttl_seconds)

    @asynccontextmanager
    async def lifespan(app):
        # Exactly one API process per state root. A second process fails startup.
        import fcntl

        config.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        owner = (config.state_root / ".service.lock").open("a")
        cleanup_task = None
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            await asyncio.to_thread(store.engine.probe)
            await asyncio.to_thread(store.python_runner.probe)
            await asyncio.to_thread(store.recover)
            cleanup_task = asyncio.create_task(sweep())
            yield
        finally:
            if cleanup_task:
                cleanup_task.cancel()
                try:
                    await cleanup_task
                except asyncio.CancelledError:
                    pass
            await asyncio.to_thread(store.shutdown)
            owner.close()

    app = FastAPI(title="Spreadsheet tools and LibreOffice reward", lifespan=lifespan)
    app.add_middleware(BodyLimit)
    app.state.store = store

    async def authenticate(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization, "Bearer " + key):
            raise HTTPException(401, "Invalid service credential")

    @app.exception_handler(ConflictError)
    async def conflict(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(EnvironmentError)
    async def environment_error(request, error):
        return JSONResponse({"detail": str(error), "error_type": "environment_error"}, status_code=503)

    @app.exception_handler(ToolError)
    async def tool_error(request, error):
        return JSONResponse({"detail": str(error), "error_type": "tool_error"}, status_code=400)

    @app.get("/health")
    async def health():
        return {
            "status": "ready",
            "engine": store.engine.version,
            "python_isolation": config.python_isolation,
            "max_pending": config.max_pending,
        }

    @app.post("/sessions", dependencies=[Depends(authenticate)])
    async def seed(body: SeedRequest):
        try:
            return await asyncio.to_thread(store.seed, body.rollout_id, body.task_id)
        except ValueError as exc:
            if isinstance(exc, ConflictError):
                raise
            raise HTTPException(400, str(exc)) from exc

    @app.post("/sessions/{session_id}/operations", dependencies=[Depends(authenticate)])
    async def submit(session_id: str, body: OperationRequest):
        return await asyncio.to_thread(store.submit, session_id, body.operation_id, body.action, body.arguments)

    @app.get("/jobs/{job_id}", dependencies=[Depends(authenticate)])
    async def result(job_id: str):
        return await asyncio.to_thread(store.job, job_id)

    @app.post("/sessions/{session_id}/close", dependencies=[Depends(authenticate)])
    async def close(session_id: str):
        return await asyncio.to_thread(store.close_session, session_id)

    @app.post("/sessions/{session_id}/execution", dependencies=[Depends(authenticate)])
    async def execution(session_id: str, body: ExecutionRequest):
        return await asyncio.to_thread(store.claim_execution, session_id, body.execution_id)

    @app.get("/tools", dependencies=[Depends(authenticate)])
    async def tools():
        return {"tools": list(TOOL_MODELS)}

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = ServiceConfig.model_validate(json.loads(args.config.read_text()))
    uvicorn.run(create_app(config), host=config.host, port=config.port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
