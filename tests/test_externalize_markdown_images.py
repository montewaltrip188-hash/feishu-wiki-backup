import base64
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "externalize_markdown_images.py"
SPEC = importlib.util.spec_from_file_location("externalize_markdown_images", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ExternalizeMarkdownImagesTests(unittest.TestCase):
    def test_directory_migration_deduplicates_across_files_and_keeps_short_links(self):
        payload = base64.b64encode(PNG_1X1).decode("ascii")
        wrapped = "\n".join(payload[index : index + 20] for index in range(0, len(payload), 20))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            output = root / "output"
            (source / "nested").mkdir(parents=True)
            (source / "assets" / "attachments").mkdir(parents=True)
            (source / "assets" / "html5").mkdir(parents=True)
            attachment = source / "assets" / "attachments" / "资料.pdf"
            sidecar = source / "assets" / "html5" / "interactive.html"
            attachment.write_bytes(b"%PDF-1.4\nsource")
            sidecar.write_text("<html>source</html>", encoding="utf-8")
            (source / "one.md").write_text(
                f'<img alt="图一" src="data:image/png;base64,\n{wrapped}\n">\n'
                "[附件](assets/attachments/%E8%B5%84%E6%96%99.pdf)\n",
                encoding="utf-8",
            )
            (source / "nested" / "two.md").write_text(
                f'![图二](data:image/png;base64,{payload})\n',
                encoding="utf-8",
            )

            report = MODULE.migrate_directory(source, output)

            digest = hashlib.sha256(PNG_1X1).hexdigest()
            self.assertEqual(report["markdown_files"], 2)
            self.assertEqual(report["image_references"], 2)
            self.assertEqual(report["unique_images"], 1)
            self.assertEqual(report["duplicate_copies_saved"], 1)
            self.assertEqual(report["copied_sidecar_files"], 2)
            self.assertEqual(list((output / "_assets").glob("*")), [output / "_assets" / f"{digest}.png"])
            self.assertEqual((output / "assets" / "attachments" / "资料.pdf").read_bytes(), attachment.read_bytes())
            self.assertEqual((output / "assets" / "html5" / "interactive.html").read_text(encoding="utf-8"), "<html>source</html>")
            self.assertIn(f"](_assets/{digest}.png)", (output / "one.md").read_text(encoding="utf-8"))
            self.assertIn(f"](../_assets/{digest}.png)", (output / "nested" / "two.md").read_text(encoding="utf-8"))
            self.assertNotIn("data:image/", (output / "one.md").read_text(encoding="utf-8"))
            self.assertEqual(json.loads((output / "migration-report.json").read_text(encoding="utf-8"))["status"], "completed")

    def test_code_fence_data_uri_is_not_treated_as_an_image(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            original = '```html\n<img src="data:image/png;base64,AAAA">\n```\n'
            (source / "sample.md").write_text(original, encoding="utf-8")
            report = MODULE.migrate_directory(source, root / "output")
            self.assertEqual(report["image_references"], 0)
            self.assertEqual((root / "output" / "sample.md").read_text(encoding="utf-8"), original)

    def test_invalid_image_aborts_without_publishing_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (source / "bad.md").write_text(
                '<img src="data:image/png;base64,bm90LWEtcG5n">\n',
                encoding="utf-8",
            )
            with self.assertRaises(MODULE.MigrationError):
                MODULE.migrate_directory(source, output)
            self.assertFalse(output.exists())

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            output.mkdir()
            marker = output / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            (source / "one.md").write_text("text\n", encoding="utf-8")
            with self.assertRaises(MODULE.MigrationError):
                MODULE.migrate_directory(source, output)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
