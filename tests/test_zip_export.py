import json
import tempfile
import unittest
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from orthanc_tools.zip_export import (
    ZIP_MANIFEST_NAME,
    ascii_slug,
    build_patient_base,
    read_zip_manifest,
    truncate_with_hash,
    validate_zip_file,
)


@dataclass
class DummyStudy:
    patient_name: str
    patient_birth_date: str
    study_date: str


class ZipExportTests(unittest.TestCase):
    def test_ascii_slug_normalizes_text(self) -> None:
        self.assertEqual(ascii_slug("João da Silva"), "Joao_da_Silva")

    def test_truncate_with_hash_keeps_limit(self) -> None:
        truncated = truncate_with_hash("x" * 300, limit=32)
        self.assertLessEqual(len(truncated), 32)

    def test_build_patient_base_uses_patient_fields(self) -> None:
        study = DummyStudy("Maria", "19800101", "20240220")
        self.assertTrue(build_patient_base(study, date(2024, 2, 20)).startswith("Maria_19800101_20240220"))

    def test_validate_zip_file_and_read_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "study.zip"
            manifest = {"study_uid": "1.2.3", "manifest_sha1": "abc"}
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("images/1.dcm", b"dicom")
                archive.writestr(ZIP_MANIFEST_NAME, json.dumps(manifest))
            validate_zip_file(path)
            self.assertEqual(read_zip_manifest(path), manifest)
