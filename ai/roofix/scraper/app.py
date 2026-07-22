"""
Roofix Scraper — FastAPI wrapper around the Playwright-based proposal fetcher.

Endpoints:
    GET  /health                     healthcheck
    GET  /session                    current session-file status
    POST /session/refresh            accept a Playwright storage_state JSON body
                                     and persist it to the mounted volume
    GET  /proposal/{project_id}      scrape a proposal by Roofix project id
                                     (or ?tracking_url=... for tokenized email links)
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import session
from scraper import fetch_proposal


app = FastAPI(title="Roofix Scraper")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/session")
def session_status() -> dict:
    return session.info()


class SessionState(BaseModel):
    """Playwright storage_state shape: {cookies: [...], origins: [...]}"""
    cookies: list = []
    origins: list = []


@app.post("/session/refresh")
def session_refresh(state: SessionState) -> dict:
    session.save(state.model_dump())
    return {"saved": True, **session.info()}


@app.get("/proposal/{project_id}")
async def proposal(project_id: str, tracking_url: Optional[str] = None) -> dict:
    try:
        return await fetch_proposal(project_id=project_id, tracking_url=tracking_url)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"scrape failed: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
