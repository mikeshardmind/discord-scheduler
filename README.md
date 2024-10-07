# discord-scheduler

A persistent scheduling implementation suitable for use with discord.py

## Minimum python

3.12

## Implementation details

This is designed for use alongside a discord.py bot or client,
uses sqlite as a persistent backend via apsw, and uses arrow for correct time handling.

This currently dispatches the first time the walltime for a zone is at or past the configured time.
folds are not disambiguated. This is considered acceptable currently but is not ideal and may be changed later.

## pypi?

This will not be published on pypi. It is currently suitable as a single file include
(include just scheduler/scheduler.py) or as PEP 508 dependency link (single file include subject to future change if needed)
Other *libraries* should not be built to depend on this as-is, it is designed to be used directly by application code.

If you build a library which depends on this, please just vendor it and do not upload it to a packaging index on my behalf.

The license is permissive to not prevent redistribution,
but I do not want to be in the position of handling issues for a means of distribution I'm not intending.

It is set up to be specifiable in project requirements and installable as a wheel

eg.
```
scheduler @ https://github.com/mikeshardmind/discord-scheduler/archive/9aa55a61977c3047834c82dde8e06ed39a53843a.zip
```
or
```
scheduler @ git+https://github.com/mikeshardmind/discord-scheduler@9aa55a61977c3047834c82dde8e06ed39a53843a
```

# Stability

I'll make an attempt to keep the existing APIs stable, but I provide no guarantees of this at this time.


## Why a synchronous sqlite backend for async code?

The specific use here shouldn't block the event loop
even at the scale of some of the largest discord bots.

If you have a case where it does or would like to request an additional backend, open an issue.

(There are also reasons related to specific durability gurantees which I have not yet documented, but intend to make)


## Alternative data backends and serializers?

Probably not, but I'm not ruling out if there's demand.

## Easier time APIs?

The time APIs provided are designed to ensure devs handle human timezones correctly.
I am open to considering changes which keep that intact, but will not be simplifying an API in a
way that introduces the liklihood of developer misuse or misunderstanding of time.


## example use?

TODO: improve this (PRs welcome)

```py

from scheduler import DiscordBotScheduler as Scheduler,

class MyBot(commands.Bot):

    def __init__(self, scheduler: Scheduler):
        self.scheduler = scheduler
        super().__init__(intents=...)

    async def setup_hook(self):
        self.scheduler.start_dispatch_to_bot(self)

    async def close(self):
        await self.scheduler.stop_gracefully()
        await super().close()


async def main():

    path = pathlib.Path("scheduler_data.db")
    scheduler = Scheduler(path, granularity=1)
    bot = MyBot(scheduler)
    async with scheduler, bot:
        bot.start(TOKEN)

# in a cog:


class Reminder(commands.Cog):

    @commands.Cog.listener("on_sinbad_scheduler_reminder")
    async def send_reminder(self, payload: ScheduledDispatch):
        extra_data = payload.unpack_extra()
        guild = self.bot.get_guild(payload.dispatch_guild)
        if not guild:
            return
        member = guild.get_member(payload.dispatch_user)
        if not member:
            return

        channel = guild.get_channel(extra_data["channel_id"])
        if not channel:
            return

        await channel.send(
            f"{member.mention} {extra_data["msg"]}",
            allowed_mentions=discord.AllowedMentions(users=member, everyone=False, roles=False),
        )


    async def create_reminder_for_member(self, channel, member, message, timedelta):
        """
        This example is for something like "in 4 hours" where absolute time makes sense,
        for scheduling at a specific time like
        "July 24 at noon US/Eastern" you'd need to handle more and pass "US/Eastern"
        A utility is included in the scheduler class for alternate construction
        """

        when = discord.utils.utcnow() + timedelta
        datestr = when.strftime("%Y-%m-%d %H:%M")

        self.bot.scheduler.schedule_event(
            dispatch_name="reminder",
            dispatch_guild=member.guild.id,
            dispatch_user=member.id,
            dispatch_time=datestr,
            dispatch_zone="utc",
            dispatch_extra=dict(channel_id=channel.id, msg=message),
        )

    # There are also ways to list or drop for a user, memebr, or guild.

```

## dispatch_extra

dispatch_extra optionally stores data that can be encoded to msgpack by msgspec.
This includes user defined msgspec structs.

ScheduledEvent.unpack_extra specifically returns NoValue, rather than None in the case of no data here
to distinguish from a lack of data and a user choosing to store None.

This is slightly more cautious that strictly neccessary, as users should know if
they store extra data or not[^1], this is part of[^2] why unpack_extra isn't done automatically


## Docs?

docstrings provide accurate info, and parameter names and types are consise and accurate, but this needs doing (PRs welcome)


## Help?

Open an issue here if you need it.


### Footnotes

[^1]: Prior versions did not, but recent discussions have shown that many python developers
are not considering where or how they recieve None or valid program states. This should
help identify logical issues specifically for those developers when calling unpack_extra
without considering if they should first.

[^2]: The other reasons here were to: Not call unpack and parsing code when data isn't needed
(allowing applications to not pay for unpacking it if they hit a case that doesn't need it)
and so that users have type safety for arbitrary storable data (The interface accepts a type to parse into)