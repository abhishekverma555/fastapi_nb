from pydantic import BaseModel
from uuid import UUID

class NoteCreate(BaseModel):
    title: str
    content: str

class Note(NoteCreate):
    id: UUID

    class Config:
        orm_mode = True
