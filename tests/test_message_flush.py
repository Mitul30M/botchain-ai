import asyncio
import logging

from app.api.v1.messages import _background_tasks, _fire_and_forget


async def _noop():
    await asyncio.sleep(0.01)


async def test_fire_and_forget_tracks_and_releases_task():
    _fire_and_forget(_noop())
    assert len(_background_tasks) == 1
    await asyncio.sleep(0.05)
    assert len(_background_tasks) == 0


async def test_fire_and_forget_surfaces_exception(caplog):
    async def _boom():
        raise RuntimeError("simulated db hiccup")

    with caplog.at_level(logging.ERROR, logger="app.api.v1.messages"):
        _fire_and_forget(_boom())
        await asyncio.sleep(0.05)

    assert len(_background_tasks) == 0
    assert any("background assistant flush failed" in r.message for r in caplog.records)