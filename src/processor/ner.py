"""BERT-based Chinese Named Entity Recognition for environmental reports.

Extracts entities: contacts (PERSON/ORG), addresses (LOC/GPE), processes,
products, and materials from Chinese environmental report text.

Uses the Hugging Face transformers pipeline with bert-base-chinese for
token classification, augmented with rule-based post-processing for
domain-specific entity types.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ── Entity types ──────────────────────────────────────────────────────────────


class EntityType(str, Enum):
    PERSON = "PERSON"  # 人员
    ORG = "ORG"  # 组织
    LOC = "LOC"  # 地点位置
    GPE = "GPE"  # 行政区划
    CONTACT = "CONTACT"  # 联系方式
    ADDRESS = "ADDRESS"  # 地址
    PROCESS = "PROCESS"  # 工艺流程
    PRODUCT = "PRODUCT"  # 产品
    MATERIAL = "MATERIAL"  # 原材料/物料
    DATE = "DATE"  # 日期
    REGULATION = "REGULATION"  # 法规标准
    EMISSION = "EMISSION"  # 排放指标


@dataclass
class Entity:
    """A named entity extracted from text."""

    text: str
    entity_type: EntityType
    start: int
    end: int
    score: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "type": self.entity_type.value,
            "start": self.start,
            "end": self.end,
            "score": round(self.score, 4),
        }


@dataclass
class NERResult:
    """Result of NER extraction."""

    entities: list[Entity] = field(default_factory=list)
    model_name: str = "bert-base-chinese"
    text_length: int = 0

    def get_by_type(self, entity_type: EntityType) -> list[Entity]:
        """Filter entities by type."""
        return [e for e in self.entities if e.entity_type == entity_type]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entities": [e.to_dict() for e in self.entities],
            "model_name": self.model_name,
            "text_length": self.text_length,
            "entity_count": len(self.entities),
        }


class NERError(Exception):
    """Raised when NER processing fails."""

    def __init__(self, message: str, code: str = "NER_ERROR", detail: str | None = None) -> None:
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__(self.message)


# ── BERT aggregation mapping ─────────────────────────────────────────────────

# Map HuggingFace bert-base-chinese default labels + common Chinese NER labels
# to our domain entity types.
_BERT_LABEL_MAP: dict[str, EntityType] = {
    "B-PER": EntityType.PERSON,
    "I-PER": EntityType.PERSON,
    "B-ORG": EntityType.ORG,
    "I-ORG": EntityType.ORG,
    "B-LOC": EntityType.LOC,
    "I-LOC": EntityType.LOC,
    "B-GPE": EntityType.GPE,
    "I-GPE": EntityType.GPE,
    "B-PERSON": EntityType.PERSON,
    "I-PERSON": EntityType.PERSON,
    "B-ORGANIZATION": EntityType.ORG,
    "I-ORGANIZATION": EntityType.ORG,
    "B-LOCATION": EntityType.LOC,
    "I-LOCATION": EntityType.LOC,
}

# ── Rule-based patterns for domain-specific entities ─────────────────────────

# 中文地址模式
_ADDRESS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"(?:[\u4e00-\u9fff]{2,}(?:省|市|自治区|特别行政区|县|区|镇|乡|村|街道|路|街|道|巷|弄|号|楼|层|单元|室|座)){2,}",
        re.UNICODE,
    ),
    re.compile(r"\d{6}(?:[\u4e00-\u9fff]+)?(?:[\dA-Za-z\u4e00-\u9fff\-]+)+", re.UNICODE),
]

# 中文手机号 / 座机
_PHONE_PATTERN = re.compile(
    r"(?:(?:\+?86)?[ -]?1[3-9]\d{9})|(?:\d{3,4}[ -]?\d{7,8})"
)

# Email
_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# 日期模式
_DATE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\d{4}[年/\-]\d{1,2}[月/\-]\d{1,2}[日号]?"),
    re.compile(r"\d{4}\.\d{1,2}\.\d{1,2}"),
]

# 工艺流程关键词
_PROCESS_KEYWORDS = [
    "工艺", "流程", "工序", "生产线", "加工", "处理", "净化",
    "过滤", "沉淀", "曝气", "厌氧", "好氧", "膜生物", "MBR",
    "反渗透", "离子交换", "活性炭", "吸附", "焚烧", "填埋",
    "堆肥", "热解", "气化", "脱硫", "脱硝", "除尘", "VOCs",
    "预处理", "预处理", "生化处理", "深度处理", "污泥处置",
]

# 产品关键词
_PRODUCT_KEYWORDS = [
    "产品", "制品", "材料", "试剂", "催化剂", "添加剂", "制剂",
    "涂料", "油墨", "胶粘剂", "清洗剂", "溶剂", "染料",
]

# 物料/原材料关键词
_MATERIAL_KEYWORDS = [
    "原料", "原材料", "物料", "辅料", "化学品", "药剂", "树脂",
    "膜元件", "滤料", "填料", "催化剂",
]

# 排放指标关键词
_EMISSION_KEYWORDS = [
    "COD", "BOD", "NH3", "氨氮", "总氮", "总磷", "SS", "悬浮物",
    "pH", "色度", "浊度", "重金属", "汞", "镉", "铅", "铬", "砷",
    "SO2", "NOx", "颗粒物", "PM2.5", "PM10", "VOCs",
    "排放浓度", "排放量", "排放限值", "排放标准",
]

# 法规标准
_REGULATION_KEYWORDS = [
    "标准", "规范", "条例", "法规", "办法", "GB", "HJ", "DB",
    "环评", "排污许可", "限期治理", "总量控制",
]


def _aggregate_bert_entities(
    pipeline_output: list[dict[str, Any]],
    text: str,
) -> list[Entity]:
    """Aggregate BIO-tagged BERT output into contiguous entities.

    Args:
        pipeline_output: Raw output from HuggingFace NER pipeline.
        text: Original text string (for span extraction).

    Returns:
        List of aggregated Entity objects.
    """
    entities: list[Entity] = []
    current_tokens: list[str] = []
    current_start = -1
    current_label: str | None = None
    current_scores: list[float] = []

    for token in pipeline_output:
        word = token["word"]
        entity_label = token.get("entity", token.get("entity_group", ""))
        score = token["score"]

        # Handle ## subword tokens (WordPiece)
        if word.startswith("##"):
            if current_tokens:
                current_tokens.append(word[2:])  # type: ignore[arg-type]
                current_scores.append(score)
            continue

        if entity_label and entity_label.startswith("B-"):
            # Finish previous entity
            if current_tokens and current_label:
                entity_type = _BERT_LABEL_MAP.get(current_label)
                if entity_type:
                    span_text = "".join(current_tokens)
                    entities.append(
                        Entity(
                            text=span_text,
                            entity_type=entity_type,
                            start=current_start,
                            end=current_start + len(span_text),
                            score=sum(current_scores) / len(current_scores),
                        )
                    )
            # Start new entity
            current_tokens = [word]
            current_start = token["start"]
            current_label = entity_label
            current_scores = [score]

        elif entity_label and entity_label.startswith("I-") and current_label:
            # Continue current entity
            current_tokens.append(word)
            current_scores.append(score)

        else:
            # O tag or unrecognized — flush
            if current_tokens and current_label:
                entity_type = _BERT_LABEL_MAP.get(current_label)
                if entity_type:
                    span_text = "".join(current_tokens)
                    entities.append(
                        Entity(
                            text=span_text,
                            entity_type=entity_type,
                            start=current_start,
                            end=current_start + len(span_text),
                            score=sum(current_scores) / len(current_scores),
                        )
                    )
            current_tokens = []
            current_label = None
            current_scores = []

    # Flush trailing entity
    if current_tokens and current_label:
        entity_type = _BERT_LABEL_MAP.get(current_label)
        if entity_type:
            span_text = "".join(current_tokens)
            entities.append(
                Entity(
                    text=span_text,
                    entity_type=entity_type,
                    start=current_start,
                    end=current_start + len(span_text),
                    score=sum(current_scores) / len(current_scores),
                )
            )

    return entities


def _extract_rule_based_entities(text: str) -> list[Entity]:
    """Extract domain-specific entities using regex and keyword matching.

    Covers entity types not handled well by general BERT NER:
    CONTACT, ADDRESS, PROCESS, PRODUCT, MATERIAL, DATE, REGULATION, EMISSION.

    Args:
        text: Input text to scan.

    Returns:
        List of Entity objects from rule-based extraction.
    """
    entities: list[Entity] = []

    # Address: regex patterns
    for pattern in _ADDRESS_PATTERNS:
        for match in pattern.finditer(text):
            span = match.group().strip()
            if len(span) >= 6:
                entities.append(
                    Entity(
                        text=span,
                        entity_type=EntityType.ADDRESS,
                        start=match.start(),
                        end=match.end(),
                        score=0.9,
                    )
                )

    # Contact: phone
    for match in _PHONE_PATTERN.finditer(text):
        entities.append(
            Entity(
                text=match.group(),
                entity_type=EntityType.CONTACT,
                start=match.start(),
                end=match.end(),
                score=0.95,
            )
        )

    # Contact: email
    for match in _EMAIL_PATTERN.finditer(text):
        entities.append(
            Entity(
                text=match.group(),
                entity_type=EntityType.CONTACT,
                start=match.start(),
                end=match.end(),
                score=0.95,
            )
        )

    # Date
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            entities.append(
                Entity(
                    text=match.group(),
                    entity_type=EntityType.DATE,
                    start=match.start(),
                    end=match.end(),
                    score=0.9,
                )
            )

    # Keyword-based entities (PROCESS, PRODUCT, MATERIAL, EMISSION, REGULATION)
    def _keyword_scan(
        keywords: list[str], entity_type: EntityType, score: float = 0.8
    ) -> None:
        for kw in keywords:
            for match in re.finditer(re.escape(kw), text):
                # Try to capture surrounding context (up to 3 chars before/after if Chinese)
                start = match.start()
                end = match.end()
                # Extend backward for prefix
                while start > 0 and text[start - 1] in "\u4e00-\u9fff\u3400-\u4dbf" and (match.start() - start) < 5:
                    start -= 1
                # Extend forward for suffix
                while end < len(text) and text[end] in "\u4e00-\u9fff\u3400-\u4dbf" and (end - match.end()) < 5:
                    end += 1
                span = text[start:end]
                entities.append(
                    Entity(
                        text=span,
                        entity_type=entity_type,
                        start=start,
                        end=end,
                        score=score,
                    )
                )

    _keyword_scan(_PROCESS_KEYWORDS, EntityType.PROCESS, 0.75)
    _keyword_scan(_PRODUCT_KEYWORDS, EntityType.PRODUCT, 0.75)
    _keyword_scan(_MATERIAL_KEYWORDS, EntityType.MATERIAL, 0.75)
    _keyword_scan(_EMISSION_KEYWORDS, EntityType.EMISSION, 0.85)
    _keyword_scan(_REGULATION_KEYWORDS, EntityType.REGULATION, 0.8)

    return entities


def _deduplicate_entities(entities: list[Entity]) -> list[Entity]:
    """Remove duplicate entities (same text, same type, overlapping spans).

    Keeps the entity with the highest score.
    """
    if not entities:
        return []

    # Sort by score descending
    sorted_ents = sorted(entities, key=lambda e: e.score, reverse=True)
    kept: list[Entity] = []

    for ent in sorted_ents:
        is_dup = False
        for k in kept:
            if k.entity_type == ent.entity_type and k.text == ent.text:
                # Check overlap
                if ent.start <= k.end and ent.end >= k.start:
                    is_dup = True
                    break
        if not is_dup:
            kept.append(ent)

    # Restore original order (by start position)
    kept.sort(key=lambda e: e.start)
    return kept


# ── Lazy-loaded NER pipeline ──────────────────────────────────────────────────

_ner_pipeline: Any | None = None


def _get_ner_pipeline() -> Any:
    """Lazily load the HuggingFace NER pipeline with bert-base-chinese.

    Returns:
        A HuggingFace pipeline object ready for NER inference.

    Raises:
        NERError: If transformers is not installed or model loading fails.
    """
    global _ner_pipeline
    if _ner_pipeline is not None:
        return _ner_pipeline

    try:
        from transformers import pipeline
    except ImportError:
        raise NERError(
            message="transformers is not installed. Run: pip install transformers torch",
            code="DEPENDENCY_MISSING",
        )

    try:
        logger.info("Loading BERT NER model", model="bert-base-chinese")
        _ner_pipeline = pipeline(
            "ner",
            model="bert-base-chinese",
            aggregation_strategy="simple",
            device=-1,  # CPU by default; set to 0 for GPU
        )
        logger.info("BERT NER model loaded successfully")
    except Exception as e:
        logger.exception("Failed to load BERT NER model")
        raise NERError(
            message=f"Failed to load BERT NER model: {e}",
            code="MODEL_LOAD_ERROR",
            detail=str(e),
        ) from e

    return _ner_pipeline


async def extract_entities(
    text: str,
    use_bert: bool = True,
    use_rules: bool = True,
    max_length: int = 512,
) -> NERResult:
    """Extract named entities from Chinese text.

    Combines BERT-based NER (persons, orgs, locations) with rule-based
    extraction for domain-specific entities (contacts, addresses, processes,
    products, materials, emissions, regulations).

    Args:
        text: Input text to extract entities from.
        use_bert: Enable BERT-based extraction (default True).
        use_rules: Enable rule-based extraction (default True).
        max_length: Maximum token length for BERT input (default 512).

    Returns:
        NERResult with all extracted entities.

    Raises:
        NERError: If NER processing fails.
    """
    if not text or not text.strip():
        return NERResult(text_length=0)

    text_length = len(text)
    all_entities: list[Entity] = []

    # ── BERT-based extraction ──
    if use_bert:
        try:
            nlp = _get_ner_pipeline()

            # Split long text into chunks respecting BERT's 512-token limit
            # Simple character-based chunking (Chinese chars ≈ tokens)
            chunk_size = max_length
            chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]

            for chunk_idx, chunk in enumerate(chunks):
                if not chunk.strip():
                    continue
                try:
                    raw_output = nlp(chunk)
                    offset = chunk_idx * chunk_size
                    # Adjust token offsets
                    for token in raw_output:
                        token["start"] += offset
                        token["end"] += offset
                    bert_entities = _aggregate_bert_entities(raw_output, text)
                    all_entities.extend(bert_entities)
                except Exception as e:
                    logger.warning(
                        "BERT NER chunk failed",
                        chunk_idx=chunk_idx,
                        error=str(e),
                    )

            logger.debug(
                "BERT NER complete",
                bert_entities=len([e for e in all_entities if e.entity_type in {EntityType.PERSON, EntityType.ORG, EntityType.LOC, EntityType.GPE}]),
            )
        except NERError:
            raise
        except Exception as e:
            logger.exception("BERT NER processing failed")
            raise NERError(
                message=f"BERT NER failed: {e}",
                code="BERT_ERROR",
                detail=str(e),
            ) from e

    # ── Rule-based extraction ──
    if use_rules:
        try:
            rule_entities = _extract_rule_based_entities(text)
            all_entities.extend(rule_entities)
            logger.debug("Rule-based NER complete", rule_entities=len(rule_entities))
        except Exception as e:
            logger.warning("Rule-based NER failed", error=str(e))

    # ── Deduplicate ──
    all_entities = _deduplicate_entities(all_entities)

    return NERResult(
        entities=all_entities,
        model_name="bert-base-chinese" if use_bert else "rule-only",
        text_length=text_length,
    )


def extract_entities_sync(
    text: str,
    use_bert: bool = True,
    use_rules: bool = True,
    max_length: int = 512,
) -> NERResult:
    """Synchronous wrapper for extract_entities.

    Args:
        text: Input text.
        use_bert: Enable BERT NER.
        use_rules: Enable rule-based NER.
        max_length: BERT max token length.

    Returns:
        NERResult with extracted entities.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    return loop.run_until_complete(
        extract_entities(text, use_bert=use_bert, use_rules=use_rules, max_length=max_length)
    )
