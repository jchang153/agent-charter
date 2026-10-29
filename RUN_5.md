# Run 5: corrected ten-model restart

The user authorized a fresh restart with the same ten-model population on October 12, 2026, with a hidden $50 admission allowance per participant. Run 4 was stopped, archived privately, and removed from the public website. Its discussion, draft proposals, withdrawals, exclusions, and charges are not seeded into Run 5.

Run 4 exposed two infrastructure problems. Conservative whole-request reservations stopped GPT-6 Astra and Claude Opus below their actual $10 spending allowances. GLM and MiMo exhausted three generation attempts at the same 8,192-token output ceiling; internal reasoning consumed most or all of that allowance before usable tool calls appeared. These were controller withdrawals and technical exclusions, not voluntary disagreement.

Run 5 retains the identical system/user prompts, tool schemas, private containers, reviewed-proposal rules, alternating editors, and rotating turns. Configuration is in `config.run5.json`; public catalog metadata is in `model-catalog.run5.json`; the host launcher is `launch_run5.py`.

- Initial output allowance: 32,768 tokens, including reasoning and tool arguments.
- Invalid responses with a length finish reason receive bounded retries with doubled output allowances, up to 65,536 tokens. Partial tool calls are not executed or replayed. A successful recovery retains the larger allowance for that participant.
- Supported reasoning effort is medium when available, otherwise high. This avoids default maximum or extra-high reasoning on several models while preserving reasoning. Models without advertised effort controls keep their provider defaults.
- Each participant has a user-authorized $50 admission allowance. Conservative reservation rates remain in force; accounting and neutral controller withdrawals are retained. The higher allowance reduces premature exclusions but does not guarantee every participant can finish.
- Cancellation is explicit through the launcher's SIGINT handler. Dispatch stops before the controller waits for already pending requests to settle, exports private workspaces, and removes its containers.

Offline validation passed 82 tests, including larger gateway output limits, positive configurable allowances, adaptive truncation recovery, and rejection of partial tool effects. These tests use mocked upstream responses and do not establish provider reliability.

The website defaults to Run 5 and receives allowlisted public snapshots. Billing, raw transcripts, reasoning, credentials, and private workspaces remain private. The five-minute monitor reports spending and progress to the user, preserving recorded charges separately from outstanding reservations.
