"""FastAPI backend on 127.0.0.1:8000 (FR-11, PRD §8.2)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .service import FactoryError, FactoryService

MAX_PRD_BYTES = 512_000


class PRDIn(BaseModel):
    text: str
    title: str | None = None
    analyzer: Literal["auto", "template", "llm"] = "auto"


class DecisionIn(BaseModel):
    approve: bool
    version: int | None = None
    approver: str = "poc_owner"
    approved_tools: list[str] = Field(default_factory=list)
    comment: str = ""


class RunIn(BaseModel):
    blueprint_id: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None
    run_id: str | None = None


def create_app(service: FactoryService | None = None) -> FastAPI:
    app = FastAPI(title="Dynamic Agent Factory", version="0.1.0")
    svc = service or FactoryService()
    app.state.service = svc

    @app.exception_handler(FactoryError)
    async def factory_error(_req: Request, exc: FactoryError) -> JSONResponse:
        return JSONResponse(status_code=exc.status,
                            content={"detail": str(exc), "details": exc.details})

    @app.get("/health")
    def health() -> dict[str, Any]:
        cfg = svc.model_config()
        return {"status": "ok", "provider": cfg.provider, "model": cfg.model,
                "tools": svc.tools.ids(), "skills": svc.skills.ids()}

    @app.get("/catalog")
    def catalog() -> dict[str, Any]:
        return {"tools": svc.tools.catalog(), "skills": svc.skills.ids()}

    @app.post("/prds", status_code=201)
    def submit_prd(body: PRDIn) -> dict[str, Any]:
        if len(body.text.encode()) > MAX_PRD_BYTES:
            raise HTTPException(413, "PRD too large")
        return svc.submit_prd(body.text, body.title, body.analyzer)

    @app.post("/prds/upload", status_code=201)
    async def upload_prd(file: UploadFile = File(...),
                         analyzer: Literal["auto", "template", "llm"] = Form("auto")):
        name = (file.filename or "").lower()
        if not name.endswith((".md", ".txt", ".markdown")):
            raise HTTPException(415, "only .md and .txt PRDs are supported")
        raw = await file.read(MAX_PRD_BYTES + 1)
        if len(raw) > MAX_PRD_BYTES:
            raise HTTPException(413, "PRD too large")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(415, "PRD must be UTF-8 text") from None
        return svc.submit_prd(text, None, analyzer)

    @app.get("/blueprints")
    def list_blueprints() -> list[dict[str, Any]]:
        return svc.list_blueprints()

    @app.get("/blueprints/{blueprint_id}")
    def get_blueprint(blueprint_id: str, version: int | None = None) -> dict[str, Any]:
        return svc.get_blueprint(blueprint_id, version)

    @app.post("/blueprints/{blueprint_id}/approve")
    def approve(blueprint_id: str, body: DecisionIn) -> dict[str, Any]:
        return svc.decide(blueprint_id, body.version, body.approve, body.approver,
                          body.approved_tools, body.comment)

    @app.post("/runs", status_code=202)
    def start_run(body: RunIn) -> dict[str, Any]:
        return svc.start_run(body.blueprint_id, body.inputs, version=body.version,
                             run_id=body.run_id)

    @app.get("/runs")
    def list_runs() -> list[dict[str, Any]]:
        return svc.list_runs()

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, events_after: int = -1) -> dict[str, Any]:
        return svc.get_run(run_id, events_after)

    @app.get("/runs/{run_id}/export")
    def export_run(run_id: str, format: Literal["md", "json"] = "md"):
        if format == "json":
            return svc.get_run(run_id)
        return PlainTextResponse(svc.export_markdown(run_id), media_type="text/markdown")

    @app.get("/audit")
    def audit(subject: str | None = None) -> list[dict[str, Any]]:
        return svc.store.audit_log(subject)

    return app


def main() -> None:
    import uvicorn

    # Loopback only (PRD §9).
    uvicorn.run("agent_factory.api:create_app", factory=True, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
