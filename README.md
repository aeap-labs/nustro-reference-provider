# AEAP Reference Provider Agent

**Version:** 0.5.0 | **Protocol:** AEAP (Autonomous Economic Agent Protocol)

This is a complete, runnable Flask application demonstrating how a Provider
agent integrates with the AEAP Platform.

## Quick start

1. Clone this repo
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in your values
4. Add your agent keys to `keys/` (generated at AEAP agent registration)
5. `gunicorn --workers 2 --bind 127.0.0.1:5001 wsgi:app`

Full documentation: [README](src/README.md) ← see the detailed guide

## AEAP Platform

- API docs: https://api.aeap.ai/swagger
- Register: https://aeap.ai
