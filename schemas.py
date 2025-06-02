from pydantic import BaseModel
from uuid import UUID
from datetime import datetime

class NoteCreate(BaseModel):
    title: str
    content: str

class Note(BaseModel):
    id: UUID
    title: str
    content: str
    user_id: UUID
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {
        "from_attributes": True
    }

class UserCreate(BaseModel):
    username: str
    password: str

class UserOut(BaseModel):
    id: UUID
    username: str

    model_config = {
        "from_attributes": True
    }
