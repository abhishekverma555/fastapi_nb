from fastapi import FastAPI, Depends, HTTPException, Body
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from uuid import UUID
from typing import List, Optional
import redis
import json
from datetime import datetime

from database import get_db, SessionLocal, engine
import models
from models import Note, User as DBUser
import schemas
from schemas import UserCreate, UserOut
from auth import hash_password, verify_password, create_access_token, get_current_user
from utils import extract_note_links, extract_links

from fastapi.middleware.cors import CORSMiddleware
from transformers import pipeline

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

summarizer = pipeline("summarization", model="sshleifer/distilbart-cnn-12-6")

models.Base.metadata.create_all(bind=engine)

redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)


@app.get("/", tags=["Root"])
def read_root():
    return {"message": "Welcome to zaa Notetaker API"}


@app.post("/register", response_model=UserOut, tags=["Authentication"])
def register(user: UserCreate, db: Session = Depends(get_db)):
    existing_user = db.query(DBUser).filter(DBUser.username == user.username).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Username already registered")
    hashed_pw = hash_password(user.password)
    db_user = DBUser(username=user.username, hashed_password=hashed_pw)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


@app.post("/login", tags=["Authentication"])
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(DBUser).filter(DBUser.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    access_token = create_access_token(data={"sub": str(user.id)})
    return {
        "message": "Login successful",
        "access_token": access_token,
        "token_type": "bearer"
    }


def safe_serialize(obj):
    """
    Convert list of dicts with UUID and datetime fields to JSON-serializable forms,
    ensure unicode/multiline text is preserved.
    """
    if isinstance(obj, list):
        new_list = []
        for item in obj:
            new_item = {}
            for k, v in item.items():
                if isinstance(v, UUID):
                    new_item[k] = str(v)
                elif isinstance(v, datetime):
                    new_item[k] = v.isoformat()
                else:
                    new_item[k] = v
            new_list.append(new_item)
        return new_list
    return obj


@app.get("/notes", response_model=List[schemas.Note], tags=["Notes"])
def get_notes(db: Session = Depends(get_db), current_user: DBUser = Depends(get_current_user)):
    redis_key = f"notes_user_{current_user.id}"
    cached = redis_client.get(redis_key)
    if cached:
        return json.loads(cached)

    notes = db.query(Note).filter(Note.user_id == current_user.id).all()
    result = [schemas.Note.model_validate(note).model_dump() for note in notes]
    safe_result = safe_serialize(result)
    # Use ensure_ascii=False to preserve unicode and newlines safely in Redis JSON string
    redis_client.set(redis_key, json.dumps(safe_result, ensure_ascii=False), ex=60)
    return result


@app.post("/notes/", response_model=schemas.Note, tags=["Notes"])
def create_note(note: schemas.NoteCreate, db: Session = Depends(get_db), current_user: DBUser = Depends(get_current_user)):
    # sanitize content to string and strip any trailing spaces but keep multiline intact
    sanitized_content = note.content if isinstance(note.content, str) else str(note.content)
    sanitized_content = sanitized_content.strip()
    db_note = Note(title=note.title.strip(), content=sanitized_content, user_id=current_user.id)
    db.add(db_note)
    db.commit()
    db.refresh(db_note)
    redis_client.delete(f"notes_user_{current_user.id}")
    return db_note


@app.put("/notes/{note_id}", response_model=schemas.Note, tags=["Notes"])
def update_note(note_id: UUID, updated_note: schemas.NoteCreate, db: Session = Depends(get_db), current_user: DBUser = Depends(get_current_user)):
    note = db.query(Note).filter(Note.id == note_id, Note.user_id == current_user.id).first()
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")

    note.title = updated_note.title.strip()
    sanitized_content = updated_note.content if isinstance(updated_note.content, str) else str(updated_note.content)
    note.content = sanitized_content.strip()
    db.commit()
    db.refresh(note)
    redis_client.delete(f"notes_user_{current_user.id}")
    return note


@app.delete("/notes/{note_id}", tags=["Notes"])
def delete_note(note_id: UUID, db: Session = Depends(get_db), current_user: DBUser = Depends(get_current_user)):
    note = db.query(Note).filter(Note.id == note_id, Note.user_id == current_user.id).first()
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    db.delete(note)
    db.commit()
    redis_client.delete(f"notes_user_{current_user.id}")
    return {"message": "Note deleted"}


@app.get("/notes/{note_id}/with_links", tags=["Notes"])
def get_note_with_links(note_id: UUID, db: Session = Depends(get_db), current_user: DBUser = Depends(get_current_user)):
    note = db.query(Note).filter(Note.id == note_id, Note.user_id == current_user.id).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    linked_titles = extract_links(note.content)
    linked_notes = db.query(Note).filter(Note.user_id == current_user.id, Note.title.in_(linked_titles)).all()

    return {
        "note": schemas.Note.model_validate(note),
        "linked_notes": [schemas.Note.model_validate(n) for n in linked_notes]
    }


def chunk_text(text, max_chunk_size=500):
    import re
    sentences = re.split(r'(?<=[.!?]) +', text)
    chunks = []
    current_chunk = ""
    for sentence in sentences:
        if len(current_chunk) + len(sentence) + 1 <= max_chunk_size:
            current_chunk += " " + sentence if current_chunk else sentence
        else:
            chunks.append(current_chunk)
            current_chunk = sentence
    if current_chunk:
        chunks.append(current_chunk)
    return chunks


@app.post("/summarize/", tags=["AI"])
def summarize_note(content: Optional[str] = Body(None, embed=True), title: Optional[str] = Body(None, embed=True),
                   db: Session = Depends(get_db), current_user: DBUser = Depends(get_current_user)):
    if not content and not title:
        raise HTTPException(status_code=400, detail="Either content or title must be provided.")

    if title:
        note = db.query(Note).filter(Note.user_id == current_user.id, Note.title == title).first()
        if not note:
            raise HTTPException(status_code=404, detail=f"Note with title '{title}' not found.")
        content_to_summarize = note.content
    else:
        content_to_summarize = content

    chunks = chunk_text(content_to_summarize, max_chunk_size=500)

    summaries = []
    for chunk in chunks:
        try:
            summary = summarizer(chunk, max_length=100, min_length=30, do_sample=False)
            summaries.append(summary[0]['summary_text'])
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Summarization failed: {str(e)}")

    combined_summary = " ".join(summaries)
    return {"summary": combined_summary}


