from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.domain.states import UserRole
from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)

    model_config = {"json_schema_extra": {"examples": [{"email": "admin@example.com",
                                                         "password": "correct horse battery staple"}]}}


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: UserRole
    workspace_id: uuid.UUID


class SessionOut(BaseModel):
    user: UserOut
    csrf_token: str
    expires_at: datetime
