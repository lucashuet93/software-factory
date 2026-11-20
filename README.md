---
title: Software Factory
description: Local app where a Foundry lead engineer agent plans a backlog and delivers it through GitHub Copilot.
---

## Overview

Software Factory runs locally and opens a browser UI where you configure a project. A Microsoft
Foundry lead engineer agent refines your business requirements document (BRD), creates task
issues in your GitHub repository, assigns them to the GitHub Copilot coding agent, routes Copilot
code review findings back for fixes, and merges clean pull requests into `main`.

See [SPEC.md](SPEC.md) for the full MVP specification.

## Prerequisites

* Windows with Python 3.13 and Node.js 22
* Azure CLI, signed in with `az login --tenant <tenant-id>`
* The **Foundry User** role on your Foundry project
* A personal GitHub account with access to Copilot cloud agent and Copilot code review
* An empty repository owned by that account
* A classic PAT with the `repo` and `project` scopes (a fine-grained PAT works, but tracks task
  status with labels because it cannot manage personal Projects boards)

In the repository's **Settings > Copilot > Cloud agent**, you can turn off **Require approval for
workflow runs**. If you leave it on, GitHub holds CI runs on Copilot pull requests, and Software
Factory starts them by re-running them, which adds a short delay.

## Configure

Copy `.env.example` to `.env` and fill in the Foundry values:

```dotenv
AZURE_AI_PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>
AZURE_AI_MODEL_DEPLOYMENT_NAME=<deployment>
AZURE_TENANT_ID=<tenant-id>
AZURE_TOKEN_CREDENTIALS=dev
```

Software Factory authenticates to Foundry with Microsoft Entra ID through your Azure CLI sign-in.
It never uses API keys. Project data lives in `%LOCALAPPDATA%\SoftwareFactory` unless you set
`SOFTWARE_FACTORY_HOME`.

## Run

```powershell
.\start.ps1
```

The script installs dependencies on first run, builds the UI, starts the server on
`http://127.0.0.1:8765`, and opens your browser. Then:

1. Select **New project**, enter the repository URL, PAT, and BRD.
2. Open **Settings** and select **Run setup** to create `main`, the ruleset, labels, and the board.
3. In **Chat**, ask the lead engineer to review the BRD and plan the work.
4. Tell the lead engineer to proceed. Track progress in **Backlog**.

Once delivery starts, Software Factory runs the pull request loop and tells the lead engineer
when a task merges or gets stuck:

* Copilot review findings rated High or Medium go back to Copilot, for up to three fix-up rounds
  per attempt. Low findings don't block the merge; they become one follow-up task.
* After each merge, the lead engineer reviews the PR's output and adds follow-up tasks if
  required work remains.
* When an attempt gets stuck, the lead engineer rewrites the issue using Copilot's review
  comments and starts a fresh attempt, up to three attempts per task.
* After three failed attempts, the project shows **Needs attention**. Chat with the lead
  engineer to agree on a rewrite or new tasks; it can retry again once you agree.

Press Ctrl+C in the terminal to stop the app. Work already assigned to Copilot continues on
GitHub, and the app catches up when you start it again.

## Develop

```powershell
cd backend
.\.venv\Scripts\python -m pytest
cd ..\frontend
npm run dev
```

`npm run dev` serves the UI on port 5173 and proxies API calls to the backend on port 8765.
Set `SOFTWARE_FACTORY_POLL_SECONDS` to poll GitHub more often while testing.
