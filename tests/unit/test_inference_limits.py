import asyncio

import pytest

from aistudio_api.application.generation_errors import GenerationFailure
from aistudio_api.application.inference_limits import InferenceLimiter


async def test_inference_capacity_and_waiting_queue_are_bounded_and_release_on_cancel():
    limiter = InferenceLimiter(concurrency=2, max_waiting=1, wait_seconds=1)
    entered = []
    async def request(number):
        async with limiter.slot():
            entered.append(number)
            await asyncio.Event().wait()
    tasks = [asyncio.create_task(request(i)) for i in range(3)]
    try:
        await asyncio.sleep(0)
        assert limiter.active == 2 and limiter.waiting == 1
        with pytest.raises(GenerationFailure) as error:
            async with limiter.slot():
                pytest.fail("queue must reject excess requests")
        assert error.value.description.status == 503 and error.value.description.retry_after_seconds == 2
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        await asyncio.sleep(0)
        assert entered == [0, 1, 2] and limiter.active == 2
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert limiter.active == 0 and limiter.waiting == 0


async def test_queued_timeout_does_not_leak_slot_or_waiter():
    limiter = InferenceLimiter(concurrency=1, max_waiting=1, wait_seconds=0.01)
    async with limiter.slot():
        with pytest.raises(GenerationFailure) as error:
            async with limiter.slot():
                pytest.fail("queue must time out")
        assert error.value.description.status == 503
        assert limiter.waiting == 0 and limiter.active == 1
    async with limiter.slot():
        assert limiter.active == 1
    assert limiter.active == 0
