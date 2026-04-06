import unittest
from datetime import date

from orthanc_tools.dicom import date_to_orthanc, parse_count, parse_iso_date, pick_tag


class DicomTests(unittest.TestCase):
    def test_parse_iso_date_accepts_yyyy_mm_dd(self) -> None:
        self.assertEqual(parse_iso_date("2024-01-31"), date(2024, 1, 31))

    def test_date_to_orthanc_formats_compact_date(self) -> None:
        self.assertEqual(date_to_orthanc(date(2024, 1, 31)), "20240131")

    def test_pick_tag_searches_requested_tags(self) -> None:
        payload = {"RequestedTags": {"StudyInstanceUID": "1.2.3"}}
        self.assertEqual(pick_tag(payload, "StudyInstanceUID"), "1.2.3")

    def test_parse_count_handles_numeric_strings(self) -> None:
        self.assertEqual(parse_count("12"), 12)
        self.assertIsNone(parse_count(True))
