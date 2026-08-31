from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class DifferenceCategory(str, Enum):
    SUBSTANTIVE = "substantive"
    TECHNICAL = "technical"
    ALIGNMENT_ERROR = "alignment_error"
    OCR_UNCERTAIN = "ocr_uncertain"


class ComparisonVerdict(str, Enum):
    IDENTICAL = "identical"
    CONTENT_EQUAL = "content_equal"
    DIFFERENT = "different"


class LineDifference(BaseModel):
    candidate_id: str | None = None
    line_number: int | None = None
    file1_line: str | None = None
    file2_line: str | None = None
    file1_span: list[int] | None = None
    file2_span: list[int] | None = None
    category: DifferenceCategory = DifferenceCategory.SUBSTANTIVE
    technical_type: str | None = None
    reason: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    protection_tags: list[str] = Field(default_factory=list)
    file1_page: int | None = Field(default=None, ge=1)
    file2_page: int | None = Field(default=None, ge=1)
    file1_block: int | None = Field(default=None, ge=1)
    file2_block: int | None = Field(default=None, ge=1)
    file1_source_type: str | None = None
    file2_source_type: str | None = None


class ComparisonResult(BaseModel):
    identical: bool
    verdict: ComparisonVerdict
    differences: list[LineDifference]

    @model_validator(mode="before")
    @classmethod
    def default_legacy_verdict(cls, value: Any) -> Any:
        if isinstance(value, dict) and "verdict" not in value:
            value = dict(value)
            value["verdict"] = (
                ComparisonVerdict.IDENTICAL
                if value.get("identical") and not value.get("differences")
                else ComparisonVerdict.DIFFERENT
            )
        return value


class FileChunkStats(BaseModel):
    filename: str
    format: str
    chunks: int


class CompareResponse(BaseModel):
    job_id: str = Field(description="ID задачи сравнения (ключ Kafka)")
    status: str = Field(description="Статус постановки задачи", examples=["queued"])
    total_chunks: int = Field(description="Общее число сообщений в raw_chunks")
    kafka_topic: str = Field(description="Топик Kafka с сырыми чанками")
    websocket_url: str = Field(description="WebSocket для прогресса и результата")
    file1: FileChunkStats
    file2: FileChunkStats


class WebSocketEvent(BaseModel):
    type: str = Field(description="status | result | error")
    job_id: str
    data: dict


class ResultRequest(BaseModel):
    ollama: dict = Field(description="Ответ Ollama от Consumer/Aggregator")


class ResultResponse(BaseModel):
    comparison: ComparisonResult = Field(description="Распарсенный JSON от модели")


class ErrorResponse(BaseModel):
    error: str
    hint: str | None = None
