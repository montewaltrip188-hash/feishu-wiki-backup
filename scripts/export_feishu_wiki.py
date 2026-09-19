#!/usr/bin/env python3
"""Read-only Feishu Wiki subtree backup using the official @larksuite/cli.

The script deliberately contains no Feishu API implementation and no credentials.
It inventories Wiki nodes, fetches each unique docx once, localizes supported
media, and creates a new immutable snapshot with a JSONL manifest.
"""

from __future__ import annotations

import argparse
import base64
import html
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, urlparse


WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
TAG_RE = re.compile(
    r"<(?P<tag>img|image|source|whiteboard)\b(?P<attrs>[^>]*)/?>\s*(?:</(?P=tag)>)?",
    re.IGNORECASE,
)
ATTR_RE = re.compile(r'(?P<key>[A-Za-z_][\w-]*)\s*=\s*"(?P<value>[^"]*)"')
FEISHU_FILE_MD_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(https://[^)\s]+/file/(?P<token>[A-Za-z0-9_-]+)[^)]*\)",
    re.IGNORECASE,
)
DATA_URI_REFERENCE_DEFINITION_RE = re.compile(
    r"(?m)^[ \t]{0,3}\[(?P<id>feishu-img-[A-Za-z0-9._-]+)\]:[ \t]*"
    r"(?P<uri>data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+)[ \t]*\r?$\n?",
    re.IGNORECASE,
)
DATA_URI_REFERENCE_IMAGE_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\[(?P<id>feishu-img-[A-Za-z0-9._-]+)\]",
    re.IGNORECASE,
)
UNRESOLVED_MEDIA_RE = re.compile(
    r"<(?:image|source|whiteboard)\b|"
    r"<img\b(?![^>]*\bsrc\s*=\s*[\"']data:image/)|"
    r"https://[^)\s]+/file/",
    re.IGNORECASE,
)
HTML5_BLOCK_RE = re.compile(
    r"<html5-block\b(?P<attrs>[^>]*)/?>\s*(?:</html5-block>)?",
    re.IGNORECASE,
)
CALLOUT_BLOCK_RE = re.compile(
    r"(?P<indent>^[ \t]*)<callout\b(?P<attrs>[^>]*)>(?P<body>.*?)</callout>[ \t]*(?=\r?$)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
TITLE_BLOCK_RE = re.compile(r"<title\b[^>]*>(?P<body>.*?)</title>", re.IGNORECASE | re.DOTALL)
CITE_TAG_RE = re.compile(r"<cite\b(?P<attrs>[^>]*)>\s*</cite>", re.IGNORECASE)
READONLY_BLOCK_RE = re.compile(
    r"<readonly-block\b(?P<attrs>[^>]*)/?>\s*(?:</readonly-block>)?",
    re.IGNORECASE,
)
SYNCED_REFERENCE_RE = re.compile(
    r"<synced_reference\b(?P<attrs>[^>]*)/?>\s*(?:</synced_reference>)?",
    re.IGNORECASE,
)
GRID_BLOCK_RE = re.compile(
    r"<grid\b[^>]*>(?P<body>.*?)</grid>",
    re.IGNORECASE | re.DOTALL,
)
COLUMN_BLOCK_RE = re.compile(
    r"<column\b[^>]*>(?P<body>.*?)</column>",
    re.IGNORECASE | re.DOTALL,
)
SYNCED_SOURCE_RE = re.compile(r"</?synced-source\b[^>]*>", re.IGNORECASE)
CHAT_CARD_RE = re.compile(r"<chat_card\b(?P<attrs>[^>]*)/?>\s*(?:</chat_card>)?", re.IGNORECASE)
BOOKMARK_RE = re.compile(r"<bookmark\b(?P<attrs>[^>]*)/?>\s*(?:</bookmark>)?", re.IGNORECASE)
ANCHOR_RE = re.compile(
    r'<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
BLOCKQUOTE_RE = re.compile(
    r"(?P<indent>^[ \t]*)<blockquote\b[^>]*>(?P<body>.*?)</blockquote>[ \t]*(?=\r?$)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
UNRESOLVED_FEISHU_MARKUP_RE = re.compile(
    r"</?(?:title|callout|grid|column|readonly-block|synced_reference|synced-source|"
    r"chat_card|bookmark)\b",
    re.IGNORECASE,
)
FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(?P<marker>`{3,}|~{3,})")
FENCE_CLOSE_RE = re.compile(r"^[ \t]{0,3}(?P<marker>`{3,}|~{3,})[ \t]*\r?\n?\Z")
REQUIRED_PROFILE = "codex-bot"
IMAGE_MODES = {"inline", "files"}
INLINE_DATA_WRAP_WIDTH = 120


class BackupError(RuntimeError):
    """Expected operational error that should be shown without a traceback."""


class LarkCommandError(BackupError):
    def __init__(self, message: str, command: Sequence[str], stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.command = list(command)
        self.stdout = stdout
        self.stderr = stderr


def now_rfc3339() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_mime_type(data: bytes, path: Path) -> str:
    """Allow only image Data URI types accepted by common Markdown renderers."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise BackupError(
        f"下载结果不是支持内嵌的 PNG/JPEG/GIF/WebP 图片，拒绝静默降级: {path.name}"
    )


def encode_image_data_uri(path: Path) -> Tuple[str, Dict[str, Any]]:
    data = path.read_bytes()
    mime_type = image_mime_type(data, path)
    encoded = base64.b64encode(data).decode("ascii")
    decoded = base64.b64decode(encoded, validate=True)
    if decoded != data:
        raise BackupError(f"Base64 回读与原图不一致: {path.name}")
    if image_mime_type(decoded, path) != mime_type:
        raise BackupError(f"Base64 回读 MIME 与原图不一致: {path.name}")
    data_uri = f"data:{mime_type};base64,{encoded}"
    return (
        data_uri,
        {
            "mime_type": mime_type,
            "source_bytes": len(data),
            "base64_characters": len(encoded),
            "data_uri_characters": len(data_uri),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )


def decode_image_data_uri(data_uri: str, label: str) -> bytes:
    match = re.fullmatch(
        r"data:(?P<mime>image/(?:png|jpeg|gif|webp));base64,(?P<payload>[A-Za-z0-9+/=]+)",
        data_uri,
        re.IGNORECASE,
    )
    if not match:
        raise BackupError(f"内嵌图片 Data URI 格式无效: {label}")
    try:
        decoded = base64.b64decode(match.group("payload"), validate=True)
    except Exception as exc:
        raise BackupError(f"内嵌图片 Base64 无法解码: {label}: {exc}") from exc
    detected_mime = image_mime_type(decoded, Path(label))
    if match.group("mime").lower() != detected_mime:
        raise BackupError(
            f"内嵌图片 MIME 与字节签名不一致: {label}; "
            f"declared={match.group('mime').lower()}, detected={detected_mime}"
        )
    return decoded


def write_text_utf8(path: Path, value: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)


def yaml_scalar(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def safe_name(value: str, fallback: str = "untitled", max_length: int = 80) -> str:
    name = INVALID_FILENAME.sub("_", (value or "").strip())
    name = re.sub(r"\s+", " ", name).rstrip(". ")
    if not name:
        name = fallback
    if name.upper() in WINDOWS_RESERVED:
        name = f"_{name}"
    if len(name) > max_length:
        name = name[:max_length].rstrip(". ") or fallback
    return name


def parse_json_output(stdout: str) -> Dict[str, Any]:
    text = stdout.strip().lstrip("\ufeff")
    if not text:
        raise BackupError("lark-cli 没有返回 JSON")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise BackupError("无法解析 lark-cli 输出为 JSON")
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise BackupError(f"无法解析 lark-cli 输出为 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise BackupError("lark-cli JSON 顶层不是对象")
    return value


def resolve_lark_command(explicit: Optional[str] = None) -> List[str]:
    if explicit:
        resolved = shutil.which(explicit) or explicit
        return [resolved]
    direct = shutil.which("lark-cli")
    if direct:
        return [direct]
    npx_name = "npx.cmd" if os.name == "nt" else "npx"
    npx = shutil.which(npx_name)
    if not npx:
        raise BackupError("未找到 lark-cli 或 npx；请先安装官方 @larksuite/cli")
    return [npx, "-y", "@larksuite/cli"]


class LarkCLI:
    def __init__(
        self,
        profile: str,
        identity: str,
        executable: Optional[str] = None,
    ) -> None:
        if profile != REQUIRED_PROFILE:
            raise BackupError(f"为保护飞书身份隔离，--profile 必须是 {REQUIRED_PROFILE}")
        if identity not in {"user", "bot"}:
            raise BackupError("identity 只能是 user 或 bot")
        self.profile = profile
        self.identity = identity
        self.base_command = resolve_lark_command(executable)

    def run(self, args: Sequence[str], cwd: Optional[Path] = None) -> Dict[str, Any]:
        command = [
            *self.base_command,
            *args,
            "--profile",
            self.profile,
            "--as",
            self.identity,
            "--format",
            "json",
        ]
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        parsed: Optional[Dict[str, Any]] = None
        for candidate in (completed.stdout, completed.stderr):
            if not candidate.strip():
                continue
            try:
                parsed = parse_json_output(candidate)
                break
            except BackupError:
                parsed = None
        if completed.returncode != 0:
            detail = self._error_detail(parsed)
            raise LarkCommandError(
                f"lark-cli 退出码 {completed.returncode}: {detail}" if detail else f"lark-cli 退出码 {completed.returncode}",
                command,
                completed.stdout,
                completed.stderr,
            )
        payload = parsed or parse_json_output(completed.stdout)
        if payload.get("ok") is False:
            detail = self._error_detail(payload) or "lark-cli 返回失败"
            raise LarkCommandError(detail, command, completed.stdout, completed.stderr)
        return payload

    @staticmethod
    def _error_detail(payload: Optional[Mapping[str, Any]]) -> str:
        if not payload:
            return ""
        error = payload.get("error")
        if isinstance(error, Mapping):
            parts = [str(error.get("message") or error.get("type") or "错误")]
            hint = error.get("hint")
            if hint:
                parts.append(f"hint: {hint}")
            scopes = error.get("missing_scopes") or error.get("required_scope")
            if scopes:
                parts.append(f"scope: {scopes}")
            return "; ".join(parts)
        if error:
            return str(error)
        if payload.get("message"):
            return str(payload["message"])
        return ""

    def node_get(self, token_or_url: str) -> Mapping[str, Any]:
        payload = self.run(["wiki", "+node-get", "--node-token", token_or_url])
        return payload.get("data") or payload

    def node_list(self, space_id: str, parent_node_token: str) -> List[Mapping[str, Any]]:
        payload = self.run(
            [
                "wiki",
                "+node-list",
                "--space-id",
                str(space_id),
                "--parent-node-token",
                parent_node_token,
                "--page-all",
                "--page-limit",
                "0",
            ]
        )
        data = payload.get("data") or {}
        nodes = data.get("nodes") or []
        if not isinstance(nodes, list):
            raise BackupError("wiki +node-list 返回的 nodes 不是数组")
        return nodes

    def fetch_doc(self, token: str, cwd: Path) -> Mapping[str, Any]:
        payload = self.run(
            [
                "docs",
                "+fetch",
                "--doc",
                token,
                "--doc-format",
                "markdown",
                "--detail",
                "full",
            ],
            cwd=cwd,
        )
        data = payload.get("data") or {}
        document = data.get("document") or {}
        if not isinstance(document, dict) or "content" not in document:
            raise BackupError("docs +fetch 未返回 document.content")
        return document

    def download_media(self, token: str, media_type: str, output_prefix: Path, cwd: Path) -> Path:
        cwd = cwd.resolve()
        output_prefix = output_prefix.resolve()
        try:
            output_arg = output_prefix.relative_to(cwd)
        except ValueError as exc:
            raise BackupError(f"媒体输出必须位于当前 staging 内: {output_prefix}") from exc
        output_prefix.parent.mkdir(parents=True, exist_ok=True)
        if media_type == "whiteboard":
            args = [
                "whiteboard",
                "+export",
                "--whiteboard-token",
                token,
                "--output-type",
                "preview",
                "--output",
                str(output_arg),
            ]
        else:
            args = [
                "docs",
                "+media-download",
                "--token",
                token,
                "--output",
                str(output_arg),
            ]
        before = {p.resolve() for p in output_prefix.parent.glob(f"{output_prefix.name}*")}
        self.run(args, cwd=cwd)
        after = [
            p
            for p in output_prefix.parent.glob(f"{output_prefix.name}*")
            if p.is_file() and p.resolve() not in before
        ]
        if not after and output_prefix.is_file():
            after = [output_prefix]
        if not after:
            existing = [p for p in output_prefix.parent.glob(f"{output_prefix.name}*") if p.is_file()]
            if existing:
                after = existing
        if not after:
            raise BackupError(f"媒体命令成功但未找到下载文件: {token}")
        return sorted(after, key=lambda p: p.stat().st_mtime_ns, reverse=True)[0]


@dataclass
class WikiNode:
    space_id: str
    node_token: str
    obj_token: str
    obj_type: str
    title: str
    parent_node_token: str = ""
    node_type: str = "origin"
    origin_node_token: str = ""
    has_child: bool = False
    depth: int = 0
    path_titles: List[str] = field(default_factory=list)
    local_segments: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def object_key(self) -> Tuple[str, str]:
        return self.obj_type, self.obj_token


def node_from_mapping(data: Mapping[str, Any], depth: int = 0) -> WikiNode:
    return WikiNode(
        space_id=str(data.get("space_id") or ""),
        node_token=str(data.get("node_token") or data.get("token") or ""),
        obj_token=str(data.get("obj_token") or ""),
        obj_type=str(data.get("obj_type") or ""),
        title=str(data.get("title") or "未命名文档"),
        parent_node_token=str(data.get("parent_node_token") or ""),
        node_type=str(data.get("node_type") or "origin"),
        origin_node_token=str(data.get("origin_node_token") or ""),
        has_child=bool(data.get("has_child")),
        depth=depth,
        raw=dict(data),
    )


def assign_local_paths(nodes: List[WikiNode], root_token: str) -> None:
    by_token = {node.node_token: node for node in nodes}
    children: Dict[str, List[WikiNode]] = {}
    for node in nodes:
        children.setdefault(node.parent_node_token, []).append(node)

    segment_by_token: Dict[str, str] = {}
    for parent, siblings in children.items():
        buckets: Dict[str, List[WikiNode]] = {}
        for node in siblings:
            buckets.setdefault(safe_name(node.title).casefold(), []).append(node)
        for bucket in buckets.values():
            duplicate = len(bucket) > 1
            for node in bucket:
                base = safe_name(node.title)
                segment_by_token[node.node_token] = (
                    f"{base}--{safe_name(node.node_token[:8], 'node')}" if duplicate else base
                )

    root = by_token[root_token]
    segment_by_token[root.node_token] = safe_name(root.title)

    def build(node: WikiNode, seen: Optional[set[str]] = None) -> Tuple[List[str], List[str]]:
        seen = set(seen or set())
        if node.node_token in seen:
            raise BackupError(f"Wiki 节点层级出现循环: {node.node_token}")
        seen.add(node.node_token)
        if node.node_token == root_token or not node.parent_node_token:
            return [node.title], [segment_by_token[node.node_token]]
        parent = by_token.get(node.parent_node_token)
        if not parent:
            return [root.title, node.title], [segment_by_token[root_token], segment_by_token[node.node_token]]
        title_path, local_path = build(parent, seen)
        return title_path + [node.title], local_path + [segment_by_token[node.node_token]]

    for node in nodes:
        node.path_titles, node.local_segments = build(node)


def inventory_wiki(client: LarkCLI, root_url: str, max_depth: int = -1) -> Tuple[WikiNode, List[WikiNode]]:
    root_data = client.node_get(root_url)
    root = node_from_mapping(root_data, depth=0)
    if not root.node_token or not root.space_id:
        raise BackupError("无法从根链接解析 node_token / space_id")
    nodes = [root]
    queue = [root]
    seen = {root.node_token}
    while queue:
        parent = queue.pop(0)
        if not parent.has_child:
            continue
        if max_depth >= 0 and parent.depth >= max_depth:
            continue
        for raw_child in client.node_list(parent.space_id, parent.node_token):
            child = node_from_mapping(raw_child, depth=parent.depth + 1)
            if not child.node_token or child.node_token in seen:
                continue
            if not child.parent_node_token:
                child.parent_node_token = parent.node_token
            nodes.append(child)
            seen.add(child.node_token)
            if child.has_child:
                queue.append(child)
    assign_local_paths(nodes, root.node_token)
    return root, nodes


def group_by_object(nodes: Iterable[WikiNode]) -> Dict[Tuple[str, str], List[WikiNode]]:
    groups: Dict[Tuple[str, str], List[WikiNode]] = {}
    for node in nodes:
        groups.setdefault(node.object_key, []).append(node)
    return groups


def choose_canonical(group: Sequence[WikiNode]) -> WikiNode:
    return sorted(
        group,
        key=lambda node: (
            0 if node.node_type == "origin" else 1,
            node.depth,
            "/".join(node.local_segments).casefold(),
            node.node_token,
        ),
    )[0]


def document_relative_path(node: WikiNode) -> Path:
    parts = [safe_name(part) for part in node.local_segments]
    if node.has_child:
        return Path(*parts, "_index.md")
    return Path(*parts[:-1], f"{parts[-1]}.md")


def shortcut_relative_path(node: WikiNode) -> Path:
    parts = [safe_name(part) for part in node.local_segments]
    if node.has_child:
        return Path(*parts, "_shortcut.md")
    return Path(*parts[:-1], f"{parts[-1]}.shortcut.md")


def ensure_path_budget(base: Path, relative: Path, max_chars: int = 240) -> Path:
    candidate = (base / relative).resolve()
    if os.name == "nt" and len(str(candidate)) > max_chars:
        raise BackupError(
            f"Windows 路径过长（{len(str(candidate))}>{max_chars}），请缩短输出根目录或飞书标题: {relative}"
        )
    return candidate


def markdown_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def markdown_link_path(value: str) -> str:
    return quote(value.replace("\\", "/"), safe="/._~-:")


def extract_attrs(attrs: str) -> Dict[str, str]:
    return {match.group("key").lower(): match.group("value") for match in ATTR_RE.finditer(attrs)}


@dataclass(frozen=True)
class MediaRef:
    token: str
    kind: str
    name: str = ""


@dataclass(frozen=True)
class InlineImage:
    embed_id: str
    data_uri: str


def render_inline_image(label: str, image: InlineImage) -> str:
    header, separator, payload = image.data_uri.partition(",")
    if not separator or not header.startswith("data:image/"):
        raise BackupError(f"无效的图片 Data URI: {image.embed_id}")
    wrapped = "\n".join(
        payload[index : index + INLINE_DATA_WRAP_WIDTH]
        for index in range(0, len(payload), INLINE_DATA_WRAP_WIDTH)
    )
    return (
        f'<img alt="{html.escape(label, quote=True)}" '
        f'data-feishu-embed-id="{html.escape(image.embed_id, quote=True)}" '
        f'src="{header},\n{wrapped}">'
    )


def migrate_reference_data_uris(content: str) -> str:
    """Upgrade legacy reference-style Data URIs to wrapped inline HTML images."""
    definitions: Dict[str, str] = {}
    for is_fenced, section in markdown_sections(content):
        if is_fenced:
            continue
        for match in DATA_URI_REFERENCE_DEFINITION_RE.finditer(section):
            embed_id = match.group("id")
            data_uri = match.group("uri")
            decoded = decode_image_data_uri(data_uri, embed_id)
            expected_id = f"feishu-img-{hashlib.sha256(decoded).hexdigest()[:24]}"
            if embed_id != expected_id:
                raise BackupError(
                    f"旧版内嵌图片 ID 与内容哈希不一致: {embed_id}; expected={expected_id}"
                )
            known = definitions.get(embed_id)
            if known is not None and known != data_uri:
                raise BackupError(f"旧版内嵌图片定义冲突: {embed_id}")
            definitions[embed_id] = data_uri
    if not definitions:
        return content

    uses: Dict[str, int] = {embed_id: 0 for embed_id in definitions}

    def transform(section: str) -> str:
        def replace_image(match: re.Match[str]) -> str:
            embed_id = match.group("id")
            data_uri = definitions.get(embed_id)
            if data_uri is None:
                return match.group(0)
            uses[embed_id] += 1
            return render_inline_image(
                match.group("alt"),
                InlineImage(embed_id=embed_id, data_uri=data_uri),
            )

        migrated = DATA_URI_REFERENCE_IMAGE_RE.sub(replace_image, section)
        migrated = DATA_URI_REFERENCE_DEFINITION_RE.sub("", migrated)
        return migrated

    rewritten = transform_outside_fences(content, transform)
    unused = sorted(embed_id for embed_id, count in uses.items() if count == 0)
    if unused:
        raise BackupError(f"旧版内嵌图片定义没有正文引用: {', '.join(unused)}")
    return re.sub(r"\n{3,}", "\n\n", rewritten)


def markdown_sections(content: str) -> List[Tuple[bool, str]]:
    """Split Markdown into (is_fenced_code, text) sections."""
    sections: List[Tuple[bool, str]] = []
    buffer: List[str] = []
    in_fence: Optional[Tuple[str, int]] = None
    for line in content.splitlines(keepends=True):
        match = FENCE_OPEN_RE.match(line)
        if in_fence is None:
            if match:
                if buffer:
                    sections.append((False, "".join(buffer)))
                    buffer = []
                in_fence = (match.group("marker")[0], len(match.group("marker")))
                buffer.append(line)
            else:
                buffer.append(line)
            continue
        buffer.append(line)
        close_match = FENCE_CLOSE_RE.match(line)
        if close_match:
            marker = close_match.group("marker")
            if marker[0] == in_fence[0] and len(marker) >= in_fence[1]:
                sections.append((True, "".join(buffer)))
                buffer = []
                in_fence = None
    if buffer:
        sections.append((in_fence is not None, "".join(buffer)))
    return sections


def transform_outside_fences(content: str, transform: Any) -> str:
    return "".join(text if is_fenced else transform(text) for is_fenced, text in markdown_sections(content))


def normalize_feishu_inline_markup(value: str) -> str:
    """Convert simple Feishu pseudo-HTML to portable Markdown."""

    def replace_cite(match: re.Match[str]) -> str:
        attrs = extract_attrs(match.group("attrs"))
        return attrs.get("title") or attrs.get("doc-id") or "飞书引用"

    def replace_anchor(match: re.Match[str]) -> str:
        attrs = extract_attrs(match.group("attrs"))
        href = html.unescape(attrs.get("href", "")).strip()
        label = match.group("body").strip() or href or "链接"
        return f"[{label}](<{href}>)" if href else label

    value = CITE_TAG_RE.sub(replace_cite, value)
    value = ANCHOR_RE.sub(replace_anchor, value)
    value = re.sub(r"<br\b[^>]*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<p\b[^>]*>", "", value, flags=re.IGNORECASE)
    value = re.sub(r"</p>", "\n\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<(?:b|strong)\b[^>]*>", "**", value, flags=re.IGNORECASE)
    value = re.sub(r"</(?:b|strong)>", "**", value, flags=re.IGNORECASE)
    value = re.sub(r"<mark\b[^>]*>", "==", value, flags=re.IGNORECASE)
    value = re.sub(r"</mark>", "==", value, flags=re.IGNORECASE)
    value = re.sub(r"<(?:i|em)\b[^>]*>", "*", value, flags=re.IGNORECASE)
    value = re.sub(r"</(?:i|em)>", "*", value, flags=re.IGNORECASE)
    value = re.sub(r"</?(?:ul|ol)\b[^>]*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<li\b[^>]*>", "- ", value, flags=re.IGNORECASE)
    value = re.sub(r"</li>", "\n", value, flags=re.IGNORECASE)
    return html.unescape(value)


def markdown_quote_block(indent: str, header: str, body: str) -> str:
    normalized = normalize_feishu_inline_markup(textwrap.dedent(body)).strip()
    lines = [f"{indent}> {header}".rstrip()]
    for line in normalized.splitlines():
        lines.append(f"{indent}> {line}".rstrip() if line else f"{indent}>")
    return "\n" + "\n".join(lines) + "\n"


def normalize_feishu_markup(content: str) -> str:
    """Flatten Feishu layout tags without touching fenced code examples."""

    outside_fences = "".join(
        section for is_fenced, section in markdown_sections(content) if not is_fenced
    )
    balanced_layout_tags = {
        tag
        for tag in ("grid", "column")
        if len(re.findall(rf"<{tag}\b[^>]*>", outside_fences, flags=re.IGNORECASE))
        == len(re.findall(rf"</{tag}\s*>", outside_fences, flags=re.IGNORECASE))
        > 0
    }

    def transform(section: str) -> str:
        def replace_callout(match: re.Match[str]) -> str:
            attrs = extract_attrs(match.group("attrs"))
            emoji = attrs.get("emoji", "").strip()
            header = f"[!note] {emoji}" if emoji else "[!note]"
            return markdown_quote_block(match.group("indent") or "", header, match.group("body"))

        def replace_blockquote(match: re.Match[str]) -> str:
            return markdown_quote_block(match.group("indent") or "", "", match.group("body"))

        def replace_title(match: re.Match[str]) -> str:
            title = normalize_feishu_inline_markup(match.group("body")).strip()
            return f"# {title}" if title else ""

        def replace_readonly(match: re.Match[str]) -> str:
            attrs = extract_attrs(match.group("attrs"))
            href = html.unescape(attrs.get("href", "")).strip()
            if not href:
                block_type = attrs.get("type", "unknown")
                return f"<!-- 飞书不可移植嵌入块 type={block_type}；源导出未提供可用 URL -->"
            return f"[飞书嵌入内容](<{href}>)"

        def replace_synced_reference(match: re.Match[str]) -> str:
            attrs = extract_attrs(match.group("attrs"))
            token = attrs.get("src-token", "")
            block_id = attrs.get("src-block-id", "")
            return f"<!-- 飞书同步块引用 src-token={token} src-block-id={block_id} -->"

        def replace_chat_card(match: re.Match[str]) -> str:
            attrs = extract_attrs(match.group("attrs"))
            name = attrs.get("name") or "飞书群聊"
            chat_id = attrs.get("chat-id", "")
            suffix = f" <!-- chat-id={chat_id} -->" if chat_id else ""
            return f"**{name}**{suffix}"

        def replace_bookmark(match: re.Match[str]) -> str:
            attrs = extract_attrs(match.group("attrs"))
            href = html.unescape(attrs.get("href") or attrs.get("url") or "").strip()
            title = attrs.get("title") or attrs.get("name") or href or "书签"
            return f"[{title}](<{href}>)" if href else match.group(0)

        def replace_grid(match: re.Match[str]) -> str:
            body = match.group("body")
            columns = list(COLUMN_BLOCK_RE.finditer(body))
            if not columns:
                return match.group(0)
            cursor = 0
            parts: List[str] = []
            for column in columns:
                between = body[cursor : column.start()]
                if between.strip():
                    return match.group(0)
                parts.append(column.group("body").strip())
                cursor = column.end()
            if body[cursor:].strip():
                return match.group(0)
            return "\n\n".join(part for part in parts if part)

        section = CALLOUT_BLOCK_RE.sub(replace_callout, section)
        section = BLOCKQUOTE_RE.sub(replace_blockquote, section)
        section = TITLE_BLOCK_RE.sub(replace_title, section)
        section = READONLY_BLOCK_RE.sub(replace_readonly, section)
        section = SYNCED_REFERENCE_RE.sub(replace_synced_reference, section)
        section = CHAT_CARD_RE.sub(replace_chat_card, section)
        section = BOOKMARK_RE.sub(replace_bookmark, section)
        section = SYNCED_SOURCE_RE.sub("\n", section)
        section = GRID_BLOCK_RE.sub(replace_grid, section)
        # A grid may wrap fenced code blocks. transform_outside_fences deliberately
        # splits at fences, so the enclosing regex cannot see the full grid in that
        # case. Removing only the layout wrappers preserves column content order.
        if balanced_layout_tags:
            fallback_tags = "|".join(sorted(balanced_layout_tags))
            section = re.sub(
                rf"</?(?:{fallback_tags})\b[^>]*>",
                "\n",
                section,
                flags=re.IGNORECASE,
            )
        section = normalize_feishu_inline_markup(section)
        section = re.sub(r"\n{3,}", "\n\n", section)
        return section

    return transform_outside_fences(content, transform)


def count_unresolved_feishu_markup(content: str) -> int:
    return sum(
        len(UNRESOLVED_FEISHU_MARKUP_RE.findall(section))
        for is_fenced, section in markdown_sections(content)
        if not is_fenced
    )


def collect_media_refs(content: str) -> List[MediaRef]:
    refs: Dict[Tuple[str, str], MediaRef] = {}
    for is_fenced, section in markdown_sections(content):
        if is_fenced:
            continue
        for match in TAG_RE.finditer(section):
            tag = match.group("tag").lower()
            attrs = extract_attrs(match.group("attrs"))
            token = attrs.get("token") or attrs.get("file-token") or attrs.get("file_token")
            if not token:
                continue
            kind = "whiteboard" if tag == "whiteboard" else ("file" if tag == "source" else "image")
            refs[(kind, token)] = MediaRef(token=token, kind=kind, name=attrs.get("name", ""))
        for match in FEISHU_FILE_MD_RE.finditer(section):
            token = match.group("token")
            refs[("image", token)] = MediaRef(token=token, kind="image", name=match.group("alt"))
    return list(refs.values())


def media_ref_occurrences(content: str) -> Dict[Tuple[str, str], int]:
    counts: Dict[Tuple[str, str], int] = {}
    for is_fenced, section in markdown_sections(content):
        if is_fenced:
            continue
        for match in TAG_RE.finditer(section):
            tag = match.group("tag").lower()
            attrs = extract_attrs(match.group("attrs"))
            token = attrs.get("token") or attrs.get("file-token") or attrs.get("file_token")
            if not token:
                continue
            kind = "whiteboard" if tag == "whiteboard" else ("file" if tag == "source" else "image")
            key = (kind, token)
            counts[key] = counts.get(key, 0) + 1
        for match in FEISHU_FILE_MD_RE.finditer(section):
            key = ("image", match.group("token"))
            counts[key] = counts.get(key, 0) + 1
    return counts


def rewrite_media(
    content: str,
    paths: Mapping[Tuple[str, str], str],
    inline_images: Optional[Mapping[Tuple[str, str], InlineImage]] = None,
) -> str:
    inline_images = inline_images or {}

    def transform(section: str) -> str:
        def replace_tag(match: re.Match[str]) -> str:
            tag = match.group("tag").lower()
            attrs = extract_attrs(match.group("attrs"))
            token = attrs.get("token") or attrs.get("file-token") or attrs.get("file_token")
            if not token:
                return match.group(0)
            kind = "whiteboard" if tag == "whiteboard" else ("file" if tag == "source" else "image")
            inline_image = inline_images.get((kind, token))
            path = paths.get((kind, token))
            if not path and not inline_image:
                return match.group(0)
            if kind == "file":
                label = attrs.get("name") or f"附件-{token[:8]}"
                return f"[{label}]({path})"
            label = "飞书画板" if kind == "whiteboard" else (attrs.get("alt") or "飞书图片")
            if inline_image:
                return render_inline_image(label, inline_image)
            return f"![{label}]({path})"

        rewritten = TAG_RE.sub(replace_tag, section)

        def replace_url(match: re.Match[str]) -> str:
            token = match.group("token")
            inline_image = inline_images.get(("image", token))
            if inline_image:
                return render_inline_image(match.group("alt"), inline_image)
            path = paths.get(("image", token))
            return f"![{match.group('alt')}]({path})" if path else match.group(0)

        return FEISHU_FILE_MD_RE.sub(replace_url, rewritten)

    return transform_outside_fences(content, transform)


def count_unresolved_media(content: str) -> int:
    return sum(
        len(UNRESOLVED_MEDIA_RE.findall(section))
        for is_fenced, section in markdown_sections(content)
        if not is_fenced
    )


def html5_references(document: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    reference_map = document.get("reference_map")
    if not isinstance(reference_map, Mapping):
        return {}
    group = reference_map.get("html5-block") or reference_map.get("html5_block")
    if not isinstance(group, Mapping):
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for ref, value in group.items():
        if not isinstance(value, Mapping):
            raise BackupError(f"HTML5 reference_map 条目不是对象: {ref}")
        if isinstance(value.get("path"), str):
            raw_path = value["path"].lstrip("@")
            posix_path = PurePosixPath(raw_path)
            if posix_path.is_absolute() or ".." in posix_path.parts:
                raise BackupError(f"HTML5 sidecar 路径不安全: {value['path']}")
            if not posix_path.parts or posix_path.parts[0] != "doc-fetch-resources":
                raise BackupError(f"HTML5 sidecar 路径不在 doc-fetch-resources 下: {value['path']}")
            result[str(ref)] = {"kind": "path", "path": posix_path}
            continue
        if isinstance(value.get("data"), str):
            result[str(ref)] = {"kind": "data", "data": value["data"]}
            continue
        raise BackupError(f"HTML5 reference_map 条目既没有 path 也没有 data: {ref}")
    return result


def rewrite_html5_blocks(content: str, paths: Mapping[str, str]) -> str:
    def transform(section: str) -> str:
        def replace(match: re.Match[str]) -> str:
            attrs = extract_attrs(match.group("attrs"))
            ref = attrs.get("data-ref") or attrs.get("ref")
            path = paths.get(ref or "")
            if not path:
                return match.group(0)
            return f"[飞书 HTML5 交互资源]({path})"

        return HTML5_BLOCK_RE.sub(replace, section)

    return transform_outside_fences(content, transform)


def count_unresolved_html5(content: str) -> int:
    return sum(
        len(HTML5_BLOCK_RE.findall(section))
        for is_fenced, section in markdown_sections(content)
        if not is_fenced
    )


def source_url_for(root_url: str, node_token: str) -> str:
    parsed = urlparse(root_url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/wiki/{node_token}"
    return root_url


def frontmatter_for(
    root_url: str,
    node: WikiNode,
    revision_id: Any,
    fetched_at: str,
    content_hash: str,
    snapshot_id: str,
) -> str:
    fields = [
        ("title", node.title),
        ("type", "raw"),
        ("source", source_url_for(root_url, node.node_token)),
        ("source_kind", "feishu-wiki"),
        ("space_id", node.space_id),
        ("node_token", node.node_token),
        ("obj_token", node.obj_token),
        ("obj_type", node.obj_type),
        ("node_type", node.node_type),
        ("revision_id", revision_id),
        ("source_path", " / ".join(node.path_titles)),
        ("snapshot_id", snapshot_id),
        ("fetched_at", fetched_at),
        ("source_content_sha256", content_hash),
    ]
    lines = ["---", *(f"{key}: {yaml_scalar(value)}" for key, value in fields), "---", ""]
    return "\n".join(lines)


def write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def plan_summary(root: WikiNode, nodes: Sequence[WikiNode]) -> Dict[str, Any]:
    groups = group_by_object(nodes)
    return {
        "status": "planned",
        "root_title": root.title,
        "space_id": root.space_id,
        "root_node_token": root.node_token,
        "nodes": len(nodes),
        "unique_objects": len(groups),
        "docx_objects": sum(1 for key in groups if key[0] == "docx"),
        "shortcuts": sum(1 for node in nodes if node.node_type == "shortcut"),
        "unsupported_objects": sorted({key[0] for key in groups if key[0] != "docx"}),
        "max_depth_seen": max((node.depth for node in nodes), default=0),
        "items": [
            {
                "title": node.title,
                "node_token": node.node_token,
                "obj_type": node.obj_type,
                "obj_token": node.obj_token,
                "node_type": node.node_type,
                "path": " / ".join(node.path_titles),
            }
            for node in nodes
        ],
    }


def build_manifest_base(
    node: WikiNode,
    root_url: str,
    snapshot_id: str,
    fetched_at: str,
) -> Dict[str, Any]:
    data = {key: value for key, value in asdict(node).items() if key != "raw"}
    data["source_url"] = source_url_for(root_url, node.node_token)
    data["source_path"] = " / ".join(node.path_titles)
    data["snapshot_id"] = snapshot_id
    data["fetched_at"] = fetched_at
    return data


def verify_report_text(report: Mapping[str, Any]) -> str:
    failures = list(report.get("failures") or [])
    lines = [
        "# 飞书 Wiki 备份验收报告",
        "",
        f"- 状态：`{report.get('status')}`",
        f"- 根节点：{report.get('root_title')}",
        f"- 快照：`{report.get('snapshot_id')}`",
        f"- 图片模式：`{report.get('image_mode')}`",
        f"- 节点数：{report.get('nodes')}",
        f"- 唯一对象数：{report.get('unique_objects')}",
        f"- Docx 对象：{report.get('docx_objects')}（本次处理 {report.get('selected_docx_objects')}）",
        f"- 快捷方式：{report.get('shortcuts')}",
        f"- 不支持对象：{report.get('unsupported_objects')}",
        f"- 内嵌视觉资源：{report.get('embedded_visuals', 0)}",
        f"- 内嵌原图字节：{report.get('embedded_image_source_bytes', 0)}",
        f"- Base64 字符：{report.get('embedded_base64_characters', 0)}",
        f"- 最大 Markdown 字节：{report.get('max_markdown_bytes', 0)}",
        f"- 图片已自包含的正文：{report.get('images_self_contained_docs', 0)}",
        f"- 无附件/HTML5 依赖的单文件正文：{report.get('standalone_markdown_docs', 0)}",
        f"- 失败项：{report.get('failed')}",
        "",
    ]
    large_markdown_files = list(report.get("large_markdown_files") or [])
    if large_markdown_files:
        lines.extend(["## 体积警告", ""])
        for item in large_markdown_files:
            lines.append(f"- `{item.get('path')}`：{item.get('bytes')} 字节（≥ 2 MiB）")
        lines.extend(
            [
                "",
                "> Base64 内嵌会增大 Markdown；转发便携性已提高，但目标阅读器的编辑性能仍需抽检。",
                "",
            ]
        )
    if failures:
        lines.extend(["## 失败与未覆盖", ""])
        for item in failures:
            lines.append(
                f"- `{item.get('scope')}` `{item.get('obj_token', item.get('token', ''))}`：{item.get('error')}"
            )
        lines.append("")
    if report.get("sample"):
        lines.extend(["> 这是 `--limit` 生成的小样快照，不代表完整知识库备份。", ""])
    return "\n".join(lines)


def export_snapshot(
    client: LarkCLI,
    root_url: str,
    output_dir: Path,
    snapshot_id: str,
    max_depth: int = -1,
    limit: Optional[int] = None,
    image_mode: str = "inline",
) -> Tuple[Path, Dict[str, Any]]:
    if image_mode not in IMAGE_MODES:
        raise BackupError(f"不支持的图片模式: {image_mode}")
    root, nodes = inventory_wiki(client, root_url, max_depth=max_depth)
    resolved_output = output_dir.resolve()
    root_name = safe_name(root.title)
    root_dir = resolved_output / root_name
    target = root_dir / safe_name(snapshot_id, "snapshot")
    if target.exists():
        raise BackupError(f"目标快照已存在，拒绝覆盖: {target}")
    # Keep failed/incomplete work outside raw. Only a verified snapshot is moved
    # into output_dir, so an interrupted run cannot masquerade as source data.
    staging_parent = resolved_output.parent / ".feishu-wiki-staging" / root_name
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = staging_parent / f".{safe_name(snapshot_id, 'snapshot')}.partial-{uuid.uuid4().hex[:8]}"
    if staging.exists():
        raise BackupError(f"临时目录意外已存在: {staging}")
    (staging / "documents").mkdir(parents=True)
    (staging / "metadata").mkdir(parents=True)
    work_root = staging / ".work"
    work_root.mkdir()

    groups = group_by_object(nodes)
    canonical_by_key = {key: choose_canonical(group) for key, group in groups.items()}
    docx_keys = sorted(
        (key for key in groups if key[0] == "docx"),
        key=lambda key: (
            canonical_by_key[key].depth,
            "/".join(canonical_by_key[key].local_segments).casefold(),
        ),
    )
    selected_keys = set(docx_keys[:limit] if limit is not None else docx_keys)
    records: List[Dict[str, Any]] = []
    object_results: Dict[Tuple[str, str], Dict[str, Any]] = {}
    failures: List[Dict[str, Any]] = []
    fetched_at = now_rfc3339()
    sample_mode = limit is not None and limit < len(docx_keys)
    running_report: Dict[str, Any] = {
        "status": "running",
        "root_title": root.title,
        "snapshot_id": snapshot_id,
        "image_mode": image_mode,
        "fetched_at": fetched_at,
        "nodes": len(nodes),
        "unique_objects": len(groups),
        "docx_objects": len(docx_keys),
        "selected_docx_objects": len(selected_keys),
        "shortcuts": sum(1 for node in nodes if node.node_type == "shortcut"),
        "unsupported_objects": sum(1 for key in groups if key[0] != "docx"),
        "embedded_visuals": 0,
        "embedded_image_source_bytes": 0,
        "embedded_base64_characters": 0,
        "max_markdown_bytes": 0,
        "images_self_contained_docs": 0,
        "standalone_markdown_docs": 0,
        "large_markdown_files": [],
        "failed": 0,
        "failures": [],
        "sample": sample_mode,
    }
    initial_records = []
    for node in nodes:
        record = build_manifest_base(node, root_url, snapshot_id, fetched_at)
        record["status"] = "queued"
        initial_records.append(record)
    write_jsonl(staging / "manifest.jsonl", initial_records)
    write_text_utf8(staging / "verify-report.md", verify_report_text(running_report))

    for key in docx_keys:
        canonical = canonical_by_key[key]
        if key not in selected_keys:
            object_results[key] = {"status": "not_exported_sample"}
            continue
        relative_doc = document_relative_path(canonical)
        markdown_path = ensure_path_budget(staging / "documents", relative_doc)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        doc_work = work_root / safe_name(canonical.obj_token, "doc")
        doc_work.mkdir(parents=True, exist_ok=True)
        try:
            document = client.fetch_doc(canonical.obj_token, doc_work)
            content = str(document.get("content") or "")
            revision_id = document.get("revision_id")
            if revision_id in (None, ""):
                raise BackupError("docs +fetch 未返回 revision_id，拒绝把未知版本写成有效快照")
            content_hash = sha256_text(content)
            metadata_path = staging / "metadata" / f"{safe_name(canonical.obj_token, 'doc')}.json"
            metadata_payload = {key: value for key, value in document.items() if key != "content"}
            media_paths: Dict[Tuple[str, str], str] = {}
            inline_images: Dict[Tuple[str, str], InlineImage] = {}
            inline_definitions: Dict[str, str] = {}
            expected_inline_uses: Dict[str, int] = {}
            occurrence_counts = media_ref_occurrences(content)
            media_records: List[Dict[str, Any]] = []
            for media in collect_media_refs(content):
                category = (
                    "whiteboard"
                    if media.kind == "whiteboard"
                    else ("attachments" if media.kind == "file" else "media")
                )
                embed_image = image_mode == "inline" and media.kind in {"image", "whiteboard"}
                media_root = work_root / "embedded-media" if embed_image else staging / "assets"
                prefix = media_root / category / safe_name(media.token, "asset")
                try:
                    existing = [p for p in prefix.parent.glob(f"{prefix.name}*") if p.is_file()]
                    downloaded = existing[0] if existing else client.download_media(
                        media.token,
                        media.kind,
                        prefix,
                        staging,
                    )
                    if embed_image:
                        data_uri, image_metadata = encode_image_data_uri(downloaded)
                        embed_id = f"feishu-img-{image_metadata['sha256'][:24]}"
                        known_uri = inline_definitions.get(embed_id)
                        if known_uri is not None and known_uri != data_uri:
                            raise BackupError(f"内嵌图片 ID 冲突: {embed_id}")
                        inline_definitions[embed_id] = data_uri
                        media_key = (media.kind, media.token)
                        inline_images[media_key] = InlineImage(embed_id=embed_id, data_uri=data_uri)
                        occurrences = occurrence_counts.get(media_key, 0)
                        expected_inline_uses[embed_id] = expected_inline_uses.get(embed_id, 0) + occurrences
                        media_records.append(
                            {
                                "token": media.token,
                                "kind": media.kind,
                                "storage": "html-inline-data-uri",
                                "status": "embedded",
                                "embed_id": embed_id,
                                "occurrences": occurrences,
                                **(
                                    {
                                        "representation": "preview",
                                        "editable_whiteboard_backed_up": False,
                                    }
                                    if media.kind == "whiteboard"
                                    else {}
                                ),
                                **image_metadata,
                            }
                        )
                    else:
                        rel_from_doc = os.path.relpath(downloaded, markdown_path.parent).replace("\\", "/")
                        media_paths[(media.kind, media.token)] = rel_from_doc
                        media_records.append(
                            {
                                "token": media.token,
                                "kind": media.kind,
                                "storage": "asset-file",
                                "path": downloaded.relative_to(staging).as_posix(),
                                "sha256": sha256_file(downloaded),
                                "status": "downloaded",
                            }
                        )
                except Exception as exc:  # keep per-asset failure visible
                    media_records.append(
                        {"token": media.token, "kind": media.kind, "status": "failed", "error": str(exc)}
                    )
                    failures.append(
                        {
                            "scope": "media",
                            "obj_token": canonical.obj_token,
                            "token": media.token,
                            "error": str(exc),
                        }
                    )
            # Normalize Feishu pseudo-HTML before generating our own <img> tags.
            # normalize_feishu_markup unescapes source entities; running it after
            # media rendering would turn an escaped `>` in alt text back into a
            # literal tag terminator and make an otherwise embedded image appear
            # unresolved.
            rewritten = normalize_feishu_markup(content)
            rewritten = rewrite_media(rewritten, media_paths, inline_images)
            inline_validation_failed = False
            for embed_id, expected_uses in expected_inline_uses.items():
                actual_uses = sum(
                    section.count(f'data-feishu-embed-id="{embed_id}"')
                    for is_fenced, section in markdown_sections(rewritten)
                    if not is_fenced
                )
                if actual_uses != expected_uses:
                    inline_validation_failed = True
                    failures.append(
                        {
                            "scope": "inline-image",
                            "obj_token": canonical.obj_token,
                            "error": (
                                f"内嵌图片引用数不一致: {embed_id}; "
                                f"expected={expected_uses}, actual={actual_uses}"
                            ),
                        }
                    )
            sidecar_source = doc_work / "doc-fetch-resources"
            expected_html5 = html5_references(document)
            sidecar_records: List[Dict[str, Any]] = []
            html5_links: Dict[str, str] = {}
            sidecar_target = staging / "assets" / "html5" / safe_name(canonical.obj_token, "doc")
            if sidecar_source.exists():
                shutil.copytree(sidecar_source, sidecar_target, dirs_exist_ok=False)
                localized_prefix = os.path.relpath(sidecar_target, markdown_path.parent).replace("\\", "/") + "/"
                rewritten = transform_outside_fences(
                    rewritten,
                    lambda section: section.replace("doc-fetch-resources/", localized_prefix),
                )
            for ref, spec in expected_html5.items():
                if spec["kind"] == "path":
                    relative_source = spec["path"]
                    source_file = doc_work.joinpath(*relative_source.parts)
                    target_file = sidecar_target / Path(*relative_source.parts[1:])
                    source_label = relative_source.as_posix()
                    if not source_file.is_file() or not target_file.is_file():
                        error = f"HTML5 sidecar 缺失: {source_label}"
                        sidecar_records.append(
                            {"ref": ref, "source_path": source_label, "status": "failed", "error": error}
                        )
                        failures.append(
                            {"scope": "html5", "obj_token": canonical.obj_token, "ref": ref, "error": error}
                        )
                        continue
                else:
                    sidecar_target.mkdir(parents=True, exist_ok=True)
                    target_file = sidecar_target / f"{safe_name(ref, 'html5')}.html"
                    write_text_utf8(target_file, str(spec["data"]))
                    source_label = "inline:data"
                rel_from_doc = os.path.relpath(target_file, markdown_path.parent).replace("\\", "/")
                html5_links[ref] = markdown_link_path(rel_from_doc)
                sidecar_records.append(
                    {
                        "ref": ref,
                        "source_path": source_label,
                        "path": target_file.relative_to(staging).as_posix(),
                        "sha256": sha256_file(target_file),
                        "status": "copied",
                    }
                )
            rewritten = rewrite_html5_blocks(rewritten, html5_links)
            remaining_html5 = count_unresolved_html5(rewritten)
            if remaining_html5:
                failures.append(
                    {
                        "scope": "html5",
                        "obj_token": canonical.obj_token,
                        "error": f"仍有 {remaining_html5} 个未本地化 HTML5 引用",
                    }
                )
            remaining_feishu_markup = count_unresolved_feishu_markup(rewritten)
            if remaining_feishu_markup:
                failures.append(
                    {
                        "scope": "document-markup",
                        "obj_token": canonical.obj_token,
                        "error": f"仍有 {remaining_feishu_markup} 个未转换的飞书布局标签",
                    }
                )
            metadata_payload["localized_html5"] = sidecar_records
            write_text_utf8(
                metadata_path,
                json.dumps(metadata_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )

            body = frontmatter_for(
                root_url,
                canonical,
                revision_id,
                fetched_at,
                content_hash,
                snapshot_id,
            ) + rewritten.rstrip() + "\n"
            write_text_utf8(markdown_path, body)
            remaining_media = count_unresolved_media(rewritten)
            if remaining_media:
                failures.append(
                    {
                        "scope": "document",
                        "obj_token": canonical.obj_token,
                        "error": f"仍有 {remaining_media} 个未本地化媒体引用",
                    }
                )
            failed_visual_media = any(
                item.get("kind") in {"image", "whiteboard"} and item.get("status") != "embedded"
                for item in media_records
            )
            images_self_contained = (
                image_mode == "inline"
                and not failed_visual_media
                and not inline_validation_failed
                and remaining_media == 0
            )
            standalone_markdown = (
                images_self_contained
                and not any(item.get("kind") == "file" for item in media_records)
                and not expected_html5
                and remaining_html5 == 0
                and remaining_feishu_markup == 0
            )
            object_results[key] = {
                "status": (
                    "written"
                    if not remaining_media
                    and not remaining_html5
                    and not remaining_feishu_markup
                    and not inline_validation_failed
                    else "written_with_errors"
                ),
                "canonical_node_token": canonical.node_token,
                "document_path": (Path("documents") / relative_doc).as_posix(),
                "revision_id": revision_id,
                "source_content_sha256": content_hash,
                "file_sha256": sha256_file(markdown_path),
                "markdown_bytes": markdown_path.stat().st_size,
                "metadata_path": metadata_path.relative_to(staging).as_posix(),
                "media": media_records,
                "html5_sidecars": sidecar_records,
                "images_self_contained": images_self_contained,
                "standalone_markdown": standalone_markdown,
                "remaining_media_refs": remaining_media,
                "remaining_html5_refs": remaining_html5,
                "remaining_feishu_markup_refs": remaining_feishu_markup,
            }
        except Exception as exc:
            message = str(exc)
            failures.append({"scope": "document", "obj_token": canonical.obj_token, "error": message})
            object_results[key] = {
                "status": "failed",
                "canonical_node_token": canonical.node_token,
                "error": message,
            }

    unsupported_keys = [key for key in groups if key[0] != "docx"]
    for key in unsupported_keys:
        object_results[key] = {"status": "unsupported", "obj_type": key[0]}
        failures.append({"scope": "object", "obj_token": key[1], "error": f"unsupported obj_type: {key[0]}"})

    for node in nodes:
        key = node.object_key
        canonical = canonical_by_key[key]
        result = dict(object_results.get(key, {"status": "unknown"}))
        record = build_manifest_base(node, root_url, snapshot_id, fetched_at)
        record.update(result)
        if node.node_token != canonical.node_token:
            record["status"] = "shortcut" if result.get("status") not in {"failed", "unsupported"} else result.get("status")
            record["canonical_node_token"] = canonical.node_token
            canonical_result = object_results.get(key, {})
            record["canonical_document_path"] = canonical_result.get("document_path")
            if canonical_result.get("document_path"):
                alias_path = ensure_path_budget(staging / "documents", shortcut_relative_path(node))
                alias_path.parent.mkdir(parents=True, exist_ok=True)
                link_target = os.path.relpath(
                    staging / str(canonical_result["document_path"]),
                    alias_path.parent,
                ).replace("\\", "/")
                alias_body = "\n".join(
                    [
                        "---",
                        f"title: {yaml_scalar(node.title)}",
                        'type: "feishu-shortcut"',
                        f"source: {yaml_scalar(source_url_for(root_url, node.node_token))}",
                        f"node_token: {yaml_scalar(node.node_token)}",
                        f"obj_token: {yaml_scalar(node.obj_token)}",
                        f"canonical_node_token: {yaml_scalar(canonical.node_token)}",
                        "---",
                        "",
                        f"此节点是飞书快捷方式。规范正文：[{markdown_label(canonical.title)}]({markdown_link_path(link_target)})",
                        "",
                    ]
                )
                write_text_utf8(alias_path, alias_body)
                record["shortcut_path"] = alias_path.relative_to(staging).as_posix()
        records.append(record)

    write_jsonl(staging / "manifest.jsonl", records)
    exported_results = [
        result
        for key, result in object_results.items()
        if key in selected_keys and result.get("status") in {"written", "written_with_errors"}
    ]
    embedded_visuals: List[Mapping[str, Any]] = []
    for result in exported_results:
        seen_embed_ids = set()
        for item in result.get("media") or []:
            embed_id = item.get("embed_id")
            if item.get("status") == "embedded" and embed_id and embed_id not in seen_embed_ids:
                embedded_visuals.append(item)
                seen_embed_ids.add(embed_id)
    large_markdown_files = [
        {"path": result.get("document_path"), "bytes": result.get("markdown_bytes")}
        for result in exported_results
        if int(result.get("markdown_bytes") or 0) >= 2 * 1024 * 1024
    ]
    report = {
        "status": "failed" if failures else "prepared",
        "root_title": root.title,
        "snapshot_id": snapshot_id,
        "image_mode": image_mode,
        "fetched_at": fetched_at,
        "nodes": len(nodes),
        "unique_objects": len(groups),
        "docx_objects": len(docx_keys),
        "selected_docx_objects": len(selected_keys),
        "shortcuts": sum(1 for node in nodes if node.node_type == "shortcut"),
        "unsupported_objects": len(unsupported_keys),
        "embedded_visuals": len(embedded_visuals),
        "embedded_image_source_bytes": sum(int(item.get("source_bytes") or 0) for item in embedded_visuals),
        "embedded_base64_characters": sum(
            int(item.get("base64_characters") or 0) for item in embedded_visuals
        ),
        "max_markdown_bytes": max(
            (int(result.get("markdown_bytes") or 0) for result in exported_results),
            default=0,
        ),
        "images_self_contained_docs": sum(
            1 for result in exported_results if result.get("images_self_contained") is True
        ),
        "standalone_markdown_docs": sum(
            1 for result in exported_results if result.get("standalone_markdown") is True
        ),
        "large_markdown_files": large_markdown_files,
        "failed": len(failures),
        "failures": failures,
        "sample": sample_mode,
    }
    write_text_utf8(staging / "verify-report.md", verify_report_text(report))

    if failures:
        report["partial_path"] = str(staging)
        return staging, report

    shutil.rmtree(work_root)
    root_dir.mkdir(parents=True, exist_ok=True)
    if target.exists():
        report["status"] = "finalize_failed"
        report["failed"] = 1
        report["failures"] = [{"scope": "finalize", "error": f"目标在运行期间出现，拒绝覆盖: {target}"}]
        write_text_utf8(staging / "verify-report.md", verify_report_text(report))
        report["partial_path"] = str(staging)
        return staging, report
    # Persist the final success receipt while still outside raw. The single
    # directory rename below is the commit point: raw never receives a
    # snapshot whose own report says running/prepared.
    report["status"] = "sample" if sample_mode else "completed"
    report["finalized_at"] = now_rfc3339()
    write_text_utf8(staging / "verify-report.md", verify_report_text(report))
    try:
        os.rename(staging, target)
    except OSError as exc:
        report["status"] = "finalize_failed"
        report.pop("finalized_at", None)
        report["failed"] = 1
        report["failures"] = [{"scope": "finalize", "error": str(exc)}]
        write_text_utf8(staging / "verify-report.md", verify_report_text(report))
        report["partial_path"] = str(staging)
        return staging, report
    return target, report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只读批量备份飞书 Wiki 为 Markdown 快照")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--url", required=True, help="飞书 Wiki 根节点 URL")
        sub.add_argument(
            "--profile",
            choices=[REQUIRED_PROFILE],
            default=REQUIRED_PROFILE,
            help="固定的 lark-cli 隔离 profile：codex-bot",
        )
        sub.add_argument("--identity", choices=["user", "bot"], default="user", help="显式身份，默认 user")
        sub.add_argument("--max-depth", type=int, default=-1, help="递归深度；-1 表示不限")
        sub.add_argument("--lark-cli", help="可选：lark-cli 可执行文件路径")

    plan = subparsers.add_parser("plan", help="只读清点，不创建快照")
    add_common(plan)

    export = subparsers.add_parser("export", help="创建新的 Markdown 快照")
    add_common(export)
    export.add_argument("--output-dir", required=True, type=Path, help="快照根目录，例如 vault/raw")
    export.add_argument("--snapshot-id", help="快照 ID；默认 YYYYMMDD-HHMMSS")
    export.add_argument("--limit", type=int, help="仅导出前 N 个唯一 Docx，用于 POC")
    export.add_argument(
        "--image-mode",
        choices=sorted(IMAGE_MODES),
        default="inline",
        help="图片与白板预览的保存方式：inline 内嵌到 Markdown（默认），files 保存为外部文件",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Windows PowerShell 5.1 often exposes a GBK console. Wiki titles may contain
    # emoji, so make the JSON command contract UTF-8 regardless of host locale.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 9):
            raise BackupError("需要 Python 3.9 或更高版本")
        if args.max_depth < -1:
            raise BackupError("--max-depth 只能是 -1 或非负整数")
        if getattr(args, "limit", None) is not None and args.limit < 1:
            raise BackupError("--limit 必须大于 0")
        client = LarkCLI(args.profile, args.identity, args.lark_cli)
        if args.command == "plan":
            root, nodes = inventory_wiki(client, args.url, max_depth=args.max_depth)
            print(json.dumps(plan_summary(root, nodes), ensure_ascii=False, indent=2))
            return 0
        snapshot_id = args.snapshot_id or datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        path, report = export_snapshot(
            client,
            args.url,
            args.output_dir,
            snapshot_id,
            max_depth=args.max_depth,
            limit=args.limit,
            image_mode=args.image_mode,
        )
        print(json.dumps({**report, "path": str(path)}, ensure_ascii=False, indent=2))
        return 0 if report["status"] in {"completed", "sample"} else 2
    except BackupError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted"}, ensure_ascii=False), file=sys.stderr)
        return 130
    except Exception as exc:
        print(
            json.dumps(
                {"status": "blocked", "error": str(exc), "error_type": type(exc).__name__},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
