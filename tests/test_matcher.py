from matcher import Event_Matcher, Song_Matcher


def config(entries):
    return {"ct_events": entries, "ct_item_defaults": {}, "ct_song_defaults": {}}


def ct_event(name="Gottesdienst", campus="Campus B", start="2026-01-04T09:30:00Z"):
    return {
        "id": 10,
        "name": name,
        "startDate": start,
        "calendar": {"domainAttributes": {"campusName": campus}},
    }


def wt_event(time="2026-01-04T10:30"):
    return {"id": "w1", "times": [time], "songs": ["s1"], "name": None, "type": "service", "mod": ""}


def test_campus_mismatch_continues_to_later_config():
    matcher = Event_Matcher(
        "Europe/Berlin",
        "Europe/Berlin",
        config(
            [
                {"name": "Wrong", "campus_name": "Campus A", "regex": "Gottesdienst", "song_placements": []},
                {"name": "Right", "campus_name": "Campus B", "regex": "Gottesdienst", "song_placements": []},
            ]
        ),
    )

    matches = matcher.match([wt_event()], [ct_event()])

    assert len(matches) == 1
    assert matches[0]["config"]["name"] == "Right"


def test_churchtools_z_time_is_converted_from_utc():
    matcher = Event_Matcher(
        "Europe/Berlin",
        "Europe/Berlin",
        config([{"name": "Gottesdienst", "campus_name": "Campus B", "regex": None, "song_placements": []}]),
    )

    matches = matcher.match([wt_event("2026-01-04T10:30")], [ct_event(start="2026-01-04T09:30:00Z")])

    assert len(matches) == 1


def test_name_fallback_matches_without_regex():
    matcher = Event_Matcher(
        "Europe/Berlin",
        "Europe/Berlin",
        config([{"name": "Gottesdienst", "campus_name": "Campus B", "regex": None, "song_placements": []}]),
    )

    matches = matcher.match([wt_event()], [ct_event(name="Sonntag Gottesdienst")])

    assert len(matches) == 1


def test_song_matcher_uses_ccli_before_name_author():
    matcher = Song_Matcher(
        [{"id": "w1", "name": "Song", "artist": "Artist", "ccli": "123", "key": "G"}],
        [{"id": 1, "name": "Other", "author": "Other", "ccli": "123", "arrangements": [], "category": {}}],
    )

    assert matcher.match("w1")["id"] == 1
