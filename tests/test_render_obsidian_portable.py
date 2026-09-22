import base64
import importlib.util
import io
import re
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


SCRIPT = Path(__file__).parents[1] / "scripts" / "render_obsidian_portable.py"
SPEC = importlib.util.spec_from_file_location("render_obsidian_portable", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def sample_png() -> bytes:
    image = Image.new("RGB", (600, 240), "white")
    draw = ImageDraw.Draw(image)
    for y in range(0, 240, 8):
        draw.rectangle((0, y, 599, y + 3), fill=((y * 7) % 255, (y * 11) % 255, (y * 17) % 255))
    for x in range(0, 600, 17):
        draw.line((x, 0, 599 - x // 2, 239), fill="black", width=2)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class PortableRenderTests(unittest.TestCase):
    def test_large_image_is_losslessly_tiled_with_bounded_lines(self):
        source_png = sample_png()
        payload = "\n".join(
            re.findall(".{1,120}", base64.b64encode(source_png).decode("ascii"))
        )
        source = (
            "---\ntitle: sample\nrender_profile: old\n---\n# Sample\n\n"
            f'<img alt="交互图" src="data:image/png;base64,\n{payload}\n">\n'
        )
        rendered, stats = MODULE.render_markdown(source, max_line=4096)
        self.assertEqual(stats["source_images"], 1)
        self.assertGreater(stats["rendered_tiles"], 1)
        self.assertLessEqual(max(map(len, rendered.splitlines())), 4096)
        self.assertNotRegex(rendered, r'src="data:image/[^\"]*\n')

        tags = list(MODULE.IMG_RE.finditer(rendered))
        restored = []
        for tag in tags:
            src = MODULE.SRC_RE.search(tag.group(0))
            self.assertIsNotNone(src)
            data = base64.b64decode(src.group("payload"), validate=True)
            with Image.open(io.BytesIO(data)) as tile:
                tile.load()
                restored.append(tile.copy())
        original = Image.open(io.BytesIO(source_png))
        stitched = Image.new(original.mode, original.size)
        top = 0
        for tile in restored:
            stitched.paste(tile, (0, top))
            top += tile.height
        self.assertEqual(top, original.height)
        self.assertEqual(stitched.convert("RGB").tobytes(), original.convert("RGB").tobytes())

    def test_file_render_is_non_destructive_and_updates_profile(self):
        source_png = sample_png()
        payload = base64.b64encode(source_png).decode("ascii")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.md"
            output = root / "derived" / "source.md"
            original = f'# T\n\n<img alt="图" src="data:image/png;base64,{payload}">\n'
            source.write_text(original, encoding="utf-8")
            record = MODULE.render_file(source, output, 4096, 2400)
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            result = output.read_text(encoding="utf-8")
            self.assertIn('render_profile: "legacy-obsidian-single-file-v3-tiled"', result)
            self.assertIn('render_status: "legacy"', result)
            self.assertIn('image_embedding: "html-data-uri-tiled"', result)
            self.assertGreater(record["rendered_tiles"], 1)
            with self.assertRaises(MODULE.RenderError):
                MODULE.render_file(source, output, 4096, 2400)


if __name__ == "__main__":
    unittest.main()
