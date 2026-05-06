"""PDF/DOCX/XLSX document parser.

Parses environmental reports into structured text and tables using:
- pdfplumber for PDF documents
- python-docx for Word documents
- openpyxl for Excel spreadsheets

File size limit: 50 MB.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".txt", ".html", ".htm"}


class FileType(Enum):
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    TXT = "txt"
    HTML = "html"


@dataclass
class ParsedTable:
    """A single extracted table."""

    rows: list[list[str]]
    page: int | None = None  # PDF page number
    sheet: str | None = None  # Excel sheet name


@dataclass
class ParsedDocument:
    """Result of parsing a document."""

    file_path: str
    file_type: FileType
    text: str
    tables: list[ParsedTable] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    page_count: int | None = None


class ParsingError(Exception):
    """Raised when document parsing fails."""

    def __init__(self, message: str, code: str = "PARSE_ERROR", detail: str | None = None) -> None:
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__(self.message)


def _validate_file(file_path: str | Path) -> Path:
    """Validate file existence, size, and extension.

    Args:
        file_path: Path to the file.

    Returns:
        Resolved Path object.

    Raises:
        ParsingError: If the file is invalid.
    """
    path = Path(file_path).resolve()

    if not path.exists():
        raise ParsingError(
            message=f"File not found: {path}",
            code="FILE_NOT_FOUND",
            detail=str(path),
        )

    if not path.is_file():
        raise ParsingError(
            message=f"Path is not a file: {path}",
            code="NOT_A_FILE",
            detail=str(path),
        )

    if path.stat().st_size > MAX_FILE_SIZE:
        size_mb = path.stat().st_size / (1024 * 1024)
        raise ParsingError(
            message=f"File exceeds maximum size of 50 MB: {size_mb:.1f} MB",
            code="FILE_TOO_LARGE",
            detail=f"Size: {size_mb:.1f} MB, Limit: 50 MB",
        )

    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ParsingError(
            message=f"Unsupported file type: {ext}",
            code="UNSUPPORTED_TYPE",
            detail=f"Supported types: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
        )

    return path


def _detect_file_type(path: Path) -> FileType:
    """Detect file type from extension."""
    ext = path.suffix.lower()
    return {
        ".pdf": FileType.PDF,
        ".docx": FileType.DOCX,
        ".xlsx": FileType.XLSX,
        ".txt": FileType.TXT,
        ".html": FileType.HTML,
        ".htm": FileType.HTML,
    }[ext]


def _parse_pdf(file_path: str | Path) -> ParsedDocument:
    """Parse a PDF file using pdfplumber.

    Args:
        file_path: Path to the PDF file.

    Returns:
        ParsedDocument with extracted text and tables.

    Raises:
        ParsingError: If pdfplumber is unavailable or parsing fails.
    """
    try:
        import pdfplumber
        import pdfplumber.utils
    except ImportError:
        raise ParsingError(
            message="pdfplumber is not installed. Run: pip install pdfplumber",
            code="DEPENDENCY_MISSING",
        )

    path = _validate_file(file_path)
    text_parts: list[str] = []
    tables: list[ParsedTable] = []

    try:
        with pdfplumber.open(str(path)) as pdf:
            page_count = len(pdf.pages)
            metadata: dict[str, Any] = {
                "title": path.name,
                "pages": page_count,
                **{k: v for k, v in (pdf.metadata or {}).items() if v},
            }

            for page_num, page in enumerate(pdf.pages, start=1):
                # Extract text
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)

                # Extract tables
                page_tables = page.extract_tables()
                for table_data in page_tables:
                    if table_data:
                        cleaned = [
                            [str(cell) if cell is not None else "" for cell in row]
                            for row in table_data
                        ]
                        tables.append(ParsedTable(rows=cleaned, page=page_num))
    except Exception as e:
        logger.exception("Failed to parse PDF", path=str(path))
        raise ParsingError(
            message=f"Failed to parse PDF: {e}",
            code="PDF_PARSE_ERROR",
            detail=str(e),
        ) from e

    return ParsedDocument(
        file_path=str(path),
        file_type=FileType.PDF,
        text="\n\n".join(text_parts),
        tables=tables,
        metadata=metadata,
        page_count=page_count,
    )


def _parse_docx(file_path: str | Path) -> ParsedDocument:
    """Parse a DOCX file using python-docx.

    Args:
        file_path: Path to the DOCX file.

    Returns:
        ParsedDocument with extracted text and tables.

    Raises:
        ParsingError: If python-docx is unavailable or parsing fails.
    """
    try:
        from docx import Document
    except ImportError:
        raise ParsingError(
            message="python-docx is not installed. Run: pip install python-docx",
            code="DEPENDENCY_MISSING",
        )

    path = _validate_file(file_path)
    text_parts: list[str] = []
    tables: list[ParsedTable] = []

    try:
        doc = Document(str(path))

        # Extract paragraphs
        for para in doc.paragraphs:
            if para.text.strip():
                text_parts.append(para.text)

        # Extract tables
        for table in doc.tables:
            rows: list[list[str]] = []
            for row in table.rows:
                cells = [cell.text for cell in row.cells]
                rows.append(cells)
            if rows:
                tables.append(ParsedTable(rows=rows))

        metadata: dict[str, Any] = {
            "title": path.name,
            "paragraphs": len(text_parts),
            "tables_count": len(tables),
        }
    except Exception as e:
        logger.exception("Failed to parse DOCX", path=str(path))
        raise ParsingError(
            message=f"Failed to parse DOCX: {e}",
            code="DOCX_PARSE_ERROR",
            detail=str(e),
        ) from e

    return ParsedDocument(
        file_path=str(path),
        file_type=FileType.DOCX,
        text="\n\n".join(text_parts),
        tables=tables,
        metadata=metadata,
    )


def _parse_xlsx(file_path: str | Path) -> ParsedDocument:
    """Parse an XLSX file using openpyxl.

    Args:
        file_path: Path to the XLSX file.

    Returns:
        ParsedDocument with extracted text and tables.

    Raises:
        ParsingError: If openpyxl is unavailable or parsing fails.
    """
    try:
        import openpyxl
    except ImportError:
        raise ParsingError(
            message="openpyxl is not installed. Run: pip install openpyxl",
            code="DEPENDENCY_MISSING",
        )

    path = _validate_file(file_path)
    text_parts: list[str] = []
    tables: list[ParsedTable] = []

    try:
        workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        sheet_names = workbook.sheetnames

        for sheet_name in sheet_names:
            sheet = workbook[sheet_name]
            sheet_text: list[str] = []
            rows: list[list[str]] = []

            for row_idx, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                if all(cell is None for cell in row):
                    continue
                row_strs = [str(cell) if cell is not None else "" for cell in row]
                rows.append(row_strs)
                sheet_text.append(" | ".join(row_strs))

            if sheet_text:
                text_parts.append(f"--- Sheet: {sheet_name} ---\n" + "\n".join(sheet_text))
            if rows:
                tables.append(ParsedTable(rows=rows, sheet=sheet_name))

        workbook.close()
        metadata: dict[str, Any] = {
            "title": path.name,
            "sheets": sheet_names,
            "tables_count": len(tables),
        }
    except Exception as e:
        logger.exception("Failed to parse XLSX", path=str(path))
        raise ParsingError(
            message=f"Failed to parse XLSX: {e}",
            code="XLSX_PARSE_ERROR",
            detail=str(e),
        ) from e

    return ParsedDocument(
        file_path=str(path),
        file_type=FileType.XLSX,
        text="\n\n".join(text_parts),
        tables=tables,
        metadata=metadata,
    )


def _parse_txt(path: Path) -> ParsedDocument:
    """Parse a plain text file."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="gbk", errors="replace")
    return ParsedDocument(
        file_path=str(path),
        file_type=FileType.TXT,
        text=text,
        metadata={"encoding": "utf-8", "file_size": path.stat().st_size},
    )


def _parse_html(path: Path) -> ParsedDocument:
    """Parse an HTML file, extracting text content."""
    from html.parser import HTMLParser
    
    class TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
        def handle_data(self, data):
            text = data.strip()
            if text:
                self.parts.append(text)
    
    extractor = TextExtractor()
    content = path.read_text(encoding="utf-8", errors="replace")
    extractor.feed(content)
    text = "\n".join(extractor.parts)
    
    return ParsedDocument(
        file_path=str(path),
        file_type=FileType.HTML,
        text=text,
        metadata={"file_size": path.stat().st_size},
    )


async def parse_document(file_path: str | Path) -> ParsedDocument:
    """Parse a document (PDF, DOCX, or XLSX) into structured text and tables.

    This is the main entry point. It detects the file type and delegates
    to the appropriate parser. Designed to be async-friendly — wraps
    potentially slow I/O-bound parsing in a sync call.

    Args:
        file_path: Path to the document file.

    Returns:
        ParsedDocument with extracted text, tables, and metadata.

    Raises:
        ParsingError: If the file is invalid, type unsupported, or parsing fails.
    """
    path = _validate_file(file_path)
    file_type = _detect_file_type(path)

    logger.info("Parsing document", path=str(path), file_type=file_type.value)

    parsers = {
        FileType.PDF: _parse_pdf,
        FileType.DOCX: _parse_docx,
        FileType.XLSX: _parse_xlsx,
        FileType.TXT: _parse_txt,
        FileType.HTML: _parse_html,
    }

    parser = parsers[file_type]
    doc = parser(path)

    logger.info(
        "Document parsed successfully",
        path=str(path),
        text_length=len(doc.text),
        tables_count=len(doc.tables),
    )

    return doc


def parse_document_sync(file_path: str | Path) -> ParsedDocument:
    """Synchronous wrapper for parse_document. Useful for non-async contexts.

    Args:
        file_path: Path to the document file.

    Returns:
        ParsedDocument with extracted text, tables, and metadata.
    """
    path = Path(file_path).resolve()
    file_type = _detect_file_type(path)
    parsers = {
        FileType.PDF: _parse_pdf,
        FileType.DOCX: _parse_docx,
        FileType.XLSX: _parse_xlsx,
        FileType.TXT: _parse_txt,
        FileType.HTML: _parse_html,
    }
    return parsers[file_type](path)
