from daemon.launch_scanner import LaunchScanner,narratives


def test_corrupt_scanner_load_is_marked_and_never_overwritten(tmp_path,monkeypatch,caplog):
    path=tmp_path/'heat.json';path.write_text('{bad')
    monkeypatch.setattr(narratives,'default_heat_path',lambda:path)
    scanner=LaunchScanner.__new__(LaunchScanner)
    tracker=scanner._narr_heat_tracker()
    assert scanner._narr_heat_status=='unavailable' and len(tracker)==0
    assert 'narrative heat unavailable' in caplog.text
    tracker.record_outcome(['dog'],2,False,1)
    scanner._narr_heat_save(2,force=True)
    assert scanner._narr_heat_status=='unavailable' and path.read_text()=='{bad'
    # Explicit external repair allows the retained pending effect to resume.
    path.unlink();scanner._narr_heat_save(3,force=True)
    assert scanner._narr_heat_status=='available' and len(narratives.NarrativeHeat.load(path))==1
