import unittest
from unittest import mock
from urllib import error

from orthanc_tools.orthanc_api import OrthancNetworkError, OrthancRestClient


class OrthancApiTests(unittest.TestCase):
    def test_url_errors_raise_specific_network_error(self) -> None:
        client = OrthancRestClient("http://127.0.0.1:8042", "admin", "secret")

        with mock.patch("orthanc_tools.orthanc_api.request.urlopen", side_effect=error.URLError("down")):
            with self.assertRaises(OrthancNetworkError) as ctx:
                client.get("/system")

        self.assertEqual(ctx.exception.method, "GET")
        self.assertEqual(ctx.exception.path, "/system")
        self.assertIn("down", str(ctx.exception))
