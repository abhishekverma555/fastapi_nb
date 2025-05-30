#plsnb
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List
from uuid import uuid4, UUID

app = FastAPI()

# In-memory "database"
notes_db = []

# Pydantic model for a Note
class Note(BaseModel):
    id: UUID
    title: str
    content: str

class NoteCreate(BaseModel):
    title: str
    content: str

@app.get("/")
def read_root():
    return {"message": "Welcome to zaa Notetaker API"}

@app.get("/notes", response_model=List[Note])
def get_notes():
    return notes_db

@app.post("/notes", response_model=Note)
def create_note(note: NoteCreate):
    new_note = Note(id=uuid4(), title=note.title, content=note.content)
    notes_db.append(new_note)
    return new_note

@app.delete("/notes/{note_id}")
def delete_note(note_id: UUID):
    for i, note in enumerate(notes_db):
        if note.id == note_id:
            del notes_db[i]
            return {"message": "Note deleted"}
    raise HTTPException(status_code=404, detail="Note not found")