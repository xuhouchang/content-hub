import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

import publish_queue
import publish_worker


class PublishPipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.queue_dir = self.root / "queue"
        paths = {
            "QUEUE_DIR": self.queue_dir,
            "PENDING": self.queue_dir / "pending.jsonl",
            "PROCESSING": self.queue_dir / "processing.jsonl",
            "DONE": self.queue_dir / "done.jsonl",
            "FAILED": self.queue_dir / "failed.jsonl",
        }
        for name, path in paths.items():
            setattr(publish_queue, name, path)
            if name != "QUEUE_DIR":
                setattr(publish_worker, name, path)
        publish_queue.QUEUE_LOCK = self.queue_dir / ".queue.lock"
        publish_worker.QUEUE_DIR = self.queue_dir
        publish_worker.LOCK = self.queue_dir / ".worker.lock"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _article_dir(self, name: str) -> Path:
        output_dir = self.root / name
        output_dir.mkdir()
        (output_dir / "article.md").write_text("test article", encoding="utf-8")
        return output_dir

    def test_enqueue_is_idempotent(self):
        article = self._article_dir("article-one")

        self.assertTrue(publish_queue.enqueue_for_publish(str(article), "digest"))
        self.assertFalse(publish_queue.enqueue_for_publish(str(article), "digest"))
        self.assertEqual(
            1, len(publish_queue.read_records_unlocked(publish_queue.PENDING))
        )

    def test_worker_does_not_erase_record_enqueued_during_publish(self):
        first = self._article_dir("article-one")
        second = self._article_dir("article-two")
        publish_queue.enqueue_for_publish(str(first))

        original_publish_one = publish_worker._publish_one

        def publish_and_enqueue(_record):
            publish_queue.enqueue_for_publish(str(second))
            return "draft-media-id", ""

        publish_worker._publish_one = publish_and_enqueue
        try:
            self.assertEqual(1, publish_worker.process_once(quiet_empty=True))
        finally:
            publish_worker._publish_one = original_publish_one

        pending = publish_queue.read_records_unlocked(publish_queue.PENDING)
        done = publish_queue.read_records_unlocked(publish_queue.DONE)
        self.assertEqual(
            [str(second.resolve())], [record["output_dir"] for record in pending]
        )
        self.assertEqual(
            [str(first.resolve())], [record["output_dir"] for record in done]
        )

    def test_failed_publish_is_retried_before_terminal_failure(self):
        article = self._article_dir("article-one")
        publish_queue.enqueue_for_publish(str(article))
        original_publish_one = publish_worker._publish_one
        publish_worker._publish_one = lambda _record: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        try:
            self.assertEqual(0, publish_worker.process_once(quiet_empty=True))
        finally:
            publish_worker._publish_one = original_publish_one

        pending = publish_queue.read_records_unlocked(publish_queue.PENDING)
        failed = publish_queue.read_records_unlocked(publish_queue.FAILED)
        processing = publish_queue.read_records_unlocked(publish_queue.PROCESSING)
        self.assertEqual(1, pending[0]["attempts"])
        self.assertIn("next_attempt_at", pending[0])
        self.assertEqual([], failed)
        self.assertEqual([], processing)


if __name__ == "__main__":
    unittest.main()
