import base64
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

API = "https://api.github.com"
COPILOT_ASSIGNEE = "copilot-swe-agent[bot]"
COPILOT_REVIEWER_LOGINS = ("copilot-pull-request-reviewer[bot]", "copilot-pull-request-reviewer", "Copilot")
COPILOT_AUTHOR_LOGINS = ("Copilot", "copilot-swe-agent[bot]", "copilot-swe-agent")
RULESET_NAME = "software-factory-main"


class GitHubError(Exception):
    def __init__(self, status: int, message: str, failure_reason: str | None = None) -> None:
        super().__init__(f"GitHub {status}: {message}")
        self.status = status
        self.message = message
        self.failure_reason = failure_reason


@dataclass
class CheckSummary:
    total: int = 0
    pending: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    awaiting_approval: list[str] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return self.total > 0 and not self.pending and not self.failed and not self.awaiting_approval


def is_copilot_reviewer(login: str | None) -> bool:
    return bool(login) and (login in COPILOT_REVIEWER_LOGINS or login.startswith("copilot-pull-request-reviewer"))


def is_copilot_author(login: str | None) -> bool:
    return bool(login) and (login in COPILOT_AUTHOR_LOGINS or login.startswith("copilot-swe-agent"))


class GitHub:
    def __init__(self, token: str, owner: str, repo: str, client: httpx.Client | None = None) -> None:
        self.owner = owner
        self.repo = repo
        self._http = client or httpx.Client(
            base_url=API,
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "software-factory",
            },
        )

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    # ---------- transport ----------
    def rest(self, method: str, path: str, *, json: Any = None, params: dict | None = None,
             headers: dict | None = None) -> Any:
        r = self._http.request(method, path, json=json, params=params, headers=headers)
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = {"message": r.text}
            raise GitHubError(r.status_code, body.get("message", r.text), body.get("failure_reason"))
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def graphql(self, query: str, variables: dict | None = None, *, features: str | None = None,
                allow_errors: bool = False) -> dict:
        headers = {"GraphQL-Features": features} if features else None
        r = self._http.post("/graphql", json={"query": query, "variables": variables or {}}, headers=headers)
        if r.status_code >= 400:
            raise GitHubError(r.status_code, r.text)
        body = r.json()
        if body.get("errors") and not allow_errors:
            msg = "; ".join(e.get("message", "") for e in body["errors"])
            etype = body["errors"][0].get("type")
            raise GitHubError(403 if etype == "FORBIDDEN" else 422, msg)
        return body

    # ---------- identity and repository ----------
    def viewer(self) -> dict:
        return self.graphql("query { viewer { login id } }")["data"]["viewer"]

    def get_repo(self) -> dict:
        return self.rest("GET", f"/repos/{self.slug}")

    def is_empty(self) -> bool:
        try:
            branches = self.rest("GET", f"/repos/{self.slug}/branches", params={"per_page": 1})
            return not branches
        except GitHubError as e:
            if e.status == 409:
                return True
            raise

    def copilot_bot_id(self) -> str | None:
        q = """query($o:String!,$r:String!){ repository(owner:$o,name:$r){
                 suggestedActors(capabilities:[CAN_BE_ASSIGNED], first:100){
                   nodes{ login __typename ... on Bot { id } } } } }"""
        nodes = self.graphql(q, {"o": self.owner, "r": self.repo})["data"]["repository"]["suggestedActors"]["nodes"]
        for n in nodes:
            if n.get("login") == "copilot-swe-agent":
                return n.get("id")
        return None

    def bootstrap_main(self) -> bool:
        """Create the initial commit on an empty repository. Returns True if a commit was created."""
        if not self.is_empty():
            return False
        readme = f"# {self.repo}\n\nThis repository is managed by Software Factory.\n"
        self.rest("PUT", f"/repos/{self.slug}/contents/README.md", json={
            "message": "Initialize repository",
            "content": base64.b64encode(readme.encode()).decode(),
            "branch": "main",
        })
        repo = self.get_repo()
        if repo.get("default_branch") != "main":
            self.rest("PATCH", f"/repos/{self.slug}", json={"default_branch": "main"})
        return True

    def ensure_ruleset(self) -> str:
        """Create a main ruleset requiring pull requests without required approvals."""
        existing = self.rest("GET", f"/repos/{self.slug}/rulesets") or []
        if any(r.get("name") == RULESET_NAME for r in existing):
            return "exists"
        self.rest("POST", f"/repos/{self.slug}/rulesets", json={
            "name": RULESET_NAME,
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {"type": "pull_request", "parameters": {
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews_on_push": False,
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": False,
                }},
            ],
        })
        return "created"

    def list_tree(self, ref: str = "main") -> list[str]:
        try:
            tree = self.rest("GET", f"/repos/{self.slug}/git/trees/{ref}", params={"recursive": "1"})
        except GitHubError as e:
            if e.status in (404, 409):
                return []
            raise
        return [t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"]

    def read_file(self, path: str, ref: str = "main") -> str | None:
        try:
            data = self.rest("GET", f"/repos/{self.slug}/contents/{path}", params={"ref": ref})
        except GitHubError as e:
            if e.status == 404:
                return None
            raise
        if isinstance(data, dict) and data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return None

    # ---------- Projects board ----------
    def create_board(self, title: str, repo_node_id: str) -> dict:
        owner_id = self.viewer()["id"]
        q = """mutation($owner:ID!,$title:String!,$repo:ID!){
                 createProjectV2(input:{ownerId:$owner,title:$title,repositoryId:$repo}){
                   projectV2{ id number url } } }"""
        return self.graphql(q, {"owner": owner_id, "title": title, "repo": repo_node_id})["data"]["createProjectV2"]["projectV2"]

    def board_status_field(self, board_id: str) -> tuple[str, dict[str, str]]:
        q = """query($id:ID!){ node(id:$id){ ... on ProjectV2 { fields(first:50){ nodes{
                 ... on ProjectV2SingleSelectField { id name options { id name } } } } } } }"""
        nodes = self.graphql(q, {"id": board_id})["data"]["node"]["fields"]["nodes"]
        for n in nodes:
            if n and n.get("name") == "Status":
                return n["id"], {o["name"]: o["id"] for o in n["options"]}
        raise GitHubError(404, "Status field not found on board")

    def set_board_status_options(self, field_id: str, names: list[str]) -> None:
        colors = {"Todo": "GRAY", "Ready": "BLUE", "In progress": "YELLOW", "In review": "PURPLE",
                  "Blocked": "RED", "Done": "GREEN"}
        q = """mutation($f:ID!,$opts:[ProjectV2SingleSelectFieldOptionInput!]){
                 updateProjectV2Field(input:{fieldId:$f, singleSelectOptions:$opts}){ clientMutationId } }"""
        opts = [{"name": n, "color": colors.get(n, "GRAY"), "description": ""} for n in names]
        self.graphql(q, {"f": field_id, "opts": opts})

    def add_board_item(self, board_id: str, content_node_id: str) -> str:
        q = """mutation($p:ID!,$c:ID!){ addProjectV2ItemById(input:{projectId:$p,contentId:$c}){ item{ id } } }"""
        return self.graphql(q, {"p": board_id, "c": content_node_id})["data"]["addProjectV2ItemById"]["item"]["id"]

    def set_board_item_status(self, board_id: str, item_id: str, field_id: str, option_id: str) -> None:
        q = """mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){ updateProjectV2ItemFieldValue(input:{
                 projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){ projectV2Item{ id } } }"""
        self.graphql(q, {"p": board_id, "i": item_id, "f": field_id, "o": option_id})

    # ---------- labels (fallback when the board is unavailable) ----------
    def ensure_labels(self, names: dict[str, str]) -> None:
        existing = {l["name"] for l in self.rest("GET", f"/repos/{self.slug}/labels", params={"per_page": 100})}
        for name, color in names.items():
            if name not in existing:
                self.rest("POST", f"/repos/{self.slug}/labels", json={"name": name, "color": color})

    def set_status_label(self, issue_number: int, status_label: str, prefix: str = "sf:") -> None:
        issue = self.rest("GET", f"/repos/{self.slug}/issues/{issue_number}")
        keep = [l["name"] for l in issue.get("labels", []) if not l["name"].startswith(prefix)]
        self.rest("PUT", f"/repos/{self.slug}/issues/{issue_number}/labels", json={"labels": [*keep, status_label]})

    # ---------- issues ----------
    def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> dict:
        return self.rest("POST", f"/repos/{self.slug}/issues", json={"title": title, "body": body, "labels": labels or []})

    def update_issue(self, number: int, **fields: Any) -> dict:
        return self.rest("PATCH", f"/repos/{self.slug}/issues/{number}", json=fields)

    def get_issue(self, number: int) -> dict:
        return self.rest("GET", f"/repos/{self.slug}/issues/{number}")

    def assign_copilot(self, number: int, instructions: str = "") -> dict:
        return self.rest("POST", f"/repos/{self.slug}/issues/{number}/assignees", json={
            "assignees": [COPILOT_ASSIGNEE],
            "agent_assignment": {
                "target_repo": self.slug,
                "base_branch": "main",
                "custom_instructions": instructions,
                "custom_agent": "",
                "model": "",
            },
        })

    def issue_assigned_to_copilot(self, issue: dict) -> bool:
        return any(is_copilot_author(a.get("login")) for a in issue.get("assignees") or [])

    def unassign_copilot(self, number: int) -> None:
        """Remove Copilot so the next assignment starts a fresh coding session."""
        self.rest("DELETE", f"/repos/{self.slug}/issues/{number}/assignees",
                  json={"assignees": [COPILOT_ASSIGNEE, "Copilot"]})
        if self.issue_assigned_to_copilot(self.get_issue(number)):
            raise GitHubError(422, f"Copilot is still assigned to issue #{number} after unassigning")

    def linked_pull_requests(self, issue_number: int) -> list[dict]:
        """PRs that reference the issue, newest first."""
        q = """query($o:String!,$r:String!,$n:Int!){ repository(owner:$o,name:$r){ issue(number:$n){
                 closedByPullRequestsReferences(first:10, includeClosedPrs:true){ nodes{ number state createdAt } }
                 timelineItems(first:50, itemTypes:[CROSS_REFERENCED_EVENT, CONNECTED_EVENT]){ nodes{
                   ... on CrossReferencedEvent { source { ... on PullRequest { number state createdAt
                         repository { nameWithOwner } } } }
                   ... on ConnectedEvent { subject { ... on PullRequest { number state createdAt } } } } } } } }"""
        data = self.graphql(q, {"o": self.owner, "r": self.repo, "n": issue_number})["data"]["repository"]["issue"]
        prs: dict[int, dict] = {}
        for n in data["closedByPullRequestsReferences"]["nodes"]:
            prs[n["number"]] = n
        for item in data["timelineItems"]["nodes"]:
            pr = (item or {}).get("source") or (item or {}).get("subject")
            if pr and pr.get("number"):
                repo = (pr.get("repository") or {}).get("nameWithOwner")
                if repo and repo.lower() != self.slug.lower():
                    continue
                prs[pr["number"]] = pr
        return sorted(prs.values(), key=lambda p: p.get("createdAt") or "", reverse=True)

    # ---------- pull requests ----------
    def get_pr(self, number: int) -> dict:
        return self.rest("GET", f"/repos/{self.slug}/pulls/{number}")

    def close_pr(self, number: int, comment: str) -> None:
        self.comment(number, comment)
        self.rest("PATCH", f"/repos/{self.slug}/pulls/{number}", json={"state": "closed"})

    def pr_files(self, number: int, limit: int = 100) -> list[str]:
        files = self.rest("GET", f"/repos/{self.slug}/pulls/{number}/files", params={"per_page": limit}) or []
        return [f["filename"] for f in files]

    def mark_ready(self, pr_node_id: str) -> None:
        q = """mutation($id:ID!){ markPullRequestReadyForReview(input:{pullRequestId:$id}){ pullRequest{ isDraft } } }"""
        self.graphql(q, {"id": pr_node_id})

    def copilot_session_active(self, pr: dict) -> bool | None:
        """True if a Copilot session is working on the PR, False if not, None if unknown."""
        try:
            data = self.rest("GET", f"/agents/repos/{self.slug}/tasks", params={"per_page": 50},
                             headers={"X-GitHub-Api-Version": "2026-03-10"})
        except GitHubError as e:
            log.debug("agent tasks API unavailable: %s", e)
            return None
        for task in (data or {}).get("tasks", []):
            for art in task.get("artifacts") or []:
                if art.get("type") == "pull" and (art.get("data") or {}).get("id") == pr["id"]:
                    return task.get("state") in ("queued", "in_progress")
        return None

    def has_workflows(self) -> bool:
        data = self.rest("GET", f"/repos/{self.slug}/actions/workflows", params={"per_page": 1})
        return bool((data or {}).get("total_count"))

    def request_copilot_review(self, number: int) -> str:
        try:
            self.rest("POST", f"/repos/{self.slug}/pulls/{number}/copilot-code-review",
                      headers={"X-GitHub-Api-Version": "2026-03-10"})
            return "requested"
        except GitHubError as e:
            if e.status == 409:
                return "pending"
            raise

    def copilot_reviews(self, number: int) -> list[dict]:
        reviews = self.rest("GET", f"/repos/{self.slug}/pulls/{number}/reviews", params={"per_page": 100}) or []
        return [r for r in reviews if is_copilot_reviewer((r.get("user") or {}).get("login"))]

    def review_comments(self, number: int, review_id: int) -> list[dict]:
        return self.rest("GET", f"/repos/{self.slug}/pulls/{number}/reviews/{review_id}/comments",
                         params={"per_page": 100}) or []

    def check_summary(self, sha: str) -> CheckSummary:
        s = CheckSummary()
        runs = self.rest("GET", f"/repos/{self.slug}/commits/{sha}/check-runs", params={"per_page": 100}) or {}
        for run in runs.get("check_runs", []):
            s.total += 1
            name = run.get("name", "check")
            if run.get("status") != "completed":
                s.pending.append(name)
            elif run.get("conclusion") in ("success", "neutral", "skipped"):
                s.passed.append(name)
            else:
                s.failed.append(f"{name} ({run.get('conclusion')})")
        status = self.rest("GET", f"/repos/{self.slug}/commits/{sha}/status") or {}
        for st in status.get("statuses", []):
            s.total += 1
            name = st.get("context", "status")
            if st.get("state") == "pending":
                s.pending.append(name)
            elif st.get("state") == "success":
                s.passed.append(name)
            else:
                s.failed.append(f"{name} ({st.get('state')})")
        # Copilot PR workflow runs held for approval have no check runs; GitHub later marks them failed with zero
        # jobs. Treat both as held so they block merging until they actually run.
        runs = self.rest("GET", f"/repos/{self.slug}/actions/runs", params={"head_sha": sha, "per_page": 50}) or {}
        for run in runs.get("workflow_runs", []):
            if run.get("event") == "dynamic":
                continue  # Copilot's own agent and review sessions
            name = run.get("name", "workflow")
            if self._run_is_held(run):
                s.total += 1
                s.awaiting_approval.append(name)
            elif run.get("status") == "completed" and run.get("conclusion") not in ("success", "neutral", "skipped") \
                    and not self.run_job_count(run["id"]):
                s.total += 1
                s.failed.append(f"{name} (workflow failed to start)")
            elif run.get("status") != "completed" and name not in s.pending:
                s.total += 1
                s.pending.append(name)
        return s

    MAX_RUN_ATTEMPTS = 3

    def run_job_count(self, run_id: int) -> int:
        jobs = self.rest("GET", f"/repos/{self.slug}/actions/runs/{run_id}/jobs", params={"per_page": 1}) or {}
        return jobs.get("total_count", 0)

    def _run_is_held(self, run: dict) -> bool:
        if run.get("status") == "action_required" or run.get("conclusion") == "action_required":
            return True
        return (run.get("status") == "completed" and run.get("conclusion") in ("failure", "cancelled")
                and run.get("run_attempt", 1) < self.MAX_RUN_ATTEMPTS and self.run_job_count(run["id"]) == 0)

    def start_held_runs(self, head_sha: str) -> tuple[int, list[str]]:
        """Start workflow runs that GitHub held instead of running. Returns (started, names that could not start)."""
        runs = self.rest("GET", f"/repos/{self.slug}/actions/runs", params={"head_sha": head_sha, "per_page": 50}) or {}
        started, stuck = 0, []
        for run in runs.get("workflow_runs", []):
            if run.get("event") == "dynamic" or not self._run_is_held(run):
                continue
            rid = run["id"]
            try:
                if run.get("status") == "completed":
                    self.rest("POST", f"/repos/{self.slug}/actions/runs/{rid}/rerun")
                else:
                    try:
                        self.rest("POST", f"/repos/{self.slug}/actions/runs/{rid}/approve")
                    except GitHubError:
                        # The approve endpoint only covers fork PRs; cancel so the run can be re-run next tick.
                        self.rest("POST", f"/repos/{self.slug}/actions/runs/{rid}/cancel")
                started += 1
            except GitHubError as e:
                log.info("Could not start held workflow run %s: %s", rid, e)
                stuck.append(run.get("name", str(rid)))
        return started, stuck

    def require_workflow_approval(self) -> bool | None:
        """Copilot cloud agent setting that holds PR workflow runs until a user approves them."""
        try:
            cfg = self.rest("GET", f"/repos/{self.slug}/copilot/cloud-agent/configuration",
                            headers={"X-GitHub-Api-Version": "2026-03-10"})
        except GitHubError:
            return None
        return (cfg or {}).get("require_actions_workflow_approval")

    def comment(self, number: int, body: str) -> dict:
        return self.rest("POST", f"/repos/{self.slug}/issues/{number}/comments", json={"body": body})

    def approve(self, number: int, sha: str, body: str) -> bool:
        try:
            self.rest("POST", f"/repos/{self.slug}/pulls/{number}/reviews",
                      json={"commit_id": sha, "event": "APPROVE", "body": body})
            return True
        except GitHubError as e:
            log.info("Approving review not accepted for PR #%s: %s", number, e)
            return False

    def merge(self, number: int, sha: str, title: str) -> dict:
        return self.rest("PUT", f"/repos/{self.slug}/pulls/{number}/merge",
                         json={"sha": sha, "merge_method": "squash", "commit_title": title})
