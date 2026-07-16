"""
wsgi.py — Gunicorn entry point for the Nustro Reference Provider Agent.

Adds the shared/ directory to the Python path so aeap_client.py
is available without installing it as a package.

Dev: `python wsgi.py` serves the local console on http://localhost:5001.
Prod: `gunicorn wsgi:app`.
"""
import sys
import os

# Add shared module directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'shared'))

from app import app

if __name__ == '__main__':
    # Explicit port — a bare app.run() would default to 5000, not the 5001 the
    # docs, discovery document and Consumer console all assume.
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5001)), debug=False)
