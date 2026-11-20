import sqlite3
import threading

import pytest

from software_factory import agent as agent_mod
from software_factory import gates
from software_factory.bus import EventBus
from software_factory.github import CheckSummary
from software_factory.orchestrator import MAX_ATTEMPTS, Orchestrator
from software_factory.store import Store

FIELDS = {"outcome": "Banner", "scope": "Add banner", "exclusions": "", "acceptance_criteria": ["Banner shows"],
          "verification": "Tests", "context": ""}


class FakeGitHub:
    slug = "me/repo"

    def __init__(self):
        self.calls = []
        self.assigned = True
        self.prs = {7: {"number": 7, "state": "open", "html_url": "https://x/pull/7", "head": {"sha": "abc"},
                        "body": "Done. TODO: add mobile test", "title": "Banner"}}
        self.linked = [{"number": 7, "state": "CLOSED"}]
        self.issue_body = None

    def get_pr(self, n):
        return self.prs[n]

    def close_pr(self, n, comment):
        self.calls.append(("close_pr", n))
        self.prs[n]["state"] = "closed"

    def unassign_copilot(self, n):
        self.calls.append(("unassign", n))
        self.assigned = False

    def update_issue(self, n, **fields):
        self.calls.append(("update_issue", n, fields.get("state")))
        self.issue_body = fields["body"]

    def get_issue(self, n):
        return {"state": "open", "html_url": f"https://x/issues/{n}",
                "assignees": [{"login": "Copilot"}] if self.assigned else []}

    def issue_assigned_to_copilot(self, issue):
        return bool(issue["assignees"])

    def linked_pull_requests(self, n):
        return self.linked

    def assign_copilot(self, n, instructions=""):
        self.calls.append(("assign", n, instructions))
        self.assigned = True

    def copilot_reviews(self, n):
        return [{"id": 1, "commit_id": "abc", "state": "COMMENTED", "body": "<!-- ccr -->Changes recommended"}]

    def review_comments(self, n, rid):
        return [{"path": "index.html", "line": 3, "body": "Banner is not blue",
                 "copilot_code_review_severity": "medium"}]

    def check_summary(self, sha):
        return CheckSummary(total=1, failed=["test (failure)"])

    def pr_files(self, n):
        return ["index.html"]

    next_issue = 10

    def create_issue(self, title, body, labels=None):
        self.calls.append(("create_issue", title))
        FakeGitHub.next_issue += 1
        n = FakeGitHub.next_issue
        return {"number": n, "node_id": f"I{n}", "html_url": f"https://x/issues/{n}"}

    def set_status_label(self, *a, **k):
        pass


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path)
    p = store.create_project("hello", "", "me", "repo", "# BRD")
    store.update_project(p["id"], setup_status="ready", delivery_started=True, board_mode="labels")
    orch = Orchestrator(store, EventBus(), 60)
    gh = FakeGitHub()
    orch._github_factory = lambda project: gh
    woken = []
    orch.on_event = woken.append
    task = store.create_task(p["id"], "Banner", "", ["FR-001"], [])
    store.update_task(p["id"], "T1", issue_number=1, pr_number=7)
    return store, orch, gh, p["id"], woken


def block_t1(store, orch, gh, pid, attempt=1):
    store.update_task(pid, "T1", attempt=attempt, state="reviewing")
    task = store.get_task(pid, "T1")
    orch.block(gh, store.get_project(pid), task, "Remediation limit of 3 cycles reached",
               ["[medium] `index.html:3`: Banner is not blue"], pr=gh.prs[7])


def test_block_notifies_agent_with_copilot_review_history(env):
    store, orch, gh, pid, woken = env
    block_t1(store, orch, gh, pid)
    event = [m for m in store.list_messages(pid) if m["role"] == "event"][-1]["content"]
    assert woken == [pid]
    assert "attempt 1 of 3" in event and "2 automatic attempt(s) remain" in event
    assert "Banner is not blue" in event and "test (failure)" in event
    assert "Banner is not blue" in store.get_task(pid, "T1")["attempt_report"]


def test_retry_abandons_pr_rewrites_issue_and_reassigns_fresh(env):
    store, orch, gh, pid, _ = env
    block_t1(store, orch, gh, pid)
    orch.retry_task(pid, "T1", "Banner (blue)", FIELDS, ["FR-001"], [], "Use background #0056b3 on the header.",
                    customer_turn=False)

    t = store.get_task(pid, "T1")
    assert (t["state"], t["attempt"], t["remediation_count"], t["pr_number"]) == ("todo", 2, 0, None)
    assert t["abandoned_prs"] == [7]
    assert ("close_pr", 7) in gh.calls and ("unassign", 1) in gh.calls and ("update_issue", 1, "open") in gh.calls
    assert "## Notes from previous attempts" in gh.issue_body and "#0056b3" in gh.issue_body

    # The abandoned, closed PR must not count as work in progress, so the next dispatch starts a fresh session.
    orch.dispatch_ready(gh, store.get_project(pid))
    assign = [c for c in gh.calls if c[0] == "assign"]
    assert len(assign) == 1 and "attempt 2 of 3" in assign[0][2]
    assert store.get_task(pid, "T1")["state"] == "dispatched"
    assert orch.find_pr(gh, store.get_task(pid, "T1")) is None


def test_attempt_limit_requires_customer_turn_but_is_not_a_dead_end(env):
    store, orch, gh, pid, _ = env
    block_t1(store, orch, gh, pid, attempt=MAX_ATTEMPTS)
    assert "last of 3 automatic attempts" in store.list_messages(pid)[-1]["content"]
    with pytest.raises(ValueError, match="all 3 automatic attempts"):
        orch.retry_task(pid, "T1", "Banner", FIELDS, [], [], "notes", customer_turn=False)
    t = orch.retry_task(pid, "T1", "Banner", FIELDS, [], [], "Customer agreed to simplify.", customer_turn=True)
    assert (t["state"], t["attempt"]) == ("todo", MAX_ATTEMPTS + 1)


def test_retry_can_add_a_new_prerequisite_but_not_a_cycle(env):
    store, orch, gh, pid, _ = env
    block_t1(store, orch, gh, pid)
    store.create_task(pid, "Fix CI", "", [], ["T1"])
    with pytest.raises(ValueError, match="cycle"):
        orch.retry_task(pid, "T1", "Banner", FIELDS, [], ["T2"], "notes", customer_turn=False)
    store.update_task(pid, "T2", depends_on=[], issue_number=2)
    t = orch.retry_task(pid, "T1", "Banner", FIELDS, [], ["T2"], "Wait for CI fix.", customer_turn=False)
    assert t["depends_on"] == ["T2"]
    assert orch.ready_tasks(pid)[0]["key"] == "T2"  # T1 now waits for its new prerequisite


def test_merge_creates_follow_up_for_low_findings_and_tells_agent(env):
    store, orch, gh, pid, woken = env
    store.update_task(pid, "T1", state="reviewing", deferred_findings=["[low] `index.html:9`: rename class"])
    orch.mark_merged(gh, store.get_project(pid), store.get_task(pid, "T1"), gh.prs[7])

    follow_up = store.get_task(pid, "T2")
    assert follow_up["follow_up_of"] == "T1" and follow_up["depends_on"] == ["T1"]
    assert "rename class" in follow_up["body"] and follow_up["state"] == "todo"
    event = store.list_messages(pid)[-1]
    assert event["role"] == "event" and woken == [pid]
    assert "created follow-up task T2" in event["content"] and "TODO: add mobile test" in event["content"]


def test_low_findings_on_a_follow_up_do_not_chain_more_follow_ups(env):
    store, orch, gh, pid, _ = env
    store.update_task(pid, "T1", state="reviewing", follow_up_of="T0", deferred_findings=["[low] `a.css:1`: nit"])
    orch.mark_merged(gh, store.get_project(pid), store.get_task(pid, "T1"), gh.prs[7])
    assert len(store.list_tasks(pid)) == 1
    assert "no automatic follow-up was created" in store.list_messages(pid)[-1]["content"]


def test_low_findings_are_deferred_while_others_block():
    pr = {"head": {"sha": "abc"}, "base": {"ref": "main"}, "draft": False, "mergeable": True}
    review = {"id": 1, "state": "COMMENTED", "commit_id": "abc"}
    ok = CheckSummary(total=1, passed=["ci"])
    low = {"path": "a.js", "line": 1, "body": "nit", "copilot_code_review_severity": "low"}
    d = gates.evaluate(pr, review, [low], ok, 0, no_checks_ok=False)
    assert d.action == "merge" and d.deferred == ["[low] `a.js:1`: nit"]
    for sev in ("medium", "high", None):
        c = {**low, "copilot_code_review_severity": sev}
        d = gates.evaluate(pr, review, [c, low], ok, 0, no_checks_ok=False)
        assert d.action == "remediate" and len(d.findings) == 1 and len(d.deferred) == 1


def test_fix_request_grace_period_prevents_rereviewing_unchanged_commit(env):
    store, orch, gh, pid, _ = env
    from software_factory.store import now
    store.update_task(pid, "T1", state="remediating", awaiting_sha="abc", remediation_started_at=now())
    gh.prs[7].update(state="open", draft=False, merged=False)
    gh.copilot_session_active = lambda pr: False
    orch.advance(gh, store.get_project(pid), store.get_task(pid, "T1"))
    assert store.get_task(pid, "T1")["state"] == "remediating"


def test_existing_database_is_migrated(tmp_path):
    db = sqlite3.connect(tmp_path / "factory.db")
    db.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, project_id TEXT, key TEXT, title TEXT, body TEXT, "
               "requirement_ids TEXT DEFAULT '[]', depends_on TEXT DEFAULT '[]', state TEXT, created_at TEXT, "
               "updated_at TEXT)")
    db.commit()
    db.close()
    Store(tmp_path)
    cols = {r[1] for r in sqlite3.connect(tmp_path / "factory.db").execute("PRAGMA table_info(tasks)")}
    assert {"attempt", "abandoned_prs", "attempt_notes", "deferred_findings", "attempt_report"} <= cols


def test_customer_turn_detection_and_event_history():
    msgs = [{"role": "user", "content": "build it"}, {"role": "assistant", "content": "ok"},
            {"role": "event", "content": "T1 blocked " + "x" * 3000}]
    assert not agent_mod.is_customer_turn(msgs)
    assert agent_mod.is_customer_turn([*msgs, {"role": "user", "content": "go ahead and retry"}])
    items = agent_mod.history_items(msgs)
    assert items[-1]["role"] == "user" and items[-1]["content"].startswith(agent_mod.EVENT_PREFIX)
    assert len(items[-1]["content"]) > 3000  # the pending event is passed in full
    old = agent_mod.history_items([*msgs, {"role": "assistant", "content": "retrying"}])
    assert "older event trimmed" in old[2]["content"]


def make_lead(tmp_path, monkeypatch):
    store = Store(tmp_path)
    pid = store.create_project("hello", "", "me", "repo", "")["id"]
    lead = agent_mod.LeadAgent(None, store, Orchestrator(store, EventBus(), 60), EventBus())
    started, release, turns = threading.Event(), threading.Event(), []

    def fake_respond(p, history, seen_id=None):
        turns.append([m["content"] for m in agent_mod.pending_messages(history, seen_id)])
        started.set()
        release.wait(5)
        return "done"

    monkeypatch.setattr(lead, "respond", fake_respond)
    return store, pid, lead, started, release, turns


def join_agent_threads(pid):
    for t in threading.enumerate():
        if t.name == f"agent-{pid}":
            t.join(5)


def test_burst_of_events_gets_one_agent_turn(tmp_path, monkeypatch):
    store, pid, lead, _, release, turns = make_lead(tmp_path, monkeypatch)
    lock = lead._chat_locks.setdefault(pid, threading.Lock())
    with lock:  # an earlier turn is still running while three events arrive
        for name in ("T1", "T2", "T3"):
            store.add_message(pid, "event", f"{name} merged")
            lead.chat_async(pid)
    release.set()
    join_agent_threads(pid)
    assert turns == [["T1 merged", "T2 merged", "T3 merged"]]


def test_event_arriving_during_a_turn_gets_its_own_turn(tmp_path, monkeypatch):
    store, pid, lead, started, release, turns = make_lead(tmp_path, monkeypatch)
    store.add_message(pid, "event", "T1 merged")
    lead.chat_async(pid)
    assert started.wait(5)
    store.add_message(pid, "event", "T2 blocked")  # after the first turn read the chat, before it replied
    lead.chat_async(pid)
    release.set()
    join_agent_threads(pid)
    join_agent_threads(pid)
    assert turns == [["T1 merged"], ["T2 blocked"]]
