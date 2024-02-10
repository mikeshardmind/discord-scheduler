"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.

Copyright (C) 2023 Michael Hall <https://github.com/mikeshardmind>
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

from scheduler import DiscordBotScheduler as Scheduler
from scheduler import ScheduledDispatch


class BotLikeThing:
    def __init__(self) -> None:
        self.recv: list[ScheduledDispatch] = []

    def dispatch(self: Self, event_name: str, /, *args: object, **kwargs: object) -> None:
        self.recv.append(args[0])  # type: ignore

    async def wait_until_ready(self: Self) -> None:
        return


async def amain(path: Path) -> list[str]:
    bot = BotLikeThing()
    async with Scheduler(path, granularity=1) as sched:
        sched.start_dispatch_to_bot(bot)

        when = (datetime.now(tz=UTC) + timedelta(seconds=10)).strftime(r"%Y-%m-%d %H:%M")

        uuid1 = await sched.schedule_event(
            dispatch_name="uuid1",
            dispatch_time=when,
            dispatch_zone="UTC",
            dispatch_extra={(1, 2, 3): None},
        )

        uuid2 = await sched.schedule_event(
            dispatch_name="uuid2",
            dispatch_time=when,
            dispatch_zone="UTC",
            dispatch_extra={(1, 2, 3): None},
        )

        uuid3 = await sched.schedule_event(
            dispatch_name="uuid3",
            dispatch_time=when,
            dispatch_zone="UTC",
            dispatch_extra={(1, 2, 3): None},
            guild_id=1,
        )

        uuid4 = await sched.schedule_event(
            dispatch_name="uuid4",
            dispatch_time=when,
            dispatch_zone="UTC",
            dispatch_extra={(1, 2, 3): None},
            guild_id=2,
            user_id=4,
        )

        uuid5 = await sched.schedule_event(
            dispatch_name="uuid5",
            dispatch_time=when,
            dispatch_zone="UTC",
            dispatch_extra={(1, 2, 3): None},
            user_id=3,
        )

        uuid6 = await sched.schedule_event(
            dispatch_name="uuid6",
            dispatch_time=when,
            dispatch_zone="UTC",
            dispatch_extra={(1, 2, 3): None},
            user_id=3,
            guild_id=2,
        )

        uuid7 = await sched.schedule_event(
            dispatch_name="uuid7",
            dispatch_time=when,
            dispatch_zone="UTC",
            dispatch_extra={(1, 2, 3): None},
            guild_id=7,
            user_id=3,
        )

        # expected_uuids: uuid1, uuid5
        # canceled_uuids:
        #     uuid2, # directly
        #     uuid3, # guild 1
        #     uuid4, # user 4
        #     uuid6, # member guild 2, user 3
        #     uuid7, # event name on guild 7

        await sched.unschedule_uuid(uuid2)
        await sched.drop_user_schedule(4)
        await sched.drop_guild_schedule(1)
        await sched.drop_member_schedule(2, 3)
        await sched.drop_event_for_guild("uuid7", 7)

        await asyncio.sleep(90)
        await sched.stop_gracefully()

    failures: list[str] = []

    # expecting uuid 1 and 5

    expected = {uuid1, uuid5}
    unexpected = {uuid2, uuid3, uuid4, uuid6, uuid7}

    for payload in bot.recv:
        expected.discard(payload.task_id)
        if payload.task_id in unexpected:
            failures.append(f"Unexpected task named {payload.dispatch_name}")
        else:
            try:
                extra = payload.unpack_extra()
            except Exception as exc:  # noqa: BLE001
                failures.append(f"Failed to unpack extra got {exc=}")
            else:
                if extra != {(1, 2, 3): None}:
                    failures.append(f"Unpacking extra did not result in expectations, {extra=}")

    if expected:
        failures.append(f"expected tasks did not dispatch {expected=}")

    return failures


def main() -> None:
    path = Path.cwd() / uuid.uuid4().hex / "db.db"
    try:
        failures = asyncio.run(amain(path))
    finally:
        parent = path.parent
        path.unlink()
        parent.rmdir()

    if failures:
        print(*failures, file=sys.stderr, sep="\n\n")  # noqa: T201
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
