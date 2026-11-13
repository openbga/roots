# Writing page help: the friendly layer (full guide)

Owner-approved 2026-11-13. The summary of these rules is in the skill's SKILL.md; this file has
the complete guide and its examples.

The Help drawer has four parts. The first three are short; the fourth is the complete reference,
collapsed by default.

| Part | JSON | Reader's question | Length |
| --- | --- | --- | --- |
| What this page is for | `summary` | "Am I in the right place?" | 1–2 sentences, at most 240 characters |
| Common tasks | `tasks[]` | "How do I do the thing I came for?" | 2–5 tasks, each 1–4 steps, each step at most 200 characters |
| Good to know | `good_to_know[]` | "What will surprise me or go wrong?" | 3–5 items, each at most 200 characters |
| All fields and actions | `sections[]` (existing) | "What exactly does this field or rule do?" | Complete. This is the reference, and `tests/test_page_docs.py` still checks its coverage |

## Tone

- Write for a lab person in the middle of their work. Use plain words, second person and the
  present tense: "Select **Apply**", not "The Apply control submits the query".
- Say what something does for them, not how it is implemented. Leave out limits like "accepts at
  most 200 characters" unless people hit them; those belong in the reference.
- Use one idea per sentence and avoid parenthetical stacks. Use American English and sentence
  case.
- Use the lab's own terms: sample, chain of custody, batch, disposition, holding time. Never use
  internal names such as route names, model fields or CSS classes.
- Never invent anything: every sentence must be traceable to the reference or the source listed in
  `sources` (AGENTS.md rule 1). No policy, thresholds, compliance claims or features that don't
  exist.

## Summary

- Name the record or workflow and what the person can find or do here. Add who can use the page
  only when access is restricted ("Only project managers and admins can use quotes.").
- Keep the existing `purpose` field for the page header and search. `summary` is the friendlier
  version shown in the drawer. When `summary` is missing, the drawer falls back to `purpose`.

## Tasks

- Use 2–5 tasks, ordered by how often people do them. The first task is the page's main job.
- A task title is a verb phrase in the user's words: "Find a quote", "Record that the sample
  moved". It is not a control name.
- Each step is one action. Wrap every exact on-screen label in `[[ ]]`: `Select [[New quote]].`
  The drawer shows labels as quiet chips, so the reader can find the same words on the page. Copy
  labels character for character from the template.
- Add an outcome only when it matters: "The client cannot see a draft." Leave out keyboard
  details, validation messages and dialog mechanics; the reference covers them.
- A task may end on another page ("Open the quote and select [[Issue version]]") when this page is
  where people start it. Take the wording from that page's entry.
- Leave out tasks a role can never perform on this page.

## Good to know

Include something only if it:

- regularly surprises people, for example "Expired is worked out from the valid-until date";
- prevents a wrong conclusion, for example "The delivery target is not a holding deadline";
- explains a block or refusal, for example "A custody transfer cannot be dated before the latest
  custody event"; or
- protects the record, for example "Earlier events are never changed".

Leave everything else in the reference. That covers default page sizes, sort tie-breakers,
character limits, dialog buttons, column lists and anything that reads like a test assertion.
Use 3–5 items; if you have more, the page probably needs a better task instead.

## The reference ("All fields and actions")

- Keep it complete and verified (SKILL.md, "What to explain"). Every item is a `label` /
  `description` pair, which the drawer shows as a definition list in collapsible sections with
  a filter box.
- Write labels exactly as on screen. Descriptions may stay precise and dense, because people reach
  them by searching, not by reading top to bottom.
- Don't repeat the tasks or "Good to know" items word for word; the reference states the rule
  fully, the friendly layer explains why it matters.

## Checklist for each page

- [ ] `summary`: 1–2 sentences, at most 240 characters, no implementation detail.
- [ ] `tasks`: 2–5, verb-phrase titles, 1–4 steps each, exact labels in `[[ ]]`.
- [ ] `good_to_know`: 3–5 items, each one surprising, preventive, explaining a block or
      protecting the record.
- [ ] Every sentence is traceable to the reference or a file in `sources`.
- [ ] Read it in the drawer beside the page: every `[[label]]` exists on that page (or on the
      page the task names).
