"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.

Copyright (C) 2023 Michael Hall <https://github.com/mikeshardmind>
"""

from __future__ import annotations

import asyncio
from itertools import chain
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from uuid import uuid4
from warnings import warn

import apsw
import arrow
import attrs
import msgpack  # type: ignore
from discord.ext import commands

__all__ = ["ScheduledDispatch", "Scheduler"]

SQLROW_TYPE = tuple[str, str, str, str, int | None, int | None, bytes | None]
DATE_FMT = r"%Y-%m-%d %H:%M"


INITIALIZATION_STATEMENTS = """
PRAGMA journal_mode = wal;
PRAGMA synchronous = NORMAL;
CREATE TABLE IF NOT EXISTS scheduled_dispatches (
    task_id TEXT PRIMARY KEY NOT NULL,
    dispatch_name TEXT NOT NULL,
    dispatch_time TEXT NOT NULL,
    dispatch_zone TEXT NOT NULL,
    associated_guild INTEGER,
    associated_user INTEGER,
    dispatch_extra BLOB
) STRICT, WITHOUT ROWID;
"""

ZONE_SELECTION_STATEMENT = """
SELECT DISTINCT dispatch_zone FROM scheduled_dispatches;
"""

UNSCHEDULE_BY_UUID_STATEMENT = """
DELETE FROM scheduled_dispatches WHERE task_id = ?;
"""

UNSCHEDULE_ALL_BY_GUILD_STATEMENT = """
DELETE FROM scheduled_dispatches
WHERE associated_guild IS NOT NULL AND associated_guild = ?;
"""

UNSCHEDULE_ALL_BY_USER_STATEMENT = """"
DELETE FROM scheduled_dispatches
WHERE associated_user IS NOT NULL AND associated_user = ?;
"""

UNSCHEDULE_ALL_BY_MEMBER_STATEMENT = """
DELETE FROM scheduled_dispatches
WHERE
    associated_guild IS NOT NULL
    AND associated_user IS NOT NULL
    AND associated_guild = ?
    AND associated_user = ?
;
"""

UNSCHEDULE_ALL_BY_DISPATCH_NAME_STATEMENT = """
DELETE FROM scheduled_dispatches WHERE dispatch_name = ?;
"""

UNSCHEDULE_ALL_BY_NAME_AND_USER_STATEMENT = """
DELETE FROM scheduled_dispatches
WHERE 
    dispatch_name = ?
    AND associated_user IS NOT NULL
    AND associated_user = ?;
"""

UNSCHEDULE_ALL_BY_NAME_AND_GUILD_STATEMENT = """
DELETE FROM scheduled_dispatches
WHERE 
    dispatch_name = ?
    AND associated_guild IS NOT NULL
    AND associated_guild = ?;
"""

UNSCHEDULE_ALL_BY_NAME_AND_MEMBER_STATEMENT = """
DELETE FROM scheduled_dispatches
WHERE
    dispatch_name = ?
    AND associated_guild IS NOT NULL
    AND associated_user IS NOT NULL
    AND associated_guild = ?
    AND associated_user = ?
;
"""

SELECT_ALL_BY_NAME_STATEMENT = """
SELECT * FROM scheduled_dispatches WHERE dispatch_name = ?;
"""

SELECT_ALL_BY_NAME_AND_GUILD_STATEMET = """
SELECT * FROM scheduled_dispatches
WHERE 
    dispatch_name = ?
    AND associated_guild IS NOT NULL
    AND associated_guild = ?;
"""

SELECT_ALL_BY_NAME_AND_USER_STATEMENT = """
SELECT * FROM scheduled_dispatches
WHERE 
    dispatch_name = ?
    AND associated_user IS NOT NULL
    AND associated_user = ?;
"""

SELECT_ALL_BY_NAME_AND_MEMBER_STATEMENT = """
SELECT * FROM scheduled_dispatches
WHERE
    dispatch_name = ?
    AND associated_guild IS NOT NULL
    AND associated_user IS NOT NULL
    AND associated_guild = ?
    AND associated_user = ?
;
"""

INSERT_SCHEDULE_STATEMENT = """
INSERT INTO scheduled_dispatches
(task_id. dispatch_name, dispatch_time, dispatch_zone, associated_guild, associated_user, dispatch_extra)
VALUES (?, ?, ?, ?, ?, ?, ?);
"""

DELETE_RETURNING_UPCOMING_IN_ZONE_STATEMENT = """
DELETE FROM scheduled_dispatches
WHERE dispatch_time < ? AND dispatch_zone = ?
RETURNING *;
"""


@attrs.define(frozen=True)
class ScheduledDispatch:
    task_id: str
    dispatch_name: str
    dispatch_time: str
    dispatch_zone: str
    associated_guild: int | None
    associated_user: int | None
    dispatch_extra: bytes | None

    @classmethod
    def from_sqlite_row(cls: type[Self], row: SQLROW_TYPE) -> Self:
        tid, name, time, zone, guild, user, extra_bytes = row
        unpacked: Any = msgpack.unpackb(extra_bytes, use_list=False, strict_map_key=False)  # type: ignore
        return cls(tid, name, time, zone, guild, user, unpacked)

    @classmethod
    def from_exposed_api(
        cls: type[Self],
        *,
        name: str,
        time: str,
        zone: str,
        guild: int | None,
        user: int | None,
        extra: Any | None,
    ) -> Self:
        packed: bytes | None = None
        if extra is not None:
            f = msgpack.packb(extra, use_list=False, strict_map_key=False)  # type: ignore
            assert isinstance(f, bytes)
            packed = f
        return cls(uuid4().hex, name, time, zone, guild, user, packed)

    def to_sqlite_row(self: Self) -> SQLROW_TYPE:
        return (
            self.task_id,
            self.dispatch_name,
            self.dispatch_time,
            self.dispatch_zone,
            self.associated_guild,
            self.associated_user,
            self.dispatch_extra,
        )

    def get_arrow_time(self: Self) -> arrow.Arrow:
        return arrow.Arrow.strptime(self.dispatch_time, DATE_FMT, self.dispatch_zone)

    def unpack_extra(self: Self) -> Any | None:
        if self.dispatch_extra:
            return msgpack.unpackb(self.dispatch_extra, use_list=False, strict_map_key=False)  # type: ignore
        return None


def _setup_db(conn: apsw.Connection) -> set[str]:
    with conn:  # type: ignore # apsw.Connection *does* implement everything needed to be a contextmanager, upstream PR?
        cursor = conn.cursor()
        cursor.execute(INITIALIZATION_STATEMENTS)
        cursor.execute(ZONE_SELECTION_STATEMENT)
        return set(chain.from_iterable(cursor))


def _get_scheduled(conn: apsw.Connection, zones: set[str]) -> list[ScheduledDispatch]:
    ret: list[ScheduledDispatch] = []
    if not zones:
        return ret

    now = arrow.utcnow()
    with conn:  # type: ignore # apsw.Connection *does* implement everything needed to be a contextmanager, upstream PR?
        cursor = conn.cursor()
        for zone in zones:
            local_time = now.to(zone).strftime(DATE_FMT)
            cursor.execute(DELETE_RETURNING_UPCOMING_IN_ZONE_STATEMENT, (local_time, zone))
            ret.extend(map(ScheduledDispatch.from_sqlite_row, cursor))

    return ret


def _schedule(
    conn: apsw.Connection,
    *,
    dispatch_name: str,
    dispatch_time: str,
    dispatch_zone: str,
    guild_id: int | None,
    user_id: int | None,
    dispatch_extra: Any | None,
) -> str:
    # do this here, so if it fails, it fails at scheduling
    _time = arrow.Arrow.strptime(dispatch_time, DATE_FMT, dispatch_zone)
    obj = ScheduledDispatch.from_exposed_api(
        name=dispatch_name,
        time=dispatch_time,
        zone=dispatch_zone,
        guild=guild_id,
        user=user_id,
        extra=dispatch_extra,
    )

    with conn:  # type: ignore # apsw.Connection *does* implement everything needed to be a contextmanager, upstream PR?
        cursor = conn.cursor()
        cursor.execute(INSERT_SCHEDULE_STATEMENT, obj.to_sqlite_row())

    return obj.task_id


def _query(conn: apsw.Connection, query_str: str, params: tuple[int | str, ...]) -> list[ScheduledDispatch]:
    cursor = conn.cursor()
    return [ScheduledDispatch.from_sqlite_row(row) for row in cursor.execute(query_str, params)]


def _drop(conn: apsw.Connection, query_str: str, params: tuple[int | str, ...]) -> None:
    with conn:  # type: ignore # apsw.Connection *does* implement everything needed to be a contextmanager, upstream PR?
        cursor = conn.cursor()
        cursor.execute(query_str, params)


class Scheduler:
    def __init__(self: Self, db_path: Path, granularity: int):
        if granularity < 1:
            msg = "Granularity must be a positive iteger number of minutes"
            raise ValueError(msg)
        asyncio.get_running_loop()
        self.granularity = granularity
        resolved_path_as_str = str(db_path.resolve(strict=True))
        self._connection = apsw.Connection(resolved_path_as_str)
        self._zones: set[str] = set()  # We don't re-narrow this anywhere currently, only expand it.
        self._queue: asyncio.Queue[ScheduledDispatch] = asyncio.Queue()
        self._last_time: arrow.Arrow
        self._ready = False
        self._closing = False
        self._lock = asyncio.Lock()
        self._loop_task: asyncio.Task[None] | None = None
        self._discord_task: asyncio.Task[None] | None = None

    def stop(self: Self) -> None:
        if self._loop_task is None:
            msg = "Contextmanager, use it"
            raise RuntimeError(msg)
        self._loop_task.cancel()
        if self._discord_task:
            self._discord_task.cancel()

    async def _loop(self: Self) -> None:
        # not currently modifiable once running
        gran = self.granularity
        while (not self._closing) and await asyncio.sleep(gran, self._ready):
            # Lock needed to ensure that once the db is dropping rows
            # that a graceful shutdown doesn't drain the queue until entries are in it.
            async with self._lock:
                # check on both ends of the await that we aren't closing
                if self._closing:
                    return
                scheduled = await asyncio.to_thread(_get_scheduled, self._connection, self._zones)
                for s in scheduled:
                    await self._queue.put(s)

    async def __aexit__(
        self: Self,
        exc_type: type[Exception],
        exc_value: Exception,
        traceback: TracebackType,
    ):
        if not self._closing:
            msg = "Exiting without use of stop_gracefully may cause loss of tasks"
            warn(msg, stacklevel=2)
        self.stop()

    async def get_next(self: Self) -> ScheduledDispatch:
        """
        gets the next scheduled event, waiting if neccessary.
        """
        try:
            return await self._queue.get()
        finally:
            self._queue.task_done()

    async def stop_gracefully(self: Self) -> None:
        """Notify the internal scheduling loop to stop scheduling and wait for the internal queue to be empty"""
        self._closing = True
        # don't remove lock, see note in _loop
        async with self._lock:
            await self._queue.join()

    async def __aenter__(self: Self) -> Self:
        self._last_time = arrow.utcnow()
        self._zones = await asyncio.to_thread(_setup_db, self._connection)
        self._ready = True
        self._loop_task = asyncio.create_task(self._loop())
        self._loop_task.add_done_callback(lambda f: f.exception())
        return self

    async def schedule_event(
        self: Self,
        *,
        dispatch_name: str,
        dispatch_time: str,
        dispatch_zone: str,
        guild_id: int | None = None,
        user_id: int | None = None,
        dispatch_extra: Any | None = None,
    ) -> str:
        """
        Schedule something to be emitted later.

        Parameters
        ----------
        dispatch_name: str
            The event name to dispatch under.
            You may drop all events dispatching to the same name
            (such as when removing a feature built ontop of this)
        dispatch_time: str
            A time string matching the format "%Y-%m-%d %H:%M" (eg. "2023-01-23 13:15")
        dispatch_zone: str
            The name of the zone for the event.
            - Use `UTC` for absolute things scheduled by machines for machines
            - Use the name of the zone (eg. US/Eastern) for things scheduled by
              humans for machines to do for humans later
        guild_id: int | None
            Optionally, an associated guild_id.
            This can be used with dispatch_name as a means of querying events
            or to drop all scheduled events for a guild.
        user_id: int | None
            Optionally, an associated user_id.
            This can be used with dispatch_name as a means of querying events
            or to drop all scheduled events for a user.
        dispatch_extra: Any | None
            Optionally, Extra data to attach to dispatch.
            This may be any object serializable by mspack with strict_map_key=False, use_list=False
            Lists will be converted to tuples

        Returns
        -------
        str
            A uuid for the task, used for unique cancelation.
        """
        self._zones.add(dispatch_zone)
        return await asyncio.to_thread(
            _schedule,
            self._connection,
            dispatch_name=dispatch_name,
            dispatch_time=dispatch_time,
            dispatch_zone=dispatch_zone,
            guild_id=guild_id,
            user_id=user_id,
            dispatch_extra=dispatch_extra,
        )

    async def unschedule_uuid(self: Self, uuid: str) -> None:
        """
        Unschedule something by uuid.
        This may miss things which should run within the next interval as defined by `granularity`
        Non-existent uuids are silently handled.
        """
        await asyncio.to_thread(_drop, self._connection, UNSCHEDULE_BY_UUID_STATEMENT, (uuid,))

    async def drop_user_schedule(self: Self, user_id: int) -> None:
        """
        Drop all scheduled events for a user (by user_id)

        Intended use case:
            removing everything associated to a user who asks for data removal, doesn't exist anymore, or is blacklisted
        """
        await asyncio.to_thread(_drop, self._connection, UNSCHEDULE_ALL_BY_USER_STATEMENT, (user_id,))

    async def drop_event_for_user(self: Self, dispatch_name: str, user_id: int) -> None:
        """
        Drop scheduled events dispatched to `dispatch_name` for user (by user_id)

        Intended use case example:
            A reminder system allowing a user to unschedule all reminders
            without effecting how other extensions might use this.
        """
        await asyncio.to_thread(
            _drop,
            self._connection,
            UNSCHEDULE_ALL_BY_NAME_AND_USER_STATEMENT,
            (dispatch_name, user_id),
        )

    async def drop_guild_schedule(self: Self, guild_id: int) -> None:
        """
        Drop all scheduled events for a guild (by guild_id)

        Intended use case:
            clearing sccheduled events for a guild when leaving it.
        """
        await asyncio.to_thread(_drop, self._connection, UNSCHEDULE_ALL_BY_GUILD_STATEMENT, (guild_id,))

    async def drop_event_for_guild(self: Self, dispatch_name: str, guild_id: int) -> None:
        """
        Drop scheduled events dispatched to `dispatch_name` for guild (by guild_id)

        Intended use case example:
            An admin command allowing clearing all scheduled messages for a guild.
        """
        await asyncio.to_thread(
            _drop,
            self._connection,
            UNSCHEDULE_ALL_BY_NAME_AND_GUILD_STATEMENT,
            (dispatch_name, guild_id),
        )

    async def drop_member_schedule(self: Self, guild_id: int, user_id: int) -> None:
        """
        Drop all scheduled events for a guild (by guild_id, user_id)

        Intended use case:
            clearing sccheduled events for a member that leaves a guild
        """
        await asyncio.to_thread(
            _drop,
            self._connection,
            UNSCHEDULE_ALL_BY_MEMBER_STATEMENT,
            (guild_id, user_id),
        )

    async def drop_event_for_member(self: Self, dispatch_name: str, guild_id: int, user_id: int) -> None:
        """
        Drop scheduled events dispatched to `dispatch_name` for member (by guild_id, user_id)

        Intended use case example:
            see user example, but in a guild
        """
        await asyncio.to_thread(
            _drop,
            self._connection,
            UNSCHEDULE_ALL_BY_NAME_AND_MEMBER_STATEMENT,
            (dispatch_name, guild_id, user_id),
        )

    async def list_event_schedule_for_user(self: Self, dispatch_name: str, user_id: int) -> list[ScheduledDispatch]:
        """
        list the events of a specified name scheduled for a user (by user_id)
        """
        return await asyncio.to_thread(
            _query,
            self._connection,
            SELECT_ALL_BY_NAME_AND_USER_STATEMENT,
            (dispatch_name, user_id),
        )

    async def list_event_schedule_for_member(
        self: Self,
        dispatch_name: str,
        guild_id: int,
        user_id: int,
    ) -> list[ScheduledDispatch]:
        """
        list the events of a specified name scheduled for a member (by guild_id, user_id)
        """
        return await asyncio.to_thread(
            _query,
            self._connection,
            SELECT_ALL_BY_NAME_AND_MEMBER_STATEMENT,
            (dispatch_name, guild_id, user_id),
        )

    async def list_event_schedule_for_guild(self: Self, dispatch_name: str, guild_id: int) -> list[ScheduledDispatch]:
        """
        list the events of a specified name scheduled for a guild (by guild_id)
        """
        return await asyncio.to_thread(
            _query,
            self._connection,
            SELECT_ALL_BY_NAME_AND_USER_STATEMENT,
            (dispatch_name, guild_id),
        )

    @staticmethod
    def time_str_from_params(year: int, month: int, day: int, hour: int, minute: int) -> str:
        """
        A quick helper for people working with other time representations
        (if you have a datetime object, just use strftime with "%Y-%m-%d %H:%M")
        """
        return arrow.Arrow(year, month, day, hour, minute).strftime(DATE_FMT)

    async def _bot_dispatch_loop(self: Self, bot: commands.Bot, wait_until_ready: bool) -> None:
        if not self._ready:
            msg = "context manager, use it"
            raise RuntimeError(msg)

        if wait_until_ready:
            await bot.wait_until_ready()

        while scheduled := await self._queue.get():
            bot.dispatch(f"sinbad_scheduler_{scheduled.dispatch_name}", scheduled)

    def start_dispatch_to_bot(self: Self, bot: commands.Bot, *, wait_until_ready: bool = True) -> None:
        """
        Starts dispatching events to the bot.

        Events will dispatch under a name with the following format:

        sinbad_scheduler_{dispatch_name}

        where dispatch_name is set when submitting events to schedule.
        This is done to avoid potential conflicts with existing or future event names,
        as well as anyone else building a scheduler on top of bot.dispatch
        (hence author name inclusion) and someone deciding to use both.

        Listeners get a single object as their argument, `ScheduledDispatch`

        to listen for an event you submit with `reminder` as the name

        @commands.Cog.listener("on_sinbad_scheduler_reminder")
        async def some_listener(self, scheduled_object: ScheduledDispatch):
            ...

        Events will not start being sent until the bot is considered ready if `wait_until_ready` is True
        """
        if not self._ready:
            msg = "context manager, use it"
            raise RuntimeError(msg)

        self._discord_task = asyncio.create_task(self._bot_dispatch_loop(bot, wait_until_ready))
        self._discord_task.add_done_callback(lambda f: f.exception())
