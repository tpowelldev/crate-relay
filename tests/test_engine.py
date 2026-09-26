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
