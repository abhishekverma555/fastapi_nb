from fastapi import FastAPI, Depends, HTTPException, Body
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from uuid import UUID, uuid4
from typing import List, Optional
import redis
import json
from datetime import datetime

from database import get_db, engine
import models
from models import Note, User as DBUser
import schemas
from schemas import UserCreate, UserOut, NoteCreate
from auth import hash_password, verify_password, create_access_token, get_current_user
from utils import extract_links         # ⬅ helper that returns titles inside [[ ... ]]

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

# Create tables (only once at start-up)
models.Base.metadata.create_all(bind=engine)

# Redis client
redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)


def safe_serialize(list_of_dicts):
    """Convert UUID / datetime so json.dumps works."""
    converted = []
    for item in list_of_dicts:
        clean = {}
        for k, v in item.items():
            if isinstance(v, UUID):
                clean[k] = str(v)
            elif isinstance(v, datetime):
                clean[k] = v.isoformat()
            else:
                clean[k] = v
        converted.append(clean)
    return converted


def invalidate_notes_cache(user_id: UUID):
    redis_client.delete(f"notes_user_{user_id}")


def upsert_stub_notes(db: Session, user_id: UUID, linked_titles: list[str]):
    """
    Ensure every [[Title]] mentioned has a corresponding Note row.
    Creates empty 'stub' notes where necessary.
    """
    for title in linked_titles:
        title = title.strip()
        if not title:
            continue
        existing = (
            db.query(Note)
            .filter(Note.user_id == user_id, Note.title == title)
            .first()
        )
        if existing is None:
            stub = Note(
                id=uuid4(),
                title=title,
                content="",  # empty placeholder
                user_id=user_id,
            )
            db.add(stub)
    db.commit()


def process_links_for_note(db: Session, note: Note):
    """Parse links in note.content and create stub notes when needed."""
    linked_titles = extract_links(note.content)
    if linked_titles:
        upsert_stub_notes(db, note.user_id, linked_titles)


@app.get("/", tags=["Root"])
def root():
    return {"message": "Welcome to zaa Notetaker API"}

@app.post("/register", response_model=UserOut, tags=["Authentication"])
def register(user: UserCreate, db: Session = Depends(get_db)):
    if db.query(DBUser).filter(DBUser.username == user.username).first():
        raise HTTPException(400, "Username already registered")
    db_user = DBUser(
        username=user.username,
        hashed_password=hash_password(user.password),
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


@app.post("/login", tags=["Authentication"])
def login(
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = db.query(DBUser).filter(DBUser.username == form.username).first()
    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(401, "Invalid credentials")
    token = create_access_token({"sub": str(user.id)})
    return {"access_token": token, "token_type": "bearer"}

# Notes CRUD 
@app.get("/notes", response_model=List[schemas.Note], tags=["Notes"])
def get_notes(
    db: Session = Depends(get_db),
    current: DBUser = Depends(get_current_user),
):
    cache_key = f"notes_user_{current.id}"
    cached = redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    notes = db.query(Note).filter(Note.user_id == current.id).all()
    result = [schemas.Note.model_validate(n).model_dump() for n in notes]
    redis_client.set(cache_key, json.dumps(safe_serialize(result), ensure_ascii=False), ex=60)
    return result


@app.post("/notes/", response_model=schemas.Note, tags=["Notes"])
def create_note(
    note_in: NoteCreate,
    db: Session = Depends(get_db),
    current: DBUser = Depends(get_current_user),
):
    db_note = Note(**note_in.model_dump(), user_id=current.id)
    db.add(db_note)
    db.commit()
    db.refresh(db_note)

    # Process links (create stubs if needed)
    process_links_for_note(db, db_note)

    invalidate_notes_cache(current.id)
    return db_note


@app.put("/notes/{note_id}", response_model=schemas.Note, tags=["Notes"])
def update_note(
    note_id: UUID,
    note_in: NoteCreate,
    db: Session = Depends(get_db),
    current: DBUser = Depends(get_current_user),
):
    note = db.query(Note).filter(Note.id == note_id, Note.user_id == current.id).first()
    if not note:
        raise HTTPException(404, "Note not found")

    note.title = note_in.title
    note.content = note_in.content
    db.commit()
    db.refresh(note)

    process_links_for_note(db, note)
    invalidate_notes_cache(current.id)
    return note


@app.delete("/notes/{note_id}", tags=["Notes"])
def delete_note(
    note_id: UUID,
    db: Session = Depends(get_db),
    current: DBUser = Depends(get_current_user),
):
    note = db.query(Note).filter(Note.id == note_id, Note.user_id == current.id).first()
    if not note:
        raise HTTPException(404, "Note not found")
    db.delete(note)
    db.commit()
    invalidate_notes_cache(current.id)
    return {"message": "Note deleted"}

# ─── Linked-note view (outgoing + backlinks) ──────────────────────────────
@app.get("/notes/{note_id}/with_links", tags=["Notes"])
def get_note_with_links(
    note_id: UUID,
    db: Session = Depends(get_db),
    current: DBUser = Depends(get_current_user),
):
    note = db.query(Note).filter(Note.id == note_id, Note.user_id == current.id).first()
    if not note:
        raise HTTPException(404, "Note not found")

    # Outgoing
    out_titles = extract_links(note.content)
    outgoing = (
        db.query(Note)
        .filter(Note.user_id == current.id, Note.title.in_(out_titles))
        .all()
        if out_titles
        else []
    )

    # Backlinks (who links to me?)
    backlinks = []
    candidates = db.query(Note).filter(Note.user_id == current.id, Note.id != note.id).all()
    for cand in candidates:
        if note.title in extract_links(cand.content):
            backlinks.append(cand)

    return {
        "note": schemas.Note.model_validate(note),
        "outgoing_links": [schemas.Note.model_validate(n) for n in outgoing],
        "backlinks": [schemas.Note.model_validate(n) for n in backlinks],
    }


def chunk_text(text, max_len=500):
    import re
    sentences = re.split(r'(?<=[.!?]) +', text)
    cur, out = "", []
    for s in sentences:
        if len(cur) + len(s) + 1 <= max_len:
            cur += (" " if cur else "") + s
        else:
            out.append(cur)
            cur = s
    if cur:
        out.append(cur)
    return out


@app.post("/summarize/", tags=["AI"])
def summarize(
    content: Optional[str] = Body(None, embed=True),
    title: Optional[str] = Body(None, embed=True),
    db: Session = Depends(get_db),
    current: DBUser = Depends(get_current_user),
):
    if not (content or title):
        raise HTTPException(400, "Either content or title must be provided")

    if title:
        note = db.query(Note).filter(Note.user_id == current.id, Note.title == title).first()
        if not note:
            raise HTTPException(404, f"No note titled '{title}'")
        content = note.content

    chunks = chunk_text(content, 500)
    partial = [summarizer(c, max_length=100, min_length=30, do_sample=False)[0]["summary_text"] for c in chunks]
    final = " ".join(partial) if len(partial) == 1 else summarizer(" ".join(partial), max_length=120, min_length=30, do_sample=False)[0]["summary_text"]
    return {"summary": final}


