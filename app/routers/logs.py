"""
Log viewer routes.

GET /logs        → log viewer page
GET /logs/data   → HTMX partial (log lines, polled every 3 s)
POST /logs/clear → clear the in-memory buffer
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.log_buffer import log_buffer

router = APIRouter(prefix="/logs", tags=["logs"])


def _templates(request: Request):
    return request.app.state.templates


@router.get("", response_class=HTMLResponse)
async def logs_page(request: Request) -> HTMLResponse:
    return _templates(request).TemplateResponse(
        "logs.html",
        {"request": request},
    )


@router.get("/data", response_class=HTMLResponse)
async def logs_data(request: Request, n: int = 100) -> HTMLResponse:
    lines = log_buffer.lines(n)
    return _templates(request).TemplateResponse(
        "partials/log_viewer.html",
        {"request": request, "lines": lines},
    )


@router.post("/clear", response_class=HTMLResponse)
async def clear_logs(request: Request) -> HTMLResponse:
    log_buffer.clear()
    return _templates(request).TemplateResponse(
        "partials/log_viewer.html",
        {"request": request, "lines": []},
    )
