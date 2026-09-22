#!/usr/bin/env python3
"""Convert inline image Data URIs into shared SHA-256 assets without overwriting sources."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote


IMG_RE = re.compile(r"<img\b.*?>", re.IGNORECASE | re.DOTALL)
SRC_RE = re.compile(
    r'\bsrc="data:(?P<mime>image/(?:png|jpeg|gif|webp));base64,'
    r'(?P<payload>[^\"]+)"',
    re.IGNORECASE | re.DOTALL,
)
ALT_RE = re.compile(r'\balt="(?P<alt>[^\"]*)"', re.IGNORECASE | re.DOTALL)
MARKDOWN_DATA_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(data:(?P<mime>image/(?:png|jpeg|gif|webp));base64,"
    r"(?P<payload>[A-Za-z0-9+/=\s]+)\)",
    re.IGNORECASE,
)
FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(?P<marker>`{3,}|~{3,})")
FENCE_CLOSE_RE = re.compile(r"^[ \t]{0,3}(?P<marker>`{3,}|~{3,})[ \t]*\r?\n?\Z")
EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


class MigrationError(RuntimeError):
    pass


def detected_mime(data: bytes, label: str) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise MigrationError(f"不是受支持的 PNG/JPEG/GIF/WebP 图片: {label}")


def decode_payload(mime: str, payload: str, label: str) -> bytes:
    try:
        data = base64.b64decode(re.sub(r"\s+", "", payload), validate=True)
    except Exception as exc:
        raise MigrationError(f"Base64 无法解码: {label}") from exc
    actual = detected_mime(data, label)
    if mime.lower() != actual:
        raise MigrationError(f"MIME 与图片字节不一致: {label}; declared={mime.lower()}, actual={actual}")
    return data


def markdown_alt(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def transform_outside_fences(text: str, transform: Callable[[str], str]) -> str:
    output: list[str] = []
    buffer: list[str] = []
    in_fence: tuple[str, int] | None = None
    for line in text.splitlines(keepends=True):
        if in_fence is None:
            match = FENCE_OPEN_RE.match(line)
            if match:
                if buffer:
                    output.append(transform("".join(buffer)))
                    buffer = []
                in_fence = (match.group("marker")[0], len(match.group("marker")))
                output.append(line)
            else:
                buffer.append(line)
            continue
        output.append(line)
        close = FENCE_CLOSE_RE.match(line)
        if close and close.group("marker")[0] == in_fence[0] and len(close.group("marker")) >= in_fence[1]:
            in_fence = None
    if buffer:
        output.append(transform("".join(buffer)))
    return "".join(output)


class AssetStore:
    def __init__(self, root: Path):
        self.root = root
        self.references = 0
        self.unique: dict[str, dict[str, Any]] = {}

    def add(self, data: bytes, mime: str) -> Path:
        digest = hashlib.sha256(data).hexdigest()
        relative = Path("_assets") / f"{digest}{EXTENSIONS[mime]}"
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != data:
                raise MigrationError(f"哈希附件冲突，拒绝覆盖: {target.name}")
        else:
            target.write_bytes(data)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise MigrationError(f"附件写入后哈希校验失败: {target.name}")
        self.references += 1
        self.unique.setdefault(
            digest,
            {"path": relative.as_posix(), "sha256": digest, "mime_type": mime, "bytes": len(data)},
        )
        return target


def externalize_text(text: str, output_file: Path, store: AssetStore) -> tuple[str, int]:
    converted = 0

    def link_for(data: bytes, mime: str, alt: str) -> str:
        nonlocal converted
        target = store.add(data, mime)
        relative = os.path.relpath(target, output_file.parent).replace("\\", "/")
        converted += 1
        return f"![{markdown_alt(alt)}]({quote(relative, safe='/._~-:')})"

    def transform(section: str) -> str:
        def replace_html(match: re.Match[str]) -> str:
            tag = match.group(0)
            src = SRC_RE.search(tag)
            if not src:
                return tag
            alt_match = ALT_RE.search(tag)
            alt = html.unescape(alt_match.group("alt")) if alt_match else "飞书图片"
            data = decode_payload(src.group("mime"), src.group("payload"), f"第 {converted + 1} 张 HTML 图片")
            return link_for(data, src.group("mime").lower(), alt)

        section = IMG_RE.sub(replace_html, section)

        def replace_markdown(match: re.Match[str]) -> str:
            data = decode_payload(
                match.group("mime"),
                match.group("payload"),
                f"第 {converted + 1} 张 Markdown 图片",
            )
            return link_for(data, match.group("mime").lower(), match.group("alt"))

        return MARKDOWN_DATA_RE.sub(replace_markdown, section)

    rendered = transform_outside_fences(text, transform)
    return rendered, converted


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def migrate_directory(source_dir: Path, output_dir: Path) -> dict[str, Any]:
    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()
    if not source_dir.is_dir():
        raise MigrationError(f"源目录不存在: {source_dir}")
    if output_dir.exists():
        raise MigrationError(f"输出目录已存在，拒绝覆盖: {output_dir}")
    if is_relative_to(output_dir, source_dir) or is_relative_to(source_dir, output_dir):
        raise MigrationError("源目录与输出目录不能互相包含")
    source_files = sorted(
        (item for item in source_dir.rglob("*") if item.is_file() or item.is_symlink()),
        key=lambda item: item.relative_to(source_dir).as_posix(),
    )
    symlinks = [item for item in source_files if item.is_symlink()]
    if symlinks:
        raise MigrationError(f"源快照含符号链接，拒绝跟随到快照外: {symlinks[0]}")
    sources = [item for item in source_files if item.suffix.lower() == ".md"]
    if not sources:
        raise MigrationError(f"源目录没有 Markdown: {source_dir}")
    if source_dir.joinpath("migration-report.json").is_file():
        raise MigrationError("源目录已含 migration-report.json；拒绝覆盖既有迁移回执")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".feishu-assets-", dir=output_dir.parent) as temp_name:
        staging = Path(temp_name) / output_dir.name
        staging.mkdir()
        store = AssetStore(staging)
        copied_sidecars: list[dict[str, Any]] = []
        for source in source_files:
            if source.suffix.lower() == ".md":
                continue
            relative = source.relative_to(source_dir)
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            if hashlib.sha256(target.read_bytes()).hexdigest() != source_hash:
                raise MigrationError(f"侧车文件复制后哈希不一致: {relative.as_posix()}")
            copied_sidecars.append(
                {"path": relative.as_posix(), "sha256": source_hash, "bytes": source.stat().st_size}
            )
        records: list[dict[str, Any]] = []
        for source in sources:
            relative = source.relative_to(source_dir)
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            source_bytes = source.read_bytes()
            text = source_bytes.decode("utf-8-sig")
            rendered, converted = externalize_text(text, target, store)
            with target.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(rendered)
            records.append(
                {
                    "source": str(source),
                    "output": relative.as_posix(),
                    "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                    "output_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "images": converted,
                }
            )

        remaining = []
        missing = []
        for target in staging.rglob("*.md"):
            text = target.read_text(encoding="utf-8")
            for fenced, section in sections(text):
                if not fenced and "data:image/" in section.lower():
                    remaining.append(str(target.relative_to(staging)))
            for match in re.finditer(r"!\[[^\]]*\]\((?P<path>[^)]+_assets/[^)]+)\)", text):
                decoded = unquote(match.group("path"))
                candidate = (target.parent / decoded).resolve()
                if not candidate.is_file():
                    missing.append(f"{target.relative_to(staging)} -> {decoded}")
        if remaining:
            raise MigrationError(f"仍有未转换的 Data URI: {remaining[0]}")
        if missing:
            raise MigrationError(f"存在失效附件链接: {missing[0]}")

        report = {
            "status": "completed",
            "format": "markdown-with-sha256-assets-v1",
            "source_dir": str(source_dir),
            "output_dir": str(output_dir),
            "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "markdown_files": len(records),
            "image_references": store.references,
            "unique_images": len(store.unique),
            "duplicate_copies_saved": store.references - len(store.unique),
            "asset_bytes": sum(item["bytes"] for item in store.unique.values()),
            "copied_sidecar_files": len(copied_sidecars),
            "copied_sidecar_bytes": sum(item["bytes"] for item in copied_sidecars),
            "sidecars": copied_sidecars,
            "assets": sorted(store.unique.values(), key=lambda item: item["path"]),
            "files": records,
        }
        with (staging / "migration-report.json").open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        os.rename(staging, output_dir)
    return report


def sections(text: str) -> list[tuple[bool, str]]:
    result: list[tuple[bool, str]] = []
    buffer: list[str] = []
    in_fence: tuple[str, int] | None = None
    for line in text.splitlines(keepends=True):
        match = FENCE_OPEN_RE.match(line)
        if in_fence is None:
            if match:
                if buffer:
                    result.append((False, "".join(buffer)))
                    buffer = []
                in_fence = (match.group("marker")[0], len(match.group("marker")))
                buffer.append(line)
            else:
                buffer.append(line)
            continue
        buffer.append(line)
        close = FENCE_CLOSE_RE.match(line)
        if close and close.group("marker")[0] == in_fence[0] and len(close.group("marker")) >= in_fence[1]:
            result.append((True, "".join(buffer)))
            buffer = []
            in_fence = None
    if buffer:
        result.append((in_fence is not None, "".join(buffer)))
    return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv or sys.argv[1:])
    try:
        report = migrate_directory(args.source_dir, args.output_dir)
    except MigrationError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
