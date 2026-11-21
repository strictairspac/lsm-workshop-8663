import pytest
from fastapi import HTTPException

from src.models.dataset import Dataset
from src.schemas.value_drill import DrillAction, ValueDrillInput
from src.services import value_drill as service


def action(**kw):
    return DrillAction(**kw)


def test_create_and_drill_from_batch(db):
    db.add(Dataset(name='big', description='x', entries=[
        {'op': 'put', 'key': 'a', 'value': 'x' * 12},
        {'op': 'put', 'key': 'b', 'value': 'ok'},
    ]))
    db.commit()
    dataset = db.query(Dataset).first()
    out = service.create(db, ValueDrillInput(name='drill', dataset_id=dataset.id,
                                             value_threshold=4, value_capacity=16))
    assert out['live_key_count'] == 2
    assert out['occupancy']['used'] == 12
    assert out['verified'] is True

    read = service.act(db, out['id'], action(action='range_read', start='a', end='z'))
    assert read['result']['held'] is True
    rid = read['result']['read_id']
    assert read['drill']['open_reads'] == 1

    done = service.act(db, out['id'], action(action='finish_read', read_id=rid))
    assert done['drill']['open_reads'] == 0
    assert done['drill']['counters']['finished_reads'] == 1


def test_create_missing_dataset_is_404(db):
    with pytest.raises(HTTPException) as exc:
        service.create(db, ValueDrillInput(name='drill', dataset_id=999))
    assert exc.value.status_code == 404


def test_duplicate_name_is_409(db):
    service.create(db, ValueDrillInput(name='drill', value_threshold=4, value_capacity=16))
    with pytest.raises(HTTPException) as exc:
        service.create(db, ValueDrillInput(name='drill', value_threshold=4, value_capacity=16))
    assert exc.value.status_code == 409


def test_rule_violation_is_422_and_state_unchanged(db):
    out = service.create(db, ValueDrillInput(name='drill', value_threshold=4, value_capacity=16))
    with pytest.raises(HTTPException) as exc:
        service.act(db, out['id'], action(action='put', key='k', value='y' * 20))
    assert exc.value.status_code == 422
    after = service.detail(db, out['id'])
    assert after['keys'] == []
    assert after['occupancy']['used'] == 0


def test_missing_drill_is_404(db):
    with pytest.raises(HTTPException) as exc:
        service.act(db, 123, action(action='verify'))
    assert exc.value.status_code == 404


def test_gc_pause_then_resume_persists_across_sessions(db):
    out = service.create(db, ValueDrillInput(name='drill', value_threshold=4, value_capacity=32))
    service.act(db, out['id'], action(action='put', key='k', value='z' * 10))
    paused = service.act(db, out['id'], action(action='gc_step', pause_after_copy=True))
    assert paused['result']['kind'] == 'relocation_copied'
    assert paused['drill']['gc']['journal']['phase'] == 'copied'
    copies = paused['drill']['counters']['copies_allocated']
    assert copies == 1

    db.expire_all()
    again = service.act(db, out['id'], action(action='resume'))
    assert again['result']['phase'] == 'released'
    assert again['drill']['counters']['copies_allocated'] == copies
    assert again['drill']['verified'] is True
