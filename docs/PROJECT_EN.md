# ClubRelay — Experience stays. People move forward.

## Inspiration

Student organizations change leaders, but a folder of old documents does not automatically help the next organizer make decisions. A new leader needs to know which advice applies, why it was suggested, and what is still unknown.

## What it does

ClubRelay turns a human-reviewed handover into a contextual preparation conversation. The next organizer states an event goal, expected attendance and constraints, asks follow-up questions, opens source evidence, and saves a checklist for the next event. It preserves the distinction between records, advice and unanswered questions.

In the fictional example, 40 attendees were planned but 23 were recorded. A projector issue caused an eight-minute delay, while the technical cause and equipment lender remained unknown. The assistant must preserve those limits, including when asked whether the approach would work for 80 people.

## How it works

Django and SQLite store immutable handovers and owner-scoped preparation sessions. A dedicated model prompt receives only the permitted reviewed entries, the future event context, and at most five recent successful turns. Responses must conform to a small JSON schema. Entry IDs are validated and mapped to source quotations by the server. Invalid references are rejected. This verifies source membership, not full semantic entailment.

Sources and permissions are checked before and after generation and before export. Changed sources stop new generation and formal export while preserving conversation history. Idempotency prevents repeated requests from triggering duplicate model calls. Browser speech input is optional; local-system read-aloud and text interaction are separate from Alexa.

## Track

Alexa+ — self-built simulated experience. No gated Alexa+ preview tools, MCP server, AWS integration, real Alexa connection or mini-challenge participation is claimed.

## What changed during the submission window

The earlier campus application supplied file ingestion, access controls and model-connection safeguards. The October 2–3 handover increment supplied review, frozen versions and source snapshots. The October 3–4 Amazon increment added contextual conversations, evidence cards, persistent preparation checklists, English/Chinese presentation, optional browser voice, new tests and a new English technical recording.

## Validation and next steps

Backend and browser regression checks passed, and real model requests were exercised against fictional data. Two student-organizer trials and final owner acceptance remain pending. The technical demonstration does not establish real-world time savings. The next step is to measure whether organizers can independently identify three actions, inspect a source, recognize a missing fact, and export a usable checklist.
