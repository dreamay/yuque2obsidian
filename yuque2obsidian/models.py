"""Pydantic models for Yuque API responses and internal data."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class User(BaseModel):
    id: int
    type: str
    login: str
    name: str
    avatar_url: Optional[str] = None


class Repo(BaseModel):
    id: int
    type: str
    slug: str
    name: str
    namespace: str
    user_id: int
    description: Optional[str] = None
    public: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    content_updated_at: Optional[datetime] = None


class DocSummary(BaseModel):
    id: int
    type: str = "Doc"
    slug: str
    title: str
    book_id: int = Field(alias="book_id")
    user_id: int = Field(alias="user_id")
    format: Optional[str] = None
    status: Optional[int] = None
    read_status: Optional[int] = None
    view_status: Optional[int] = None
    published_at: Optional[datetime] = None
    first_published_at: Optional[datetime] = None
    content_updated_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    cover: Optional[str] = None
    description: Optional[str] = None
    custom_description: Optional[str] = None

    @field_validator("book_id", "user_id", mode="before")
    @classmethod
    def coerce_int(cls, v: Any) -> int:
        return int(v) if v is not None else 0


class DocDetail(DocSummary):
    body: str = ""
    body_html: Optional[str] = None
    comments_count: Optional[int] = 0
    likes_count: Optional[int] = 0


class TocNode(BaseModel):
    uuid: str
    type: str  # DOC, TITLE, UNCREATED, etc.
    title: Optional[str] = None
    doc_id: Optional[int] = None
    parent_uuid: Optional[str] = None
    parent_id: Optional[str] = None
    level: Optional[int] = 1
    url: Optional[str] = None
    seq: Optional[int] = 0
    child_uuid: Optional[str] = None
    sibling_uuid: Optional[str] = None

    @field_validator("doc_id", mode="before")
    @classmethod
    def _coerce_doc_id(cls, v: Any) -> Optional[int]:
        if v is None or v == "":
            return None
        return int(v)

    def effective_parent_id(self) -> Optional[str]:
        return self.parent_uuid or self.parent_id


class Group(BaseModel):
    id: int
    type: str
    login: str
    name: str
    description: Optional[str] = None


class SyncState(BaseModel):
    namespace: str
    slug: str
    title: str
    content_updated_at: Optional[datetime] = None
    last_synced_at: Optional[datetime] = None
    file_path: Optional[str] = None
