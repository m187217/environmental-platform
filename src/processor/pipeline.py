"""Document processing pipeline orchestrator.

Orchestrates the full processing flow:
    file → parse → NER → sanitize → embed → {metadata, entities, embedding}

Designed to be async-friendly and composable. Each stage can be
independently configured or skipped.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import structlog

from src.processor.embedder import EmbeddingResult, embed_text
from src.processor.ner import NERResult, extract_entities
from src.processor.parser import ParsedDocument, parse_document
from src.processor.sanitizer import SanitizerResult, sanitize_file_content

logger = structlog.get_logger(__name__)


class PipelineStage(str, Enum):
    """Stages in the processing pipeline."""

    PARSE = "parse"
    NER = "ner"
    SANITIZE = "sanitize"
    EMBED = "embed"


@dataclass
class ProcessingResult:
    """Complete result of processing a document through the pipeline."""

    file_path: str
    file_name: str

    # Parse stage
    parsed: ParsedDocument | None = None
    text_raw: str = ""

    # Sanitize stage
    sanitized: SanitizerResult | None = None
    text_clean: str = ""

    # NER stage
    entities: NERResult | None = None

    # Embed stage
    embedding: EmbeddingResult | None = None

    # Metadata
    metadata: dict[str, Any] = field(default_factory=dict)
    processing_time_ms: float = 0.0
    stages_completed: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        result: dict[str, Any] = {
            "file_path": self.file_path,
            "file_name": self.file_name,
            "metadata": self.metadata,
            "processing_time_ms": self.processing_time_ms,
            "stages_completed": self.stages_completed,
            "errors": self.errors,
        }

        if self.parsed:
            result["parsed"] = {
                "file_type": self.parsed.file_type.value,
                "text_length": len(self.parsed.text),
                "tables_count": len(self.parsed.tables),
                "metadata": self.parsed.metadata,
            }

        if self.entities:
            result["entities"] = self.entities.to_dict()

        if self.embedding:
            result["embedding"] = self.embedding.to_dict()

        if self.sanitized:
            result["sanitized"] = {
                "masks_applied": self.sanitized.masks_count,
            }

        return result


class PipelineError(Exception):
    """Raised when the pipeline encounters a non-recoverable error."""

    def __init__(self, message: str, code: str = "PIPELINE_ERROR", detail: str | None = None) -> None:
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__(self.message)


@dataclass
class PipelineConfig:
    """Configuration for the processing pipeline."""

    stages: list[PipelineStage] = field(
        default_factory=lambda: [
            PipelineStage.PARSE,
            PipelineStage.NER,
            PipelineStage.SANITIZE,
            PipelineStage.EMBED,
        ]
    )
    use_bert_ner: bool = True
    use_rules_ner: bool = True
    embed_max_features: int = 5000
    max_text_length: int = 1_000_000  # 1M chars
    continue_on_error: bool = True  # Continue to next stage if one fails


class DocumentProcessor:
    """Orchestrates document processing: parse → NER → sanitize → embed.

    Usage:
        processor = DocumentProcessor()
        result = await processor.process("/path/to/report.pdf")
        print(result.entities)

    Configurable via PipelineConfig to skip stages or adjust parameters.
    """

    def __init__(self, config: PipelineConfig | None = None) -> None:
        self.config = config or PipelineConfig()
        logger.info(
            "DocumentProcessor initialized",
            stages=[s.value for s in self.config.stages],
        )

    async def process(self, file_path: str | Path) -> ProcessingResult:
        """Process a document through the full pipeline.

        Args:
            file_path: Path to the document file.

        Returns:
            ProcessingResult with all extracted data.

        Raises:
            PipelineError: If the parse stage fails (critical).
        """
        path = Path(file_path).resolve()
        start_time = time.monotonic()

        result = ProcessingResult(
            file_path=str(path),
            file_name=path.name,
        )

        stages = self.config.stages

        # ── Stage 1: Parse ──────────────────────────────────────────
        if PipelineStage.PARSE in stages:
            try:
                logger.info("Pipeline: parsing document", path=path.name)
                parsed = await parse_document(path)
                result.parsed = parsed
                result.text_raw = parsed.text
                result.metadata.update(parsed.metadata)
                result.metadata["file_type"] = parsed.file_type.value
                result.stages_completed.append(PipelineStage.PARSE.value)

                # Truncate if needed
                if len(parsed.text) > self.config.max_text_length:
                    logger.warning(
                        "Text truncated",
                        original_length=len(parsed.text),
                        max_length=self.config.max_text_length,
                    )
                    result.text_raw = parsed.text[: self.config.max_text_length]
            except Exception as e:
                logger.exception("Pipeline: parse failed", path=path.name)
                result.errors.append({
                    "stage": PipelineStage.PARSE.value,
                    "error": str(e),
                })
                if not self.config.continue_on_error:
                    result.processing_time_ms = (time.monotonic() - start_time) * 1000
                    return result

        # Get the text to use for subsequent stages
        work_text = result.text_raw

        # ── Stage 2: Sanitize ───────────────────────────────────────
        if PipelineStage.SANITIZE in stages:
            try:
                logger.info("Pipeline: sanitizing text", path=path.name)
                clean_text = sanitize_file_content(work_text)
                result.text_clean = clean_text
                result.stages_completed.append(PipelineStage.SANITIZE.value)
                # Use clean text for subsequent stages
                work_text = clean_text
            except Exception as e:
                logger.exception("Pipeline: sanitize failed", path=path.name)
                result.errors.append({
                    "stage": PipelineStage.SANITIZE.value,
                    "error": str(e),
                })
                if not self.config.continue_on_error:
                    result.processing_time_ms = (time.monotonic() - start_time) * 1000
                    return result

        # ── Stage 3: NER ────────────────────────────────────────────
        if PipelineStage.NER in stages:
            try:
                logger.info("Pipeline: running NER", path=path.name)
                entities = await extract_entities(
                    work_text,
                    use_bert=self.config.use_bert_ner,
                    use_rules=self.config.use_rules_ner,
                )
                result.entities = entities
                result.stages_completed.append(PipelineStage.NER.value)
                result.metadata["entity_count"] = len(entities.entities)
            except Exception as e:
                logger.exception("Pipeline: NER failed", path=path.name)
                result.errors.append({
                    "stage": PipelineStage.NER.value,
                    "error": str(e),
                })
                if not self.config.continue_on_error:
                    result.processing_time_ms = (time.monotonic() - start_time) * 1000
                    return result

        # ── Stage 4: Embed ──────────────────────────────────────────
        if PipelineStage.EMBED in stages:
            try:
                logger.info("Pipeline: generating embedding", path=path.name)
                embedding = await embed_text(
                    work_text,
                    fit=False,
                    max_features=self.config.embed_max_features,
                )
                result.embedding = embedding
                result.stages_completed.append(PipelineStage.EMBED.value)
            except Exception as e:
                logger.exception("Pipeline: embed failed", path=path.name)
                result.errors.append({
                    "stage": PipelineStage.EMBED.value,
                    "error": str(e),
                })
                if not self.config.continue_on_error:
                    result.processing_time_ms = (time.monotonic() - start_time) * 1000
                    return result

        result.processing_time_ms = (time.monotonic() - start_time) * 1000
        logger.info(
            "Pipeline: processing complete",
            path=path.name,
            time_ms=round(result.processing_time_ms, 1),
            stages=result.stages_completed,
            errors=len(result.errors),
        )

        return result

    async def process_batch(
        self,
        file_paths: list[str | Path],
    ) -> list[ProcessingResult]:
        """Process multiple documents through the pipeline.

        Args:
            file_paths: List of paths to document files.

        Returns:
            List of ProcessingResult, one per input file.
        """
        results: list[ProcessingResult] = []
        for fp in file_paths:
            result = await self.process(fp)
            results.append(result)
        return results


# ── Convenience function ─────────────────────────────────────────────────────


async def process_document(
    file_path: str | Path,
    config: PipelineConfig | None = None,
) -> ProcessingResult:
    """Convenience: process a single document through the pipeline.

    Args:
        file_path: Path to the document file.
        config: Optional pipeline configuration.

    Returns:
        ProcessingResult with extracted data.
    """
    processor = DocumentProcessor(config=config)
    return await processor.process(file_path)
