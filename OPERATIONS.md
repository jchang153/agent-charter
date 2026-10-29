# Authorized seminar operations

The seven-agent seminar was authorized on October 8, 2026, with one of each selected model and $10 per agent. No provider key other than OpenRouter is used for inference. The existing host-only credential reader loads the OpenRouter key from the original project; it accepts an additional Anthropic credential entry solely for parsing compatibility, discards unused credentials, and sends only the OpenRouter key to OpenRouter. No credentials were copied or mounted.

The first paid startup segment (`20261008T212104Z_seminar_8f71538b`) failed before discussion because Haiku's endpoints rejected forced tool choice. It recorded $0.0067887 in known charges and retained a $0.06505625 conservative reservation. A fresh corrected startup subtracted both against the original allowances and used tool choice `auto` for every model.

The next segment (`20261008T212412Z_seminar_27da924a`) produced three discussion posts, then GLM's selected provider rate-limited. The gateway's initial fail-closed handling interrupted peers too. The implementation now omits the optional `parallel_tool_calls` request field, which had unnecessarily restricted GLM routing to the limited endpoint. Recoverable uncertain requests remain fully reserved; other agents can continue, and transient transport retries are bounded to three.

The current segment (`20261008T212911Z_seminar_dabe5908`) resumes the exact saved transcripts, raw responses, shared journal, read provenance, and private files. Host recovery events identify the reactivated technical interruptions. No discussion posts or tool effects were replayed. Prompt equality and complete tool-call/result pairing were validated. Every participant's remaining allowance equals its prior limit minus prior charges and prior reservations, retaining all earlier deductions.

Startup reservations remain uncertainty, not confirmed charges. Final accounting distinguishes them and reports cumulative actual recorded spending. These infrastructure interruptions are not voluntary model withdrawals or observed disagreement. User-requested budget withdrawals remain separately attributed to the controller in private evidence.
