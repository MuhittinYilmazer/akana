# Pack: route the authoring request

Entry point (umbrella) that TAKES a pack authoring/maintenance request and routes it
to the right sub-skill. Knows the packs/contract contract; runs offline.

## Preconditions

- It must be clear what the user wants: a new pack, an addition to an existing pack,
  or validation. If unclear, clarify with a single question.

## Steps

1. Classify the intent and pick the sub-skill:
   - **Brand-new pack from scratch** → `pack_scaffold`
   - **Add a skill to an existing pack** → `pack_skill_add`
   - **Validate (schema + files)** → `pack_validate`
2. Natural order for a new pack: `pack_scaffold` → `pack_validate` → (if needed)
   `pack_skill_add`. Suggest this.
3. Apply the chosen sub-skill; when done, suggest the sensible next step (usually validation).

## Failure modes

- If it's unclear which sub-skill the request belongs to: suggest the two closest ones
  and let the user choose.
- If the user doesn't yet know the pack's purpose: first clarify "what problem will it solve?".

## Notes

- This skill is a router; the actual production is done by the sub-skills.
