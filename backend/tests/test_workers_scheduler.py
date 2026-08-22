import asyncio

import pytest

from app import main as app_main


async def test_the_scheduler_ticks_both_workers_and_stops_cleanly_on_cancel(monkeypatch):
    holds_calls = []
    overdue_calls = []

    async def fake_holds_run_once():
        holds_calls.append(1)
        return 0

    async def fake_overdue_run_once():
        overdue_calls.append(1)
        return {"reminded": 0, "expired": 0, "grace": 0, "overdue": 0, "to_remove": 0}

    monkeypatch.setattr(app_main.holds, "run_once", fake_holds_run_once)
    monkeypatch.setattr(app_main.overdue, "run_once", fake_overdue_run_once)

    task = asyncio.create_task(app_main._run_workers_forever(0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(holds_calls) >= 1
    assert len(overdue_calls) >= 1


async def test_a_persistently_failing_holds_tick_does_not_starve_overdue(monkeypatch):
    holds_calls = []
    overdue_calls = []

    async def failing_holds_run_once():
        holds_calls.append(1)
        raise RuntimeError("boom")

    async def fake_overdue_run_once():
        overdue_calls.append(1)
        return {"reminded": 0, "expired": 0, "grace": 0, "overdue": 0, "to_remove": 0}

    monkeypatch.setattr(app_main.holds, "run_once", failing_holds_run_once)
    monkeypatch.setattr(app_main.overdue, "run_once", fake_overdue_run_once)

    task = asyncio.create_task(app_main._run_workers_forever(0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # A tick that raised did not end the loop: it was called more than once...
    assert len(holds_calls) >= 2
    # ...and did not block its sibling either: overdue ran on every tick that
    # holds failed, not just the ones before the first failure.
    assert len(overdue_calls) >= 2


async def test_a_persistently_failing_overdue_tick_does_not_starve_holds(monkeypatch):
    holds_calls = []
    overdue_calls = []

    async def fake_holds_run_once():
        holds_calls.append(1)
        return 0

    async def failing_overdue_run_once():
        overdue_calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(app_main.holds, "run_once", fake_holds_run_once)
    monkeypatch.setattr(app_main.overdue, "run_once", failing_overdue_run_once)

    task = asyncio.create_task(app_main._run_workers_forever(0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(overdue_calls) >= 2
    assert len(holds_calls) >= 2
