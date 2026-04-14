"""
wsgi.py — Gunicorn entry point for the AEAP Reference Provider Agent.

Adds the shared/ directory to the Python path so aeap_client.py
is available without installing it as a package.
"""
import sys
import os

# Add shared module directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'shared'))

from app import app

if __name__ == '__main__':
    app.run()
