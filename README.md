# fastapi_nb
FastAPI AI Notetaker App

This project is a backend-based note-taking application built using FastAPI, designed to store, manage, and enhance notes with modern features like authentication, caching, and AI-powered summarization.

The application allows users to securely create, update, and delete notes while ensuring that each user can only access their own data. It uses PostgreSQL for persistent storage and Redis for caching to improve performance.
A key feature of this project is Obsidian-style note linking, where users can reference other notes using [[Note Title]] syntax. The backend processes these links and fetches related notes dynamically.

Additionally, the app integrates an AI summarization feature using a pre-trained model to generate concise summaries of note content.
