from pydantic import BaseModel, Field


class LineDifference(BaseModel):
    line_number: int | None = None
    file1_line: str | None = None
    file2_line: str | None = None


class ComparisonResult(BaseModel):
    identical: bool
    differences: list[LineDifference]


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
