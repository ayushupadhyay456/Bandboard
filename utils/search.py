"""
utils/search.py
───────────────
Full-text search for BandBoard auditions.
Elasticsearch removed — SQL LIKE fallback only.
Returns None always so callers use SQL path.
"""

import logging

log = logging.getLogger(__name__)


def search_auditions(q="", instrument="", genre="", city="", country="",
                     page=1, per_page=20):
    """Always returns None → callers fall back to SQL LIKE queries."""
    return None


def index_audition(audition_dict: dict):
    pass


def delete_audition(audition_id: int):
    pass


def reindex_all(auditions):
    pass


def ensure_index():
    pass