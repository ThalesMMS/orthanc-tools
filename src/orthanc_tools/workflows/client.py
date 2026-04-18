from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib import parse

from orthanc_tools.dicom import date_to_orthanc, pick_tag, safe_text
from orthanc_tools.orthanc_api import OrthancRestClient
from orthanc_tools.workflows.primitives import OrthancSettings, extract_resource_id


LOGGER = logging.getLogger(__name__)
_DICOM_UID_RE = re.compile(r"^[0-9.]+$")

REQUESTED_TAGS = [
    "StudyInstanceUID",
    "PatientName",
    "PatientBirthDate",
    "StudyDate",
    "StudyDescription",
    "AccessionNumber",
]


def _validate_study_uid(study_uid: str) -> str:
    value = safe_text(study_uid).strip()
    if not value:
        raise ValueError("StudyInstanceUID is required for retrieval.")
    if _DICOM_UID_RE.fullmatch(value) is None:
        raise ValueError("Invalid StudyInstanceUID; expected only digits and dots.")
    return value


@dataclass(frozen=True)
class StudyInfo:
    orthanc_id: str
    study_uid: str
    patient_name: str
    patient_birth_date: str
    study_date: str
    study_description: str
    accession_number: str
    is_stable: bool | None


@dataclass(frozen=True)
class InstanceInfo:
    orthanc_id: str
    parent_series_id: str
    sop_instance_uid: str
    instance_number: str
    index_in_series: int | None
    file_size: int | None


ExportInstanceInfo = InstanceInfo


class OrthancClient(OrthancRestClient):
    def __init__(self, settings: OrthancSettings, timeout: float | None = None):
        self.settings = settings
        super().__init__(
            settings.base_url,
            settings.username,
            settings.password,
            timeout=settings.timeout if timeout is None else timeout,
        )

    def download(self, path: str, output_path: Path) -> dict[str, Any]:
        return self.request("GET", path, accept="application/octet-stream", stream_to=output_path)

    def download_into_handle(self, path: str, handle: Any) -> dict[str, Any]:
        return self.request("GET", path, accept="application/octet-stream", stream_handle=handle)

    def system(self) -> dict[str, Any]:
        payload = self.get("/system")
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected /system response from Orthanc.")
        return payload

    def list_modalities(self) -> list[str]:
        payload = self.get("/modalities")
        if not isinstance(payload, list):
            raise RuntimeError("/modalities did not return a list.")
        return [safe_text(item) for item in payload]

    def put_modality(self, name: str, aet: str, host: str, port: int) -> Any:
        payload = {
            "AET": aet,
            "Host": host,
            "Port": int(port),
            "Manufacturer": "Generic",
            "AllowEcho": True,
            "AllowFind": True,
            "AllowGet": True,
            "AllowMove": True,
            "AllowStore": False,
        }
        return self.put(f"/modalities/{parse.quote(name, safe='')}", payload)

    def delete_modality(self, name: str) -> Any:
        return self.delete(f"/modalities/{parse.quote(name, safe='')}")

    def echo_modality(self, name: str, timeout: int = 10) -> Any:
        return self.post(f"/modalities/{parse.quote(name, safe='')}/echo", {"Timeout": int(timeout)})

    def create_remote_query(
        self,
        modality: str,
        level: str,
        query_fields: dict[str, str],
        normalize: bool | None = None,
    ) -> str:
        payload: dict[str, Any] = {"Level": level, "Query": query_fields}
        if normalize is not None:
            payload["Normalize"] = normalize
        response = self.post(f"/modalities/{parse.quote(modality, safe='')}/query", payload)
        return extract_resource_id(response)

    def create_remote_study_query(self, modality: str) -> str:
        return self.create_remote_query(
            modality,
            "Study",
            {
                "StudyInstanceUID": "",
                "PatientName": "",
                "PatientID": "",
                "StudyDate": "",
                "StudyDescription": "",
                "AccessionNumber": "",
                "NumberOfStudyRelatedSeries": "",
                "NumberOfStudyRelatedInstances": "",
            },
        )

    def create_child_query(
        self,
        query_id: str,
        answer_id: str,
        child_level: str,
        query_fields: dict[str, str],
    ) -> str:
        response = self.post(
            f"/queries/{parse.quote(query_id, safe='')}/answers/{parse.quote(str(answer_id), safe='')}/query-{child_level}",
            {"Query": query_fields},
        )
        return extract_resource_id(response)

    def get_query_answers(self, query_id: str) -> list[str]:
        payload = self.get(f"/queries/{parse.quote(query_id, safe='')}/answers")
        if not isinstance(payload, list):
            raise RuntimeError(f"Unexpected /queries/{query_id}/answers response.")
        return [safe_text(item) for item in payload]

    def get_query_answer_content(self, query_id: str, answer_id: str) -> dict[str, Any]:
        payload = self.get(
            f"/queries/{parse.quote(query_id, safe='')}/answers/{parse.quote(str(answer_id), safe='')}/content"
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected answer content for query {query_id}, answer {answer_id}.")
        return payload

    def get_query_answer_pairs(self, query_id: str) -> list[tuple[str, dict[str, Any]]]:
        answer_ids = self.get_query_answers(query_id)
        if not answer_ids:
            return []
        try:
            expanded = self.get(f"/queries/{parse.quote(query_id, safe='')}/answers?expand&simplify")
            if isinstance(expanded, list) and all(isinstance(item, dict) for item in expanded):
                return list(zip(answer_ids, expanded, strict=True))
            LOGGER.warning(
                "Falling back to per-answer content for /queries/%s/answers?expand&simplify; "
                "unexpected expanded response shape.",
                query_id,
            )
        except Exception as exc:
            LOGGER.warning(
                "Falling back to per-answer content for /queries/%s/answers?expand&simplify "
                "after get_query_answer_pairs failed: %s",
                query_id,
                exc,
                exc_info=True,
            )
        return [(answer_id, self.get_query_answer_content(query_id, answer_id)) for answer_id in answer_ids]

    def delete_query(self, query_id: str) -> Any:
        return self.delete(f"/queries/{parse.quote(query_id, safe='')}")

    def lookup_local_study(self, study_uid: str) -> dict[str, Any] | None:
        payload = {
            "Level": "Study",
            "Expand": True,
            "Query": {"StudyInstanceUID": study_uid},
        }
        result = self.post("/tools/find", payload)
        if not isinstance(result, list):
            raise RuntimeError("Unexpected /tools/find response.")
        if not result:
            return None
        if len(result) > 1:
            raise RuntimeError(f"Multiple local studies found for StudyInstanceUID {study_uid}.")
        item = result[0]
        if not isinstance(item, dict):
            raise RuntimeError("Expanded /tools/find response item is not an object.")
        return item

    def get_study_statistics(self, study_id: str) -> dict[str, Any]:
        payload = self.get(f"/studies/{parse.quote(study_id, safe='')}/statistics")
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected statistics response for study {study_id}.")
        return payload

    def get_study_series_expanded(self, study_id: str) -> list[dict[str, Any]]:
        payload = self.get(f"/studies/{parse.quote(study_id, safe='')}/series?expand")
        if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
            return payload
        if isinstance(payload, list):
            result = [self.get(f"/series/{parse.quote(safe_text(series_id), safe='')}") for series_id in payload]
            if all(isinstance(item, dict) for item in result):
                return result
        raise RuntimeError(f"Unexpected /studies/{study_id}/series?expand response.")

    def get_study_instances_expanded(self, study_id: str) -> list[dict[str, Any]]:
        payload = self.get(f"/studies/{parse.quote(study_id, safe='')}/instances?expand")
        if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
            return payload
        if isinstance(payload, list):
            result = [self.get(f"/instances/{parse.quote(safe_text(instance_id), safe='')}") for instance_id in payload]
            if all(isinstance(item, dict) for item in result):
                return result
        raise RuntimeError(f"Unexpected /studies/{study_id}/instances?expand response.")

    def find_studies_for_day(self, day: date, page_size: int) -> list[StudyInfo]:
        day_string = date_to_orthanc(day)
        studies: list[StudyInfo] = []
        seen_ids: set[str] = set()
        since = 0

        while True:
            payload = {
                "Level": "Study",
                "Expand": True,
                "Query": {"StudyDate": day_string},
                "RequestedTags": REQUESTED_TAGS,
                "Limit": page_size,
                "Since": since,
            }
            page = self.post("/tools/find", payload)
            if not isinstance(page, list):
                raise RuntimeError("/tools/find did not return a list")
            if not page:
                break

            new_items = 0
            for item in page:
                if not isinstance(item, dict):
                    raise RuntimeError("/tools/find returned a non-object item")
                study = self._study_info_from_item(item)
                if study.orthanc_id in seen_ids:
                    continue
                seen_ids.add(study.orthanc_id)
                studies.append(study)
                new_items += 1

            if new_items == 0:
                raise RuntimeError("Pagination stalled while listing studies for the day")
            since += len(page)

        studies.sort(
            key=lambda study: (
                _normalize_study_day(study.study_date, day),
                study.study_uid,
                study.orthanc_id,
            )
        )
        return studies

    def find_study_by_uid(self, study_uid: str) -> StudyInfo | None:
        payload = {
            "Level": "Study",
            "Expand": True,
            "Query": {"StudyInstanceUID": study_uid},
            "RequestedTags": REQUESTED_TAGS,
            "Limit": 2,
            "Since": 0,
        }
        result = self.post("/tools/find", payload)
        if not isinstance(result, list):
            raise RuntimeError("/tools/find did not return a list while locating StudyInstanceUID")
        if not result:
            return None
        if len(result) > 1:
            raise RuntimeError(f"More than one study matched StudyInstanceUID {study_uid}")
        item = result[0]
        if not isinstance(item, dict):
            raise RuntimeError("/tools/find returned a non-object item while locating a study")
        return self._study_info_from_item(item, fallback_study_uid=study_uid)

    def list_study_series(self, study_id: str) -> list[dict[str, Any]]:
        return self.get_study_series_expanded(study_id)

    def list_study_instances(self, study_id: str) -> list[InstanceInfo]:
        payload = self.get_study_instances_expanded(study_id)
        result: list[InstanceInfo] = []
        for item in payload:
            instance_id = safe_text(item.get("ID")).strip()
            if not instance_id:
                raise RuntimeError(f"/studies/{study_id}/instances returned an item without ID")
            result.append(
                InstanceInfo(
                    orthanc_id=instance_id,
                    parent_series_id=safe_text(item.get("ParentSeries")).strip(),
                    sop_instance_uid=(pick_tag(item, "SOPInstanceUID") or "").strip(),
                    instance_number=(pick_tag(item, "InstanceNumber") or "").strip(),
                    index_in_series=item.get("IndexInSeries") if isinstance(item.get("IndexInSeries"), int) else None,
                    file_size=item.get("FileSize") if isinstance(item.get("FileSize"), int) else None,
                )
            )
        result.sort(
            key=lambda item: (
                item.parent_series_id,
                item.index_in_series if item.index_in_series is not None else 10**9,
                item.instance_number,
                item.orthanc_id,
            )
        )
        return result

    def list_study_instances_for_export(self, study_id: str) -> list[ExportInstanceInfo]:
        return self.list_study_instances(study_id)

    def import_dicom_file(self, path: Path) -> Any:
        return self.post("/instances", path.read_bytes(), content_type="application/dicom")

    def upload_instance_file(self, path: Path) -> Any:
        return self.import_dicom_file(path)

    def delete_study(self, study_id: str) -> Any:
        return self.delete(f"/studies/{parse.quote(study_id, safe='')}")

    def download_study_archive(self, study_id: str, output_path: Path) -> dict[str, Any]:
        return self.download(f"/studies/{parse.quote(study_id, safe='')}/archive", output_path)

    def download_instance_file_into_handle(self, instance_id: str, handle: Any) -> dict[str, Any]:
        return self.download_into_handle(f"/instances/{parse.quote(instance_id, safe='')}/file", handle)

    def retrieve_study(
        self,
        modality: str,
        study_uid: str,
        method: str,
        target_aet: str | None,
    ) -> Any:
        study_uid = _validate_study_uid(study_uid)
        if method == "move":
            target = target_aet or safe_text(self.settings.dicom_aet)
            if not target:
                raise RuntimeError("A DICOM target AE title is required for C-MOVE retrieval.")
            payload: dict[str, Any] = {
                "Level": "Study",
                "Resources": [{"StudyInstanceUID": study_uid}],
                "Synchronous": True,
                "TargetAet": target,
            }
            return self.post(f"/modalities/{parse.quote(modality, safe='')}/move", payload)

        remote = (self.settings.dicom_modalities or {}).get(modality)
        if remote is None:
            raise RuntimeError(f"Remote modality {modality!r} is missing from DicomModalities.")
        remote_aet, remote_host, remote_port = _extract_modality_endpoint(remote)
        calling_aet = safe_text(self.settings.dicom_aet)
        if not calling_aet:
            raise RuntimeError("A local DICOM AE title is required for C-GET retrieval.")

        with tempfile.TemporaryDirectory(prefix="orthanc-getscu-") as temp_dir:
            command = [
                "getscu",
                "-S",
                "-od",
                temp_dir,
                "-aet",
                calling_aet,
                "-aec",
                remote_aet,
                remote_host,
                str(remote_port),
                "-k",
                "QueryRetrieveLevel=STUDY",
                "-k",
                f"StudyInstanceUID={study_uid}",
            ]
            timeout = float(getattr(self.settings, "getscu_timeout", 60.0))
            try:
                result = subprocess.run(command, timeout=timeout, capture_output=True, text=True, check=False)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"getscu timed out after {timeout}s: {exc}") from exc
            if result.returncode != 0:
                details = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
                raise RuntimeError(
                    f"getscu failed with exit code {result.returncode}: {details or 'no output'}"
                )

            files = sorted(path for path in Path(temp_dir).rglob("*") if path.is_file())
            if not files:
                raise RuntimeError("getscu completed without receiving any instances.")
            for instance_path in files:
                self.upload_instance_file(instance_path)
            return {"Imported": len(files)}

    def list_local_study_map(self) -> dict[str, str]:
        try:
            items = self.get("/studies?expand")
            if isinstance(items, list) and all(isinstance(item, dict) for item in items):
                return _build_study_uid_map(items)
            LOGGER.warning("Falling back to /studies after /studies?expand returned an unexpected response shape.")
        except Exception as exc:
            LOGGER.warning(
                "Falling back to /studies after /studies?expand or _build_study_uid_map failed: %s",
                exc,
                exc_info=True,
            )
        study_ids = self.get("/studies")
        if not isinstance(study_ids, list):
            raise RuntimeError("Unexpected response from /studies.")
        result: dict[str, str] = {}
        for study_id in study_ids:
            study = self.get(f"/studies/{parse.quote(safe_text(study_id), safe='')}")
            if isinstance(study, dict):
                study_uid = pick_tag(study, "StudyInstanceUID")
                if study_uid:
                    result[study_uid] = safe_text(study.get("ID") or study_id)
        return result

    def _study_info_from_item(self, item: dict[str, Any], fallback_study_uid: str = "") -> StudyInfo:
        orthanc_id = safe_text(item.get("ID")).strip()
        study_uid = (pick_tag(item, "StudyInstanceUID") or "").strip() or fallback_study_uid
        if not orthanc_id:
            raise RuntimeError("/tools/find returned a study without ID")
        if not study_uid:
            raise RuntimeError(
                f"Study {orthanc_id} is missing StudyInstanceUID in the /tools/find response"
            )
        return StudyInfo(
            orthanc_id=orthanc_id,
            study_uid=study_uid,
            patient_name=(pick_tag(item, "PatientName") or "").strip(),
            patient_birth_date=(pick_tag(item, "PatientBirthDate") or "").strip(),
            study_date=(pick_tag(item, "StudyDate") or "").strip(),
            study_description=(pick_tag(item, "StudyDescription") or "").strip(),
            accession_number=(pick_tag(item, "AccessionNumber") or "").strip(),
            is_stable=item.get("IsStable") if isinstance(item.get("IsStable"), bool) else None,
        )


def _build_study_uid_map(items: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in items:
        study_uid = pick_tag(item, "StudyInstanceUID")
        study_id = safe_text(item.get("ID"))
        if study_uid and study_id:
            mapping[study_uid] = study_id
    return mapping


def _extract_modality_endpoint(value: Any) -> tuple[str, str, int]:
    if isinstance(value, dict):
        aet = safe_text(value.get("AET"))
        host = safe_text(value.get("Host"))
        port_value = value.get("Port")
    elif isinstance(value, (list, tuple)) and len(value) >= 3:
        aet = safe_text(value[0])
        host = safe_text(value[1])
        port_value = value[2]
    else:
        raise RuntimeError(f"Unsupported DicomModalities entry: {value!r}")

    if isinstance(port_value, int):
        port = port_value
    elif isinstance(port_value, str) and port_value.isdigit():
        port = int(port_value)
    else:
        raise RuntimeError(f"Invalid remote modality port: {port_value!r}")

    if not aet or not host:
        raise RuntimeError(f"Incomplete remote modality configuration: {value!r}")
    return aet, host, port


def _normalize_study_day(study_date: str | None, fallback: date) -> str:
    value = (study_date or "").strip()
    if len(value) == 8 and value.isdigit():
        return value
    return date_to_orthanc(fallback)
