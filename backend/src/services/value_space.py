"""Deterministic value-space reclamation engine.

Large telemetry values live in a separately bounded value space; the key table
only holds a reference.  Reclamation is modelled as a two-phase relocation
(copy, switch, release) with pinned range readers:

* a reader that has started keeps the values it saw, even if keys are later
  overwritten/deleted and the value is relocated meanwhile;
* an old address is released only after the redirect to the new address is
  effective *and* every reader that pinned the old address has finished;
* relocation copies occupy the bounded value space, so a relocation is refused
  while the copy does not fit instead of deleting a value that is still
  readable;
* progress, the queue cursor and a per-relocation journal are kept in state, so
  an interrupted relocation resumes where it stopped and a retry never counts
  the same stored copy twice.

The state is plain JSON-serialisable data; the caller persists it verbatim.
This is a teaching model, not a production storage engine.
"""

from __future__ import annotations

MAX_KEY = 64
MAX_VALUE = 200
PREVIEW = 56
EVENT_LIMIT = 240
QUEUE_COMPACT_AT = 2048


class DrillRuleError(ValueError):
    """A drill rule was violated; the HTTP layer maps this to 422."""


# ---------------------------------------------------------------------------
# small helpers


def _check_key(key):
    if not isinstance(key, str) or not 1 <= len(key) <= MAX_KEY:
        raise DrillRuleError(f'键必须是1到{MAX_KEY}个字符的字符串')


def _check_value(value):
    if not isinstance(value, str) or len(value) > MAX_VALUE:
        raise DrillRuleError(f'值必须是不超过{MAX_VALUE}个字符的字符串')


def _used(state):
    return sum(b['size'] for b in state['blobs'].values())


def used_units(state):
    return _used(state)


def _preview(value):
    if value is None:
        return None
    return value if len(value) <= PREVIEW else value[:PREVIEW] + '…'


def _event(state, action, event, **detail):
    state['event_seq'] += 1
    item = {'step': state['event_seq'], 'action': action, 'event': event, **detail}
    events = state['events']
    events.append(item)
    if len(events) > EVENT_LIMIT:
        del events[: len(events) - EVENT_LIMIT]
    state['gc']['last_event'] = item
    return item


def _alloc_blob(state, value, size, *, kind, copy_of=None, queue=False):
    capacity = state['params']['value_capacity']
    if _used(state) + size > capacity:
        raise DrillRuleError(
            f'值空间容量不足：需要{size}单位，已用{_used(state)}/{capacity}；'
            '已拒绝写入或搬迁，原有可读值未删除'
        )
    bid = f"v{state['next_blob']}"
    state['next_blob'] += 1
    blob = {
        'id': bid,
        'value': value,
        'size': size,
        'kind': kind,
        'copy_of': copy_of,
        'pinned_by': [],
        'queued': False,
    }
    state['blobs'][bid] = blob
    if queue:
        _enqueue(state, bid)
    return blob


def _enqueue(state, bid):
    blob = state['blobs'].get(bid)
    if blob is not None and not blob['queued']:
        blob['queued'] = True
        gc = state['gc']
        gc['queue'].append(bid)
        # Consumed entries are dropped so repeated drilling does not grow the
        # persisted queue without bound; the cursor is rebased accordingly.
        if len(gc['queue']) > QUEUE_COMPACT_AT and gc['cursor'] > QUEUE_COMPACT_AT // 2:
            del gc['queue'][: gc['cursor']]
            gc['cursor'] = 0


def _live_entry(state, bid):
    """The live key currently redirected to ``bid``, if any."""
    for key, entry in state['keys'].items():
        if not entry['deleted'] and entry.get('ref') == bid:
            return key
    return None


def _entry_value(state, entry):
    if entry is None or entry['deleted']:
        return None
    if entry.get('ref') is not None:
        blob = state['blobs'].get(entry['ref'])
        if blob is None:
            raise DrillRuleError('键表引用指向已释放的值')
        return blob['value']
    return entry.get('inline')


# ---------------------------------------------------------------------------
# write / read actions


def put(state, key, value):
    _check_key(key)
    _check_value(value)
    size = len(value)
    threshold = state['params']['value_threshold']
    external = size > threshold
    old = state['keys'].get(key)
    old_ref = old.get('ref') if old and not old['deleted'] else None

    if external:
        # Capacity is checked before any key-table mutation, so a refused put
        # leaves every readable value untouched.
        blob = _alloc_blob(state, value, size, kind='value', queue=True)
        entry = {'seq': state['seq'], 'deleted': False, 'ref': blob['id'], 'inline': None}
    else:
        entry = {'seq': state['seq'], 'deleted': False, 'ref': None, 'inline': value}

    state['seq'] += 1
    state['keys'][key] = entry
    state['oracle'][key] = value
    if old_ref is not None:
        # The superseded value may still be pinned by a reader; it enters the
        # GC queue and is freed only once safe.
        _enqueue(state, old_ref)
    stats = state['stats']
    stats['writes'] += 1
    if external:
        stats['external_writes'] += 1
    else:
        stats['inline_writes'] += 1
    return _event(
        state,
        'put',
        'value_referenced' if external else 'value_inline',
        key=key,
        size=size,
        threshold=threshold,
        ref=entry['ref'],
        used=_used(state),
    )


def delete(state, key):
    _check_key(key)
    old = state['keys'].get(key)
    old_ref = old.get('ref') if old and not old['deleted'] else None
    state['keys'][key] = {'seq': state['seq'], 'deleted': True, 'ref': None, 'inline': None}
    state['seq'] += 1
    state['oracle'].pop(key, None)
    if old_ref is not None:
        _enqueue(state, old_ref)
    state['stats']['deletes'] += 1
    return _event(state, 'delete', 'tombstone_written', key=key,
                  released_ref=old_ref, used=_used(state))


def begin_range(state, start, end, hold=True):
    if not isinstance(start, str) or not isinstance(end, str):
        raise DrillRuleError('范围读取需要字符串 start 与 end')
    if start > end:
        raise DrillRuleError('范围起点不能大于终点')
    keys = sorted(k for k in state['keys'] if start <= k <= end)
    snapshot = {}
    pins = []
    for key in keys:
        value = _entry_value(state, state['keys'][key])
        if value is None:
            continue
        snapshot[key] = value
        ref = state['keys'][key].get('ref')
        if ref is not None and ref not in pins:
            pins.append(ref)
    rid = f"r{state['next_reader']}"
    state['next_reader'] += 1
    reader = {
        'id': rid,
        'start': start,
        'end': end,
        'snapshot': snapshot,
        'expected': dict(snapshot),
        'pins': pins,
        'active': True,
        'started_seq': state['seq'],
        'started_used': _used(state),
    }
    state['readers'][rid] = reader
    for bid in pins:
        state['blobs'][bid]['pinned_by'].append(rid)
    state['stats']['range_reads'] += 1
    _event(state, 'range_read', 'snapshot_started', read_id=rid, start=start, end=end,
           keys=len(snapshot), pins=pins, hold=hold, used=_used(state))
    if not hold:
        finished = finish_read(state, rid)
        return {'read_id': rid, 'snapshot': snapshot, 'pins': pins,
                'held': False, 'finished': finished}
    return {'read_id': rid, 'snapshot': snapshot, 'pins': pins,
            'held': True, 'finished': None}


def _release_ready(state):
    """Free retired blobs that no active reader pins.

    A blob is freed only when it is no longer referenced by a live key entry and
    no reader holds it; this is where "old address released after switch and
    after old reads finish" is enforced.
    """
    released = []
    for bid in list(state['gc']['pending_free']):
        blob = state['blobs'].get(bid)
        if blob is None:
            state['gc']['pending_free'].remove(bid)
            continue
        if blob['pinned_by']:
            continue
        if _live_entry(state, bid) is not None:
            continue
        del state['blobs'][bid]
        state['redirects'].pop(bid, None)
        state['gc']['pending_free'].remove(bid)
        blob['queued'] = False
        state['stats']['reclaimed_units'] += blob['size']
        released.append({'id': bid, 'size': blob['size']})
    return released


def finish_read(state, read_id):
    reader = state['readers'].get(read_id)
    if not reader or not reader['active']:
        raise DrillRuleError('范围读不存在或已结束')
    missing = [bid for bid in reader['pins'] if bid not in state['blobs']]
    if missing:
        raise DrillRuleError('内部错误：范围读持有的旧地址已被提前释放')
    if reader['snapshot'] != reader['expected']:
        raise DrillRuleError('内部错误：范围读快照与开始时记录不一致')
    reader['active'] = False
    for bid in reader['pins']:
        blob = state['blobs'].get(bid)
        if blob is not None and read_id in blob['pinned_by']:
            blob['pinned_by'].remove(read_id)
    del state['readers'][read_id]
    released = _release_ready(state)
    state['stats']['finished_reads'] += 1
    record = {
        'read_id': read_id,
        'start': reader['start'],
        'end': reader['end'],
        'snapshot': reader['snapshot'],
        'pins': reader['pins'],
        'verified': reader['snapshot'] == reader['expected'],
        'released_on_finish': released,
    }
    history = state['read_history']
    history.append(record)
    if len(history) > 20:
        del history[: len(history) - 20]
    _event(state, 'finish_read', 'snapshot_finished', read_id=read_id,
           keys=len(reader['snapshot']), pins=reader['pins'],
           released=[x['id'] for x in released], used=_used(state))
    return record


# ---------------------------------------------------------------------------
# relocation journal


def _advance_journal(state, pause_after_copy):
    """Continue a persisted two-phase relocation exactly where it stopped."""
    journal = state['gc']['journal']
    if journal is None:
        return None
    result = {'previous_phase': journal['phase'], 'copied_again': False,
              'switched': False, 'phase': journal['phase']}
    old = journal['old']
    new = journal['new']

    if journal['phase'] == 'copied':
        if pause_after_copy:
            # The copy already exists; resume-with-pause stops again before the
            # redirect is switched, so no second copy is ever allocated.
            state['gc']['halted'] = True
            result.update({'paused': True, 'phase': 'copied'})
            return result
        # A concurrent overwrite/delete may have replaced the old reference
        # while the copy was parked.  Switching must never resurrect the old
        # value in that case.
        switched = False
        for entry in state['keys'].values():
            if not entry['deleted'] and entry.get('ref') == old:
                entry['ref'] = new
                switched = True
        if not switched:
            # The parked copy is unreferenced; both addresses can be retired.
            journal['phase'] = 'aborted'
            for bid in (new, old):
                if bid in state['blobs']:
                    state['blobs'][bid]['queued'] = False
                    state['gc']['pending_free'].append(bid)
            state['gc']['cursor'] += 1
            state['gc']['journal'] = None
            released = _release_ready(state)
            state['stats']['relocations_aborted'] += 1
            _event(state, 'gc_step', 'relocation_aborted_concurrent_write',
                   old=old, new=new, released=[x['id'] for x in released],
                   used=_used(state))
            result.update({'phase': 'aborted', 'released': released})
            return result
        state['redirects'][old] = new
        journal['phase'] = 'switched'
        new_blob = state['blobs'].get(new)
        if new_blob is not None:
            new_blob['kind'] = 'value'
            new_blob['copied_from'] = old
        old_blob = state['blobs'].get(old)
        if old_blob is not None:
            old_blob['queued'] = False
            state['gc']['pending_free'].append(old)
        _event(state, 'gc_step', 'redirect_switched', old=old, new=new,
               size=journal['size'], used=_used(state))
        result.update({'switched': True, 'phase': 'switched'})

    if journal['phase'] == 'switched':
        old_blob = state['blobs'].get(old)
        if old_blob is not None and old_blob['pinned_by']:
            # The redirect is effective, but an old range read still holds the
            # old address.  Keep the journal open; finish the read first.
            state['gc']['halted'] = True
            state['stats']['deferred_relocations'] += 1
            _event(state, 'gc_step', 'release_deferred_reader_pin', old=old, new=new,
                   pins=list(old_blob['pinned_by']), used=_used(state))
            result.update({'phase': 'switched', 'deferred_pins': list(old_blob['pinned_by'])})
            return result
        released = _release_ready(state)
        state['gc']['cursor'] += 1
        state['gc']['journal'] = None
        state['stats']['relocations_completed'] += 1
        _event(state, 'gc_step', 'old_address_released', old=old, new=new,
               released=[x['id'] for x in released], used=_used(state))
        result.update({'phase': 'released', 'released': released})
    return result


def gc_step(state, pause_after_copy=False):
    gc = state['gc']
    state['stats']['gc_steps'] += 1
    gc['halted'] = False

    if gc['journal'] is not None:
        result = _advance_journal(state, pause_after_copy)
        gc['halted'] = bool(pause_after_copy) or bool(result and result.get('deferred_pins'))
        return {'kind': 'journal_resumed', **(result or {})}

    queue = gc['queue']
    if gc['cursor'] >= len(queue):
        return {'kind': 'queue_empty', 'cursor': gc['cursor'], 'queued': len(queue)}

    bid = queue[gc['cursor']]
    blob = state['blobs'].get(bid)
    if blob is None:
        gc['cursor'] += 1
        return {'kind': 'already_released', 'blob': bid}

    pins = list(blob['pinned_by'])
    if pins:
        # Never free or relocate a value an open range read may still see.
        gc['halted'] = True
        state['stats']['deferred_relocations'] += 1
        event = _event(state, 'gc_step', 'deferred_reader_pin', blob=bid,
                       pins=pins, used=_used(state))
        return {'kind': 'deferred_reader_pin', 'blob': bid, 'pins': pins, 'event': event}

    blob['queued'] = False
    key = _live_entry(state, bid)
    if key is None:
        state['gc']['pending_free'].append(bid)
        released = _release_ready(state)
        gc['cursor'] += 1
        state['stats']['garbage_freed'] += 1
        event = _event(state, 'gc_step', 'garbage_reclaimed', blob=bid,
                       released=[x['id'] for x in released], used=_used(state))
        return {'kind': 'garbage_reclaimed', 'blob': bid, 'released': released, 'event': event}

    capacity = state['params']['value_capacity']
    if _used(state) + blob['size'] > capacity:
        # The copy would exceed the bound.  Stop here and leave the readable
        # value in place; a later retry can continue from the same cursor.
        blob['queued'] = True
        gc['halted'] = True
        state['stats']['refused_relocations'] += 1
        event = _event(state, 'gc_step', 'relocation_refused_capacity', key=key,
                       blob=bid, size=blob['size'], used=_used(state),
                       capacity=capacity)
        return {'kind': 'relocation_refused_capacity', 'key': key, 'blob': bid,
                'size': blob['size'], 'used': _used(state), 'capacity': capacity,
                'event': event}

    copy = _alloc_blob(state, blob['value'], blob['size'], kind='copy',
                       copy_of=bid, queue=False)
    gc['journal'] = {'old': bid, 'new': copy['id'], 'size': blob['size'], 'phase': 'copied'}
    state['stats']['copies_allocated'] += 1
    _event(state, 'gc_step', 'relocation_copied', key=key, old=bid, new=copy['id'],
           size=blob['size'], used=_used(state), capacity=capacity,
           copies_allocated=state['stats']['copies_allocated'])
    if pause_after_copy:
        gc['halted'] = True
        return {'kind': 'relocation_copied', 'key': key, 'old': bid, 'new': copy['id'],
                'size': blob['size'], 'paused': True,
                'copies_allocated': state['stats']['copies_allocated'],
                'used': _used(state)}

    result = _advance_journal(state, pause_after_copy=False)
    return {'kind': 'relocation_done', 'key': key, 'old': bid, 'new': copy['id'],
            'size': blob['size'], **(result or {})}


def resume(state, pause_after_copy=False):
    state['gc']['halted'] = False
    if state['gc']['journal'] is not None:
        result = _advance_journal(state, pause_after_copy)
        state['gc']['halted'] = bool(pause_after_copy) or bool(result and result.get('deferred_pins'))
        return {'kind': 'journal_resumed', **(result or {})}
    return gc_step(state, pause_after_copy=pause_after_copy)


# ---------------------------------------------------------------------------
# verification and creation


def occupancy(state):
    used = _used(state)
    capacity = state['params']['value_capacity']
    copy_units = sum(b['size'] for b in state['blobs'].values() if b['kind'] == 'copy')
    garbage_units = 0
    retired_units = 0
    pending = set(state['gc']['pending_free'])
    for bid, blob in state['blobs'].items():
        if _live_entry(state, bid) is not None:
            continue
        if bid in state['redirects'] or bid in pending:
            retired_units += blob['size']
        else:
            garbage_units += blob['size']
    inline_units = sum(len(e.get('inline') or '') for e in state['keys'].values()
                       if not e['deleted'])
    return {
        'capacity': capacity,
        'used': used,
        'free': capacity - used,
        'inline_units': inline_units,
        'copy_units': copy_units,
        'copy_count': sum(1 for b in state['blobs'].values() if b['kind'] == 'copy'),
        'garbage_units': garbage_units,
        'retired_units': retired_units,
        'blob_count': len(state['blobs']),
        'pinned_blobs': sum(1 for b in state['blobs'].values() if b['pinned_by']),
    }


def verify_state(state):
    checks = []

    def check(name, ok, detail):
        checks.append({'name': name, 'ok': bool(ok), 'detail': detail})

    used = _used(state)
    check('占用等于值空间实际大小', used == sum(b['size'] for b in state['blobs'].values()),
          f'used={used}')
    capacity = state['params']['value_capacity']
    check('不超过值空间上限', used <= capacity, f'{used}/{capacity}')

    missing_refs = [k for k, e in state['keys'].items()
                    if not e['deleted'] and e.get('ref') and e['ref'] not in state['blobs']]
    check('键表引用均可解析', not missing_refs, missing_refs or 'all refs resolve')

    missing_pins = []
    for reader in state['readers'].values():
        for bid in reader['pins']:
            if bid not in state['blobs']:
                missing_pins.append((reader['id'], bid))
    check('范围读持有的旧地址仍存在', not missing_pins, missing_pins or 'pins valid')

    oracle_bad = []
    for key in sorted(set(state['keys']) | set(state['oracle'])):
        try:
            actual = _entry_value(state, state['keys'].get(key))
        except DrillRuleError:
            actual = '<dangling>'
        if actual != state['oracle'].get(key):
            oracle_bad.append(key)
    check('键表读值等于最后写入记录', not oracle_bad, oracle_bad or 'all keys match')

    redirect_bad = [bid for bid in state['redirects'] if bid not in state['blobs']]
    check('重定向只在原址仍被持有时保留', not redirect_bad, redirect_bad or 'redirects valid')

    queue = state['gc']['queue']
    future = set(queue[state['gc']['cursor']:])
    journal = state['gc']['journal']
    in_journal = {journal['old'], journal['new']} if journal else set()
    leaked = []
    for bid, blob in state['blobs'].items():
        if blob['pinned_by'] or _live_entry(state, bid) is not None:
            continue
        if bid in state['redirects'] or bid in state['gc']['pending_free']:
            continue
        if bid in future or bid in in_journal:
            continue
        leaked.append(bid)
    check('未引用空间都有释放记录', not leaked, leaked or 'no unaccounted garbage')

    if journal:
        check('搬迁日志的新地址仍存在', journal['new'] in state['blobs'], journal)

    return {
        'ok': all(c['ok'] for c in checks),
        'checks': checks,
        'occupancy': occupancy(state),
        'counters': state['stats'],
    }


def new_state(entries, params):
    threshold = params['value_threshold']
    capacity = params['value_capacity']
    if not 1 <= threshold <= MAX_VALUE:
        raise DrillRuleError(f'大值阈值必须在1到{MAX_VALUE}之间')
    if not 8 <= capacity <= 100000:
        raise DrillRuleError('值空间上限必须在8到100000单位之间')
    state = {
        'version': 1,
        'params': {'value_threshold': threshold, 'value_capacity': capacity},
        'seq': 0,
        'next_blob': 1,
        'next_reader': 1,
        'keys': {},
        'blobs': {},
        'redirects': {},
        'readers': {},
        'read_history': [],
        'oracle': {},
        'gc': {
            'queue': [],
            'cursor': 0,
            'journal': None,
            'halted': False,
            'pending_free': [],
            'last_event': None,
        },
        'stats': {
            'writes': 0, 'deletes': 0, 'inline_writes': 0, 'external_writes': 0,
            'range_reads': 0, 'finished_reads': 0, 'gc_steps': 0,
            'copies_allocated': 0, 'relocations_completed': 0,
            'relocations_aborted': 0, 'garbage_freed': 0,
            'refused_relocations': 0, 'deferred_relocations': 0,
            'reclaimed_units': 0,
        },
        'events': [],
        'event_seq': 0,
    }
    for entry in entries:
        if entry.get('op') == 'put':
            put(state, entry['key'], entry.get('value') or '')
        elif entry.get('op') == 'delete':
            delete(state, entry['key'])
        else:
            raise DrillRuleError('初值批次包含不支持的写操作')
    return state


def run_action(state, action):
    name = action.get('action')
    if name == 'put':
        return put(state, action.get('key'), action.get('value'))
    if name == 'delete':
        return delete(state, action.get('key'))
    if name == 'range_read':
        return begin_range(state, action.get('start'), action.get('end'),
                           hold=action.get('hold', True))
    if name == 'finish_read':
        return finish_read(state, action.get('read_id'))
    if name == 'gc_step':
        return gc_step(state, pause_after_copy=bool(action.get('pause_after_copy', False)))
    if name == 'resume':
        return resume(state, pause_after_copy=bool(action.get('pause_after_copy', False)))
    if name == 'verify':
        result = verify_state(state)
        _event(state, 'verify', 'verified', ok=result['ok'],
               used=result['occupancy']['used'], capacity=result['occupancy']['capacity'])
        return result
    raise DrillRuleError('不支持的值空间操作')


def serialize(state):
    keys = []
    for key in sorted(state['keys']):
        entry = state['keys'][key]
        item = {
            'key': key,
            'seq': entry['seq'],
            'deleted': entry['deleted'],
            'storage': 'tombstone' if entry['deleted'] else
                       ('blob' if entry.get('ref') else 'inline'),
            'ref': entry.get('ref'),
            'inline': entry.get('inline'),
            'size': 0 if entry['deleted'] else len(_entry_value(state, entry) or ''),
        }
        if entry.get('ref') and entry['ref'] in state['blobs']:
            item['preview'] = _preview(state['blobs'][entry['ref']]['value'])
        keys.append(item)

    live_refs = {}
    for key, entry in state['keys'].items():
        if not entry['deleted'] and entry.get('ref'):
            live_refs[entry['ref']] = key
    blobs = []
    pending = set(state['gc']['pending_free'])
    for bid in sorted(state['blobs'], key=lambda x: int(x[1:])):
        blob = state['blobs'][bid]
        if bid in live_refs:
            status = 'live'
        elif bid in state['redirects'] or bid in pending:
            status = 'retired'
        else:
            status = 'garbage'
        blobs.append({
            'id': bid,
            'size': blob['size'],
            'kind': blob['kind'],
            'copy_of': blob.get('copy_of'),
            'status': status,
            'live_key': live_refs.get(bid),
            'redirect_to': state['redirects'].get(bid),
            'pinned_by': list(blob['pinned_by']),
            'queued': blob['queued'],
            'preview': _preview(blob['value']),
        })

    readers = [{
        'read_id': r['id'],
        'start': r['start'],
        'end': r['end'],
        'pins': list(r['pins']),
        'keys': len(r['snapshot']),
        'snapshot': r['snapshot'],
        'started_seq': r['started_seq'],
    } for r in state['readers'].values()]

    gc = state['gc']
    return {
        'params': state['params'],
        'keys': keys,
        'blobs': blobs,
        'readers': readers,
        'read_history': list(reversed(state['read_history'])),
        'occupancy': occupancy(state),
        'stats': state['stats'],
        'gc': {
            'cursor': gc['cursor'],
            'queue_length': len(gc['queue']),
            'queued_remaining': max(0, len(gc['queue']) - gc['cursor']),
            'journal': gc['journal'],
            'halted': gc['halted'],
            'pending_free': list(gc['pending_free']),
            'last_event': gc['last_event'],
        },
        'events': state['events'][-60:],
        'verify': verify_state(state),
    }
