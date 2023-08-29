# discord-scheduler

A persistent scheduling implementation suitable for use with discord.py

## Minimum python

3.11

## Implementation details

This is designed for use alongside a discord.py bot or client,
uses sqlite as a persistent backend via apsw, and uses arrow for correct time handling.

This currently dispatches the first time the walltime for a zone is at or past the configured time.
folds are not disambiguated. This is considered acceptable currently but is not ideal and may be changed later.

## pypi?

This will not be published on pypi. It is suitable as a single file include
(include just scheduler/scheduler.py) or as an external link
Other *libraries* should not be built to depend on this as-is,
it is designed to be used directly by application code.

# Stability

I'll make an attempt to keep the existing APIs stable, but I provide no guarantees of this at this time.


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



## Docs?

docstrings provide accurate info, and parameter names and types are consise and accurate, but this needs doing (PRs welcome)


## Help?

Open an issue here if you need it.
