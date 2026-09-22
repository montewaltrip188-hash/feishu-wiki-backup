import base64
import hashlib
import importlib.util
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_feishu_wiki.py"
SPEC = importlib.util.spec_from_file_location("feishu_wiki_backup", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
import sys
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeClient:
    def __init__(self):
        self.children = {
            "root": [
                {
                    "space_id": "space",
                    "node_token": "doc-a",
                    "obj_token": "obj-a",
                    "obj_type": "docx",
                    "parent_node_token": "root",
                    "node_type": "origin",
                    "title": "同名",
                    "has_child": True,
                },
                {
                    "space_id": "space",
                    "node_token": "shortcut-a",
                    "obj_token": "obj-a",
                    "obj_type": "docx",
                    "parent_node_token": "root",
                    "node_type": "shortcut",
                    "origin_node_token": "doc-a",
                    "title": "同名",
                    "has_child": False,
                },
            ],
            "doc-a": [
                {
                    "space_id": "space",
                    "node_token": "child",
                    "obj_token": "obj-child",
                    "obj_type": "docx",
                    "parent_node_token": "doc-a",
                    "node_type": "origin",
                    "title": "CON",
                    "has_child": False,
                }
            ],
        }

    def node_get(self, value):
        return {
            "space_id": "space",
            "node_token": "root",
            "obj_token": "obj-root",
            "obj_type": "docx",
            "title": "Root",
            "has_child": True,
            "node_type": "origin",
        }

    def node_list(self, space_id, parent_node_token):
        return self.children.get(parent_node_token, [])


class SingleDocClient:
    def __init__(self, revision_id=7):
        self.revision_id = revision_id

    def node_get(self, value):
        return {
            "space_id": "space",
            "node_token": "root",
            "obj_token": "obj-root",
            "obj_type": "docx",
            "title": "Root",
            "has_child": False,
            "node_type": "origin",
        }

    def node_list(self, space_id, parent_node_token):
        return []

    def fetch_doc(self, token, cwd):
        return {"document_id": token, "revision_id": self.revision_id, "content": "# hello\n"}


class Html5Client(SingleDocClient):
    def __init__(self, create_sidecar=True):
        super().__init__(revision_id=9)
        self.create_sidecar = create_sidecar

    def fetch_doc(self, token, cwd):
        if self.create_sidecar:
            sidecar = Path(cwd) / "doc-fetch-resources" / token / "html5_1.html"
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text("<html>ok</html>", encoding="utf-8")
        return {
            "document_id": token,
            "revision_id": 9,
            "content": '<html5-block data-ref="html5_1"></html5-block>\n',
            "reference_map": {
                "html5-block": {
                    "html5_1": {"path": f"@doc-fetch-resources/{token}/html5_1.html"}
                }
            },
        }


class InlineHtml5Client(SingleDocClient):
    def __init__(self):
        super().__init__(revision_id=10)

    def fetch_doc(self, token, cwd):
        return {
            "document_id": token,
            "revision_id": 10,
            "content": '<html5-block data-ref="html5_inline"></html5-block>\n',
            "reference_map": {
                "html5-block": {
                    "html5_inline": {"data": "<html><body>inline</body></html>"}
                }
            },
        }


class MalformedMarkupClient(SingleDocClient):
    def fetch_doc(self, token, cwd):
        return {
            "document_id": token,
            "revision_id": 13,
            "content": "<column width-ratio=\"0.5\">broken\n",
        }


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class MixedMediaClient(SingleDocClient):
    def __init__(self):
        super().__init__(revision_id=11)

    def fetch_doc(self, token, cwd):
        return {
            "document_id": token,
            "revision_id": 11,
            "content": (
                '<img token="img1"/><img token="img1"/>'
                '<whiteboard token="wb1"></whiteboard>'
                '<source token="file1" name="附件.pdf"/>'
            ),
        }

    def download_media(self, token, media_type, output_prefix, cwd):
        suffix = ".pdf" if media_type == "file" else ".png"
        output = Path(output_prefix).with_suffix(suffix)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"%PDF-1.4\n" if media_type == "file" else PNG_1X1)
        return output


class InvalidImageClient(SingleDocClient):
    def fetch_doc(self, token, cwd):
        return {"document_id": token, "revision_id": 12, "content": '<img token="bad1"/>'}

    def download_media(self, token, media_type, output_prefix, cwd):
        output = Path(output_prefix).with_suffix(".png")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"not-an-image")
        return output


class BackupTests(unittest.TestCase):
    def test_inline_mime_detection_accepts_only_supported_signatures(self):
        self.assertEqual(MODULE.image_mime_type(b"\x89PNG\r\n\x1a\nrest", Path("x.bin")), "image/png")
        self.assertEqual(MODULE.image_mime_type(b"\xff\xd8\xffrest", Path("x.bin")), "image/jpeg")
        self.assertEqual(MODULE.image_mime_type(b"GIF89arest", Path("x.bin")), "image/gif")
        self.assertEqual(MODULE.image_mime_type(b"RIFF\x00\x00\x00\x00WEBPrest", Path("x.bin")), "image/webp")
        with self.assertRaises(MODULE.BackupError):
            MODULE.image_mime_type(b"<svg></svg>", Path("looks-like.png"))

    def test_lark_cli_subprocess_is_argument_list_and_parses_json(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"ok": True, "data": {"x": 1}}), stderr=""
        )
        with mock.patch.object(MODULE, "resolve_lark_command", return_value=["lark-cli"]), mock.patch.object(
            MODULE.subprocess, "run", return_value=completed
        ) as run:
            client = MODULE.LarkCLI("codex-bot", "user")
            result = client.run(["wiki", "+node-get", "--node-token", "abc"])
        self.assertTrue(result["ok"])
        command = run.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertIn("codex-bot", command)
        self.assertIn("user", command)

    def test_lark_cli_preserves_typed_error_from_stderr(self):
        error_payload = {
            "ok": False,
            "error": {
                "type": "authorization",
                "message": "missing required scope(s)",
                "missing_scopes": ["board:whiteboard:node:read"],
            },
        }
        completed = subprocess.CompletedProcess(
            args=[], returncode=4, stdout="", stderr=json.dumps(error_payload)
        )
        with mock.patch.object(MODULE, "resolve_lark_command", return_value=["lark-cli"]), mock.patch.object(
            MODULE.subprocess, "run", return_value=completed
        ):
            client = MODULE.LarkCLI("codex-bot", "user")
            with self.assertRaises(MODULE.LarkCommandError) as raised:
                client.run(["whiteboard", "+export"])
        self.assertIn("board:whiteboard:node:read", str(raised.exception))

    def test_profile_is_locked_to_codex_bot(self):
        with self.assertRaises(MODULE.BackupError):
            MODULE.LarkCLI("another-profile", "user", executable="lark-cli")

    def test_recursive_inventory_and_shortcut_dedup(self):
        root, nodes = MODULE.inventory_wiki(FakeClient(), "https://example/wiki/root")
        self.assertEqual(root.node_token, "root")
        self.assertEqual(len(nodes), 4)
        groups = MODULE.group_by_object(nodes)
        self.assertEqual(len(groups), 3)
        canonical = MODULE.choose_canonical(groups[("docx", "obj-a")])
        self.assertEqual(canonical.node_token, "doc-a")
        self.assertEqual(canonical.node_type, "origin")

    def test_duplicate_sibling_names_and_windows_reserved_are_safe(self):
        _, nodes = MODULE.inventory_wiki(FakeClient(), "https://example/wiki/root")
        doc_a = next(node for node in nodes if node.node_token == "doc-a")
        shortcut = next(node for node in nodes if node.node_token == "shortcut-a")
        child = next(node for node in nodes if node.node_token == "child")
        self.assertNotEqual(doc_a.local_segments[-1], shortcut.local_segments[-1])
        self.assertEqual(child.local_segments[-1], "_CON")

    def test_media_collect_and_rewrite(self):
        content = (
            '<img token="img123"/><source token="file456" name="附件.pdf"/>'
            '<whiteboard token="wb789"></whiteboard>\n'
            '```html\n<img token="example-only"/>\n```\n'
        )
        refs = MODULE.collect_media_refs(content)
        self.assertEqual({(ref.kind, ref.token) for ref in refs}, {("image", "img123"), ("file", "file456"), ("whiteboard", "wb789")})
        rewritten = MODULE.rewrite_media(
            content,
            {
                ("image", "img123"): "../assets/img.png",
                ("file", "file456"): "../assets/file.pdf",
                ("whiteboard", "wb789"): "../assets/wb.png",
            },
        )
        self.assertIn("附件.pdf", rewritten)
        self.assertNotIn("</whiteboard>", rewritten)
        self.assertIn('<img token="example-only"/>', rewritten)
        self.assertEqual(MODULE.count_unresolved_media(rewritten), 0)

    def test_file_and_image_labels_are_escaped_for_markdown(self):
        rewritten = MODULE.rewrite_media(
            '<img token="img1" alt="A]B\\C"/><source token="file1" name="D]E\\F.pdf"/>',
            {
                ("image", "img1"): "../_assets/image.png",
                ("file", "file1"): "../assets/file.pdf",
            },
        )
        self.assertIn(r"![A\]B\\C](../_assets/image.png)", rewritten)
        self.assertIn(r"[D\]E\\F.pdf](../assets/file.pdf)", rewritten)

    def test_nested_list_callout_becomes_native_obsidian_callout(self):
        content = (
            "1. 建仓库。\n\n"
            "   <callout emoji=\"📍\">\n"
            "   **为什么是 Git 仓库？**\n"
            "   Git 对 Agent 最友好。\n"
            "   </callout>\n"
            "2. 下一步。\n"
        )
        rewritten = MODULE.normalize_feishu_markup(content)
        self.assertIn("   > [!note] 📍", rewritten)
        self.assertIn("   > **为什么是 Git 仓库？**", rewritten)
        self.assertIn("   > Git 对 Agent 最友好。", rewritten)
        self.assertIn("2. 下一步。", rewritten)
        self.assertNotIn("<callout", rewritten)
        self.assertEqual(MODULE.count_unresolved_feishu_markup(rewritten), 0)

    def test_inline_callout_grid_and_simple_feishu_tags_are_portable(self):
        content = (
            '<title>示例</title>\n'
            '<callout emoji="🗞️"><p><b>TL;DR</b></p><p>查看 '
            '<cite doc-id="doc1" title="正文"></cite></p></callout>\n'
            '<grid><column width-ratio="0.5"><p><b>左列</b></p>![左图][img-left]</column>'
            '<column width-ratio="0.5"><p>右列</p>![右图][img-right]</column></grid>\n'
            '<readonly-block href="https://example.com/embed" type="iframe"></readonly-block>\n'
            '<synced_reference src-block-id="block1" src-token="token1"></synced_reference>\n'
            '```xml\n<callout emoji="保留">示例代码</callout>\n```\n'
        )
        rewritten = MODULE.normalize_feishu_markup(content)
        self.assertIn("# 示例", rewritten)
        self.assertIn("> [!note] 🗞️", rewritten)
        self.assertIn("**TL;DR**", rewritten)
        self.assertIn("正文", rewritten)
        self.assertEqual(rewritten.count("![左图][img-left]"), 1)
        self.assertEqual(rewritten.count("![右图][img-right]"), 1)
        self.assertIn("[飞书嵌入内容](<https://example.com/embed>)", rewritten)
        self.assertIn("飞书同步块引用 src-token=token1 src-block-id=block1", rewritten)
        self.assertNotIn("<grid>", rewritten)
        self.assertNotIn("<column", rewritten)
        self.assertIn('<callout emoji="保留">示例代码</callout>', rewritten)
        self.assertEqual(MODULE.count_unresolved_feishu_markup(rewritten), 0)

    def test_empty_readonly_block_becomes_trace_comment(self):
        content = '<readonly-block type="isv"></readonly-block>\n'
        rewritten = MODULE.normalize_feishu_markup(content)
        self.assertIn("飞书不可移植嵌入块 type=isv", rewritten)
        self.assertEqual(MODULE.count_unresolved_feishu_markup(rewritten), 0)

    def test_grid_wrapping_fenced_code_keeps_code_and_drops_layout_wrappers(self):
        content = (
            '<grid>\n<column width-ratio="0.5">\n'
            '```toml\nname = "worker"\n```\n'
            '</column>\n<column width-ratio="0.5">\n'
            '```toml\nname = "explorer"\n```\n'
            '</column>\n</grid>\n'
        )
        rewritten = MODULE.normalize_feishu_markup(content)
        self.assertIn('name = "worker"', rewritten)
        self.assertIn('name = "explorer"', rewritten)
        self.assertNotIn("<grid", rewritten)
        self.assertNotIn("<column", rewritten)
        self.assertEqual(MODULE.count_unresolved_feishu_markup(rewritten), 0)

    def test_fence_prefix_with_trailing_text_does_not_close_code_block(self):
        content = (
            "```text\n"
            '<callout emoji="保留">第一段</callout>\n'
            "```not-a-valid-closing-fence\n"
            '<callout emoji="仍保留">第二段</callout>\n'
            "```\n"
        )
        rewritten = MODULE.normalize_feishu_markup(content)
        self.assertEqual(rewritten, content)

    def test_malformed_feishu_layout_never_finalizes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_dir = Path(temp_dir) / "raw"
            partial, report = MODULE.export_snapshot(
                MalformedMarkupClient(), "https://example/wiki/root", raw_dir, "snap"
            )
            self.assertEqual(report["status"], "failed")
            self.assertTrue(partial.exists())
            self.assertFalse((raw_dir / "Root" / "snap").exists())
            self.assertIn("未转换的飞书布局标签", (partial / "verify-report.md").read_text(encoding="utf-8"))

    def test_whiteboard_uses_dedicated_export_and_relative_staging_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            staging = Path(temp_dir)
            output = staging / "assets" / "whiteboard" / "wb-token"
            client = object.__new__(MODULE.LarkCLI)
            captured = {}

            def fake_run(args, cwd=None):
                captured["args"] = list(args)
                captured["cwd"] = Path(cwd)
                output_arg = Path(args[args.index("--output") + 1])
                produced = Path(cwd) / output_arg.with_suffix(".png")
                produced.parent.mkdir(parents=True, exist_ok=True)
                produced.write_bytes(b"png")
                return {"ok": True}

            client.run = fake_run
            result = client.download_media("wb-token", "whiteboard", output, staging)
            self.assertEqual(result.suffix, ".png")
            self.assertEqual(captured["args"][:2], ["whiteboard", "+export"])
            self.assertFalse(Path(captured["args"][captured["args"].index("--output") + 1]).is_absolute())

    def test_html5_sidecar_is_hashed_and_linked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target, report = MODULE.export_snapshot(
                Html5Client(), "https://example/wiki/root", Path(temp_dir) / "raw", "snap"
            )
            self.assertEqual(report["status"], "completed")
            record = json.loads((target / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(record["html5_sidecars"][0]["status"], "copied")
            self.assertTrue(record["html5_sidecars"][0]["sha256"])
            markdown = next((target / "documents").rglob("*.md")).read_text(encoding="utf-8")
            self.assertIn("飞书 HTML5 交互资源", markdown)
            self.assertNotIn("<html5-block", markdown)

    def test_missing_html5_sidecar_never_finalizes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_dir = Path(temp_dir) / "raw"
            partial, report = MODULE.export_snapshot(
                Html5Client(create_sidecar=False), "https://example/wiki/root", raw_dir, "snap"
            )
            self.assertEqual(report["status"], "failed")
            self.assertFalse((raw_dir / "Root" / "snap").exists())
            self.assertIn("HTML5 sidecar", (partial / "verify-report.md").read_text(encoding="utf-8"))

    def test_inline_html5_data_is_materialized_hashed_and_linked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target, report = MODULE.export_snapshot(
                InlineHtml5Client(), "https://example/wiki/root", Path(temp_dir) / "raw", "snap"
            )
            self.assertEqual(report["status"], "completed")
            record = json.loads((target / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[0])
            sidecar = record["html5_sidecars"][0]
            self.assertEqual(sidecar["source_path"], "inline:data")
            self.assertEqual(sidecar["status"], "copied")
            self.assertTrue(sidecar["sha256"])
            self.assertTrue((target / sidecar["path"]).is_file())
            markdown = next((target / "documents").rglob("*.md")).read_text(encoding="utf-8")
            self.assertIn("飞书 HTML5 交互资源", markdown)
            self.assertNotIn("<html5-block", markdown)

    def test_default_mode_uses_shared_sha256_assets_and_short_links(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target, report = MODULE.export_snapshot(
                MixedMediaClient(), "https://example/wiki/root", Path(temp_dir) / "raw", "snap"
            )
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["image_mode"], "dedup")
            markdown = next((target / "documents").rglob("*.md")).read_text(encoding="utf-8")
            digest = hashlib.sha256(PNG_1X1).hexdigest()
            self.assertNotIn("data:image/", markdown)
            self.assertEqual(markdown.count(f"_assets/{digest}.png"), 3)
            self.assertIn("assets/attachments/file1.pdf", markdown)
            self.assertFalse((target / "assets" / "whiteboard").exists())
            self.assertFalse((target / "assets" / "media").exists())
            self.assertEqual(list((target / "_assets").glob("*")), [target / "_assets" / f"{digest}.png"])
            self.assertTrue(any((target / "assets" / "attachments").glob("file1*.pdf")))
            record = json.loads((target / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[0])
            media = {(item["kind"], item["token"]): item for item in record["media"]}
            self.assertEqual(media[("image", "img1")]["status"], "downloaded")
            self.assertEqual(media[("image", "img1")]["storage"], "sha256-asset")
            self.assertEqual(media[("whiteboard", "wb1")]["path"], media[("image", "img1")]["path"])
            self.assertEqual(media[("file", "file1")]["storage"], "asset-file")
            self.assertEqual(report["unique_deduplicated_visuals"], 1)
            self.assertEqual(report["deduplicated_visual_references"], 3)
            self.assertEqual(report["deduplicated_media_records"], 2)
            self.assertEqual(report["deduplicated_copies_saved"], 1)
            self.assertTrue(record["images_localized"])
            self.assertFalse(record["images_self_contained"])
            self.assertFalse(record["standalone_markdown"])

    def test_explicit_inline_mode_remains_available_for_legacy_snapshots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target, report = MODULE.export_snapshot(
                MixedMediaClient(),
                "https://example/wiki/root",
                Path(temp_dir) / "raw",
                "snap",
                image_mode="inline",
            )
            self.assertEqual(report["status"], "completed")
            markdown = next((target / "documents").rglob("*.md")).read_text(encoding="utf-8")
            self.assertEqual(markdown.count("data:image/png;base64,"), 3)
            self.assertTrue(json.loads((target / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[0])["images_self_contained"])

    def test_long_data_uri_stays_at_image_position_without_reference_tail(self):
        data_uri = "data:image/png;base64," + ("A" * 200_000)
        content = '<img token="img1"/>\n\n正文结尾。\n'
        image = MODULE.InlineImage(embed_id="feishu-img-long", data_uri=data_uri)
        rewritten = MODULE.rewrite_media(content, {}, {("image", "img1"): image})
        self.assertTrue(rewritten.startswith('<img alt="飞书图片" data-feishu-embed-id="feishu-img-long"'))
        self.assertTrue(rewritten.endswith("正文结尾。\n"))
        self.assertNotRegex(rewritten, r"(?m)^\[[^]]+\]: data:image/")
        self.assertLessEqual(max(len(line) for line in rewritten.splitlines()), MODULE.INLINE_DATA_WRAP_WIDTH)
        embedded = rewritten.split('src="', 1)[1].split('"', 1)[0]
        self.assertEqual("".join(embedded.split()), data_uri)
        self.assertEqual(MODULE.count_unresolved_media(rewritten), 0)

    def test_media_alt_text_with_tag_characters_remains_valid_html(self):
        data_uri = "data:image/png;base64," + base64.b64encode(PNG_1X1).decode("ascii")
        content = '![γ>1 &amp; stable](https://feishu.cn/file/img1)\n'
        normalized = MODULE.normalize_feishu_markup(content)
        rewritten = MODULE.rewrite_media(
            normalized,
            {},
            {("image", "img1"): MODULE.InlineImage("feishu-img-safe-alt", data_uri)},
        )
        self.assertIn('alt="γ&gt;1 &amp; stable"', rewritten)
        self.assertEqual(MODULE.count_unresolved_media(rewritten), 0)

    def test_legacy_reference_data_uris_migrate_to_wrapped_html(self):
        image_bytes = PNG_1X1 + (b"\x00" * 100_000)
        payload = base64.b64encode(image_bytes).decode("ascii")
        data_uri = f"data:image/png;base64,{payload}"
        embed_id = f"feishu-img-{hashlib.sha256(image_bytes).hexdigest()[:24]}"
        content = (
            f"![第一处][{embed_id}]\n\n"
            "正文。\n\n"
            f"![第二处][{embed_id}]\n\n"
            f"[{embed_id}]: {data_uri}\n"
        )
        rewritten = MODULE.migrate_reference_data_uris(content)
        self.assertEqual(rewritten.count(f'data-feishu-embed-id="{embed_id}"'), 2)
        self.assertEqual(rewritten.count("data:image/png;base64,"), 2)
        self.assertNotIn(f"![第一处][{embed_id}]", rewritten)
        self.assertNotRegex(rewritten, rf"(?m)^\[{re.escape(embed_id)}\]:")
        self.assertLessEqual(max(len(line) for line in rewritten.splitlines()), MODULE.INLINE_DATA_WRAP_WIDTH)
        self.assertTrue(rewritten.rstrip().endswith('>'))

    def test_files_mode_preserves_external_image_assets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target, report = MODULE.export_snapshot(
                MixedMediaClient(),
                "https://example/wiki/root",
                Path(temp_dir) / "raw",
                "snap",
                image_mode="files",
            )
            self.assertEqual(report["status"], "completed")
            markdown = next((target / "documents").rglob("*.md")).read_text(encoding="utf-8")
            self.assertNotIn("data:image/", markdown)
            self.assertTrue(any((target / "assets" / "media").glob("img1*.png")))
            self.assertTrue(any((target / "assets" / "whiteboard").glob("wb1*.png")))
            self.assertTrue(any((target / "assets" / "attachments").glob("file1*.pdf")))
            record = json.loads((target / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[0])
            self.assertTrue(all(item["storage"] == "asset-file" for item in record["media"]))

    def test_invalid_image_bytes_cannot_be_claimed_as_embedded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_dir = Path(temp_dir) / "raw"
            partial, report = MODULE.export_snapshot(
                InvalidImageClient(), "https://example/wiki/root", raw_dir, "snap"
            )
            self.assertEqual(report["status"], "failed")
            self.assertTrue(partial.exists())
            self.assertFalse((raw_dir / "Root" / "snap").exists())
            self.assertIn("不是支持内嵌的", (partial / "verify-report.md").read_text(encoding="utf-8"))

    def test_existing_snapshot_is_never_overwritten(self):
        class MinimalClient(FakeClient):
            pass

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "Root" / "snap"
            target.mkdir(parents=True)
            marker = target / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            with self.assertRaises(MODULE.BackupError):
                MODULE.export_snapshot(MinimalClient(), "https://example/wiki/root", Path(temp_dir), "snap", limit=1)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_failed_run_keeps_partial_outside_raw_output(self):
        class FailingClient:
            def node_get(self, value):
                return {
                    "space_id": "space",
                    "node_token": "root",
                    "obj_token": "obj-root",
                    "obj_type": "docx",
                    "title": "Root",
                    "has_child": False,
                    "node_type": "origin",
                }

            def node_list(self, space_id, parent_node_token):
                return []

            def fetch_doc(self, token, cwd):
                raise MODULE.BackupError("synthetic failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            raw_dir = Path(temp_dir) / "raw"
            partial, report = MODULE.export_snapshot(
                FailingClient(), "https://example/wiki/root", raw_dir, "snap"
            )
            self.assertEqual(report["status"], "failed")
            self.assertTrue(partial.exists())
            self.assertFalse((raw_dir / "Root" / "snap").exists())
            self.assertNotEqual(partial.parents[1], raw_dir)

    def test_success_snapshot_has_revision_and_trace_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_dir = Path(temp_dir) / "raw"
            target, report = MODULE.export_snapshot(
                SingleDocClient(), "https://example/wiki/root", raw_dir, "snap"
            )
            self.assertEqual(report["status"], "completed")
            self.assertTrue(target.exists())
            record = json.loads((target / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(record["revision_id"], 7)
            self.assertEqual(record["snapshot_id"], "snap")
            self.assertTrue(record["fetched_at"])
            self.assertIn("completed", (target / "verify-report.md").read_text(encoding="utf-8"))

    def test_missing_revision_never_finalizes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_dir = Path(temp_dir) / "raw"
            partial, report = MODULE.export_snapshot(
                SingleDocClient(revision_id=None), "https://example/wiki/root", raw_dir, "snap"
            )
            self.assertEqual(report["status"], "failed")
            self.assertTrue(partial.exists())
            self.assertFalse((raw_dir / "Root" / "snap").exists())
            self.assertIn("revision_id", (partial / "verify-report.md").read_text(encoding="utf-8"))

    def test_final_move_failure_does_not_claim_completed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_dir = Path(temp_dir) / "raw"
            with mock.patch.object(MODULE.os, "rename", side_effect=OSError("synthetic move failure")):
                partial, report = MODULE.export_snapshot(
                    SingleDocClient(), "https://example/wiki/root", raw_dir, "snap"
                )
            self.assertEqual(report["status"], "finalize_failed")
            self.assertTrue(partial.exists())
            report_text = (partial / "verify-report.md").read_text(encoding="utf-8")
            self.assertNotIn("`completed`", report_text)


if __name__ == "__main__":
    unittest.main()
