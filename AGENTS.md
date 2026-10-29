# Project instructions

- Do not launch containers, pull/build images, call paid model APIs, or start the experiment until the user explicitly authorizes the run and chooses its population.
- Keep all agents' system and user prompts identical. Identity comes from the controller/tools.
- There is no turn or session-time cap. Each agent initially has a hidden $10 admission allowance; the user explicitly authorized no dollar admission cap for the Run 3 continuation and $50 per agent for the fresh Run 5 restart of the ten-model population. Keep cost accounting and do not disclose policy changes or costs to agents. Never disclose billing amounts or budget stop reasons in agent-visible prompts or tools.
- Use only `charter.secrets.read_credentials` inside an authorized host process to read `.env`. Never display, log, copy, or mount credential values or credential files into agent containers.
- Convert local budget exhaustion into a controller-triggered withdrawal, removing that agent from the quorum. Hide its cause from peers; retain controller origin and budget cause in private audit/result metadata. Never fabricate agent-authored dissent. Bounded recovery failures may be explicitly marked technical_excluded by the controller, removing the participant from the final quorum without recording consent. Preserve their contributions and distinguish technical exclusion from voluntary and budget withdrawals. Ordinary interrupted participants remain unresolved and never count as consent.
- Distinguish scripted controls and mocked isolation checks from actual model behavior or verified Docker runtime containment.
- Mathematical formulas must be rendered in LaTeX unless referring to code.
