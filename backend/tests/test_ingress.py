import hashlib
import json
import unittest
from uuid import uuid4

from app.workflow.states import can_transition


class IngressHashTests(unittest.TestCase):
    def test_same_files_same_hash(self) -> None:
        user_id = str(uuid4())
        payload = {
            "user_id": user_id,
            "file1": "abc",
            "file2": "def",
            "file1_name": "a.pdf",
            "file2_name": "b.pdf",
        }
        first = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        second = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        self.assertEqual(first, second)

    def test_different_content_different_hash(self) -> None:
        user_id = str(uuid4())
        a = hashlib.sha256(
            json.dumps(
                {
                    "user_id": user_id,
                    "file1": "aaa",
                    "file2": "bbb",
                    "file1_name": "a.pdf",
                    "file2_name": "b.pdf",
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        b = hashlib.sha256(
            json.dumps(
                {
                    "user_id": user_id,
                    "file1": "aaa",
                    "file2": "ccc",
                    "file1_name": "a.pdf",
                    "file2_name": "b.pdf",
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        self.assertNotEqual(a, b)

    def test_completed_is_terminal_for_late_events(self) -> None:
        self.assertFalse(can_transition("completed", "preparing"))


if __name__ == "__main__":
    unittest.main()
