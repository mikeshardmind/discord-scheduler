"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.

Copyright (C) 2023 Michael Hall <https://github.com/mikeshardmind>
"""

from __future__ import annotations

import asyncio
import enum
import random
import time
from collections.abc import Callable
from datetime import timedelta
from itertools import chain, cycle
from pathlib import Path
from typing import Literal, Protocol, Self
from warnings import warn

import apsw
import apsw.ext
import arrow
import pytz
from msgspec import Struct
from msgspec.msgpack import decode as msgpack_decode
from msgspec.msgpack import encode as msgpack_encode


class _Internal(enum.Enum):
    NoValue = enum.auto()

    def __bool__(self):
        return False


#: Used as a Sentinel to mark a value absence seperately from user-provided None
#: You should not provide this value manually, but may recieve it where documented.
NoValue = _Internal.NoValue

#: T, or the library specific NoValue sentinel value.
type Maybe[T] = T | Literal[_Internal.NoValue]


class BotLike(Protocol):
    def dispatch(
        self: Self, event_name: str, /, *args: object, **kwargs: object
    ) -> None: ...

    async def wait_until_ready(self: Self) -> None: ...


def _uuid7gen() -> Callable[[], str]:
    """UUIDv7 has been accepted as part of rfc9562

    This is intended to be a compliant implementation, but I am not advertising it
    in public, exported APIs as such *yet*

    In particular, this is:
    UUIDv7 as described in rfc9562 section 5.7 utilizing the
    optional sub-millisecond timestamp fraction described in section 6.2 method 3
    """
    last_timestamp: int | None = None

    def uuid7() -> str:
        """This is unique identifer generator

        This was chosen to increase performance of indexing and
        to pick something likely to get specific database support
        for this to be a portably efficient choice should someone
        decide to have this be backed by something other than sqlite

        This should not be relied on as always generating valid UUIDs of
        any version or variant at this time. The current intent is that
        this is a UUIDv7 in str form, but this should not be relied
        on outside of this library and may be changed in the future for
        better performance within this library.
        """
        nonlocal last_timestamp
        nanoseconds = time.time_ns()
        timestamp_ms = nanoseconds // 1_000_000
        if last_timestamp is not None and timestamp_ms <= last_timestamp:
            timestamp_ms = last_timestamp + 1
        last_timestamp = timestamp_ms
        uuid_int = (timestamp_ms & 0xFFFFFFFFFFFF) << 80
        uuid_int |= random.SystemRandom().getrandbits(76)
        uuid_int &= ~(0xC000 << 48)
        uuid_int |= 0x8000 << 48
        uuid_int &= ~(0xF000 << 64)
        uuid_int |= 7 << 76
        return "%032x" % uuid_int

    return uuid7


_uuid7 = _uuid7gen()


__all__ = ["DiscordBotScheduler", "ScheduledDispatch", "Scheduler"]

SQLROW_RET_ROW_TYPE = tuple[
    str, str, str, str, int | None, int | None, bytes | None, bool
]
SQL_INSERT_ROW_TYPE = tuple[str, str, str, str, int | None, int | None, bytes | None]
DATE_FMT = r"%Y-%m-%d %H:%M"

# There's a slight overhead of using TEXT here for a uuid.
# It's not that much, but it's worth considering a migration strategy later
INITIALIZATION_STATEMENTS = """
CREATE TABLE IF NOT EXISTS scheduled_dispatches (
    task_id TEXT PRIMARY KEY NOT NULL,
    dispatch_name TEXT NOT NULL,
    dispatch_time TEXT NOT NULL,
    dispatch_zone TEXT NOT NULL,
    associated_guild INTEGER,
    associated_user INTEGER,
    dispatch_extra BLOB,
    fetched INTEGER DEFAULT false
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

UNSCHEDULE_ALL_BY_USER_STATEMENT = """
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
SELECT *
FROM scheduled_dispatches
WHERE dispatch_name = ? AND fetched = FALSE;
"""

SELECT_ALL_BY_NAME_AND_GUILD_STATEMET = """
SELECT *
FROM scheduled_dispatches
WHERE
    dispatch_name = ?
    AND associated_guild IS NOT NULL
    AND associated_guild = ?
    AND fetched = FALSE;
"""

SELECT_ALL_BY_NAME_AND_USER_STATEMENT = """
SELECT *
FROM scheduled_dispatches
WHERE
    dispatch_name = ?
    AND associated_user IS NOT NULL
    AND associated_user = ?
    AND fetched = FALSE;
"""

SELECT_ALL_BY_NAME_AND_MEMBER_STATEMENT = """
SELECT *
FROM scheduled_dispatches
WHERE
    dispatch_name = ?
    AND associated_guild IS NOT NULL
    AND associated_user IS NOT NULL
    AND associated_guild = ?
    AND associated_user = ?
    AND fetched = FALSE;
"""

INSERT_SCHEDULE_STATEMENT = """
INSERT INTO scheduled_dispatches
(task_id, dispatch_name, dispatch_time, dispatch_zone, associated_guild, associated_user, dispatch_extra)
VALUES (?, ?, ?, ?, ?, ?, ?);
"""

FETCH_UPCOMING_IN_ZONE_WITH = """
UPDATE scheduled_dispatches
SET fetched = TRUE
WHERE dispatch_time < ? AND dispatch_zone = ? AND fetched = FALSE
RETURNING *;
"""

SELECT_PREVIOUSLY_FETCHED = """
SELECT *
FROM scheduled_dispatches
WHERE fetched = TRUE;
"""


class ScheduledDispatch(Struct, frozen=True, gc=False):
    task_id: str
    dispatch_name: str
    dispatch_time: str
    dispatch_zone: str
    associated_guild: int | None
    associated_user: int | None
    dispatch_extra: bytes | None
    fetched: bool = False

    def __lt__(self: Self, other: object) -> bool:
        if type(self) is type(other):
            assert isinstance(other, ScheduledDispatch)
            return (self.get_arrow_time(), self.task_id) < (
                other.get_arrow_time(),
                other.task_id,
            )
        return False

    def __gt__(self: Self, other: object) -> bool:
        if type(self) is type(other):
            assert isinstance(other, ScheduledDispatch)
            return (self.get_arrow_time(), self.task_id) > (
                other.get_arrow_time(),
                other.task_id,
            )
        return False

    @classmethod
    def from_sqlite_row(cls: type[Self], row: SQLROW_RET_ROW_TYPE) -> Self:
        tid, name, time, zone, guild, user, extra_bytes, fetched = row
        return cls(tid, name, time, zone, guild, user, extra_bytes, fetched)

    @classmethod
    def from_exposed_api(
        cls: type[Self],
        *,
        name: str,
        time: str,
        zone: str,
        guild: int | None = None,
        user: int | None = None,
        extra: object = NoValue,
    ) -> Self:
        packed = None if extra is NoValue else msgpack_encode(extra)
        return cls(_uuid7(), name, time, zone, guild, user, packed)

    def to_sqlite_row(self: Self) -> SQL_INSERT_ROW_TYPE:
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
        return arrow.Arrow.strptime(
            self.dispatch_time, DATE_FMT, pytz.timezone(self.dispatch_zone)
        )

    def unpack_extra[T](self: Self, typ: type[T] = object) -> Maybe[T]:
        """If a type is provided, attempt to deserialize to this type via msgspec"""
        if self.dispatch_extra is not None:
            if typ is object:
                return msgpack_decode(self.dispatch_extra, strict=True)
            return msgpack_decode(self.dispatch_extra, strict=True, type=typ)
        return _Internal.NoValue


def _setup_db(conn: apsw.Connection) -> set[str]:
    conn.pragma("optimize", 0x10002)
    conn.pragma("analysis_limit", 400)
    cursor = conn.cursor()
    cursor.execute(INITIALIZATION_STATEMENTS)
    cursor.execute(ZONE_SELECTION_STATEMENT)
    return set(chain.from_iterable(cursor))


def _get_scheduled(
    conn: apsw.Connection, granularity: int, zones: set[str]
) -> list[ScheduledDispatch]:
    ret: list[ScheduledDispatch] = []
    if not zones:
        return ret

    cutoff = arrow.utcnow() + timedelta(minutes=granularity)
    with conn:
        cursor = conn.cursor()
        for zone in zones:
            local_time = cutoff.to(pytz.timezone(zone)).strftime(DATE_FMT)
            cursor.execute(FETCH_UPCOMING_IN_ZONE_WITH, (local_time, zone))
            ret.extend(map(ScheduledDispatch.from_sqlite_row, cursor))

    return ret


def _get_previously_fetched(conn: apsw.Connection) -> list[ScheduledDispatch]:
    with conn:
        cursor = conn.cursor()
        cursor.execute(SELECT_PREVIOUSLY_FETCHED)
        return [*map(ScheduledDispatch.from_sqlite_row, cursor)]


def _schedule(
    conn: apsw.Connection,
    *,
    dispatch_name: str,
    dispatch_time: str,
    dispatch_zone: str,
    guild_id: int | None,
    user_id: int | None,
    dispatch_extra: object,
) -> str:
    # do this here, so if it fails, it fails at scheduling
    zone = pytz.timezone(dispatch_zone)
    _time = arrow.Arrow.strptime(dispatch_time, DATE_FMT, zone)
    obj = ScheduledDispatch.from_exposed_api(
        name=dispatch_name,
        time=dispatch_time,
        zone=dispatch_zone,
        guild=guild_id,
        user=user_id,
        extra=dispatch_extra,
    )

    with conn:
        cursor = conn.cursor()
        cursor.execute(INSERT_SCHEDULE_STATEMENT, obj.to_sqlite_row())

    return obj.task_id


def _query(
    conn: apsw.Connection, query_str: str, params: tuple[int | str, ...]
) -> list[ScheduledDispatch]:
    cursor = conn.cursor()
    return [
        ScheduledDispatch.from_sqlite_row(row)
        for row in cursor.execute(query_str, params)
    ]


def _drop(conn: apsw.Connection, query_str: str, params: tuple[int | str, ...]) -> None:
    with conn:
        cursor = conn.cursor()
        cursor.execute(query_str, params)


def resolve_path_with_links(path: Path, *, folder: bool = False) -> Path:
    """
    Python only resolves with strict=True if the path exists.
    """
    try:
        return path.resolve(strict=True)
    except FileNotFoundError:
        path = resolve_path_with_links(path.parent, folder=True) / path.name
        if folder:
            path.mkdir(
                mode=0o700
            )  # python's default is world read/write/traversable... (0o777)
        else:
            path.touch(mode=0o600)  # python's default is world read/writable... (0o666)
        return path.resolve(strict=True)


class Scheduler:
    def __init__(self, db_path: Path, granularity: int = 1, *, use_threads: bool = False):
        if granularity < 1:
            msg = "Granularity must be a positive iteger number of minutes"
            raise ValueError(msg)
        self.granularity = granularity
        resolved_path_as_str = str(resolve_path_with_links(db_path))
        self._connection = conn = apsw.Connection(resolved_path_as_str)
        conn.pragma("journal_mode", "wal")
        # May set this lower in future, corresponds with asyncio's default debug ms
        # But is significantly higher than expected for this usage pattern.
        conn.set_busy_timeout(100)
        conn.pragma("foreign_keys", "ON")
        conn.config(apsw.SQLITE_DBCONFIG_DQS_DML, 0)
        conn.config(apsw.SQLITE_DBCONFIG_DQS_DDL, 0)
        self._zones = set[str]()
        self._queue = asyncio.PriorityQueue[ScheduledDispatch]()
        self._ready = False
        self._closing = False
        self._lock = asyncio.Lock()
        self._loop_task: asyncio.Task[None] | None = None
        self._discord_task: asyncio.Task[None] | None = None
        self._use_threads: bool = use_threads

    def stop(self: Self) -> None:
        if self._loop_task is None:
            msg = "Contextmanager, use it"
            raise RuntimeError(msg)
        self._loop_task.cancel()
        if self._discord_task:
            self._discord_task.cancel()

    async def _get_scheduled(self) -> list[ScheduledDispatch]:
        f = _get_scheduled
        args = (self._connection, self.granularity, self._zones)
        if self._use_threads:
            return await asyncio.to_thread(f, *args)
        return f(*args)

    async def _loop(self: Self) -> None:
        # not currently modifiable once running
        # differing granularities here, + a delay on retrieving in .get_next()
        # ensures closest
        sleep_gran = self.granularity * 25
        should_optimize = cycle([False] * 99 + [True])
        while (not self._closing) and await asyncio.sleep(sleep_gran, self._ready):
            # Lock needed to ensure that once the db is dropping rows
            # that a graceful shutdown doesn't drain the queue until entries are in it.
            async with self._lock:
                # check again after the potential async context switch that we aren't closing,
                # we don't want to remove rows from the db anymore if we're closing now
                if self._closing:
                    return
                scheduled = await self._get_scheduled()
                for s in scheduled:
                    await self._queue.put(s)
                if next(should_optimize):
                    self._connection.pragma("optimize")

    async def __aexit__(self: Self, *_dont_care: object):
        if not self._closing:
            msg = "Exiting without use of stop_gracefully may cause loss of tasks"
            warn(msg, stacklevel=2)
        self.stop()

    async def get_next(self: Self) -> ScheduledDispatch:
        """
        gets the next scheduled event, waiting if neccessary.
        """
        dispatch: ScheduledDispatch | None = None
        try:
            dispatch = await self._queue.get()
            now = arrow.utcnow()
            scheduled_for = dispatch.get_arrow_time()
            if now < scheduled_for:
                delay = (now - scheduled_for).total_seconds()
                await asyncio.sleep(delay)
            return dispatch
        finally:
            if dispatch is not None:
                self._queue.task_done()

    async def get_previously_fetched(self: Self) -> list[ScheduledDispatch]:
        """Get a list of all previously fetched, but not deleted items.
        Intended use: recovery of a task queue.
        """
        return _get_previously_fetched(self._connection)

    async def task_done(self: Self, obj: ScheduledDispatch, /) -> None:
        """Mark a scheduled dispatch as having been done, removing it from the backing store"""
        if self._use_threads:
            await asyncio.to_thread(
                _drop,
                self._connection,
                UNSCHEDULE_BY_UUID_STATEMENT,
                (obj.task_id,),
            )
        else:
            _drop(self._connection, UNSCHEDULE_BY_UUID_STATEMENT, (obj.task_id,))

    async def stop_gracefully(self: Self) -> None:
        """Notify the internal scheduling loop to stop scheduling and wait for the internal queue to be empty"""
        self._closing = True
        # don't remove lock, see note in _loop
        async with self._lock:
            await self._queue.join()
            self._connection.close()

    async def __aenter__(self: Self) -> Self:
        self._zones = _setup_db(self._connection)
        self._ready = True
        self._loop_task = asyncio.create_task(self._loop())
        self._loop_task.add_done_callback(
            lambda f: f.exception() if not f.cancelled() else None
        )
        return self

    async def schedule_event(
        self: Self,
        *,
        dispatch_name: str,
        dispatch_time: str,
        dispatch_zone: str,
        guild_id: int | None = None,
        user_id: int | None = None,
        dispatch_extra: object = NoValue,
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
        dispatch_extra: object | None
            Optionally, Extra data to attach to dispatch.
            This may be any object serializable by msgspec.msgpack.encode
            where the result is round-trip decodable with
            msgspec.msgpack.decode(..., strict=True)

        Returns
        -------
        str
            A unique id for the task, usable for cancelation.
            This is a uuid-like string, but is not guaranteed to be compatible
            with the RFCs describing UUIDs
        """
        self._zones.add(dispatch_zone)
        if self._use_threads:
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

        return _schedule(
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
        Unschedule something by unqiue id.
        This may miss things which should run within the next interval as defined by `granularity`
        Non-existent uuids are silently handled.
        """
        if self._use_threads:
            await asyncio.to_thread(
                _drop, self._connection, UNSCHEDULE_BY_UUID_STATEMENT, (uuid,)
            )
        else:
            _drop(self._connection, UNSCHEDULE_BY_UUID_STATEMENT, (uuid,))

    async def drop_user_schedule(self: Self, user_id: int) -> None:
        """
        Drop all scheduled events for a user (by user_id)

        Intended use case:
            removing everything associated to a user who asks for data removal, doesn't exist anymore, or is blacklisted
        """
        if self._use_threads:
            await asyncio.to_thread(
                _drop,
                self._connection,
                UNSCHEDULE_ALL_BY_USER_STATEMENT,
                (user_id,),
            )
        else:
            _drop(self._connection, UNSCHEDULE_ALL_BY_USER_STATEMENT, (user_id,))

    async def drop_event_for_user(self: Self, dispatch_name: str, user_id: int) -> None:
        """
        Drop scheduled events dispatched to `dispatch_name` for user (by user_id)

        Intended use case example:
            A reminder system allowing a user to unschedule all reminders
            without effecting how other extensions might use this.
        """
        f = _drop
        args = (
            self._connection,
            UNSCHEDULE_ALL_BY_NAME_AND_USER_STATEMENT,
            (dispatch_name, user_id),
        )
        if self._use_threads:
            await asyncio.to_thread(f, *args)
        else:
            f(*args)

    async def drop_guild_schedule(self: Self, guild_id: int) -> None:
        """
        Drop all scheduled events for a guild (by guild_id)

        Intended use case:
            clearing sccheduled events for a guild when leaving it.
        """
        if self._use_threads:
            await asyncio.to_thread(
                _drop,
                self._connection,
                UNSCHEDULE_ALL_BY_GUILD_STATEMENT,
                (guild_id,),
            )
        else:
            _drop(self._connection, UNSCHEDULE_ALL_BY_GUILD_STATEMENT, (guild_id,))

    async def drop_event_for_guild(self: Self, dispatch_name: str, guild_id: int) -> None:
        """
        Drop scheduled events dispatched to `dispatch_name` for guild (by guild_id)

        Intended use case example:
            An admin command allowing clearing all scheduled messages for a guild.
        """
        f = _drop
        args = (
            self._connection,
            UNSCHEDULE_ALL_BY_NAME_AND_GUILD_STATEMENT,
            (dispatch_name, guild_id),
        )
        if self._use_threads:
            await asyncio.to_thread(f, *args)
        else:
            f(*args)

    async def drop_member_schedule(self: Self, guild_id: int, user_id: int) -> None:
        """
        Drop all scheduled events for a guild (by guild_id, user_id)

        Intended use case:
            clearing sccheduled events for a member that leaves a guild
        """
        f = _drop
        args = (
            self._connection,
            UNSCHEDULE_ALL_BY_MEMBER_STATEMENT,
            (guild_id, user_id),
        )
        if self._use_threads:
            await asyncio.to_thread(f, *args)
        else:
            f(*args)

    async def drop_event_for_member(
        self: Self, dispatch_name: str, guild_id: int, user_id: int
    ) -> None:
        """
        Drop scheduled events dispatched to `dispatch_name` for member (by guild_id, user_id)

        Intended use case example:
            see user example, but in a guild
        """
        f = _drop
        args = (
            self._connection,
            UNSCHEDULE_ALL_BY_NAME_AND_MEMBER_STATEMENT,
            (dispatch_name, guild_id, user_id),
        )
        if self._use_threads:
            await asyncio.to_thread(f, *args)
        else:
            f(*args)

    async def list_event_schedule_for_user(
        self: Self, dispatch_name: str, user_id: int
    ) -> list[ScheduledDispatch]:
        """
        list the events of a specified name scheduled for a user (by user_id)
        """
        f = _query
        args = (
            self._connection,
            SELECT_ALL_BY_NAME_AND_USER_STATEMENT,
            (dispatch_name, user_id),
        )
        if self._use_threads:
            return await asyncio.to_thread(f, *args)
        return f(*args)

    async def list_event_schedule_for_member(
        self: Self,
        dispatch_name: str,
        guild_id: int,
        user_id: int,
    ) -> list[ScheduledDispatch]:
        """
        list the events of a specified name scheduled for a member (by guild_id, user_id)
        """
        f = _query
        args = (
            self._connection,
            SELECT_ALL_BY_NAME_AND_MEMBER_STATEMENT,
            (dispatch_name, guild_id, user_id),
        )
        if self._use_threads:
            return await asyncio.to_thread(f, *args)
        return f(*args)

    async def list_event_schedule_for_guild(
        self: Self, dispatch_name: str, guild_id: int
    ) -> list[ScheduledDispatch]:
        """
        list the events of a specified name scheduled for a guild (by guild_id)
        """
        f = _query
        args = (
            self._connection,
            SELECT_ALL_BY_NAME_AND_USER_STATEMENT,
            (dispatch_name, guild_id),
        )
        if self._use_threads:
            return await asyncio.to_thread(f, *args)
        return f(*args)

    @staticmethod
    def time_str_from_params(
        year: int, month: int, day: int, hour: int, minute: int
    ) -> str:
        """
        A quick helper for people working with other time representations
        (if you have a datetime object, just use strftime with "%Y-%m-%d %H:%M")
        """
        return arrow.Arrow(year, month, day, hour, minute).strftime(DATE_FMT)


class DiscordBotScheduler(Scheduler):
    """Scheduler with convienence dispatches compatible with discord.py's commands extenstion
    Note: long-term compatability not guaranteed, dispatch isn't covered by discord.py's version guarantees.

    The method for initiating dispatch is typed in a way that
    attempts to ensure that if it becomes incompatible due to changes to discord.py's dispatch system,
    you get a notice of it statically.
    """

    async def _bot_dispatch_loop(
        self: Self,
        bot: BotLike,
        *,
        wait_until_ready: bool,
        redispatch_fetched: bool,
    ) -> None:
        if not self._ready:
            msg = "context manager, use it"
            raise RuntimeError(msg)

        if wait_until_ready:
            await bot.wait_until_ready()

        if redispatch_fetched:
            for scheduled in await self.get_previously_fetched():
                bot.dispatch(f"sinbad_scheduler_{scheduled.dispatch_name}", scheduled)

        while scheduled := await self.get_next():
            bot.dispatch(f"sinbad_scheduler_{scheduled.dispatch_name}", scheduled)

    def start_dispatch_to_bot(
        self: Self,
        bot: BotLike,
        *,
        wait_until_ready: bool = True,
        redispatch_fetched_first: bool = False,
    ) -> None:
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
        If `redispatch_fetched_once` is True (defaults False) will redispatch the items which have
        been fetched but not marked with task_done once before fetching others.
        """
        if not self._ready:
            msg = "context manager, use it"
            raise RuntimeError(msg)

        coro = self._bot_dispatch_loop(
            bot,
            wait_until_ready=wait_until_ready,
            redispatch_fetched=redispatch_fetched_first,
        )
        t = self._discord_task = asyncio.create_task(coro)
        t.add_done_callback(lambda f: None if f.cancelled() else f.exception())
