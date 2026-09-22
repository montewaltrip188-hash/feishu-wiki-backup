#!/usr/bin/env python3
"""Immutable Minutes transcript export through the official Lark CLI."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from export_feishu_wiki import BackupError, LarkCLI, now_rfc3339, safe_name


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_new(path, data):
    """Publish a complete file without ever replacing an existing raw file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".minutes-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(name, str(path))
    finally:
        Path(name).unlink(missing_ok=True)


def validate_request(request):
    for key in ("meeting_id", "minute_token", "title", "meeting_time", "source_url"):
        if not isinstance(request.get(key), str) or not request[key].strip():
            raise BackupError("Missing string: " + key)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", request["minute_token"]):
        raise BackupError("Invalid minute_token")
    kind = request.get("source_type", "feishu_minutes_transcript")
    if kind not in ("feishu_minutes_transcript", "feishu_docx_transcript"):
        raise BackupError("Only complete transcript sources are accepted")
    source_token = request["minute_token"]
    if kind == "feishu_docx_transcript":
        if not request.get("note_id") or not request.get("doc_token"):
            raise BackupError("Docx transcript requires verified note_id and doc_token")
        source_token = request["doc_token"]
    parsed = urlparse(request["source_url"])
    if parsed.scheme != "https" or parsed.path.rstrip("/").split("/")[-1] != source_token:
        raise BackupError("Source URL must identify the exact source token")
    if request.get("participation_verified") is not True:
        raise BackupError("Actual meeting participation must be verified before export")
    if not isinstance(request.get("participants"), list):
        raise BackupError("participants must be a list of verified names")
    try:
        when = datetime.fromisoformat(request["meeting_time"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupError("Invalid meeting_time") from exc
    if when.tzinfo is None:
        raise BackupError("meeting_time must include a timezone")
    return when.astimezone(timezone(timedelta(hours=8)))


def plan(request, output_root):
    when = validate_request(request)
    title = safe_name(request["title"], max_length=65)
    if not title.endswith("逐字稿"):
        title += "逐字稿"
    folder = output_root.resolve() / when.strftime("%Y-%m") / when.strftime("%Y-%m-%d")
    return folder / (title + ".md")


def fetch_docx_transcript(client, request, stage, audit_root, key):
    payload = client.run(["note", "+detail", "--note-id", request["note_id"]], cwd=stage)
    note = (payload.get("data") or {}).get("note") or {}
    if payload.get("ok") is not True or note.get("verbatim_doc_token") != request["doc_token"]:
        raise BackupError("Docx is not the linked verbatim transcript; refusing summary substitution")
    document = client.fetch_doc(request["doc_token"], stage)
    if document.get("document_id") != request["doc_token"] or document.get("revision_id") is None:
        raise BackupError("Missing exact Docx identity or revision")
    content = document.get("content")
    if not isinstance(content, str) or not content.strip() or "<fragment" in content:
        raise BackupError("Full Docx transcript was not returned")
    if re.search(r"<(?:img|image|whiteboard|source|html5-block|synced_reference)\b", content, re.I):
        raise BackupError("Transcript has embedded resources; needs Wiki/Docx resource export")
    raw = content.encode("utf-8")
    evidence = audit_root / (key + ".source-" + digest(raw)[:16] + ".json")
    if not evidence.exists():
        atomic_new(evidence, json.dumps({"note": note, "document": document}, ensure_ascii=False, indent=2).encode("utf-8"))
    # User citations otherwise render as empty HTML; preserve the displayed name.
    def speaker(match):
        name = re.search(r'user-name="([^"]+)"', match.group(0))
        if not name:
            raise BackupError("User citation has no speaker name; refusing information loss")
        return html.unescape(name.group(1))
    text = re.sub(r'<cite\b(?=[^>]*\btype="user")[^>]*>\s*</cite>', speaker, content)
    return raw, text, {"source_type": "feishu_docx_transcript", "doc_token": request["doc_token"],
                       "note_id": request["note_id"], "revision_id": document["revision_id"],
                       "source_evidence": str(evidence), "serialization": "user_cite_to_visible_speaker_name"}


def export(client, request, output_root, audit_root):
    target = plan(request, output_root)
    if client.profile != "codex-bot" or client.identity != "user":
        raise BackupError("Minutes export requires codex-bot / user")
    audit_root = audit_root.resolve()
    if audit_root == output_root.resolve() or output_root.resolve() in audit_root.parents:
        raise BackupError("Audit/staging must be outside raw output")
    audit_root.mkdir(parents=True, exist_ok=True)
    key = digest((request["meeting_id"] + ":" + request["minute_token"]).encode())
    receipt_path = audit_root / (key + ".json")
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        existing = Path(receipt["path"]).resolve()
        if output_root.resolve() not in existing.parents:
            raise BackupError("Receipt path outside output root")
        if not existing.is_file() or digest(existing.read_bytes()) != receipt["file_sha256"]:
            raise BackupError("Archived file missing or hash changed; refusing overwrite")
        return dict(receipt, status="already_archived")
    # TemporaryDirectory only deletes the newly created, isolated staging directory.
    with tempfile.TemporaryDirectory(prefix="minutes-", dir=str(audit_root)) as tmp:
        stage = Path(tmp).resolve()
        source_metadata = {"source_type": "feishu_minutes_transcript"}
        if request.get("source_type") == "feishu_docx_transcript":
            raw, text, source_metadata = fetch_docx_transcript(client, request, stage, audit_root, key)
        else:
            payload = client.run(["minutes", "+detail", "--minute-tokens", request["minute_token"],
                              "--transcript", "--output-dir", "artifacts"], cwd=stage)
            if payload.get("ok") is not True:
                raise BackupError("Minutes request did not report success")
            data = payload.get("data") or {}
            entries = data.get("minutes") or []
            matches = [item for item in entries if item.get("minute_token") == request["minute_token"]]
            if len(matches) != 1 or matches[0].get("error") or matches[0].get("ok") is False:
                raise BackupError("Exact Minutes artifact missing or failed; retry later")
            filename = (matches[0].get("artifacts") or {}).get("transcript_file")
            if not filename:
                raise BackupError("Transcript not ready or not permitted; no archive created")
            source = (stage / filename).resolve()
            if stage not in source.parents or not source.is_file():
                raise BackupError("Transcript path must resolve inside staging")
            raw = source.read_bytes()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeError as exc:
                raise BackupError("Transcript is not valid UTF-8") from exc
        if not text.strip() or "\x00" in text or text.lstrip().lower().startswith(("<!doctype html", "<html")):
            raise BackupError("Empty or invalid transcript; no archive created")
        # Preserve every source character, including CRLF; only strip an encoding BOM.
        body = text.encode("utf-8")
        metadata = {k: request[k] for k in ("title", "meeting_id", "minute_token", "meeting_time", "participants", "source_url")}
        metadata.update(source_metadata)
        metadata.update(captured_at=now_rfc3339(),
                        transcript_sha256=digest(body), source_bytes_sha256=digest(raw), archive_key=key)
        prefix = "---\n" + "\n".join(k + ": " + json.dumps(v, ensure_ascii=False) for k, v in metadata.items()) + "\n---\n\n"
        result = prefix.encode("utf-8") + body
        target.parent.mkdir(parents=True, exist_ok=True)
        # Recover publication-before-receipt crashes without duplicate files.
        for candidate in target.parent.glob("*.md"):
            content = candidate.read_bytes()
            head, separator, old_body = content.partition(b"\n---\n\n")
            if separator and ('archive_key: "' + key + '"').encode() in head.splitlines():
                if old_body != body:
                    raise BackupError("Source transcript changed; archived raw remains immutable")
                target = candidate
                result = content
                break
        else:
            if target.exists():
                target = target.with_name(target.stem + "-" + key[:12] + ".md")
            try:
                atomic_new(target, result)
            except FileExistsError as exc:
                raise BackupError("Concurrent export or filename collision; retry with existing receipt") from exc
        if target.read_bytes() != result:
            raise BackupError("Archive read-back verification failed")
        receipt = dict(status="archived", path=str(target), meeting_id=request["meeting_id"],
                       minute_token=request["minute_token"], transcript_sha256=digest(body),
                       file_sha256=digest(result), captured_at=metadata["captured_at"],
                       characters=len(text), verification="full_body_equal", skill="feishu-wiki-backup")
        receipt.update(source_metadata)
        encoded = (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        try:
            atomic_new(receipt_path, encoded)
        except FileExistsError:
            existing = json.loads(receipt_path.read_text(encoding="utf-8"))
            if existing.get("file_sha256") != receipt["file_sha256"]:
                raise BackupError("Concurrent receipt conflict")
        return receipt


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "export"])
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--audit-root", type=Path, required=True)
    parser.add_argument("--profile", choices=["codex-bot"], required=True)
    parser.add_argument("--identity", choices=["user"], required=True)
    parser.add_argument("--lark-cli")
    args = parser.parse_args()
    try:
        request = json.loads(args.request.read_text(encoding="utf-8-sig"))
        target = plan(request, args.output_root)
        result = {"status": "planned", "count": 1, "path": str(target), "source_type": request.get("source_type", "feishu_minutes_transcript")}
        if args.action == "export":
            result = export(LarkCLI(args.profile, args.identity, args.lark_cli), request, args.output_root, args.audit_root)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (BackupError, OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
