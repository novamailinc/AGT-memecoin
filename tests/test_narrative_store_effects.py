import json
import pytest
from daemon.launch_scanner.narratives import NarrativeHeat,append_heat,read_heat


def test_stale_tracker_save_merges_exact_pending(tmp_path):
    path=tmp_path/'heat.json'
    a=NarrativeHeat.load(path);b=NarrativeHeat.load(path)
    a.record_outcome(['dog'],2,False,100);a.save(path)
    b.record_outcome(['cat'],1,False,101);b.save(path);b.save(path)
    assert len(read_heat(path)['outcomes'])==2
    assert len(a._outcomes)==1
    a.save(path)
    assert len(a._outcomes)==2


def test_legacy_import_exact_conflict_and_corruption(tmp_path):
    path=tmp_path/'heat.json';path.write_text(json.dumps({'version':1,'outcomes':[[1,['dog'],2,False]]}))
    assert append_heat(path,[2,['cat'],1,False],'close')[0]
    assert not append_heat(path,[2,['cat'],1,False],'close')[0]
    with pytest.raises(ValueError):append_heat(path,[2,['cat'],9,False],'close')
    path.write_text('{bad')
    with pytest.raises(ValueError):append_heat(path,[2,['cat'],1,False],'new')
    assert path.read_text()=='{bad'


def test_outoforder_cannot_evict_newer(tmp_path,monkeypatch):
    import daemon.launch_scanner.narratives as n
    monkeypatch.setattr(n,'MAX_OUTCOMES',2)
    path=tmp_path/'heat.json'
    append_heat(path,[1000000,['cat'],1,False],'a');append_heat(path,[1000001,['dog'],2,False],'b')
    append_heat(path,[1,['ai'],3,False],'old')
    assert [o[0] for o in read_heat(path)['outcomes']]==[1000000,1000001]


def test_visible_append_fault_retries_same_pending_identity(tmp_path,monkeypatch):
    from daemon.launch_scanner import learning_effects as effects
    path=tmp_path/'heat.json';tracker=NarrativeHeat();tracker.record_outcome(['dog'],2,False,100)
    identity=tracker._pending[0][0];real=effects.os.replace
    def fault(a,b):real(a,b);raise OSError('visible replace, no return')
    with monkeypatch.context() as m:
        m.setattr(effects.os,'replace',fault)
        with pytest.raises(OSError):tracker.save(path)
    assert tracker._pending[0][0]==identity
    tracker.save(path)
    assert len(read_heat(path)['outcomes'])==1 and tracker._pending==[]


@pytest.mark.parametrize('mult,now,rug',[(float('nan'),1,False),(-1,1,False),(2,float('inf'),False),('bad',1,False),(2,1,'false')])
def test_invalid_record_cannot_poison_pending(tmp_path,mult,now,rug):
    tracker=NarrativeHeat();tracker.record_outcome(['dog'],2,False,1)
    before=list(tracker._outcomes),list(tracker._pending)
    with pytest.raises(ValueError):tracker.record_outcome(['cat'],mult,rug,now)
    assert (list(tracker._outcomes),list(tracker._pending))==before
    tracker.record_outcome(['cat'],3,False,2);tracker.save(tmp_path/'heat.json')
    assert len(read_heat(tmp_path/'heat.json')['outcomes'])==2


def test_local_prune_watermark_survives_refresh(tmp_path):
    path=tmp_path/'heat.json';tracker=NarrativeHeat()
    tracker.record_outcome(['dog'],2,False,1);tracker.save(path)
    tracker.prune(1000000);tracker.save(path);tracker.save(path)
    assert len(tracker)==0
    # Explicit local-view contract: no shared deletion, newly loaded view can
    # still inspect source records until source-time retention removes them.
    assert len(NarrativeHeat.load(path))==1


def test_partial_multiappend_keeps_all_retry_ids(tmp_path,monkeypatch):
    import daemon.launch_scanner.narratives as n
    path=tmp_path/'heat.json';tracker=NarrativeHeat()
    tracker.record_outcome(['dog'],2,False,1);tracker.record_outcome(['cat'],3,False,2)
    pending=list(tracker._pending);real=n.append_heat;calls=[]
    def fail_second(*args):
        calls.append(args)
        if len(calls)==2:raise OSError('second append unavailable')
        return real(*args)
    with monkeypatch.context() as m:
        m.setattr(n,'append_heat',fail_second)
        with pytest.raises(OSError):tracker.save(path)
    assert tracker._pending==pending and len(read_heat(path)['outcomes'])==1
    tracker.save(path)
    assert len(read_heat(path)['outcomes'])==2 and tracker._pending==[]
