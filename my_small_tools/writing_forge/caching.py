import json
from pathlib import Path
from typing import Optional

from .models import AnalysisCache


def get_cache_path(doc_path: Path) -> Path:
    """Get cache file path for a document."""
    return doc_path.parent / f".{doc_path.stem}_forge_cache.json"


def load_cache(doc_path: Path) -> Optional[AnalysisCache]:
    """Load cache for document if it exists."""
    cache_path = get_cache_path(doc_path)
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            return AnalysisCache(**data)
        except Exception:
            return None
    return None


def save_cache(doc_path: Path, cache: AnalysisCache):
    """Save cache for document."""
    cache_path = get_cache_path(doc_path)
    cache_path.write_text(cache.model_dump_json(indent=2))
