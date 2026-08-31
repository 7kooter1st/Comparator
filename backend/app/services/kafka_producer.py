import json
import logging

from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaConnectionError

from app.config import settings
from app.logging_config import format_payload_for_log

logger = logging.getLogger(__name__)

_producer: AIOKafkaProducer | None = None


class KafkaProducerError(Exception):
    pass


def is_kafka_producer_ready() -> bool:
    return _producer is not None


def _chunk_summary(message: dict) -> str:
    parts = []
    for side in ("file1", "file2"):
        part = message.get(side)
        if part is None:
            parts.append(f"{side}=null")
        else:
            content = part.get("content", "")
            parts.append(
                f"{side}({part.get('content_type')}, {len(content)} chars)"
            )
    return ", ".join(parts)


async def start_kafka_producer() -> None:
    global _producer
    if _producer is not None:
        return

    producer = AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        acks="all",
        max_request_size=settings.kafka_max_request_size_bytes,
    )
    try:
        await producer.start()
    except Exception:
        # Do not leave a non-started object behind: publish_raw_chunks used to
        # mistake it for a healthy producer and accept jobs that could not run.
        try:
            await producer.stop()
        except Exception:
            pass
        raise

    _producer = producer
    logger.info(
        "[Kafka] producer подключён: %s | topic=%s | max_request_size=%s",
        settings.kafka_bootstrap_servers,
        settings.kafka_topic_raw_chunks,
        settings.kafka_max_request_size_bytes,
    )


async def stop_kafka_producer() -> None:
    global _producer
    if _producer is None:
        return
    await _producer.stop()
    _producer = None
    logger.info("[Kafka] producer остановлен")


async def publish_raw_chunks(job_id: str, messages: list[dict]) -> None:
    if _producer is None:
        raise KafkaProducerError("Kafka producer не инициализирован")

    key = job_id.encode("utf-8")
    topic = settings.kafka_topic_raw_chunks

    try:
        for message in messages:
            payload_bytes = json.dumps(message, ensure_ascii=False).encode("utf-8")
            chunk_idx = message.get("chunk_index")
            total = message.get("total_chunks")
            logger.info(
                "[Kafka → %s] job_id=%s chunk %s/%s | %s | size=%s bytes",
                topic,
                job_id,
                chunk_idx,
                total,
                _chunk_summary(message),
                len(payload_bytes),
            )
            logger.debug(
                "[Kafka payload] job_id=%s chunk %s/%s: %s",
                job_id,
                chunk_idx,
                total,
                format_payload_for_log(message),
            )
            await _producer.send_and_wait(topic, key=key, value=payload_bytes)
            logger.info(
                "[Kafka ✓] job_id=%s chunk %s/%s опубликован",
                job_id,
                chunk_idx,
                total,
            )
    except KafkaConnectionError as exc:
        logger.error("[Kafka ✗] нет соединения: %s", exc)
        raise KafkaProducerError(f"Нет соединения с Kafka: {exc}") from exc
    except Exception as exc:
        logger.error("[Kafka ✗] ошибка публикации job_id=%s: %s", job_id, exc)
        raise KafkaProducerError(f"Ошибка публикации в Kafka: {exc}") from exc

    logger.info(
        "[Kafka ✓] job_id=%s: всего опубликовано %s сообщений в %s",
        job_id,
        len(messages),
        topic,
    )
