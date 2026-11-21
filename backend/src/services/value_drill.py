from copy import deepcopy

from fastapi import HTTPException
from sqlalchemy import select
from ..models.value_drill import ValueDrill
from ..models.dataset import Dataset
from . import value_space as engine


def _summary(x):
    state = x.state
    return {
        'id': x.id,
        'name': x.name,
        'dataset_id': x.dataset_id,
        'value_threshold': x.value_threshold,
        'value_capacity': x.value_capacity,
        'created_at': str(x.created_at),
        'updated_at': str(x.updated_at),
        'occupancy': engine.occupancy(state),
        'counters': state['stats'],
        'gc': {
            'cursor': state['gc']['cursor'],
            'queued_remaining': max(0, len(state['gc']['queue']) - state['gc']['cursor']),
            'journal': state['gc']['journal'],
            'halted': state['gc']['halted'],
        },
        'verified': engine.verify_state(state)['ok'],
        'key_count': len(state['keys']),
        'live_key_count': sum(1 for e in state['keys'].values() if not e['deleted']),
        'open_reads': len(state['readers']),
    }


def serialize(x):
    return {**_summary(x), **engine.serialize(x.state)}


def listing(db):
    return [_summary(x) for x in db.scalars(select(ValueDrill).order_by(ValueDrill.id.desc()))]


def detail(db, ident):
    x = db.get(ValueDrill, ident)
    if not x:
        raise HTTPException(404, '值空间演练不存在')
    return serialize(x)


def create(db, data):
    if db.scalar(select(ValueDrill).where(ValueDrill.name == data.name)):
        raise HTTPException(409, '值空间演练名称已存在')
    dataset = None
    if data.dataset_id is not None:
        dataset = db.get(Dataset, data.dataset_id)
        if not dataset:
            raise HTTPException(404, '初值批次不存在')
    try:
        state = engine.new_state(dataset.entries if dataset else [], {
            'value_threshold': data.value_threshold,
            'value_capacity': data.value_capacity,
        })
    except engine.DrillRuleError as exc:
        raise HTTPException(422, str(exc))
    x = ValueDrill(
        name=data.name,
        dataset_id=data.dataset_id,
        value_threshold=data.value_threshold,
        value_capacity=data.value_capacity,
        state=state,
    )
    db.add(x)
    db.commit()
    return serialize(x)


def act(db, ident, action):
    # Row lock keeps a worker/API race from applying two actions on one stale
    # state snapshot; the state itself is still driven by the deterministic
    # action sequence submitted by the caller.
    x = db.scalar(select(ValueDrill).where(ValueDrill.id == ident).with_for_update())
    if not x:
        raise HTTPException(404, '值空间演练不存在')
    state = deepcopy(x.state)
    try:
        result = engine.run_action(state, action.model_dump())
    except engine.DrillRuleError as exc:
        db.rollback()
        raise HTTPException(422, str(exc))
    # Persist a fresh object so SQLAlchemy records the whole JSON column; the
    # in-memory copy is also left untouched when an action raises.
    x.state = state
    db.commit()
    return {'action': action.action, 'result': result, 'drill': serialize(x)}
