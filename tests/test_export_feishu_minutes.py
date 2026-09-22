import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import export_feishu_minutes as m


class FakeClient:
    profile = "codex-bot"
    identity = "user"

    def __init__(self, content=b"00:01 Speaker\r\noriginal words\r\n", escape=False, missing=False):
        self.content, self.escape, self.missing = content, escape, missing
        self.calls = 0

    def run(self, args, cwd):
        self.calls += 1
        assert "--transcript" in args and "--summary" not in args
        source = cwd / "transcript.txt"
        source.write_bytes(self.content)
        artifacts = {} if self.missing else {"transcript_file": "../outside.txt" if self.escape else str(source)}
        return {"ok": True, "data": {"minutes": [{"minute_token": "obctest", "artifacts": artifacts}]}}


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.output, self.audit = self.root / "raw", self.root / "audit"
        self.request = dict(meeting_id="123456789012345", minute_token="obctest", title="测试/会议",
                            meeting_time="2026-09-19T18:00:00+00:00", participants=["甲"],
                            source_url="https://test.feishu.cn/minutes/obctest", participation_verified=True)

    def tearDown(self):
        self.tmp.cleanup()

    def run_export(self, client=None):
        return m.export(client or FakeClient(), self.request, self.output, self.audit)

    def test_exact_body_and_date(self):
        client = FakeClient("00:01 张三\r\n原文：会议纪要不代替逐字稿。\r\n".encode())
        result = self.run_export(client)
        path = Path(result["path"])
        self.assertEqual(path.parent, (self.output / "2026-09" / "2026-09-20").resolve())
        self.assertEqual(path.read_bytes().partition(b"\n---\n\n")[2], client.content)

    def test_receipt_reuse_no_network(self):
        client = FakeClient()
        first = self.run_export(client)
        second = self.run_export(client)
        self.assertEqual(client.calls, 1)
        self.assertEqual(first["path"], second["path"])
        self.assertEqual(second["status"], "already_archived")

    def test_recover_publication_before_receipt(self):
        first = self.run_export()
        for path in self.audit.glob("*.json"):
            path.unlink()
        self.assertEqual(self.run_export()["path"], first["path"])
        self.assertEqual(len(list(self.output.rglob("*.md"))), 1)

    def test_changed_source_never_overwritten(self):
        first = self.run_export()
        original = Path(first["path"]).read_bytes()
        for path in self.audit.glob("*.json"):
            path.unlink()
        with self.assertRaises(m.BackupError):
            self.run_export(FakeClient(b"changed transcript"))
        self.assertEqual(Path(first["path"]).read_bytes(), original)

    def test_invalid_sources_leave_no_raw(self):
        for client in (FakeClient(b""), FakeClient(b"<html>Login</html>"), FakeClient(escape=True), FakeClient(missing=True)):
            with self.subTest(client=client), self.assertRaises(m.BackupError):
                self.run_export(client)
        self.assertEqual(list(self.output.rglob("*.md")), [])

    def test_unverified_participation_rejected(self):
        self.request["participation_verified"] = False
        with self.assertRaises(m.BackupError):
            self.run_export()

    def test_same_title_other_meeting_does_not_overwrite(self):
        first = self.run_export()
        self.request["meeting_id"] = "999999999999999"
        second = self.run_export()
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(len(list(self.output.rglob("*.md"))), 2)

    def test_docx_transcript_and_speaker_preserved(self):
        class DocClient(FakeClient):
            def run(self, args, cwd):
                return {"ok": True, "data": {"note": {"verbatim_doc_token": "docx1"}}}
            def fetch_doc(self, token, cwd):
                return {"document_id": token, "revision_id": 4, "content": '<cite type="user" user-name="张三"></cite> 00:01\n逐字原文\n'}
        self.request.update(source_type="feishu_docx_transcript", doc_token="docx1", note_id="note1",
                            source_url="https://test.feishu.cn/docx/docx1")
        result = self.run_export(DocClient())
        self.assertEqual(Path(result["path"]).read_bytes().partition(b"\n---\n\n")[2].decode(), '张三 00:01\n逐字原文\n')
        self.assertEqual(result["revision_id"], 4)
        self.assertTrue(Path(result["source_evidence"]).is_file())

    def test_docx_summary_cannot_be_substituted(self):
        class SummaryClient(FakeClient):
            def run(self, args, cwd):
                return {"ok": True, "data": {"note": {"verbatim_doc_token": "transcript", "note_doc_token": "summary"}}}
        self.request.update(source_type="feishu_docx_transcript", doc_token="summary", note_id="note1",
                            source_url="https://test.feishu.cn/docx/summary")
        with self.assertRaises(m.BackupError):
            self.run_export(SummaryClient())
        self.assertEqual(list(self.output.rglob("*.md")), [])


if __name__ == "__main__":
    unittest.main()
