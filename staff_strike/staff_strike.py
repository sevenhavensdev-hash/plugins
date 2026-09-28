"""Modmail entry point for the Staff Strike plugin."""

from discord.ext import commands

from .moderation import ModerationCog
from .staff_manager import StaffManagerCog


async def setup(bot: commands.Bot) -> None:
    """Load Staff Strike without duplicating an existing Staff Manager."""
    if bot.get_cog("Staff Manager") is None:
        await bot.add_cog(StaffManagerCog(bot))
    await bot.add_cog(ModerationCog(bot))
