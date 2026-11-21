import json

import pytest

from src.services.value_space import (
    DrillRuleError,
    begin_range,
    delete,
    finish_read,
    gc_step,
    new_state,
    put,
    resume,
    serialize,
    verify_state,
)


def make(entries=None, threshold=8, capacity=40):
    return new_state(entries or [], {'value_threshold': threshold, 'value_capacity': capacity})


def test_large_values_leave_key_table_reference():
    state = make()
    put(state, 'sensor-01', 'x' * 20)
    entry = state['keys']['sensor-01']
    assert entry['ref'] is not None
    assert entry['inline'] is None
    assert state['blobs'][entry['ref']]['value'] == 'x' * 20
    assert serialize(state)['keys'][0]['storage'] == 'blob'


def test_small_values_stay_inline_and_unbounded_by_value_space():
    state = make(capacity=8)
    for i in range(6):
        put(state, f'k{i}', 'ab')
    assert state['blobs'] == {}
    assert state['stats']['inline_writes'] == 6
    assert verify_state(state)['ok']


def test_overwriting_large_value_keeps_old_until_gc_releases_it():
    state = make()
    put(state, 'k', 'a' * 10)
    old_ref = state['keys']['k']['ref']
    put(state, 'k', 'b' * 12)
    assert state['keys']['k']['ref'] != old_ref
    assert old_ref in state['blobs']  # still readable by an in-flight reader

    # Non-live but referenced by a key -> relocation is not needed; GC copies
    # then switches only for live values, so a superseded value is freed.
    gc_step(state)
    assert old_ref not in state['blobs']
    assert serialize(state)['keys'][0]['ref'] == state['keys']['k']['ref']


def test_relocation_copy_releases_old_only_after_switch():
    state = make()
    put(state, 'k', 'a' * 10)
    old_ref = state['keys']['k']['ref']

    result = gc_step(state, pause_after_copy=True)
    assert result['kind'] == 'relocation_copied'
    new_ref = state['keys']['k']['ref']
    assert new_ref == old_ref  # switch not effective yet
    assert old_ref in state['blobs']
    assert result['new'] in state['blobs']
    assert state['stats']['copies_allocated'] == 1

    done = resume(state)
    assert done['switched'] is True
    assert done['phase'] == 'released'
    assert old_ref not in state['blobs']
    assert state['keys']['k']['ref'] in state['blobs']
    assert verify_state(state)['ok']


def test_interrupted_relocation_resumes_without_recopying():
    state = make()
    put(state, 'k', 'a' * 10)

    first = gc_step(state, pause_after_copy=True)
    assert state['stats']['copies_allocated'] == 1
    # Retry / resume twice: the existing copy is reused, never duplicated.
    again = resume(state, pause_after_copy=True)
    assert again['copied_again'] is False
    assert state['stats']['copies_allocated'] == 1
    assert state['gc']['journal']['new'] == first['new']

    done = resume(state)
    assert state['stats']['copies_allocated'] == 1
    assert state['stats']['relocations_completed'] == 1
    assert state['gc']['journal'] is None
    assert first['old'] not in state['blobs']
    assert len(state['blobs']) == 1


def test_open_range_read_keeps_old_value_readable_through_overwrite_and_gc():
    state = make()
    put(state, 'a', 'a' * 10)
    put(state, 'b', 'b' * 10)
    old_ref = state['keys']['a']['ref']

    read = begin_range(state, 'a', 'z')
    assert set(read['pins']) == {old_ref, state['keys']['b']['ref']}

    put(state, 'a', 'c' * 10)  # concurrent overwrite while reader is open
    assert old_ref in state['blobs']

    # GC must defer both relocation and free while the reader holds the pin.
    step = gc_step(state)
    assert step['kind'] == 'deferred_reader_pin'
    assert old_ref in state['blobs']

    finished = finish_read(state, read['read_id'])
    assert finished['snapshot'] == {'a': 'a' * 10, 'b': 'b' * 10}
    assert finished['verified'] is True
    # The reader's own returned values survive; reclamation still happens on
    # the GC step so the queue cursor advances in one place.
    assert old_ref in state['blobs']
    gc_step(state)  # frees the superseded value (no reader pins it now)
    assert old_ref not in state['blobs']
    assert verify_state(state)['ok']


def test_switch_waits_for_pinned_reader_before_releasing_old_address():
    state = make()
    put(state, 'k', 'a' * 10)
    old_ref = state['keys']['k']['ref']
    read = begin_range(state, 'k', 'k')
    assert read['pins'] == [old_ref]

    gc_step(state)  # relocation deferred while pinned
    # Release the pin; the next step performs copy+switch+release.
    finish_read(state, read['read_id'])
    done = gc_step(state)
    assert done['phase'] == 'released'
    assert old_ref not in state['blobs']
    assert state['keys']['k']['ref'] in state['blobs']


def test_switch_then_defer_release_while_old_read_open():
    state = make()
    put(state, 'k', 'a' * 10)
    old_ref = state['keys']['k']['ref']

    # Start the read, then let GC copy the value.  A read pinned *before* the
    # copy forces a defer instead of releasing the old address.
    read = begin_range(state, 'k', 'k')
    step = gc_step(state)
    assert step['kind'] == 'deferred_reader_pin'

    # A different model: start the range read after the read snapshot, copy
    # starts, then the read pins.  Covered by the journal-switch test below.
    finish_read(state, read['read_id'])


def test_capacity_refuses_relocation_without_deleting_readable_value():
    # capacity 20 holds one 10-unit value; the copy does not fit, so GC must
    # refuse and keep the old value intact.
    state = make(capacity=16)
    put(state, 'a', 'a' * 10)
    old_ref = state['keys']['a']['ref']

    step = gc_step(state)
    assert step['kind'] == 'relocation_refused_capacity'
    assert state['keys']['a']['ref'] == old_ref
    assert old_ref in state['blobs']
    assert state['gc']['halted'] is True
    assert verify_state(state)['ok']

    # Putting beyond the bound is refused before mutating the key table.
    with pytest.raises(DrillRuleError):
        put(state, 'b', 'b' * 12)
    assert 'b' not in state['keys']
    assert verify_state(state)['ok']


def test_concurrent_overwrite_during_paused_relocation_never_resurrects():
    state = make()
    put(state, 'k', 'a' * 10)
    old_ref = state['keys']['k']['ref']

    gc_step(state, pause_after_copy=True)
    put(state, 'k', 'b' * 10)  # overwrite while copy is parked
    done = resume(state)
    assert done['phase'] == 'aborted'
    assert state['keys']['k']['ref'] != old_ref
    assert state['stats']['relocations_aborted'] == 1
    # Oracle still equals what a key-table read returns.
    assert verify_state(state)['ok']
    assert serialize(state)['keys'][0]['storage'] == 'blob'


def test_state_survives_json_roundtrip_and_resume():
    state = make()
    put(state, 'k', 'a' * 10)
    gc_step(state, pause_after_copy=True)
    loaded = json.loads(json.dumps(state))
    gc = loaded['gc']
    assert gc['journal'] is not None
    resumed = resume(loaded)
    assert resumed['phase'] == 'released'
    assert verify_state(loaded)['ok']


def test_hold_false_range_read_finishes_immediately():
    state = make()
    put(state, 'a', 'a' * 10)
    read = begin_range(state, 'a', 'a', hold=False)
    assert read['held'] is False
    assert read['finished']['verified'] is True
    assert not state['readers']
    # Value is still live, so no pin remains.
    assert state['blobs'][state['keys']['a']['ref']]['pinned_by'] == []


def test_delete_reclaims_value_after_reader_finishes():
    state = make()
    put(state, 'k', 'a' * 10)
    ref = state['keys']['k']['ref']
    read = begin_range(state, 'k', 'k')
    delete(state, 'k')
    assert ref in state['blobs']  # pinned: the open read may still see it

    finish_read(state, read['read_id'])
    # The deleted value is queued and reclaimed without any relocation copy.
    step = gc_step(state)
    assert step['kind'] == 'garbage_reclaimed'
    assert ref not in state['blobs']
    assert verify_state(state)['ok']


def test_random_actions_preserve_invariants():
    import random
    rng = random.Random(20261121)
    state = make(capacity=120, threshold=6)
    open_reads = []
    for _ in range(400):
        choice = rng.random()
        try:
            if choice < 0.34:
                put(state, f'k{rng.randrange(6)}', 'v' * rng.randrange(0, 22))
            elif choice < 0.47:
                delete(state, f'k{rng.randrange(6)}')
            elif choice < 0.57:
                r = begin_range(state, 'k0', 'k9')
                open_reads.append(r)
            elif choice < 0.70 and open_reads:
                r = open_reads.pop(rng.randrange(len(open_reads)))
                result = finish_read(state, r['read_id'])
                assert result['verified']
            elif choice < 0.84:
                gc_step(state)
            elif choice < 0.94 and state['gc']['journal'] is not None:
                resume(state)
            else:
                resume(state, pause_after_copy=rng.random() < 0.5)
        except DrillRuleError:
            pass  # capacity refusals are allowed; invariants must still hold
        verdict = verify_state(state)
        assert verdict['ok'], verdict['checks']
        for bid, blob in state['blobs'].items():
            assert len(blob['pinned_by']) == len(set(blob['pinned_by']))
            for rid in blob['pinned_by']:
                assert rid in state['readers']
                assert bid in state['readers'][rid]['pins']
    for r in list(open_reads):
        result = finish_read(state, r['read_id'])
        assert result['verified']
    assert verify_state(state)['ok']


def test_capacity_refusal_keeps_progress_for_later_retry():
    state = make(capacity=16)
    put(state, 'a', 'a' * 10)
    ref = state['keys']['a']['ref']
    first = gc_step(state)
    assert first['kind'] == 'relocation_refused_capacity'
    assert state['gc']['cursor'] == 0
    assert state['keys']['a']['ref'] == ref
    # Overwrite to a small inline value frees the queued blob; retry proceeds.
    put(state, 'a', 'tiny')
    assert state['stats']['refused_relocations'] == 1
    for _ in range(3):
        gc_step(state)
    assert ref not in state['blobs']
    assert verify_state(state)['ok']


def test_reader_started_during_pause_blocks_old_address_release_after_switch():
    state = make(capacity=60)
    put(state, 'k', 'a' * 10)
    old_ref = state['keys']['k']['ref']
    gc_step(state, pause_after_copy=True)  # copy parked, key still -> old
    assert state['keys']['k']['ref'] == old_ref

    # A reader begins while parked, so it pins the old address.
    read = begin_range(state, 'k', 'k')
    assert read['pins'] == [old_ref]

    resumed = resume(state)  # switch happens; release must wait
    assert resumed['phase'] == 'switched'
    assert old_ref in state['blobs']
    assert state['keys']['k']['ref'] != old_ref
    assert state['keys']['k']['ref'] in state['blobs']

    # The reader still sees the value it started with, after the switch.
    finished = finish_read(state, read['read_id'])
    assert finished['snapshot'] == {'k': 'a' * 10}
    # Release was gated on this read finishing, so it can happen right here.
    assert old_ref not in state['blobs']
    assert verify_state(state)['ok']
