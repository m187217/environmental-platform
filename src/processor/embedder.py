"""TF-IDF vector embedding for semantic search over environmental documents.

Generates sparse vector representations using sklearn's TfidfVectorizer.
Designed for Chinese text with character-level n-grams and word-level features.
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import structlog

logger = structlog.get_logger(__name__)


class EmbedderError(Exception):
    """Raised when embedding fails."""

    def __init__(self, message: str, code: str = "EMBED_ERROR", detail: str | None = None) -> None:
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__(self.message)


@dataclass
class EmbeddingResult:
    """Result of TF-IDF embedding."""

    vector: np.ndarray
    feature_names: list[str]
    document_length: int
    nonzero_terms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "vector_shape": list(self.vector.shape),
            "nonzero_terms": self.nonzero_terms,
            "document_length": self.document_length,
            "top_terms": sorted(
                zip(self.feature_names, self.vector.tolist()),
                key=lambda x: x[1],
                reverse=True,
            )[:20],
        }

    def to_array(self) -> np.ndarray:
        """Return the TF-IDF vector as a dense numpy array."""
        return self.vector.toarray().flatten() if hasattr(self.vector, "toarray") else self.vector


# ── Global vectorizer instance ──────────────────────────────────────────────

_vectorizer: Any | None = None
_vectorizer_fitted: bool = False


def _get_vectorizer(
    max_features: int = 5000,
    ngram_range: tuple[int, int] = (1, 3),
) -> Any:
    """Get or create a TfidfVectorizer configured for Chinese text.

    Uses character-level n-grams (1-3 chars) which work well for Chinese
    text without requiring a separate segmentation step.

    Args:
        max_features: Maximum number of features (vocabulary size).
        ngram_range: Range of n-gram sizes.

    Returns:
        A sklearn TfidfVectorizer instance.
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError:
        raise EmbedderError(
            message="scikit-learn is not installed. Run: pip install scikit-learn",
            code="DEPENDENCY_MISSING",
        )

    return TfidfVectorizer(
        max_features=max_features,
        ngram_range=ngram_range,
        analyzer="char",  # Character-level for Chinese
        lowercase=False,
        token_pattern=None,
        sublinear_tf=True,  # 1 + log(tf)
        strip_accents=None,
    )


async def embed_text(
    text: str,
    fit: bool = False,
    max_features: int = 5000,
) -> EmbeddingResult:
    """Generate a TF-IDF vector for a single document.

    Args:
        text: Input text to embed.
        fit: If True, fit the vectorizer on this text (for initial use).
            If False, use a pre-fitted vectorizer (for subsequent documents).
        max_features: Max vocabulary size when fitting.

    Returns:
        EmbeddingResult with the TF-IDF vector and metadata.

    Raises:
        EmbedderError: If embedding fails or vectorizer is not fitted.
    """
    global _vectorizer, _vectorizer_fitted

    if not text or not text.strip():
        return EmbeddingResult(
            vector=np.array([]),
            feature_names=[],
            document_length=0,
            nonzero_terms=0,
        )

    try:
        if fit or _vectorizer is None:
            _vectorizer = _get_vectorizer(max_features=max_features)
            logger.info("Fitting TF-IDF vectorizer", max_features=max_features)
            tfidf_matrix = _vectorizer.fit_transform([text])
            _vectorizer_fitted = True
        else:
            if not _vectorizer_fitted:
                raise EmbedderError(
                    message="Vectorizer has not been fitted yet. Call with fit=True first.",
                    code="VECTORIZER_NOT_FITTED",
                )
            tfidf_matrix = _vectorizer.transform([text])

        feature_names: list[str] = _vectorizer.get_feature_names_out().tolist()
        vector = tfidf_matrix[0]

        nonzero = int((vector.toarray() > 0).sum()) if hasattr(vector, "toarray") else int(np.count_nonzero(vector.toarray()))

        logger.debug(
            "Text embedded",
            document_length=len(text),
            nonzero_terms=nonzero,
        )

        return EmbeddingResult(
            vector=vector,
            feature_names=feature_names,
            document_length=len(text),
            nonzero_terms=nonzero,
        )
    except EmbedderError:
        raise
    except Exception as e:
        logger.exception("TF-IDF embedding failed")
        raise EmbedderError(
            message=f"Embedding failed: {e}",
            code="EMBED_ERROR",
            detail=str(e),
        ) from e


async def embed_documents(
    texts: list[str],
    fit: bool = False,
    max_features: int = 5000,
) -> list[EmbeddingResult]:
    """Generate TF-IDF vectors for multiple documents.

    Args:
        texts: List of input texts.
        fit: If True, fit the vectorizer on these texts.
        max_features: Max vocabulary size.

    Returns:
        List of EmbeddingResult, one per input text.
    """
    results: list[EmbeddingResult] = []
    first = True

    for text in texts:
        result = await embed_text(text, fit=(fit and first), max_features=max_features)
        results.append(result)
        first = False

    return results


async def compute_similarity(
    query: str,
    documents: list[str],
    max_features: int = 5000,
) -> np.ndarray:
    """Compute cosine similarity between a query and a list of documents.

    Args:
        query: Query text.
        documents: List of document texts.
        max_features: Max vocabulary size.

    Returns:
        Numpy array of cosine similarity scores (shape: n_documents,).

    Raises:
        EmbedderError: If computation fails.
    """
    try:
        from sklearn.metrics.pairwise import cosine_similarity
    except ImportError:
        raise EmbedderError(
            message="scikit-learn is not installed. Run: pip install scikit-learn",
            code="DEPENDENCY_MISSING",
        )

    if not documents:
        return np.array([])

    all_texts = [query] + documents
    vectorizer = _get_vectorizer(max_features=max_features)
    tfidf_matrix = vectorizer.fit_transform(all_texts)

    query_vec = tfidf_matrix[0:1]
    doc_vecs = tfidf_matrix[1:]

    similarities = cosine_similarity(query_vec, doc_vecs).flatten()
    return similarities


def embed_text_sync(text: str, fit: bool = False, max_features: int = 5000) -> EmbeddingResult:
    """Synchronous wrapper for embed_text."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(embed_text(text, fit=fit, max_features=max_features))
    return loop.run_until_complete(embed_text(text, fit=fit, max_features=max_features))


def save_vectorizer(path: str | Path) -> None:
    """Save the fitted vectorizer to disk.

    Args:
        path: File path for the pickle file.
    """
    global _vectorizer
    if _vectorizer is None:
        raise EmbedderError(
            message="No fitted vectorizer to save.",
            code="NO_VECTORIZER",
        )
    with open(path, "wb") as f:
        pickle.dump(_vectorizer, f)
    logger.info("Vectorizer saved", path=str(path))


def load_vectorizer(path: str | Path) -> None:
    """Load a fitted vectorizer from disk.

    Args:
        path: File path to the pickle file.
    """
    global _vectorizer, _vectorizer_fitted
    with open(path, "rb") as f:
        _vectorizer = pickle.load(f)
    _vectorizer_fitted = True
    logger.info("Vectorizer loaded", path=str(path))
