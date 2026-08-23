from __future__ import annotations

from datetime import timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import get_current_user
from app.core.db import get_db
from app.models.tables import ProcessingJob, User

router = APIRouter(prefix="/admin/jobs", tags=["jobs"])


def _compute_duration(started_at, finished_at) -> int | None:
    if started_at and finished_at:
        diff = finished_at - started_at
        return int(diff.total_seconds())
    return None


class JobOut(BaseModel):
    id: str
    book_id: str
    status: str
    progress_pct: int
    current_step: str | None
    error_msg: str | None
    started_at: str | None
    finished_at: str | None
    duration_seconds: int | None


@router.get("/active")
async def list_jobs(
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> list[JobOut]:
    result = await session.execute(
        select(ProcessingJob)
        .where(ProcessingJob.tenant_id == settings.DEFAULT_TENANT_ID)
        .order_by(ProcessingJob.created_at.desc())
    )
    jobs = result.scalars().all()
    return [
        JobOut(
            id=str(j.id),
            book_id=str(j.book_id),
            status=j.status,
            progress_pct=j.progress_pct,
            current_step=j.current_step,
            error_msg=j.error_msg,
            started_at=j.started_at.isoformat() if j.started_at else None,
            finished_at=j.finished_at.isoformat() if j.finished_at else None,
            duration_seconds=_compute_duration(j.started_at, j.finished_at),
        )
        for j in jobs
    ]


@router.get("/{job_id}")
async def get_job(
    job_id: str,
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> JobOut:
    result = await session.execute(
        select(ProcessingJob).where(ProcessingJob.id == job_id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Job not found")

    return JobOut(
        id=str(job.id),
        book_id=str(job.book_id),
        status=job.status,
        progress_pct=job.progress_pct,
        current_step=job.current_step,
        error_msg=job.error_msg,
        started_at=job.started_at.isoformat() if job.started_at else None,
        finished_at=job.finished_at.isoformat() if job.finished_at else None,
        duration_seconds=_compute_duration(job.started_at, job.finished_at),
    )
