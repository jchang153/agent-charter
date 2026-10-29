"""Provider-neutral function schemas; no web/search tools or caller IDs."""


def tool(name, description, properties=None, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {},
                           "required": list(required), "additionalProperties": False}}}


STRING = {"type": "string"}
REVISION = {"type": "integer", "minimum": 0}
RESEARCH_TOOL_PARAMETERS = {
    "openrouter:web_search": {"engine": "exa", "mode": "fast", "max_uses": 1,
                              "max_results": 5, "max_total_results": 5, "max_characters": 2000},
    "openrouter:web_fetch": {"engine": "openrouter", "max_uses": 1, "max_content_tokens": 8000},
}


def tool_definitions(shared_authorship=False, draft_workspace=False, discussion_max_chars=64000, web_research=False):
    definitions = [
        tool("session_status", "Discover your identity, roster, opening discussion requirements, document approval state, and unread messages."),
        tool("board_list_threads", "List thread metadata without marking message bodies read."),
        tool("board_read", "Read a thread, or threads containing unread messages. Set unread_only=false to read all. Returned messages are marked read.",
             {"thread_id": REVISION, "unread_only": {"type": "boolean"}}),
        tool("board_post", "Start a shared discussion thread.", {"title": STRING, "text": STRING}, ("title", "text")),
        tool("board_reply", "Reply to a message you have read.", {"parent_id": REVISION, "text": STRING}, ("parent_id", "text")),
        tool("constitution_read", "Read the current or a historical revision. Read the current revision before signing it.", {"revision": REVISION}),
        tool("constitution_update", "Replace the shared document atomically, giving a rationale. Before the first draft, every active participant must contribute a board post or reply. expected_revision must match the current revision. Every edit invalidates current signatures and preserves their history.",
             {"expected_revision": REVISION, "text": STRING, "rationale": STRING}, ("expected_revision", "text", "rationale")),
        tool("constitution_sign", "Personally approve the exact current revision you have read. Signing does not end your session; use wait_for_activity or continue deliberating.",
             {"revision": REVISION, "statement": STRING}, ("revision", "statement")),
        tool("constitution_unsign", "Revoke your approval and explain why.", {"reason": STRING}, ("reason",)),
        tool("withdraw", "Permanently leave without endorsing the document. Preserve your reason and unresolved objections. This ends your session.",
             {"reason": STRING, "unresolved_objections": STRING}, ("reason", "unresolved_objections")),
        tool("constitution_submit", "Submit only when every remaining participant has signed this exact nonempty revision. Withdrawn and technically excluded participants do not count toward required approval; exclusion is not endorsement. Success freezes the document and ends remaining sessions.",
             {"revision": REVISION}, ("revision",)),
        tool("wait_for_activity", "Wait for relevant shared activity without generating model responses while waiting. Returns compact changes and new message bodies, marking delivered bodies read. Historical unread backlog does not repeatedly wake waits; use board_read to inspect it. Returns at activity or after up to 30 seconds.",
             {"timeout_seconds": {"type": "number", "minimum": 0, "maximum": 30}}),
        tool("bash", "Run a command in your private Docker workspace. Commands are bounded to 30 seconds and output is truncated. Shared state is accessible only through shared tools.",
             {"command": STRING}, ("command",)),
    ]

    if shared_authorship and not draft_workspace:
        definitions.insert(6, tool("constitution_propose", "Post the exact proposed replacement and rationale as a discussion thread, after reading the current revision. The opening discussion must be complete. Another participant must reply directly before committing; a reply is review, not approval. Changed wording requires a new proposal.",
            {"expected_revision": REVISION, "title": STRING, "text": STRING, "rationale": STRING},
            ("expected_revision", "title", "text", "rationale")))
        update = next(t["function"] for t in definitions if t["function"]["name"] == "constitution_update")
        update["description"] += " In this run, supply proposal_message_id for an exact matching proposal at this revision; read the current revision, proposal, and a direct reply by someone other than its proposer. You cannot be the previous editor unless you are the sole required participant. Peer response is not consent; sole-participant exceptions are recorded."
        update["parameters"]["properties"]["proposal_message_id"] = REVISION
        update["parameters"]["required"].append("proposal_message_id")
    if draft_workspace:
        update = next(t["function"] for t in definitions if t["function"]["name"] == "constitution_update")
        update["description"] = "Edit the shared WORKING DRAFT directly, including the first draft. This is not final approval. Read the current revision, supply its expected_revision, the full replacement text and a short change rationale. Keep full drafts out of discussion threads. Edits invalidate signatures. Another participant must make the next edit while multiple required participants remain."
    if discussion_max_chars < 64000:
        for entry in definitions:
            fn = entry["function"]
            if fn["name"] in {"board_post", "board_reply"}:
                fn["parameters"]["properties"]["text"] = {"type": "string", "maxLength": discussion_max_chars}
                fn["description"] += f" Discussion only: at most {discussion_max_chars} characters; put full drafts in constitution_update."
    if web_research:
        definitions.extend({"type": name, "parameters": dict(parameters)}
                           for name, parameters in RESEARCH_TOOL_PARAMETERS.items())
    return definitions
