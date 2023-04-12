# discord-scheduler

A persistent scheduling implementation suitable for use with discord.py

## Minimum python

3.11

## Implementation details

This is designed for use alongside a discord.py bot or client, uses sqlite as a persistent backend via apsw, and uses arrow for correct time handling.


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
way that introduces the lilihood of developer misuse or misunderstanding of time.


## example use?

TODO (PRs welcome)


## Docs?

docstrings provide accurate info, and parameter names and types are consise and accurate, but this needs doing (PRs welcome)


## Help?

Open an issue here if you need it.
