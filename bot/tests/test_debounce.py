import asyncio

from src.core.debounce import Accumulator


async def test_burst_within_window_fires_once_with_every_item():
    got = []

    async def cb(key, items):
        got.append((key, items))

    acc = Accumulator(cb, delay=0.05)
    for n in (1, 2, 3):
        acc.add("u", n)
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    assert got == [("u", [1, 2, 3])]


async def test_add_after_the_window_is_a_second_callback():
    got = []

    async def cb(key, items):
        got.append(items)

    acc = Accumulator(cb, delay=0.03)
    acc.add("u", 1)
    await asyncio.sleep(0.1)
    acc.add("u", 2)
    await asyncio.sleep(0.1)
    assert got == [[1], [2]]


async def test_single_item_still_goes_through():
    got = []

    async def cb(key, items):
        got.append(items)

    acc = Accumulator(cb, delay=0.02)
    acc.add("u", "only")
    await asyncio.sleep(0.08)
    assert got == [["only"]]


async def test_keys_accumulate_independently():
    got = {}

    async def cb(key, items):
        got[key] = items

    acc = Accumulator(cb, delay=0.03)
    acc.add("a", 1)
    acc.add("b", 2)
    await asyncio.sleep(0.1)
    assert got == {"a": [1], "b": [2]}
