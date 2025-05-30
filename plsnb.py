from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from uuid import UUID
from typing import List
import redis
import json

import models
from models import Note
import schemas
import database
from database import SessionLocal, engine

app = FastAPI()

# database tables
models.Base.metadata.create_all(bind=database.engine)

# Redis connection
redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

# database session
def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/")
def read_root():
    return {"message": "Welcome to zaa Notetaker API"}

@app.get("/notes", response_model=List[schemas.Note])
def get_notes(db: Session = Depends(get_db)):
    cached_notes = redis_client.get("all_notes")
    if cached_notes:
        return json.loads(cached_notes)
    
    notes = db.query(models.Note).all()
    redis_client.set("all_notes", json.dumps([note.__dict__ for note in notes], default=str), ex=60)
    return notes

@app.post("/notes", response_model=schemas.Note)
def create_note(note: schemas.NoteCreate, db: Session = Depends(get_db)):
    db_note = models.Note(title=note.title, content=note.content)
    db.add(db_note)
    db.commit()
    db.refresh(db_note)

    # Invalidate cache
    redis_client.delete("all_notes")

    return db_note

@app.delete("/notes/{note_id}")
def delete_note(note_id: UUID, db: Session = Depends(get_db)):
    note = db.query(models.Note).filter(models.Note.id == note_id).first()
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")

    db.delete(note)
    db.commit()

    # Invalidate cache
    redis_client.delete("all_notes")

    return {"message": "Note deleted"}


