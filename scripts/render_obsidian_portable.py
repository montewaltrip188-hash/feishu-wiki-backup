#!/usr/bin/env python3
"""Legacy compatibility renderer for old single-file Data URI snapshots.

Older exporter versions kept image bytes in HTML Data URIs. Obsidian's Live
Preview parser can expose very large attributes as raw Base64. This renderer
decodes those old embeds and writes bounded WebP strips; large images may be
resized or encoded lossily. It is not the current archival format: new exports
use shared SHA-256 assets. The source Markdown is never modified.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover - exercised by real installs
    raise SystemExit("缺少 Pillow。请先运行: python -m pip install Pillow") from exc


IMG_RE = re.compile(r"<img\b.*?>", re.IGNORECASE | re.DOTALL)
SRC_RE = re.compile(
    r'\bsrc="data:(?P<mime>image/(?:png|jpeg|gif|webp));base64,'
    r'(?P<payload>[^\"]+)"',
    re.IGNORECASE | re.DOTALL,
)
ALT_RE = re.compile(r'\balt="(?P<alt>[^\"]*)"', re.IGNORECASE | re.DOTALL)
EMBED_ID_RE = re.compile(r'\bdata-feishu-embed-id="(?P<id>[^\"]+)"', re.IGNORECASE)
DEFAULT_MAX_LINE = 4096
TAG_RESERVE = 640
DEFAULT_MAX_WIDTH = 2400
LOSSY_THRESHOLD_BYTES = 500_000


class RenderError(RuntimeError):
    pass


def split_frontmatter(text: str) -> tuple[list[str], str]:
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return [], normalized
    marker = normalized.find("\n---\n", 4)
    if marker < 0:
        raise RenderError("YAML 元字段未闭合")
    return normalized[4:marker].splitlines(), normalized[marker + 5 :]


def set_frontmatter(text: str, fields: dict[str, str]) -> str:
    existing, body = split_frontmatter(text)
    owned = set(fields)
    kept = [line for line in existing if line.split(":", 1)[0].strip() not in owned]
    merged = kept + [f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items()]
    return "---\n" + "\n".join(merged) + "\n---\n" + body.lstrip("\n")


def safe_alt(tag: str, fallback: str) -> str:
    match = ALT_RE.search(tag)
    return html.unescape(match.group("alt")) if match else fallback


def encode_webp(image: Image.Image, *, lossless: bool) -> bytes:
    if image.mode not in {"RGB", "RGBA"}:
        image = image.convert("RGBA" if "transparency" in image.info else "RGB")
    output = BytesIO()
    image.save(
        output,
        format="WEBP",
        lossless=lossless,
        quality=92,
        method=6,
    )
    return output.getvalue()


def normalized_image(data: bytes) -> Image.Image:
    with Image.open(BytesIO(data)) as source:
        if getattr(source, "n_frames", 1) != 1:
            raise RenderError("动画图片不能无损分片，请保留归档版或改用附件模式")
        source.load()
        if source.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
            return source.convert("RGBA")
        return source.copy()


def tile_payloads(image: Image.Image, payload_budget: int, *, lossless: bool) -> list[str]:
    if image.width < 1 or image.height < 1:
        raise RenderError("图片尺寸无效")
    tiles: list[str] = []
    top = 0
    while top < image.height:
        height = min(128, image.height - top)
        selected: str | None = None
        selected_height = 0
        while height >= 1:
            crop = image.crop((0, top, image.width, top + height))
            encoded = base64.b64encode(encode_webp(crop, lossless=lossless)).decode("ascii")
            if len(encoded) <= payload_budget:
                selected = encoded
                selected_height = height
                break
            height //= 2
        if selected is None:
            raise RenderError(
                f"单像素行仍超过内嵌上限；image_width={image.width}, budget={payload_budget}"
            )
        tiles.append(selected)
        top += selected_height
    return tiles


def render_tag_lines(
    tag: str,
    data: bytes,
    mime: str,
    ordinal: int,
    max_line: int,
    max_width: int,
) -> tuple[str, int, bool, bool]:
    label = safe_alt(tag, f"内嵌图片 {ordinal}")
    digest = hashlib.sha256(data).hexdigest()
    embed_match = EMBED_ID_RE.search(tag)
    embed_attr = (
        f' data-feishu-embed-id="{html.escape(embed_match.group("id"), quote=True)}"'
        if embed_match
        else ""
    )
    original_payload = base64.b64encode(data).decode("ascii")
    one_line = (
        f'<img alt="{html.escape(label, quote=True)}"{embed_attr} '
        f'data-feishu-source-sha256="{digest}" data-feishu-part="1/1" '
        f'style="display:block;margin:0;width:100%;height:auto" '
        f'src="data:{mime};base64,{original_payload}">'
    )
    if len(one_line) <= max_line:
        return one_line, 1, False, False

    image = normalized_image(data)
    resized = image.width > max_width
    if resized:
        target_height = max(1, round(image.height * max_width / image.width))
        image = image.resize((max_width, target_height), Image.Resampling.LANCZOS)
    lossless = len(data) <= LOSSY_THRESHOLD_BYTES and not resized
    payloads = tile_payloads(
        image,
        max_line - TAG_RESERVE,
        lossless=lossless,
    )
    lines: list[str] = []
    total = len(payloads)
    for index, payload in enumerate(payloads, 1):
        line = (
            f'<img alt="{html.escape(label, quote=True)} · 分片 {index}/{total}"{embed_attr} '
            f'data-feishu-source-sha256="{digest}" data-feishu-part="{index}/{total}" '
            f'data-feishu-render="{"lossless" if lossless else "optimized"}" '
            f'style="display:block;margin:0;width:100%;height:auto" '
            f'src="data:image/webp;base64,{payload}">'
        )
        if len(line) > max_line:
            raise RenderError(f"分片行仍超过上限: {len(line)}>{max_line}")
        lines.append(line)
    return "\n".join(lines), total, not lossless, resized


def render_markdown(
    text: str,
    max_line: int = DEFAULT_MAX_LINE,
    max_width: int = DEFAULT_MAX_WIDTH,
) -> tuple[str, dict[str, Any]]:
    if max_line < 2048:
        raise RenderError("max_line 不能小于 2048")
    image_count = 0
    tile_count = 0
    decoded_bytes = 0
    optimized_images = 0
    resized_images = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal image_count, tile_count, decoded_bytes, optimized_images, resized_images
        tag = match.group(0)
        src = SRC_RE.search(tag)
        if not src:
            return tag
        payload = re.sub(r"\s+", "", src.group("payload"))
        try:
            data = base64.b64decode(payload, validate=True)
        except Exception as exc:
            raise RenderError(f"第 {image_count + 1} 张图片 Base64 无效") from exc
        image_count += 1
        decoded_bytes += len(data)
        rendered, parts, optimized, resized = render_tag_lines(
            tag,
            data,
            src.group("mime").lower(),
            image_count,
            max_line,
            max_width,
        )
        tile_count += parts
        optimized_images += int(optimized)
        resized_images += int(resized)
        return rendered

    rendered = IMG_RE.sub(replace, text.replace("\r\n", "\n"))
    if rendered.count('src="data:image/') != tile_count:
        raise RenderError("渲染后的 Data URI 计数不一致")
    longest = max((len(line) for line in rendered.splitlines()), default=0)
    if longest > max_line:
        raise RenderError(f"渲染后仍有超长行: {longest}>{max_line}")
    return rendered, {
        "source_images": image_count,
        "rendered_tiles": tile_count,
        "decoded_source_bytes": decoded_bytes,
        "max_line_length": longest,
        "optimized_images": optimized_images,
        "resized_images": resized_images,
    }


def render_file(source: Path, output: Path, max_line: int, max_width: int) -> dict[str, Any]:
    if source.resolve() == output.resolve():
        raise RenderError("拒绝覆盖源文件；请指定独立输出目录")
    if output.exists():
        raise RenderError(f"输出已存在，拒绝覆盖: {output}")
    source_bytes = source.read_bytes()
    source_text = source_bytes.decode("utf-8-sig")
    rendered, stats = render_markdown(
        source_text,
        max_line=max_line,
        max_width=max_width,
    )
    _, body = split_frontmatter(rendered)
    rendered = set_frontmatter(
        rendered,
        {
            "image_embedding": "html-data-uri-tiled",
            "render_status": "legacy",
            "render_profile": "legacy-obsidian-single-file-v3-tiled",
            "rendered_from_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "portable_body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "rendered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)
    return {
        "source": str(source),
        "output": str(output),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "output_bytes": output.stat().st_size,
        **stats,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--max-line", type=int, default=DEFAULT_MAX_LINE)
    parser.add_argument("--max-width", type=int, default=DEFAULT_MAX_WIDTH)
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.report and args.report.exists():
        raise RenderError(f"报告已存在，拒绝覆盖: {args.report}")
    targets = [args.output_dir / source.name for source in args.sources]
    existing = [target for target in targets if target.exists()]
    if existing:
        raise RenderError(f"输出已存在，拒绝覆盖: {existing[0]}")
    args.output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".feishu-obsidian-render-",
        dir=args.output_dir.parent,
    ) as temp_dir:
        staging = Path(temp_dir)
        records = [
            render_file(source, staging / source.name, args.max_line, args.max_width)
            for source in args.sources
        ]
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for record, target in zip(records, targets):
            staged = Path(record["output"])
            shutil.move(str(staged), str(target))
            record["output"] = str(target)
    report = {
        "status": "completed",
        "files": len(records),
        "source_images": sum(item["source_images"] for item in records),
        "rendered_tiles": sum(item["rendered_tiles"] for item in records),
        "items": records,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
