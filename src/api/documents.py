"""Document processing API — upload, process, and manage environmental reports."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/documents", tags=["documents"])

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/tmp/environmental_uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB

# ── Schemas ────────────────────────────────────────────────────────────────────


class ProcessRequest(BaseModel):
    """Request to process an already-uploaded document."""
    file_id: str = Field(..., description="Uploaded file identifier")
    stages: Optional[list[str]] = Field(
        None, description="Pipeline stages: parse, ner, sanitize, embed"
    )


class ProcessResponse(BaseModel):
    """Result of document processing."""
    file_id: str
    file_name: str
    file_type: Optional[str] = None
    text_length: int = 0
    entities_count: int = 0
    stages_completed: list[str] = []
    errors: list[dict] = []
    processing_time_ms: float = 0.0

    @classmethod
    def from_result(cls, file_id: str, result) -> "ProcessResponse":
        return cls(
            file_id=file_id,
            file_name=result.file_name,
            file_type=result.metadata.get("file_type"),
            text_length=len(result.text_raw),
            entities_count=len(result.entities.entities) if result.entities else 0,
            stages_completed=result.stages_completed,
            errors=result.errors,
            processing_time_ms=round(result.processing_time_ms, 1),
        )


class UploadResponse(BaseModel):
    file_id: str
    file_name: str
    file_size: int
    upload_path: str


# ── Routes ─────────────────────────────────────────────────────────────────────


@router.post("/upload", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(file: UploadFile = File(...)) -> UploadResponse:
    """Upload an environmental report document (PDF, DOCX, XLSX, etc.).

    Max file size: 50 MB.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    # Validate extension
    ext = Path(file.filename).suffix.lower()
    allowed = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".html", ".htm"}
    if ext not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}. Allowed: {', '.join(allowed)}",
        )

    # Read and hash
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail=f"File exceeds {MAX_UPLOAD_SIZE // 1024 // 1024}MB limit")

    file_hash = hashlib.sha256(content).hexdigest()
    file_id = f"{file_hash[:16]}_{file.filename}"

    save_path = UPLOAD_DIR / file_id
    save_path.write_bytes(content)

    return UploadResponse(
        file_id=file_id,
        file_name=file.filename,
        file_size=len(content),
        upload_path=str(save_path),
    )


@router.post("/process", response_model=ProcessResponse)
async def process_document(req: ProcessRequest) -> ProcessResponse:
    """Run the document processing pipeline on an uploaded file.

    Stages: parse → sanitize → ner → embed (all by default).
    """
    file_path = UPLOAD_DIR / req.file_id
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found. Upload first.")

    # Configure pipeline
    from src.processor.pipeline import DocumentProcessor, PipelineConfig, PipelineStage
    config = PipelineConfig()
    if req.stages:
        try:
            config.stages = [PipelineStage(s) for s in req.stages]
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid stage: {e}")

    processor = DocumentProcessor(config=config)
    result = await processor.process(file_path)

    return ProcessResponse.from_result(req.file_id, result)


@router.get("/status/{file_id}")
async def document_status(file_id: str) -> dict:
    """Check if a document has been uploaded."""
    file_path = UPLOAD_DIR / file_id
    if not file_path.exists():
        return {"file_id": file_id, "status": "not_found"}
    return {
        "file_id": file_id,
        "status": "uploaded",
        "file_size": file_path.stat().st_size,
    }
