from __future__ import annotations

from argparse import Namespace

from orthanc_tools.workflows.mirror_retrieval import MirrorWorkflowMixin, StudyState


class FakeDashboard:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def log(self, message: str) -> None:
        self.messages.append(message)


class FakeMirrorClient:
    """Minimal client double used by MirrorWorkflowMixin methods."""

    def __init__(self) -> None:
        self.deleted_queries: list[str] = []
        self.retrieved_studies: list[tuple] = []
        self.deleted_studies: list[str] = []

        self._system_info: dict = {"Name": "TestOrthanc", "Version": "1.12.0"}
        self._local_study: dict | None = None
        self._study_stats: dict = {}
        self._query_answer_pairs: dict[str, list[tuple[str, dict]]] = {}
        self._local_study_map: dict[str, str] = {}
        self._series_expanded: list[dict] = []
        self._instances_expanded: list[dict] = []

        self.settings = Namespace(base_url="http://orthanc:8042")

    def system(self) -> dict:
        return self._system_info

    def lookup_local_study(self, study_uid: str) -> dict | None:
        return self._local_study

    def get_study_statistics(self, orthanc_id: str) -> dict:
        return self._study_stats

    def create_remote_study_query(self, modality: str) -> str:
        self._last_study_query_modality = modality
        return "remote-query-1"

    def get_query_answer_pairs(self, query_id: str) -> list[tuple[str, dict]]:
        return self._query_answer_pairs.get(query_id, [])

    def create_child_query(
        self,
        query_id: str,
        answer_id: str,
        level: str,
        query_fields: dict,
    ) -> str:
        key = (query_id, answer_id, level)
        self._child_query_map = getattr(self, "_child_query_map", {})
        return self._child_query_map.get(key, f"child-{query_id}-{answer_id}-{level}")

    def delete_query(self, query_id: str) -> None:
        self.deleted_queries.append(query_id)

    def retrieve_study(
        self,
        modality: str,
        study_uid: str,
        method: str,
        target_aet: str,
    ) -> None:
        self.retrieved_studies.append((modality, study_uid, method, target_aet))

    def delete_study(self, study_id: str) -> None:
        self.deleted_studies.append(study_id)

    def get_study_series_expanded(self, study_id: str) -> list[dict]:
        return self._series_expanded

    def get_study_instances_expanded(self, study_id: str) -> list[dict]:
        return self._instances_expanded

    def list_local_study_map(self) -> dict[str, str]:
        return self._local_study_map


def _make_args(**overrides) -> Namespace:
    defaults = dict(
        retrieve_method="move",
        target_aet="MY_AET",
        limit_studies=None,
        allow_empty_remote=False,
        max_retries=2,
        settle_seconds=0,
        repair_mode="skip",
    )
    defaults.update(overrides)
    return Namespace(**defaults)


class ConcreteMirrorWorkflow(MirrorWorkflowMixin):
    """Concrete subclass that satisfies the attribute contract expected by the mixin."""

    def __init__(self, *, args=None, client=None) -> None:
        self.args = args or _make_args()
        self.client = client or FakeMirrorClient()
        self.dashboard = FakeDashboard()
        self.remote_modality = "REMOTE_PACS"
        self.remote_query_id: str | None = None
        self.studies: list[StudyState] = []
        self.current_study: StudyState | None = None
        self.extra_local_studies: dict[str, str] = {}
        self.phase = "idle"
