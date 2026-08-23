import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.config import settings
from app.core.db import Base


def _tenant_id_default() -> str:
    return settings.DEFAULT_TENANT_ID


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(
        String(255),
        nullable=False,
        default=_tenant_id_default,
        index=True,
    )
    email = Column(String(300), unique=True, nullable=False)
    # ORM name matches fastapi-users; physical column remains hashed_pw (TASK-04).
    hashed_password = Column("hashed_pw", String(500), nullable=False)
    role = Column(String(20), nullable=False, default="user")
    is_active = Column(Boolean, nullable=False, default=True)
    is_superuser = Column(Boolean, nullable=False, default=False)
    is_verified = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Book(Base):
    __tablename__ = "books"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(
        String(255),
        nullable=False,
        default=_tenant_id_default,
        index=True,
    )
    title = Column(String(500), nullable=False)
    author = Column(String(300), nullable=True)
    language = Column(String(10), nullable=False)
    book_type = Column(String(50), nullable=True)
    total_pages = Column(Integer, nullable=True)
    total_chunks = Column(Integer, nullable=False, default=0)
    minio_path = Column(String(1000), nullable=True)
    status = Column(String(20), nullable=False, default="pending")
    uploaded_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    book_id = Column(
        UUID(as_uuid=True),
        ForeignKey("books.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id = Column(String(255), nullable=False, default=_tenant_id_default)
    status = Column(String(20), nullable=False, default="queued")
    progress_pct = Column(Integer, nullable=False, default=0)
    current_step = Column(String(100), nullable=True)
    error_msg = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    checkpoint = Column(JSONB, nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    task_id = Column(String(100), nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)


class AdminSettings(Base):
    __tablename__ = "admin_settings"

    id = Column(Integer, primary_key=True, default=1)
    system_prompt = Column(Text, nullable=False, default="")
    product_name = Column(String(255), nullable=False, default="Al-Mawsu'at al-Deobandiyyah")
    primary_color = Column(String(7), nullable=False, default="#1D9E75")
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id = Column(String(255), nullable=False, default=_tenant_id_default)
    token_hash = Column(String(500), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
