"""Document Processor Agent — PDF parser + BERT NER pipeline.

Intelligent document processing for environmental reports:
- Parse PDF/DOCX/XLSX files into structured text and tables
- Extract named entities (contacts, addresses, processes, products, materials)
- Generate TF-IDF embeddings for semantic search
- Sanitize sensitive information (phone, ID, email)

Lazy-loading: heavy deps (numpy, scikit-learn, transformers, torch) are
only imported when processing is actually invoked, NOT at app startup.
"""


def _get_processor():
    """Lazy import to avoid loading ML deps at app startup."""
    from src.processor.pipeline import DocumentProcessor
    return DocumentProcessor


def _get_config():
    from src.processor.pipeline import PipelineConfig
    return PipelineConfig


def _get_stage():
    from src.processor.pipeline import PipelineStage
    return PipelineStage


__all__ = ["_get_processor", "_get_config", "_get_stage"]
