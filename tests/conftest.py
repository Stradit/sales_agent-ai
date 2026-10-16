"""Test-wide setup, loaded by pytest before any test module is imported.

auth.py refuses to import without JWT_SECRET_KEY (correct in production — a missing secret
would let anyone forge tokens). Unit tests never issue real tokens, so CI runners and other
machines without a .env get a random throwaway key here. A key that is already set (e.g.
from a developer's .env, which config.py loads) is left untouched.
"""
import os
import secrets

os.environ.setdefault("JWT_SECRET_KEY", "test-only-" + secrets.token_urlsafe(32))
