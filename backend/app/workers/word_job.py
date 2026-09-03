from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.services.files import prepare_file


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    content = Path(args.input).read_bytes()
    prepared = prepare_file(content, args.name)
    Path(args.output).write_text(
        json.dumps(
            {
                "filename": prepared.filename,
                "format": prepared.format,
                "text_pages": prepared.text_pages,
                "text": prepared.text,
                "images_b64": None,
                "image_count": len(prepared.images),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    images_dir = Path(args.output).with_suffix(".images")
    if prepared.images:
        images_dir.mkdir(parents=True, exist_ok=True)
        for index, image in enumerate(prepared.images, start=1):
            (images_dir / f"{index:04d}.png").write_bytes(image)


if __name__ == "__main__":
    main()
