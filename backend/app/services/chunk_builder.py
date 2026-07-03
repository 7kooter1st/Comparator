from dataclasses import dataclass

from app.services.files import ChunkPart, PreparedFile, file_to_chunks


@dataclass
class ChunkBuildResult:
    messages: list[dict]
    chunks1: int
    chunks2: int


def _part_to_dict(part: ChunkPart | None) -> dict | None:
    if part is None:
        return None
    return {
        "filename": part.filename,
        "format": part.format,
        "content_type": part.content_type,
        "content": part.content,
    }


def build_raw_chunk_messages(
    job_id: str,
    prepared1: PreparedFile,
    prepared2: PreparedFile,
) -> ChunkBuildResult:
    """
    Собирает сообщения для топика raw_chunks.
    Каждое сообщение — пара фрагментов двух документов с одним chunk_index.
    """
    chunks1 = file_to_chunks(prepared1)
    chunks2 = file_to_chunks(prepared2)
    total_chunks = max(len(chunks1), len(chunks2))

    messages: list[dict] = []
    for index in range(total_chunks):
        part1 = chunks1[index] if index < len(chunks1) else None
        part2 = chunks2[index] if index < len(chunks2) else None
        messages.append(
            {
                "job_id": job_id,
                "document_id": job_id,
                "chunk_index": index + 1,
                "total_chunks": total_chunks,
                "file1": _part_to_dict(part1),
                "file2": _part_to_dict(part2),
            }
        )

    return ChunkBuildResult(
        messages=messages,
        chunks1=len(chunks1),
        chunks2=len(chunks2),
    )
