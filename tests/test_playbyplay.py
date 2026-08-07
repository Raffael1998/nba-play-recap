from __future__ import annotations

import unittest

from nba_play_recap.playbyplay import attach_video_metadata, extract_candidate_actions
from nba_play_recap.render import EXCLUDED_ACTION_TYPES


def actions() -> list[dict]:
    """The shape of `__NEXT_DATA__.props.pageProps.playByPlay.actions` on a game page."""
    return [
        {
            "actionNumber": 10,
            "actionType": "3pt",
            "period": 1,
            "clock": "PT10M28.00S",
            "teamTricode": "SAS",
            "description": "Fox 26' 3PT Jump Shot",
            "scoreHome": "3",
            "scoreAway": "0",
            "videoAvailable": 1,
        },
        {
            "actionNumber": 11,
            "actionType": "rebound",
            "period": 1,
            "clock": "PT10M25.00S",
            "teamTricode": "NYK",
            "description": "Brunson REBOUND",
            "scoreHome": "3",
            "scoreAway": "0",
            "videoAvailable": 0,
        },
        {
            "actionNumber": 13,
            "actionType": "",
            "period": 1,
            "clock": "PT08M40.00S",
            "teamTricode": "SAS",
            "description": "Wembanyama BLOCK (1 BLK)",
            "scoreHome": "3",
            "scoreAway": "0",
            "videoAvailable": 1,
        },
        {
            "actionNumber": 12,
            "actionType": "Substitution",
            "period": 1,
            "clock": "PT09M00.00S",
            "teamTricode": "NYK",
            "description": "SUB: Robinson FOR Towns",
            "scoreHome": "3",
            "scoreAway": "0",
            "videoAvailable": 1,
        },
    ]


class ExtractCandidateActionsTests(unittest.TestCase):
    def test_events_without_video_are_dropped_before_any_request_is_made(self) -> None:
        candidates = extract_candidate_actions(actions(), "0042500402")

        # 11 has videoAvailable 0; 10 and 12 both have a clip. No action-type filter
        # is applied unless one is passed — the default set belongs to the caller.
        self.assertEqual([candidate.event_num for candidate in candidates], [10, 13, 12])

    def test_video_only_can_be_turned_off(self) -> None:
        candidates = extract_candidate_actions(actions(), "0042500402", video_only=False)

        self.assertEqual([candidate.event_num for candidate in candidates], [10, 11, 13, 12])

    def test_the_denylist_drops_non_plays_and_keeps_everything_else(self) -> None:
        candidates = extract_candidate_actions(
            actions(), "0042500402", exclude_action_types=EXCLUDED_ACTION_TYPES
        )

        # The substitution has a clip but is not basketball. The block does not name
        # its action type at all, and must survive precisely because of that.
        self.assertEqual([candidate.event_num for candidate in candidates], [10, 13])

    def test_iso_clock_is_converted_to_a_display_clock_and_seconds(self) -> None:
        candidate = extract_candidate_actions(actions(), "0042500402")[0]

        self.assertEqual(candidate.clock, "10:28.00")
        self.assertEqual(candidate.clock_seconds_remaining, 628.0)
        self.assertEqual(candidate.team_tricode, "SAS")
        self.assertTrue(candidate.video_available)

    def test_malformed_entries_are_skipped_rather_than_raising(self) -> None:
        payload = actions() + ["not a dict", {"actionType": "2pt", "videoAvailable": 1}]

        candidates = extract_candidate_actions(payload, "0042500402")

        # The string and the action with no actionNumber are both skipped.
        self.assertEqual([candidate.event_num for candidate in candidates], [10, 13, 12])


class AttachVideoMetadataTests(unittest.TestCase):
    def test_a_clip_url_and_duration_are_taken_from_the_asset_payload(self) -> None:
        candidate = extract_candidate_actions(actions(), "0042500402")[0]

        attach_video_metadata(
            candidate,
            {
                "resultSets": {
                    "Meta": {
                        "videoUrls": [
                            {"lurl": "https://videos.nba.com/x_1280x720.mp4", "ldur": 8516}
                        ]
                    }
                }
            },
        )

        self.assertEqual(candidate.clip_url, "https://videos.nba.com/x_1280x720.mp4")
        self.assertEqual(candidate.clip_duration_seconds, 8.516)
        self.assertEqual(candidate.availability_status, "available")

    def test_an_empty_video_url_list_marks_the_event_missing_rather_than_available(self) -> None:
        candidate = extract_candidate_actions(actions(), "0042500402")[0]

        attach_video_metadata(candidate, {"resultSets": {"Meta": {"videoUrls": []}}})

        self.assertFalse(candidate.video_available)
        self.assertEqual(candidate.availability_status, "missing")


if __name__ == "__main__":
    unittest.main()
