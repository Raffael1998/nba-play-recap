# Implementation Plan

## Phase 1: Validate Data Access

Deliverable: one script that accepts a `GameID` and outputs a structured list of candidate plays with video availability.

Tasks:

1. Set up a Python project with a small HTTP client layer.
2. Implement a `playbyplay` fetcher.
3. Implement a `videoevents` or clip-details fetcher for one `GameEventID`.
4. Verify against the example game `0042500151`.
5. Save raw JSON samples for debugging.

## Phase 2: Build Recap Selection

Deliverable: a ranked recap timeline in JSON.

Tasks:

1. Normalize events into one schema.
2. Create an importance heuristic:
   - made shots weighted by points,
   - boosts for lead changes and ties,
   - boosts for clutch time,
   - boosts for blocks, steals, and and-ones,
   - boosts for star-player filters,
   - penalties for duplicate adjacent events from the same sequence.
3. Group related events into possessions or mini-runs.
4. Select a target recap length, for example 20 to 40 clips.

## Phase 3: Produce Video Output

Deliverable: one merged recap video file.

Tasks:

1. Download selected clips locally.
2. Normalize clip formats if needed.
3. Stitch with `ffmpeg`.
4. Overlay lightweight text cards only if necessary:
   - quarter and clock,
   - score,
   - short event label.

## Phase 4: UX

Deliverable: a usable local interface.

Options:

1. CLI first: easiest and most robust.
2. Streamlit app: useful for browsing one game interactively.
3. Web UI later if the project proves stable.

## Recommended First Build

Start with a CLI that does this:

```text
nba-recap candidates --game-id 0042500151
nba-recap render-full-game --game-id 0042500151
```

This version prioritizes completeness over summarization: all clip-backed events are kept in chronological order.
