from fastapi import FastAPI, Depends, HTTPException, Body
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
from schemas import NoteCreate
from utils import extract_links

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

redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)


def safe_serialize(list_of_dicts):
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


def upsert_stub_notes(db: Session, user_id: UUID, linked_titles: list[str]):
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
                content="",
                user_id=user_id,
            )
            db.add(stub)
    db.commit()


def process_links_for_note(db: Session, note: Note):
    linked_titles = extract_links(note.content)
    if linked_titles:
        upsert_stub_notes(db, note.user_id, linked_titles)


@app.get("/", tags=["Root"])
def root():
    return {"message": "Welcome to the open Notetaker API!"}


@app.get("/notes", response_model=List[schemas.Note], tags=["Notes"])
def get_notes(db: Session = Depends(get_db)):
    notes = db.query(Note).all()
    result = [schemas.Note.model_validate(n).model_dump() for n in notes]
    return result


@app.post("/notes/", response_model=schemas.Note, tags=["Notes"])
def create_note(note_in: NoteCreate, db: Session = Depends(get_db)):
    # Default: use a dummy user_id since auth is removed
    dummy_user = db.query(DBUser).first()
    if not dummy_user:
        dummy_user = DBUser(id=uuid4(), username="agent")
        db.add(dummy_user)
        db.commit()
        db.refresh(dummy_user)

    db_note = Note(**note_in.model_dump(), user_id=dummy_user.id)
    db.add(db_note)
    db.commit()
    db.refresh(db_note)

    process_links_for_note(db, db_note)
    return db_note


@app.put("/notes/{note_id}", response_model=schemas.Note, tags=["Notes"])
def update_note(note_id: UUID, note_in: NoteCreate, db: Session = Depends(get_db)):
    note = db.query(Note).filter(Note.id == note_id).first()
    if not note:
        raise HTTPException(404, "Note not found")

    note.title = note_in.title
    note.content = note_in.content
    db.commit()
    db.refresh(note)

    process_links_for_note(db, note)
    return note


@app.get("/notes/{note_id}/with_links", tags=["Notes"])
def get_note_with_links(note_id: UUID, db: Session = Depends(get_db)):
    note = db.query(Note).filter(Note.id == note_id).first()
    if not note:
        raise HTTPException(404, "Note not found")

    out_titles = extract_links(note.content)
    outgoing = (
        db.query(Note)
        .filter(Note.title.in_(out_titles))
        .all()
        if out_titles else []
    )

    backlinks = []
    candidates = db.query(Note).filter(Note.id != note.id).all()
    for cand in candidates:
        if note.title in extract_links(cand.content):
            backlinks.append(cand)

    return {
        "note": schemas.Note.model_validate(note),
        "outgoing_links": [schemas.Note.model_validate(n) for n in outgoing],
        "backlinks": [schemas.Note.model_validate(n) for n in backlinks],
    }


@app.post("/summarize/", tags=["AI"])
def summarize(content: Optional[str] = Body(None, embed=True), db: Session = Depends(get_db)):
    if not content:
        raise HTTPException(400, "Content must be provided")

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

    chunks = chunk_text(content, 500)
    partial = [summarizer(c, max_length=100, min_length=30, do_sample=False)[0]["summary_text"] for c in chunks]
    final = " ".join(partial) if len(partial) == 1 else summarizer(" ".join(partial), max_length=120, min_length=30, do_sample=False)[0]["summary_text"]
    return {"summary": final}


