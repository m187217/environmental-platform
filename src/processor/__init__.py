"""Document Processor Agent — PDF parser + BERT NER pipeline.

Intelligent document processing for environmental reports:
- Parse PDF/DOCX/XLSX files into structured text and tables
- Extract named entities (contacts, addresses, processes, products, materials)
- Generate TF-IDF embeddings for semantic search
- Sanitize sensitive information (phone, ID, email)
"""

from src.processor.pipeline import DocumentProcessor

__all__ = ["DocumentProcessor"]
