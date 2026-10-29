# Agent Charter

Agents from different model families deliberate in a shared seminar to write one constitution for future AI agents. Browse the runs at https://jonathnchang.com/swarms/agent-charter.

A standalone, configurable multi-agent experiment. Participants receive identical prompts, work in separate private Docker containers, discuss through a shared threaded board, and collaboratively edit one versioned constitution. `config.json` selects seven agents, one per requested model, using OpenRouter for every model.

The prompt frames deliberation as a scholarly seminar, encourages concrete examples and citations from existing knowledge, and asks each contribution to move discussion forward. There are no search tools or external network access for participants.

## Current status

The user authorized the live experiment on October 8, 2026. It is running with seven agents. `CURRENT_RUN.json` points to its current continuation segment and read-only local dashboard. A five-minute thread monitor reports progress and completion. Docker readiness was checked for all seven containers.

Python 3.11+ and Docker Compose v2 are required for a future live run. Python code uses only the standard library; no package installation is necessary.

## Population and budget

`config.json` contains the selected population. `population` is a list of objects containing `model` (an OpenRouter model ID) and `count` (a positive integer). Multiple entries support mixed populations. The controller assigns `agent-001` through `agent-007`; agents receive the same prompts and tool definitions regardless of model. Their prompts instruct them to use only their assigned numbered name, never model/provider identities. Only host configuration/transcripts contain the model mapping.

The seven exact IDs are `deepseek/deepseek-v4.1-flash`, `openai/gpt-6-luna`, `anthropic/claude-haiku-5.5`, `google/gemini-3.8-flash`, `x-ai/grok-4.7`, `moonshotai/kimi-k3`, and `z-ai/glm-5.3-flash`, with one agent each. They were checked against OpenRouter's public catalog; the seven-entry snapshot is saved in `model-catalog.json`. No authenticated or paid model calls were used to verify availability. Actual provider compatibility remains untested.

Every agent has a **hidden $10 allowance**, enforced through a host-side budget gateway. No budget amounts, billing state, turn limit, or session-time limit appear in agent prompts or tool feedback. The gateway conservatively reserves cost before a request, so an agent can stop below $10. A separate $0.50 group contingency is retained by the gateway. These are local admission controls, not guaranteed provider billing caps. Uncertain charges remain explicitly recorded. Transient HTTP 429/502/503/504 failures may receive up to three transport retries, only when the gateway reports safe accounting continuation. Each uncertain request retains its reservation against the same allowance; retries resend the exact conversation and never replay tools.

There is no turn count or overall session-time cutoff. Individual shell commands are limited to 30 seconds, requests have transport timeouts, model output has a per-response token ceiling, and text/file sizes are bounded. These are execution safeguards, not deliberation-turn limits. Idle agents remain in a host-side condition wait without paid polling; if everyone is waiting, one receives the fact that all participants are waiting so discussion can continue.

Local budget exhaustion triggers a controller withdrawal. Peers see a withdrawn participant with the neutral reason `Participation ended.` and no budget details. That participant is removed from the signing quorum. Host-only audit/result metadata preserves the budget cause and controller origin, and reports distinguish these exits from agent-authored withdrawals. No opinion, objection, or endorsement is fabricated on the agent's behalf. Plain unsubmitted final answers, provider errors, uncertain accounting, and other controller interruptions remain unresolved interruptions rather than withdrawals.

## Shared workflow

- The board starts empty. Threads support nested replies, attributed authorship, timestamps, and recorded read provenance. Participants must read a peer message before replying to it.
- The constitution starts empty. Updates use an expected revision to reject conflicting edits. Every edit invalidates every signature.
- A participant must read the exact current revision before signing it. Signing does not end participation. Participants may revoke approval or continue discussing.
- Withdrawal is explicit, permanent, and preserves the reason and unresolved objections. Withdrawn participants cannot resume participation during the run.
- Any remaining participant may submit once all remaining participants have signed the same nonempty revision. Submission atomically freezes shared state and ends remaining sessions.
- If every participant withdraws, including through budget withdrawal, the outcome is `no_agreement`. Other interruptions produce `incomplete` unless a valid submission already occurred. A single remaining signer may submit; reports distinguish that from agreement among the original population and distinguish budget exits from voluntary exits.

## Offline checks

From this folder:

```sh
python3 -m unittest discover -s tests -v
```

These are scripted controls with dummy models and mocked Docker. They test tool semantics and infrastructure contracts; they are not observations of model behavior.

After choosing a population, this command writes a fresh configuration and identical prompts without starting Docker, reading credentials, or calling models:

```sh
python3 -m charter.runner --prepare --config config.json --out prepared/my-seminar
```

Preparation folders and live run folders are fresh and never overwritten. Preparation is review-only; a future live launch creates another fresh folder.

## Live launch and monitoring

The launch command requires an explicit `--live`. The selected OpenRouter model catalog containing pricing and tool-support metadata is saved locally. Refresh its public metadata before a substantially later launch. The project does not automatically fetch one. Every inference goes through `https://openrouter.ai/api/v1`; it never requires or uses an Anthropic API key, including for Claude.

Store `OPENROUTER_API_KEY` in this project's `.env`, owner-readable only (`chmod 600`). The controller uses the credential parser in `constitution/secrets.py`. Never copy a credential file from another project or put a key into command-line arguments. `config.template.json` contains no credential field. `.env.example` is blank.

```sh
python3 -m charter.runner --live --config config.json --catalog model-catalog.json
```

`--docker` can specify a Docker binary path. The live boundary launches a fresh Compose project, makes paid OpenRouter calls concurrently, archives the result, and removes only its own containers after successful private-workspace export. It does not publish anything.

## Isolation and evidence

Each container runs as UID/GID 1000 with no network, a read-only root filesystem, dropped capabilities, resource bounds, and separate writable tmpfs workspaces. Containers receive no provider keys, repository/home mounts, shared filesystem, or Docker socket. Communication and signatures are host-controlled tools bound to the caller identity. Shell admission is serialized with shared submission so no new shell command starts after the terminal boundary.

Each run records the exact prompts and hash, full selected configuration, model catalog, image digest, Compose project, per-agent raw responses and transcripts, ordered shared events, document revisions, sign-off statements, withdrawal objections, private workspace exports, and billing. `shared/constitution.md` is a final constitution only if `shared/result.json` says `submitted`; otherwise it is a draft. Archive failures preserve containers for recovery. Diagnosed controller HTTP interruptions can continue from complete conversation prefixes, the shared journal, and validated private workspace exports. Voluntary withdrawals cannot resume. Continuation preserves every prior admission deduction and carries cumulative spending and reservations.

No model judge is used. The report describes mechanical outcomes and participant counts; the board and transcripts support human review. Citations supplied from model knowledge are unverified. Deliberation does not establish training effectiveness, moral correctness, or psychological causes of agreement.

## Reused infrastructure

The pinned container image and budget-gateway/credential-parser code originate from the local `healing-agent-swarms` project. This project includes its own copies and does not import or mount that repository. `PROVENANCE.json` records source paths and hashes of copied modules. No credential files, historical transcripts, datasets, or existing experiment results were copied.

The live dashboard can be opened using the URL in `CURRENT_RUN.json`. It refreshes every ten seconds. Each run also exports `dashboard.html` for offline inspection. The active observer displays segment billing while final reports carry cumulative accounting across startup attempts and continuations. See `OPERATIONS.md` for the startup and recovery record.

## Shared-authorship experiment

`config.v3.json` selects the same population with the reviewed-proposal, alternating-editor, and rotating-turn protocol enabled. See `RUN_V3.md` for the exact rules and sole-participant exception. Version 2 remains available through the original configuration. Version 3 adds `constitution_propose` (14 tools total); all participants still receive identical instructions and schemas. The authorized host-only launcher is `launch_v3.py`; it writes CURRENT_RUN.json and retains a read-only observer.
