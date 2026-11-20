import pytest

from software_factory.bus import EventBus
from software_factory.orchestrator import Orchestrator
from software_factory.store import DocumentPathError, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


@pytest.fixture
def project(store):
    p = store.create_project("hello", "demo", "me", "repo", "# BRD")
    return store.update_project(p["id"], setup_status="ready", delivery_started=True, board_mode="labels")


@pytest.mark.parametrize("name", ["../secrets/x.md", "documents/../../x.md", "C:/x.md", "notes.txt", "documents/a/b.md"])
def test_document_paths_are_confined_to_project(store, project, name):
    with pytest.raises(DocumentPathError):
        store.write_document(project["id"], name, "x")


def test_allowed_documents_round_trip(store, project):
    store.write_document(project["id"], "documents/architecture.md", "# Arch")
    assert store.read_document(project["id"], "documents/architecture.md") == "# Arch"


def test_crlf_documents_keep_single_line_breaks_across_saves(store, project):
    table = "| ID | Requirement |\r\n|---|---|\r\n| FR-001 | Hello |\r\n"
    store.write_document(project["id"], "BRD.md", table)
    store.write_document(project["id"], "BRD.md", store.read_document(project["id"], "BRD.md"))
    assert store.read_document(project["id"], "BRD.md") == "| ID | Requirement |\n|---|---|\n| FR-001 | Hello |\n"
    created = store.create_project("crlf", "", "me", "repo", "a\r\nb")
    assert store.read_document(created["id"], "BRD.md") == "a\nb"


class FakeGitHub:
    def __init__(self, assigned=False, linked=()):
        self.assigned = assigned
        self.linked = list(linked)
        self.assign_calls = []

    def get_issue(self, n):
        return {"state": "open", "html_url": f"https://x/{n}",
                "assignees": [{"login": "Copilot"}] if self.assigned else []}

    def issue_assigned_to_copilot(self, issue):
        return bool(issue["assignees"])

    def linked_pull_requests(self, n):
        return self.linked

    def assign_copilot(self, n, instructions=""):
        self.assign_calls.append(n)

    def set_status_label(self, *a, **k):
        pass


def make_task(store, pid, deps=()):
    t = store.create_task(pid, f"task {len(store.list_tasks(pid)) + 1}", "", [], list(deps))
    return store.update_task(pid, t["key"], issue_number=len(store.list_tasks(pid)) + 10)


def test_dispatch_respects_dependencies_and_parallel_limit(store, project):
    pid = project["id"]
    store.update_project(pid, parallel_limit=2)
    for deps in ((), (), (), ("T1",)):
        make_task(store, pid, deps)
    gh = FakeGitHub()
    Orchestrator(store, EventBus(), 60).dispatch_ready(gh, store.get_project(pid))
    states = {t["key"]: t["state"] for t in store.list_tasks(pid)}
    assert states == {"T1": "dispatched", "T2": "dispatched", "T3": "todo", "T4": "todo"}
    assert len(gh.assign_calls) == 2


def test_dispatch_does_not_reassign_work_already_started_on_github(store, project):
    pid = project["id"]
    make_task(store, pid)
    gh = FakeGitHub(assigned=True)
    Orchestrator(store, EventBus(), 60).dispatch_ready(gh, store.get_project(pid))
    assert gh.assign_calls == []
    assert store.get_task(pid, "T1")["state"] == "dispatched"


def test_find_pr_accepts_copilot_branch_attributed_to_user(store, project):
    pid = project["id"]
    task = make_task(store, pid)

    class PrGitHub(FakeGitHub):
        def get_pr(self, n):
            return {"number": n, "user": {"login": "me"}, "head": {"ref": "copilot/setup"}, "base": {"ref": "main"}}

    gh = PrGitHub(linked=[{"number": 3}])
    assert Orchestrator(store, EventBus(), 60).find_pr(gh, task)["number"] == 3
