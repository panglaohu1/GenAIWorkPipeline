"""Compute admission tests: fake workers only; never start local models."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from genai_pipeline.compute import LocalCompute, ComputeQueueTimeout
from genai_pipeline import services
from genai_pipeline.services import Service, ServiceManager, MemoryBudgetError, ServiceBusyError


async def test_fifo_and_request_cancellation_drain():
    scheduler = LocalCompute()
    entered, release = asyncio.Event(), asyncio.Event()
    order = []

    async def first():
        order.append('first')
        entered.set()
        await release.wait()
        order.append('first-finished')

    async def second():
        order.append('second')
        return 2

    a = asyncio.create_task(scheduler.run('first', first))
    await entered.wait()
    b = asyncio.create_task(scheduler.run('second', second))
    await asyncio.sleep(0)
    a.cancel()
    await asyncio.sleep(0)
    assert not a.done()
    assert scheduler.snapshot()['active']['operation'] == 'first'
    assert [q['operation'] for q in scheduler.snapshot()['queued']] == ['second']
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await a
    assert await b == 2
    assert order == ['first', 'first-finished', 'second']
    assert scheduler.snapshot()['active'] is None


async def test_cancel_or_timeout_waiter_never_runs():
    scheduler = LocalCompute()
    ready, release = asyncio.Event(), asyncio.Event()
    async def held():
        ready.set()
        await release.wait()
    async def unexpected():
        pytest.fail('queued operation must not execute')
    first = asyncio.create_task(scheduler.run('held', held))
    await ready.wait()
    waiting = asyncio.create_task(scheduler.run('cancelled', unexpected))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    with pytest.raises(ComputeQueueTimeout):
        await scheduler.run('timed-out', unexpected, queue_timeout=0)
    assert scheduler.snapshot()['queued'] == []
    release.set()
    await first


async def test_failure_releases_slot_and_events_keep_context():
    scheduler = LocalCompute()
    events = []
    async def broken():
        raise ValueError('test error')
    with scheduler.context('run-1', 'model', lambda *args: events.append(args)):
        with pytest.raises(ValueError):
            await scheduler.run('broken', broken)
    assert scheduler.snapshot()['active'] is None
    assert events[-1][2]['state'] == 'failed'
    assert events[-1][2]['run_id'] == 'run-1'
    assert events[-1][2]['stage'] == 'model'


def test_scheduler_supports_different_event_loops():
    scheduler = LocalCompute()
    state_lock = threading.Lock()
    state = {'active': 0, 'max': 0}
    barrier = threading.Barrier(2)
    def run():
        barrier.wait(timeout=5)
        async def job():
            with state_lock:
                state['active'] += 1
                state['max'] = max(state['max'], state['active'])
            await asyncio.sleep(.03)
            with state_lock:
                state['active'] -= 1
        asyncio.run(scheduler.run('thread', job))
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        for f in futures:
            f.result(timeout=5)
    assert state['max'] == 1


def manager_fixture(monkeypatch, budget=40):
    m = ServiceManager()
    main = Service('main', 'main', 1, 16, '/', 0)
    image = Service('image', 'image', 2, 12, '/', 2)
    incoming = Service('incoming', 'incoming', 3, 26, '/', 1)
    m.services = {s.name: s for s in (main,image,incoming)}
    alive = {'main','image'}
    stopped = []
    monkeypatch.setattr(m, '_alive', lambda s:s.name in alive)
    monkeypatch.setattr(m, 'stop', lambda n:(alive.remove(n),stopped.append(n)))
    old_get = services.config.get
    monkeypatch.setattr(services.config, 'get', lambda k,d=None: {'memory.gpu_budget_gb':budget+8,'memory.engine_reserve_gb':8}.get(k,old_get(k,d)))
    return m,incoming,stopped


def test_idle_models_evicted_to_fit_budget(monkeypatch):
    m, incoming, stopped = manager_fixture(monkeypatch)
    m._make_room(incoming)
    assert stopped == ['image','main']


def test_impossible_admission_does_not_evict_anything(monkeypatch):
    m,incoming,stopped = manager_fixture(monkeypatch)
    m._leases['main'] = 1
    with pytest.raises(MemoryBudgetError):
        m._make_room(incoming)
    assert stopped == []


async def test_lease_covers_startup_and_response(monkeypatch):
    m = ServiceManager()
    monkeypatch.setattr(services, 'compute', LocalCompute())
    def ensure(name, timeout):
        assert m._leases[name] == 1
        return True
    monkeypatch.setattr(m,'ensure',ensure)
    async def call():
        with pytest.raises(ServiceBusyError):
            m.stop('llm-main')
        return 42
    assert await m.run('llm-main',call) == 42
    assert m._leases['llm-main'] == 0
