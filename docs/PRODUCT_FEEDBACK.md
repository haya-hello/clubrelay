# Product feedback — draft for submission

## Alexa+ simulated-experience path

We used the permitted self-built simulation path to test a contextual event-preparation assistant. We did not use the gated Category SDK, Alexa MCP Toolkit, CLI or official simulator, so we cannot report experience with those tools or with real Alexa runtime quality.

The rules and FAQ made the local-repository-and-video path feasible without hardware. The strongest product requirement for us was preserving context and rendering evidence alongside short conversational answers. The distinction between actual Alexa integration and a simulated experience needs to stay prominent in setup documentation and example submissions.

Would we continue? Yes, if organizer trials support the workflow. A real Alexa integration would be a separate phase requiring access and compatibility verification.

## Compatible model connection

Used a pre-existing localhost OpenAI-compatible gateway for genuine model requests. It is not an offline-model claim or an official-provider direct connection. No new paid service was purchased. The schema and cited-entry checks made responses inspectable; model semantics still require review. The fictional trial preserved the attendance distinction and unknown equipment contact.

## Django / SQLite / browser speech

Django and SQLite let us add preparation without migrating the existing application. Owner-scoped sessions and conditional updates supported persistent progress and stale-page protection. Browser speech was implemented as an optional enhancement. Actual microphone recognition remains to be tested by a person; it is not presented as validated Alexa voice functionality.

## Friction log

No Amazon SDK runtime failure is claimed because no gated Amazon SDK was run. Observed documentation friction: the main Alexa+ wording can suggest an MCP-first requirement; the submission-rules alternative explicitly permits a simulation without an MCP surface. We resolved this by reading the rules and FAQ together. Suggested improvement: place the three accepted paths and preview-access limitation in one setup checklist. Severity: minor; no blocked implementation.

Development-environment browser screenshots timed out; the established isolated project browser workflow produced the design and application evidence. This is a local tooling issue, not an Amazon platform bug and not submitted as one.
