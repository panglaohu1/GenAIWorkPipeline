"""Queue controls exercise real scheduler state without model or GPU calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from genai_pipeline import config, pipeline
from genai_pipeline.pipeline import Orchestrator
from genai_pipeline.store import Store


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setitem(config.load()['pipeline'], 'runs_dir', str(tmp_path/'runs'))
    store = Store(tmp_path/'db.sqlite')
    orch = Orchestrator(store, start_worker=False)
    task = store.create_run('排队测试', 'test', 'asset', {}, ['plan'])
    return store, orch, task


async def test_paused_queue_resumes_and_executes_once(queue, monkeypatch):
    store, orch, task = queue
    calls = []
    async def stage(ctx):
        calls.append(ctx.id)
    monkeypatch.setitem(pipeline.STAGE_FUNCS, 'plan', stage)
    orch.control_action(task, 'pause')
    assert orch._claim_next() is None
    orch.control_action(task, 'resume')
    assert store.get_run(task)['status'] == 'queued'
    run = orch._claim_next()
    assert run['id'] == task and orch._claim_next() is None
    await orch._execute(run)
    orch.current = None
    assert calls == [task] and store.get_run(task)['status'] == 'done'
    assert orch._claim_next() is None
    with pytest.raises(RuntimeError):
        orch.control_action(task, 'pause')
    assert store.get_run(task)['status'] == 'done'


def test_cancel_held_queue_and_retry(queue):
    store, orch, task = queue
    orch.control_action(task, 'pause')
    orch.control_action(task, 'cancel')
    assert store.get_run(task)['status'] == 'cancelled'
    assert orch._claim_next() is None
    with pytest.raises(RuntimeError):
        orch.control_action(task, 'resume')
    orch.retry(task)
    assert not orch.control(task).cancelled
    assert orch._claim_next()['id'] == task
    with pytest.raises(RuntimeError):
        orch.retry(task)


async def test_pause_after_claim_does_not_start_stage_and_cancel_is_acknowledged(queue, monkeypatch):
    store, orch, task = queue
    calls = []
    async def stage(ctx):
        calls.append(ctx.id)
    monkeypatch.setitem(pipeline.STAGE_FUNCS, 'plan', stage)
    run = orch._claim_next()
    orch.control_action(task, 'pause')
    execution = asyncio.create_task(orch._execute(run))
    await asyncio.sleep(0.02)
    assert calls == [] and store.get_run(task)['status'] == 'paused'
    orch.control_action(task, 'cancel')
    await asyncio.wait_for(execution, 1)
    assert calls == [] and store.get_run(task)['status'] == 'cancelled'


async def test_resume_approval_gate_stays_waiting(queue, monkeypatch):
    store, orch, task = queue
    store.update_run(task, options={'approval_gates':['concept']})
    async def stage(ctx):
        await ctx.wait_approval('concept')
    monkeypatch.setitem(pipeline.STAGE_FUNCS, 'plan', stage)
    run = orch._claim_next()
    execution = asyncio.create_task(orch._execute(run))
    await asyncio.sleep(0.02)
    assert store.get_run(task)['status'] == 'waiting'
    orch.control_action(task, 'pause')
    orch.control_action(task, 'resume')
    assert store.get_run(task)['status'] == 'waiting'
    orch.control(task).approvals['concept'] = {}
    await asyncio.wait_for(execution, 1)
    assert store.get_run(task)['status'] == 'done'


def test_claim_and_pause_race_has_one_owner(queue):
    store, orch, task = queue
    barrier = threading.Barrier(2)
    def claim():
        barrier.wait()
        return orch._claim_next()
    def pause():
        barrier.wait()
        orch.control_action(task, 'pause')
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(claim), pool.submit(pause)
        claimed = a.result(); b.result()
    assert store.get_run(task)['status'] == 'paused'
    orch.control_action(task, 'resume')
    assert store.get_run(task)['status'] == ('running' if claimed else 'queued')
    assert (orch.current == task) == bool(claimed)


def test_pause_waiting_for_terminal_write_cannot_revive_finished_task(queue):
    store, orch, task = queue
    c = orch.control(task)
    store.update_run(task, status='running')
    # Simulate the worker holding the control lock while it commits completion.
    entered = threading.Event()
    held = threading.Lock()
    class ObservableLock:
        def __enter__(self):
            entered.set()
            held.acquire()
        def __exit__(self, *args):
            held.release()
    c.lock = ObservableLock()
    held.acquire()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(orch.control_action, task, 'pause')
        try:
            assert entered.wait(1)
            store.update_run(task, status='done')
        finally:
            held.release()
        with pytest.raises(RuntimeError, match='可以暂停'):
            pending.result(timeout=1)
    assert store.get_run(task)['status'] == 'done'
    assert not c.paused


def test_retry_drops_previous_approval_but_invalid_retry_preserves_it(queue):
    store, orch, task = queue
    c = orch.control(task)
    c.approvals['concept'] = {'selections':{'hero':2}}
    c.requests.append({'type':'regenerate','asset':'hero'})
    with pytest.raises(RuntimeError):
        orch.retry(task, 'unknown')
    assert c.approvals and c.requests
    orch.retry(task, 'plan')
    assert c.approvals == {} and c.requests == []


@pytest.mark.parametrize('invalid',['{broken','[]'])
async def test_invalid_saved_state_fails_visibly_before_any_stage(queue,monkeypatch,invalid):
    from pathlib import Path
    store,orch,task=queue
    directory=Path(config.get('pipeline.runs_dir'))/task
    directory.mkdir(parents=True);(directory/'state.json').write_text(invalid)
    calls=[]
    async def stage(ctx):calls.append(ctx.id)
    monkeypatch.setitem(pipeline.STAGE_FUNCS,'plan',stage)
    await orch._execute(orch._claim_next())
    assert store.get_run(task)['status']=='failed'
    assert '任务输入检查失败' in store.get_run(task)['error']
    assert calls==[] and store.events(task)[-1]['kind']=='error'
    # Restoring the saved state makes a normal retry possible.
    (directory/'state.json').write_text('{}');orch.current=None
    orch.retry(task);await orch._execute(orch._claim_next())
    assert store.get_run(task)['status']=='done' and calls==[task]
