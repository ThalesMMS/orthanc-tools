import subprocess
import unittest
from pathlib import Path
import tempfile
from unittest import mock
from datetime import date

from orthanc_tools.workflows.client import OrthancClient
from orthanc_tools.workflows.primitives import OrthancSettings


class WorkflowClientTests(unittest.TestCase):
    def make_client(self, getscu_timeout: float = 60.0) -> OrthancClient:
        settings = OrthancSettings(
            base_url="http://127.0.0.1:8042",
            username="admin",
            password="secret",
            dicom_aet="ORTHANC",
            timeout=12.5,
            getscu_timeout=getscu_timeout,
            dicom_modalities={"REMOTE": {"AET": "REMOTE", "Host": "127.0.0.1", "Port": 4242}},
        )
        return OrthancClient(settings)

    def test_create_remote_query_adds_normalize_when_requested(self) -> None:
        client = self.make_client()
        with mock.patch.object(client, "post", return_value={"ID": "query-1"}) as post:
            query_id = client.create_remote_query("REMOTE", "Study", {"StudyDate": "20240101"}, normalize=True)

        self.assertEqual(query_id, "query-1")
        post.assert_called_once_with(
            "/modalities/REMOTE/query",
            {"Level": "Study", "Query": {"StudyDate": "20240101"}, "Normalize": True},
        )

    def test_get_study_series_expanded_falls_back_to_series_ids(self) -> None:
        client = self.make_client()
        with mock.patch.object(client, "get", side_effect=[["series-1"], {"ID": "series-1"}]):
            payload = client.get_study_series_expanded("study-1")

        self.assertEqual(payload, [{"ID": "series-1"}])

    def test_list_study_instances_sorts_and_maps_payload(self) -> None:
        client = self.make_client()
        payload = [
            {
                "ID": "instance-2",
                "ParentSeries": "series-b",
                "SOPInstanceUID": "2.3",
                "InstanceNumber": "2",
                "IndexInSeries": 2,
                "FileSize": 200,
            },
            {
                "ID": "instance-1",
                "ParentSeries": "series-a",
                "SOPInstanceUID": "1.2",
                "InstanceNumber": "1",
                "IndexInSeries": 1,
                "FileSize": 100,
            },
        ]
        with mock.patch.object(client, "get", return_value=payload):
            items = client.list_study_instances("study-1")

        self.assertEqual([item.orthanc_id for item in items], ["instance-1", "instance-2"])
        self.assertEqual(items[0].sop_instance_uid, "1.2")
        self.assertEqual(items[1].file_size, 200)

    def test_list_study_instances_expands_instance_ids_before_mapping(self) -> None:
        client = self.make_client()
        with mock.patch.object(
            client,
            "get",
            side_effect=[
                ["instance-1"],
                {
                    "ID": "instance-1",
                    "ParentSeries": "series-a",
                    "SOPInstanceUID": "1.2",
                    "InstanceNumber": "7",
                },
            ],
        ):
            items = client.list_study_instances("study-1")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].orthanc_id, "instance-1")
        self.assertEqual(items[0].instance_number, "7")

    def test_find_studies_for_day_sorts_missing_dates_with_requested_day_fallback(self) -> None:
        client = self.make_client()
        payload = [
            {
                "ID": "study-2",
                "StudyInstanceUID": "2",
                "StudyDate": "",
            },
            {
                "ID": "study-1",
                "StudyInstanceUID": "1",
                "StudyDate": "20240101",
            },
        ]
        with mock.patch.object(client, "post", side_effect=[payload, []]):
            studies = client.find_studies_for_day(date(2024, 1, 2), page_size=100)

        self.assertEqual([study.orthanc_id for study in studies], ["study-1", "study-2"])

    def test_system_and_modalities_validate_payloads(self) -> None:
        client = self.make_client()
        with mock.patch.object(client, "get", side_effect=[{"Version": "1.0"}, ["REMOTE", 3]]):
            system = client.system()
            modalities = client.list_modalities()

        self.assertEqual(system["Version"], "1.0")
        self.assertEqual(modalities, ["REMOTE", "3"])

    def test_modality_and_query_wrappers_build_expected_paths(self) -> None:
        client = self.make_client()
        with mock.patch.object(client, "put", return_value={"ok": True}) as put:
            client.put_modality("REMOTE", "AET", "host", 4242)
        with mock.patch.object(client, "delete", return_value={"ok": True}) as delete:
            client.delete_modality("REMOTE")
            client.delete_query("query-1")
            client.delete_study("study-1")
        with mock.patch.object(client, "post", side_effect=[{"ID": "child"}, {"Echo": "ok"}]) as post:
            child_id = client.create_child_query("query", "answer", "instances", {"SOPInstanceUID": ""})
            client.echo_modality("REMOTE", timeout=7)

        self.assertEqual(child_id, "child")
        put.assert_called_once_with(
            "/modalities/REMOTE",
            {
                "AET": "AET",
                "Host": "host",
                "Port": 4242,
                "Manufacturer": "Generic",
                "AllowEcho": True,
                "AllowFind": True,
                "AllowGet": True,
                "AllowMove": True,
                "AllowStore": False,
            },
        )
        self.assertEqual(delete.call_args_list[0].args[0], "/modalities/REMOTE")
        self.assertEqual(delete.call_args_list[1].args[0], "/queries/query-1")
        self.assertEqual(delete.call_args_list[2].args[0], "/studies/study-1")
        self.assertEqual(
            post.call_args_list[0].args,
            ("/queries/query/answers/answer/query-instances", {"Query": {"SOPInstanceUID": ""}}),
        )
        self.assertEqual(post.call_args_list[1].args, ("/modalities/REMOTE/echo", {"Timeout": 7}))

    def test_query_answer_helpers_and_lookup_local_study(self) -> None:
        client = self.make_client()
        with mock.patch.object(client, "get", side_effect=[["1", 2], {"Tag": "value"}]):
            answers = client.get_query_answers("query")
            content = client.get_query_answer_content("query", "1")
        with mock.patch.object(client, "post", return_value=[{"ID": "study-1"}]):
            item = client.lookup_local_study("1.2.3")

        self.assertEqual(answers, ["1", "2"])
        self.assertEqual(content, {"Tag": "value"})
        self.assertEqual(item, {"ID": "study-1"})

    def test_statistics_and_instance_expansion_helpers(self) -> None:
        client = self.make_client()
        with mock.patch.object(client, "get", side_effect=[{"CountInstances": 2}, [{"ID": "i-1"}], [{"ID": "i-2"}]]):
            stats = client.get_study_statistics("study")
            series = client.get_study_series_expanded("study")
            instances = client.get_study_instances_expanded("study")

        self.assertEqual(stats["CountInstances"], 2)
        self.assertEqual(series, [{"ID": "i-1"}])
        self.assertEqual(instances, [{"ID": "i-2"}])

    def test_download_and_upload_wrappers_delegate_to_request_or_post(self) -> None:
        client = self.make_client()
        target = Path("/tmp/file.zip")
        handle = object()
        with mock.patch.object(client, "request", side_effect=[{"bytes_written": 10}, {"bytes_written": 20}]) as request:
            download = client.download("/studies/a/archive", target)
            into_handle = client.download_into_handle("/instances/a/file", handle)
        with tempfile.TemporaryDirectory() as tmpdir:
            dicom_path = Path(tmpdir) / "image.dcm"
            dicom_path.write_bytes(b"DICOM")
            with mock.patch.object(client, "post", return_value={"Status": "Success"}) as post:
                imported = client.import_dicom_file(dicom_path)
                uploaded = client.upload_instance_file(dicom_path)

        self.assertEqual(download["bytes_written"], 10)
        self.assertEqual(into_handle["bytes_written"], 20)
        self.assertEqual(request.call_args_list[0].kwargs["stream_to"], target)
        self.assertEqual(request.call_args_list[1].kwargs["stream_handle"], handle)
        self.assertEqual(imported["Status"], "Success")
        self.assertEqual(uploaded["Status"], "Success")
        self.assertEqual(post.call_count, 2)

    def test_archive_and_instance_download_wrappers_use_expected_paths(self) -> None:
        client = self.make_client()
        with mock.patch.object(client, "download", return_value={"bytes_written": 1}) as download:
            client.download_study_archive("study-1", Path("/tmp/a.zip"))
        with mock.patch.object(client, "download_into_handle", return_value={"bytes_written": 2}) as into_handle:
            client.download_instance_file_into_handle("instance-1", object())

        self.assertEqual(download.call_args.args[0], "/studies/study-1/archive")
        self.assertEqual(into_handle.call_args.args[0], "/instances/instance-1/file")

    def test_create_remote_study_query_and_answer_pairs(self) -> None:
        client = self.make_client()
        with mock.patch.object(client, "create_remote_query", return_value="query-1") as create_remote_query:
            query_id = client.create_remote_study_query("REMOTE")
        with self.assertLogs("orthanc_tools.workflows.client", level="WARNING") as logs:
            with mock.patch.object(client, "get_query_answers", return_value=["1", "2"]), mock.patch.object(
                client,
                "get",
                side_effect=RuntimeError("expand unavailable"),
            ), mock.patch.object(
                client,
                "get_query_answer_content",
                side_effect=[{"StudyInstanceUID": "1"}, {"StudyInstanceUID": "2"}],
            ):
                pairs = client.get_query_answer_pairs("query-1")

        self.assertEqual(query_id, "query-1")
        self.assertEqual(create_remote_query.call_args.args[1], "Study")
        self.assertEqual(pairs, [("1", {"StudyInstanceUID": "1"}), ("2", {"StudyInstanceUID": "2"})])
        self.assertIn("get_query_answer_pairs failed", "\n".join(logs.output))

    def test_get_query_answer_pairs_falls_back_on_strict_length_mismatch(self) -> None:
        client = self.make_client()
        with self.assertLogs("orthanc_tools.workflows.client", level="WARNING") as logs:
            with mock.patch.object(client, "get_query_answers", return_value=["1", "2"]), mock.patch.object(
                client,
                "get",
                return_value=[{"StudyInstanceUID": "expanded"}],
            ), mock.patch.object(
                client,
                "get_query_answer_content",
                side_effect=[{"StudyInstanceUID": "1"}, {"StudyInstanceUID": "2"}],
            ):
                pairs = client.get_query_answer_pairs("query-1")

        self.assertEqual(pairs, [("1", {"StudyInstanceUID": "1"}), ("2", {"StudyInstanceUID": "2"})])
        self.assertIn("get_query_answer_pairs failed", "\n".join(logs.output))

    def test_find_study_by_uid_and_series_aliases(self) -> None:
        client = self.make_client()
        with mock.patch.object(client, "post", return_value=[{"ID": "study-1", "StudyInstanceUID": "1.2.3"}]):
            study = client.find_study_by_uid("1.2.3")
        with mock.patch.object(client, "get_study_series_expanded", return_value=[{"ID": "series-1"}]) as expanded:
            series = client.list_study_series("study-1")
        with mock.patch.object(client, "list_study_instances", return_value=["instance"]) as instances:
            export_instances = client.list_study_instances_for_export("study-1")

        self.assertEqual(study.study_uid, "1.2.3")
        self.assertEqual(series, [{"ID": "series-1"}])
        self.assertEqual(export_instances, ["instance"])
        expanded.assert_called_once_with("study-1")
        instances.assert_called_once_with("study-1")

    def test_retrieve_study_move_and_get_paths(self) -> None:
        client = self.make_client()
        with mock.patch.object(client, "post", return_value={"ID": "job-1"}) as post:
            response = client.retrieve_study("REMOTE", "1.2.3", "move", None)
        self.assertEqual(response["ID"], "job-1")
        self.assertEqual(post.call_args.args[0], "/modalities/REMOTE/move")
        self.assertEqual(post.call_args.args[1]["TargetAet"], "ORTHANC")

        def fake_run(
            command: list[str],
            timeout: float,
            capture_output: bool,
            text: bool,
            check: bool,
        ) -> mock.Mock:
            self.assertEqual(timeout, 60.0)
            output_dir = Path(command[3])
            (output_dir / "instance.dcm").write_bytes(b"DICOM")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("orthanc_tools.workflows.client.subprocess.run", side_effect=fake_run), mock.patch.object(
            client, "upload_instance_file", return_value={"Status": "Success"}
        ) as upload:
            imported = client.retrieve_study("REMOTE", "1.2.3", "get", None)

        self.assertEqual(imported, {"Imported": 1})
        upload.assert_called_once()

    def test_retrieve_study_get_raises_when_getscu_times_out(self) -> None:
        client = self.make_client(getscu_timeout=0.5)
        with mock.patch(
            "orthanc_tools.workflows.client.subprocess.run",
            side_effect=subprocess.TimeoutExpired("getscu", 0.5),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                client.retrieve_study("REMOTE", "1.2.3", "get", None)

        self.assertIn("getscu timed out after 0.5s", str(ctx.exception))

    def test_retrieve_study_rejects_invalid_uid_before_getscu(self) -> None:
        client = self.make_client()
        with mock.patch("orthanc_tools.workflows.client.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "StudyInstanceUID"):
                client.retrieve_study("REMOTE", "1.2.bad", "get", None)

        run.assert_not_called()

    def test_list_local_study_map_falls_back_to_individual_fetch(self) -> None:
        client = self.make_client()
        with self.assertLogs("orthanc_tools.workflows.client", level="WARNING") as logs:
            with mock.patch.object(
                client,
                "get",
                side_effect=[
                    RuntimeError("expand failed"),
                    ["study-1"],
                    {"ID": "study-1", "StudyInstanceUID": "1.2.3"},
                ],
            ):
                mapping = client.list_local_study_map()

        self.assertEqual(mapping, {"1.2.3": "study-1"})
        self.assertIn("/studies?expand", "\n".join(logs.output))

    def test_find_studies_for_day_raises_on_stalled_pagination(self) -> None:
        client = self.make_client()
        # First page returns items, second page returns the same orthanc_id → stall
        page = [{"ID": "study-1", "StudyInstanceUID": "1"}]
        with mock.patch.object(client, "post", return_value=page):
            with self.assertRaises(RuntimeError) as ctx:
                client.find_studies_for_day(date(2024, 1, 1), page_size=100)

        self.assertIn("stalled", str(ctx.exception).lower())

    def test_find_study_by_uid_returns_none_when_missing(self) -> None:
        client = self.make_client()
        with mock.patch.object(client, "post", return_value=[]):
            result = client.find_study_by_uid("1.2.3")

        self.assertIsNone(result)

    def test_find_study_by_uid_raises_on_duplicate_match(self) -> None:
        client = self.make_client()
        items = [
            {"ID": "study-1", "StudyInstanceUID": "1.2.3"},
            {"ID": "study-2", "StudyInstanceUID": "1.2.3"},
        ]
        with mock.patch.object(client, "post", return_value=items):
            with self.assertRaises(RuntimeError) as ctx:
                client.find_study_by_uid("1.2.3")

        self.assertIn("More than one", str(ctx.exception))

    def test_get_query_answer_pairs_returns_zip_on_success(self) -> None:
        client = self.make_client()
        expanded_content = [{"StudyInstanceUID": "1"}, {"StudyInstanceUID": "2"}]
        with mock.patch.object(client, "get_query_answers", return_value=["a", "b"]), mock.patch.object(
            client, "get", return_value=expanded_content
        ):
            pairs = client.get_query_answer_pairs("query-1")

        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0], ("a", {"StudyInstanceUID": "1"}))
        self.assertEqual(pairs[1], ("b", {"StudyInstanceUID": "2"}))

    def test_get_query_answer_pairs_returns_empty_list_when_no_answers(self) -> None:
        client = self.make_client()
        with mock.patch.object(client, "get_query_answers", return_value=[]):
            pairs = client.get_query_answer_pairs("query-empty")

        self.assertEqual(pairs, [])

    def test_retrieve_study_move_uses_explicit_target_aet(self) -> None:
        client = self.make_client()
        with mock.patch.object(client, "post", return_value={"ID": "job"}) as post:
            client.retrieve_study("REMOTE", "1.2.3", "move", "OTHER-AET")

        self.assertEqual(post.call_args.args[1]["TargetAet"], "OTHER-AET")

    def test_retrieve_study_get_raises_when_getscu_fails(self) -> None:
        client = self.make_client()
        with mock.patch(
            "orthanc_tools.workflows.client.subprocess.run",
            return_value=mock.Mock(returncode=1, stdout="err", stderr=""),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                client.retrieve_study("REMOTE", "1.2.3", "get", None)

        self.assertIn("getscu failed", str(ctx.exception))

    def test_retrieve_study_get_raises_when_no_files_received(self) -> None:
        client = self.make_client()
        with mock.patch(
            "orthanc_tools.workflows.client.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout="", stderr=""),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                client.retrieve_study("REMOTE", "1.2.3", "get", None)

        self.assertIn("without receiving any instances", str(ctx.exception))

    def test_retrieve_study_get_raises_when_modality_missing_from_settings(self) -> None:
        from orthanc_tools.workflows.primitives import OrthancSettings

        client = OrthancClient(
            OrthancSettings(
                base_url="http://127.0.0.1:8042",
                username="admin",
                password="secret",
                dicom_aet="ORTHANC",
                dicom_modalities={},
            )
        )
        with self.assertRaises(RuntimeError) as ctx:
            client.retrieve_study("MISSING", "1.2.3", "get", None)

        self.assertIn("missing from DicomModalities", str(ctx.exception))

    def test_extract_modality_endpoint_accepts_list_and_dict_forms(self) -> None:
        from orthanc_tools.workflows.client import _extract_modality_endpoint

        aet, host, port = _extract_modality_endpoint({"AET": "MY-AET", "Host": "192.168.1.1", "Port": 4242})
        self.assertEqual((aet, host, port), ("MY-AET", "192.168.1.1", 4242))

        aet, host, port = _extract_modality_endpoint(["LIST-AET", "10.0.0.1", 104])
        self.assertEqual((aet, host, port), ("LIST-AET", "10.0.0.1", 104))

    def test_extract_modality_endpoint_accepts_string_port(self) -> None:
        from orthanc_tools.workflows.client import _extract_modality_endpoint

        aet, host, port = _extract_modality_endpoint({"AET": "A", "Host": "h", "Port": "11112"})
        self.assertEqual(port, 11112)

    def test_extract_modality_endpoint_raises_on_invalid_config(self) -> None:
        from orthanc_tools.workflows.client import _extract_modality_endpoint

        with self.assertRaises(RuntimeError):
            _extract_modality_endpoint("not-a-dict-or-list")
        with self.assertRaises(RuntimeError):
            _extract_modality_endpoint({"AET": "A", "Host": "h", "Port": "invalid"})
        with self.assertRaises(RuntimeError):
            _extract_modality_endpoint({"AET": "", "Host": "h", "Port": 4242})

    def test_normalize_study_day_returns_fallback_for_non_eight_digit_date(self) -> None:
        from orthanc_tools.workflows.client import _normalize_study_day

        self.assertEqual(_normalize_study_day("20240101", date(2024, 2, 1)), "20240101")
        self.assertEqual(_normalize_study_day("", date(2024, 2, 1)), "20240201")
        self.assertEqual(_normalize_study_day(None, date(2024, 3, 15)), "20240315")
        self.assertEqual(_normalize_study_day("2024-01-01", date(2024, 2, 1)), "20240201")

    def test_list_local_study_map_succeeds_with_expanded_response(self) -> None:
        client = self.make_client()
        expanded = [
            {"ID": "study-a", "StudyInstanceUID": "1.1.1"},
            {"ID": "study-b", "StudyInstanceUID": "2.2.2"},
        ]
        with mock.patch.object(client, "get", return_value=expanded):
            mapping = client.list_local_study_map()

        self.assertEqual(mapping, {"1.1.1": "study-a", "2.2.2": "study-b"})

    def test_lookup_local_study_raises_on_multiple_matches(self) -> None:
        client = self.make_client()
        with mock.patch.object(client, "post", return_value=[{"ID": "a"}, {"ID": "b"}]):
            with self.assertRaises(RuntimeError) as ctx:
                client.lookup_local_study("1.2.3")

        self.assertIn("Multiple local studies", str(ctx.exception))

    def test_get_study_instances_expanded_falls_back_to_instance_ids(self) -> None:
        client = self.make_client()
        with mock.patch.object(client, "get", side_effect=[["instance-1"], {"ID": "instance-1"}]):
            payload = client.get_study_instances_expanded("study-1")

        self.assertEqual(payload, [{"ID": "instance-1"}])

    def test_build_study_uid_map_filters_incomplete_entries(self) -> None:
        from orthanc_tools.workflows.client import _build_study_uid_map

        items = [
            {"ID": "study-1", "StudyInstanceUID": "1.2.3"},
            {"ID": "study-2"},  # missing UID
            {"StudyInstanceUID": "4.5.6"},  # missing ID
        ]
        mapping = _build_study_uid_map(items)

        self.assertEqual(mapping, {"1.2.3": "study-1"})
