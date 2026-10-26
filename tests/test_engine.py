import unicodedata

import pytest

from crate_relay import engine

XML = """<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0">
 <PRODUCT Name="MIXO" Version="0.0.1" Company="MIXO"/>
 <COLLECTION Entries="3">
  <TRACK TrackID="0" Name="A" Location="file://localhost/Users/dj/Music/A%20Track.mp3"/>
  <TRACK TrackID="1" Name="B" Location="file://localhost/Users/dj/Music/Caf%C3%A9%20(Mix).mp3"/>
  <TRACK TrackID="2" Name="C" Location="file:///Users/dj/Music/C.wav"/>
 </COLLECTION>
 <PLAYLISTS>
  <NODE Type="0" Name="ROOT" Count="2">
   <NODE Name="House" Type="1" KeyType="0" Entries="2">
    <TRACK Key="1"/>
    <TRACK Key="0"/>
   </NODE>
   <NODE Type="0" Name="Sets" Count="1">
    <NODE Name="Warmup" Type="1" KeyType="1" Entries="1">
     <TRACK Key="file://localhost/Users/dj/Music/C.wav"/>
    </NODE>
   </NODE>
  </NODE>
 </PLAYLISTS>
</DJ_PLAYLISTS>
"""


@pytest.fixture
def export(tmp_path):
    p = tmp_path / "rekordbox.xml"
    p.write_text(XML, encoding="utf-8")
    return p


def test_reads_tree_in_order(export):
    tree = engine.read_mixo_export(export)
    assert [n["name"] for n in tree] == ["House", "Sets"]
    house, sets = tree
    assert house["folder"] is False
    assert house["paths"] == ["/Users/dj/Music/Café (Mix).mp3", "/Users/dj/Music/A Track.mp3"]
    assert sets["folder"] is True
    assert sets["children"][0]["name"] == "Warmup"
    assert sets["children"][0]["paths"] == ["/Users/dj/Music/C.wav"]  # KeyType=1 (by location)


def test_paths_are_nfc_normalised():
    # "e" + combining acute (NFD, as macOS often stores it) must match "é" (NFC)
    path = engine.location_to_path("file://localhost/Users/dj/Cafe%CC%81.mp3")
    assert path == "/Users/dj/Café.mp3"
    assert engine.norm_path(unicodedata.normalize("NFD", "Café")) == "Café"


def test_missing_export(tmp_path):
    with pytest.raises(engine.SyncError, match="not found"):
        engine.read_mixo_export(tmp_path / "nope.xml")


def test_truncated_export(tmp_path):
    p = tmp_path / "rekordbox.xml"
    p.write_text(XML[:400])
    with pytest.raises(engine.SyncError, match="still be being written"):
        engine.read_mixo_export(p)


def test_change_text():
    assert str(engine.Change("create", "playlist", "MIXO/House", "12 tracks")) == "Add playlist MIXO/House (12 tracks)"
    assert str(engine.Change("reorder", "playlist", "MIXO/House")) == "Reorder playlist MIXO/House"


def test_result_dict():
    r = engine.SyncResult(changes=[engine.Change("delete", "folder", "MIXO/Old")], playlists=3, entries=10)
    d = r.to_dict()
    assert d["changes"] == ["Remove folder MIXO/Old"]
    assert d["in_sync"] is False


def test_backups_rotate(tmp_path):
    lib = tmp_path / "rekordbox"
    lib.mkdir()
    (lib / "master.db").write_bytes(b"db")
    (lib / "masterPlaylists6.xml").write_text("<x/>")
    backups = tmp_path / "backups"
    import time

    made = []
    for _ in range(3):
        made.append(engine.backup(lib / "master.db", backups, keep=2))
        time.sleep(1.01)
    kept = engine.list_backups(backups)
    assert len(kept) == 2
    assert kept[0] == made[-1]


# --------------------------------------------------------------------------- track info
TRACK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0">
 <COLLECTION Entries="3">
  <TRACK TrackID="0" Name="Warriah (VIP)" Artist="Moresounds" Album="Warriah VIP" Genre="Drum &amp; Bass"
         Location="file://localhost/m/Moresounds%20-%20Warriah_PN.mp3"/>
  <TRACK TrackID="1" Name="Same" Artist="Known" Genre="" Location="file://localhost/m/same.mp3"/>
  <TRACK TrackID="2" Name="Not In Rekordbox" Location="file://localhost/m/missing.mp3"/>
 </COLLECTION>
 <PLAYLISTS><NODE Type="0" Name="ROOT" Count="0"/></PLAYLISTS>
</DJ_PLAYLISTS>
"""


class Row:
    def __init__(self, **kw):
        self.rb_local_deleted = 0
        self.__dict__.update(kw)


class FakeDB:
    def __init__(self):
        self.artists = [Row(ID="a1", Name="Known")]
        self.albums = []
        self.genres = [Row(ID="g1", Name="House")]
        self.content = [
            Row(ID="c1", FolderPath="/m/Moresounds - Warriah_PN.mp3", Title="Moresounds - Warriah_PN",
                ArtistID=None, AlbumID=None, GenreID="g1", TrackInfoUpdated="2", updated_at=None, BPM=17400),
            Row(ID="c2", FolderPath="/m/same.mp3", Title="Same ", ArtistID="a1", AlbumID=None, GenreID="g1",
                TrackInfoUpdated="1", updated_at=None, BPM=12800),
        ]
        self.flushed = False

    def get_content(self):
        return self.content

    def get_artist(self):
        return self.artists

    def get_album(self):
        return self.albums

    def get_genre(self):
        return self.genres

    def _add(self, table, prefix, name):
        row = Row(ID=f"{prefix}{len(table) + 1}", Name=name)
        table.append(row)
        return row

    def add_artist(self, name):
        return self._add(self.artists, "a", name)

    def add_album(self, name):
        return self._add(self.albums, "al", name)

    def add_genre(self, name):
        return self._add(self.genres, "g", name)

    def flush(self):
        self.flushed = True


def test_reads_track_info(tmp_path):
    p = tmp_path / "rekordbox.xml"
    p.write_text(TRACK_XML, encoding="utf-8")
    tracks = engine.read_mixo(p).tracks
    assert tracks["/m/Moresounds - Warriah_PN.mp3"] == {
        "title": "Warriah (VIP)", "artist": "Moresounds", "album": "Warriah VIP", "genre": "Drum & Bass",
    }


def test_track_info_preview_changes_nothing(tmp_path):
    p = tmp_path / "rekordbox.xml"
    p.write_text(TRACK_XML, encoding="utf-8")
    db = FakeDB()
    mirror = engine._Mirror(db, "MIXO", dry_run=True)
    mirror.update_track_info(engine.read_mixo(p).tracks)
    assert [str(c) for c in mirror.result.changes] == [
        "Update track info for Moresounds – Warriah (VIP): title, artist, album, genre"
    ]
    assert db.content[0].Title == "Moresounds - Warriah_PN"
    assert len(db.artists) == 1 and not db.flushed


def test_track_info_apply(tmp_path):
    p = tmp_path / "rekordbox.xml"
    p.write_text(TRACK_XML, encoding="utf-8")
    db = FakeDB()
    mirror = engine._Mirror(db, "MIXO", dry_run=False)
    mirror.update_track_info(engine.read_mixo(p).tracks)
    c1, c2 = db.content
    assert c1.Title == "Warriah (VIP)"
    assert [a.Name for a in db.artists if a.ID == c1.ArtistID] == ["Moresounds"]
    assert [g.Name for g in db.genres if g.ID == c1.GenreID] == ["Drum & Bass"]
    assert [a.Name for a in db.albums if a.ID == c1.AlbumID] == ["Warriah VIP"]
    assert c1.TrackInfoUpdated == "3" and c1.updated_at is not None
    assert c1.BPM == 17400  # analysis untouched
    # unchanged track: same title (ignoring whitespace), same artist, empty MIXO genre leaves "House" alone
    assert c2.Title == "Same " and c2.ArtistID == "a1" and c2.GenreID == "g1" and c2.TrackInfoUpdated == "1"
    assert mirror.result.tracks_updated == 1


def test_track_info_reuses_existing_names(tmp_path):
    p = tmp_path / "rekordbox.xml"
    p.write_text(TRACK_XML, encoding="utf-8")
    db = FakeDB()
    db.artists.append(Row(ID="a9", Name="Moresounds"))
    engine._Mirror(db, "MIXO", dry_run=False).update_track_info(engine.read_mixo(p).tracks)
    assert db.content[0].ArtistID == "a9"
    assert len(db.artists) == 2
