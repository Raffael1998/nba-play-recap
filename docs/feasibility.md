# Feasibility Notes

## Existing Building Blocks

1. `nba_api` documents a `VideoEvents` endpoint at `https://stats.nba.com/stats/videoevents` with required parameters `GameEventID` and `GameID`.
2. `nba_api` also documents `PlayByPlay`, which includes an `AvailableVideo` dataset with `VIDEO_AVAILABLE_FLAG`.
3. `hoopR` documents `nba_videodetailsasset()` returning `videoUrls` fields such as `surl`, `murl`, and `lurl`, which strongly suggests direct clip URLs can be resolved.
4. `CrossoverClips` is an existing public project for browsing/downloading NBA highlights by game and filters.
5. `alijkhalil/nba_pbp_video_dataset` explicitly describes downloading large volumes of short NBA play clips plus event labels.
6. `cp6/nba-live` exposes a "play by play clips" wrapper by `game_id` and `event_number`.

## What This Means

- The data path is likely viable:
  - get game play-by-play,
  - identify event numbers with video,
  - query clip metadata per event,
  - rank and assemble recap moments.
- This does not appear to be a novel data-access problem.
- The novel part is recap quality: event selection, ordering, de-duplication, and narrative.

## Confirmed Working Path

For the example game `0042500151`, the most reliable combination in this environment is:

1. timeline from `https://cdn.nba.com/static/json/liveData/playbyplay/playbyplay_{game_id}.json`
2. clip metadata from `https://stats.nba.com/stats/videoeventsasset?GameEventID={event_num}&GameID={game_id}`

The `videoeventsasset` response for event `7` returned direct `surl`, `murl`, and `lurl` MP4 links.

The older `stats.nba.com/stats/playbyplayv2` endpoint returned an empty `{}` response during local validation, so it should be treated as optional or fallback-only.

## Clip Timing Metadata

The validated `videoeventsasset` payload exposes:

- direct clip URLs,
- thumbnail URLs,
- subtitle file URLs,
- clip durations such as `sdur`, `mdur`, and `ldur`.

It does not appear to expose exact game-relative clip start and end boundaries. That means:

- chronological stitching is straightforward,
- exact overlap removal is not reliable from endpoint metadata alone,
- any future de-duplication or precise trimming will likely require analyzing the downloaded media or subtitle timing.

## Likely Technical Pipeline

1. Resolve a `GameID`.
2. Pull play-by-play for all periods.
3. Filter events where video is available.
4. For each candidate event, fetch clip metadata or direct clip URLs.
5. Score candidate events by importance.
6. Emit a recap timeline:
   - quarter
   - clock
   - score state
   - description
   - clip URL
   - importance score
7. Optionally download and stitch the selected clips into one MP4.

## Risks

- Some NBA endpoints are fragile and often require browser-like headers.
- Not every meaningful event has a clip.
- The event page URL on `nba.com/stats/events` is probably a viewer page, not the underlying video asset itself.
- A stitched downloadable recap may create more rights issues than a metadata-first local tool.

## Usage Constraints

The current NBA.com Terms of Use indicate:

- basketball content, including video, is restricted and generally requires permission for reproduction or redistribution,
- downloads are framed as personal, non-commercial use where the function is available,
- NBA statistics require attribution and have additional restrictions around comprehensive, regularly updated play-by-play products.

This project should therefore be treated as a personal-use local tool unless licensing is clarified.
