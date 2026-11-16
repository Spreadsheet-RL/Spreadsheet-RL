"""Lightweight NeMo Gym adapter to a separately deployed spreadsheet service."""

import asyncio

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import ConfigDict, Field, SecretStr
from starlette.middleware.sessions import SessionMiddleware

from nemo_gym.base_resources_server import (
    BaseResourcesServerConfig,
    BaseSeedSessionRequest,
    BaseSeedSessionResponse,
    BaseVerifyRequest,
    BaseVerifyResponse,
    SimpleResourcesServer,
)
from nemo_gym.server_utils import get_response_json
from nemo_gym.server_utils import request as gym_request
from resources_servers.spreadsheet.schemas import TOOL_MODELS


class SpreadsheetConfig(BaseResourcesServerConfig):
    service_url: str
    api_key: SecretStr = Field(min_length=32)
    session_secret_key: SecretStr = Field(min_length=32)
    operation_timeout: float = Field(default=300, gt=0, le=1800)
    poll_interval: float = Field(default=0.2, gt=0, le=10)


class SpreadsheetSeedRequest(BaseSeedSessionRequest):
    task_id: str
    rollout_id: str


class SpreadsheetVerifyRequest(BaseVerifyRequest):
    model_config = ConfigDict(extra="allow")
    task_id: str


class SpreadsheetVerifyResponse(BaseVerifyResponse):
    model_config = ConfigDict(extra="allow")
    cell_accuracy: float
    engine: str
    workbook_sha256: str


class SpreadsheetResourcesServer(SimpleResourcesServer):
    config: SpreadsheetConfig

    def setup_session_middleware(self, app: FastAPI):
        app.add_middleware(
            SessionMiddleware,
            secret_key=self.config.session_secret_key.get_secret_value(),
            session_cookie="spreadsheet_" + self.config.name,
        )

    async def _call(self, method, path, payload=None):
        response = await gym_request(
            method,
            self.config.service_url.rstrip("/") + path,
            headers={"Authorization": "Bearer " + self.config.api_key.get_secret_value()},
            **({"json": payload} if payload is not None else {}),
        )
        try:
            result = await get_response_json(response)
            if response.status >= 400:
                # Do not include remote URLs, credentials, or filesystem paths.
                raise HTTPException(
                    503, {"error_type": "environment_error", "error": "Spreadsheet service rejected the operation"}
                )
            return result
        finally:
            response.release()

    @staticmethod
    def _session(request):
        session = request.session.get("spreadsheet_session")
        if not session:
            raise HTTPException(409, "Call seed_session before using spreadsheet tools")
        return session

    async def _operation(self, request, operation_id, action, arguments):
        session = self._session(request)
        try:
            async with asyncio.timeout(self.config.operation_timeout):
                job = await self._call(
                    "POST",
                    f"/sessions/{session}/operations",
                    {
                        "operation_id": operation_id,
                        "action": action,
                        "arguments": arguments,
                    },
                )
                while job["status"] != "DONE":
                    await asyncio.sleep(self.config.poll_interval)
                    job = await self._call("GET", "/jobs/" + job["job_id"])
                result = job["result"]
                if result.get("error_type") == "environment_error":
                    raise HTTPException(503, result)
                return result
        except TimeoutError:
            raise HTTPException(
                503, {"error_type": "environment_error", "error": "Spreadsheet operation deadline exceeded"}
            ) from None

    def setup_webserver(self) -> FastAPI:
        app = super().setup_webserver()

        def register(name):
            async def tool(
                body: dict, request: Request, x_spreadsheet_operation_id: str = Header(min_length=1, max_length=256)
            ):
                result = await self._operation(request, x_spreadsheet_operation_id, name, body)
                return result["output"] if result.get("ok") else result

            tool.__name__ = name
            app.post("/" + name)(tool)

        for name in TOOL_MODELS:
            register(name)
        app.post("/close_session")(self.close_session)
        app.post("/begin_execution")(self.begin_execution)
        return app

    async def begin_execution(self, request: Request, body: dict):
        async with asyncio.timeout(self.config.operation_timeout):
            return await self._call("POST", f"/sessions/{self._session(request)}/execution", body)

    async def seed_session(self, request: Request, body: SpreadsheetSeedRequest) -> BaseSeedSessionResponse:
        try:
            async with asyncio.timeout(self.config.operation_timeout):
                seeded = await self._call("POST", "/sessions", body.model_dump())
        except TimeoutError:
            raise HTTPException(503, "Spreadsheet session setup deadline exceeded") from None
        existing = request.session.get("spreadsheet_session")
        if existing and existing != seeded["session_id"]:
            raise HTTPException(409, "A Gym session cannot switch spreadsheet rollouts")
        request.session["spreadsheet_session"] = seeded["session_id"]
        request.session["spreadsheet_task"] = body.task_id
        return BaseSeedSessionResponse()

    async def verify(self, request: Request, body: SpreadsheetVerifyRequest) -> SpreadsheetVerifyResponse:
        if request.session.get("spreadsheet_task") != body.task_id:
            raise HTTPException(409, "Verifier task does not match the seeded session")
        result = await self._operation(request, "verify", "verify", {})
        if not result.get("ok"):
            raise HTTPException(
                503, {"error_type": "environment_error", "error": "Verifier could not score the workbook"}
            )
        return SpreadsheetVerifyResponse(
            **(body.model_dump() | result["output"]),
            engine=result["engine"],
            workbook_sha256=result["workbook_sha256"],
        )

    async def close_session(self, request: Request):
        try:
            async with asyncio.timeout(10):
                return await self._call("POST", f"/sessions/{self._session(request)}/close", {})
        except TimeoutError:
            raise HTTPException(503, "Close deadline exceeded; TTL cleanup will retain the session") from None


if __name__ == "__main__":
    SpreadsheetResourcesServer.run_webserver()
