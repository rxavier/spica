"""Small in-memory library/track doubles shared by the picker tests."""

from __future__ import annotations


class FakeTrack:
    def __init__(self, track_id: str, artist: str):
        self.id = track_id
        self.uri = f"http://navidrome/{track_id}.flac"
        self.title = f"title {track_id}"
        self.artist = artist
        self.album_artist = artist

    def as_line(self) -> str:
        return f"{self.artist} - {self.title}"


class FakeLibrary:
    draw_seed = 0

    def __init__(self, tracks):
        self.tracks = {track.id: track for track in tracks}

    def by_id(self, track_id):
        return self.tracks.get(track_id)

    def by_ids(self, track_ids):
        return {track_id: self.tracks[track_id] for track_id in track_ids if track_id in self.tracks}

    def artists_by_ids(self, track_ids):
        return {
            track_id: (self.tracks[track_id].album_artist or self.tracks[track_id].artist)
            for track_id in track_ids if track_id in self.tracks
        }

    def on_station(self, station, track_id):
        return track_id in self.tracks
