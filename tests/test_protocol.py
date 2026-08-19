from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from protocol import ProtocolError, atomic_write_json, parse_request_bytes, read_secure_request, secure_runtime_directory


class ProtocolTests(unittest.TestCase):
    def request(self) -> dict:
        return {
            "protocolVersion": 1,
            "requestId": str(uuid.uuid4()),
            "createdAt": int(time.time()),
            "brokerSessionId": str(uuid.uuid4()),
            "head": "a" * 40,
            "worktreeDigest": "b" * 64,
        }

    def test_strict_request_schema_and_filename(self) -> None:
        request = self.request()
        data = json.dumps(request).encode()
        self.assertEqual(request["requestId"], parse_request_bytes(data, request["requestId"] + ".json").request_id)
        for mutation, code in [
            ({**request, "unknown": 1}, "invalid_request"),
            ({**request, "protocolVersion": True}, "unsupported_protocol"),
            ({**request, "createdAt": int(time.time()) - 1801}, "request_expired"),
        ]:
            with self.subTest(code=code), self.assertRaisesRegex(ProtocolError, ".*") as raised:
                parse_request_bytes(json.dumps(mutation).encode(), request["requestId"] + ".json")
            self.assertEqual(code, raised.exception.code)
        duplicate = data[:-1] + b',"head":"' + b"c" * 40 + b'"}'
        with self.assertRaises(ProtocolError):
            parse_request_bytes(duplicate, request["requestId"] + ".json")

    def test_request_file_rejects_symlink_mode_hardlink_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = self.request()
            good = root / f"{request['requestId']}.json"
            atomic_write_json(good, request)
            self.assertEqual(request["requestId"], read_secure_request(good).request_id)
            os.chmod(good, 0o644)
            with self.assertRaises(ProtocolError):
                read_secure_request(good)
            os.chmod(good, 0o600)
            link = root / "link.json"
            link.symlink_to(good)
            with self.assertRaises(ProtocolError):
                read_secure_request(link)
            hard = root / "hard.json"
            os.link(good, hard)
            with self.assertRaises(ProtocolError):
                read_secure_request(good)
            hard.unlink()
            good.write_bytes(b"x" * (16 * 1024 + 1))
            with self.assertRaises(ProtocolError):
                read_secure_request(good)

    def test_runtime_directory_rejects_symlink_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"; outside.mkdir()
            runtime = root / "runtime"; runtime.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ProtocolError):
                secure_runtime_directory(runtime)


if __name__ == "__main__":
    unittest.main()
