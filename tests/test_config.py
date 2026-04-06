import unittest
from pathlib import Path

from orthanc_tools.config import build_base_url, first_registered_user, resolve_config_paths


class ConfigTests(unittest.TestCase):
    def test_first_registered_user_reads_first_pair(self) -> None:
        username, password = first_registered_user({"RegisteredUsers": {"admin": "secret"}})
        self.assertEqual(username, "admin")
        self.assertEqual(password, "secret")

    def test_resolve_config_paths_uses_defaults(self) -> None:
        main_config, credentials = resolve_config_paths("/etc/orthanc")
        self.assertEqual(main_config, Path("/etc/orthanc/orthanc.json"))
        self.assertEqual(credentials, Path("/etc/orthanc/credentials.json"))

    def test_build_base_url_uses_http_port(self) -> None:
        self.assertEqual(build_base_url({"HttpPort": 8043}), "http://127.0.0.1:8043")
