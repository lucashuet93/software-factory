from fastapi.testclient import TestClient

from software_factory.agent import LeadAgent
from software_factory.api import create_app
from software_factory.bus import EventBus
from software_factory.config import Settings
from software_factory.orchestrator import Orchestrator
from software_factory.store import Store


def make_client(tmp_path):
    settings = Settings(project_endpoint="", model_deployment="", tenant_id="", agent_name="t", home=tmp_path,
                        log_level="INFO", poll_seconds=60, host="127.0.0.1", port=0)
    store = Store(tmp_path)
    bus = EventBus()
    orch = Orchestrator(store, bus, 60)
    app = create_app(settings, store, bus, orch, LeadAgent(settings, store, orch, bus), start_worker=False)
    return TestClient(app), store


def test_delete_removes_all_local_project_data(tmp_path):
    client, store = make_client(tmp_path)
    p = client.post("/api/projects", json={"name": "hello", "repo_url": "https://github.com/me/repo",
                                           "pat": "placeholder-token", "brd": "# BRD"}).json()
    keep = client.post("/api/projects", json={"name": "other", "repo_url": "https://github.com/me/other",
                                              "pat": "placeholder-token"}).json()
    store.add_message(p["id"], "user", "hi")
    store.create_task(p["id"], "task", "", [], [])

    assert client.delete(f"/api/projects/{p['id']}").status_code == 204

    assert client.get(f"/api/projects/{p['id']}").status_code == 404
    assert not (tmp_path / "projects" / p["id"]).exists()
    assert store.get_pat(p["id"]) is None
    assert store.list_messages(p["id"]) == [] and store.list_tasks(p["id"]) == []
    assert [x["id"] for x in client.get("/api/projects").json()] == [keep["id"]]
    assert client.delete(f"/api/projects/{p['id']}").status_code == 404


def test_late_background_writes_for_deleted_project_are_ignored(tmp_path):
    _, store = make_client(tmp_path)
    p = store.create_project("hello", "", "me", "repo", "# BRD")
    store.delete_project(p["id"])
    assert store.add_message(p["id"], "assistant", "late reply") is None
    assert store.add_event(p["id"], "merged", "late event") is None
    assert store.list_messages(p["id"]) == [] and store.list_events(p["id"]) == []
