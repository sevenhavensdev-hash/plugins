"""Modmail entry point for the Staff Strike plugin."""

from discord.ext import commands

from .moderation import DynoTransferCog, ModerationCog
from .staff_manager import StaffManagerCog, StaffStatsTransferCog


async def setup(bot: commands.Bot) -> None:
    """Load Staff Strike without duplicating an existing Staff Manager."""
    if bot.get_cog("Staff Manager") is None:
        await bot.add_cog(StaffManagerCog(bot))
    if bot.get_cog("Staff Strike Moderation") is None:
        await bot.add_cog(ModerationCog(bot))
    if bot.get_cog("Staff Strike Dyno Transfer") is None:
        await bot.add_cog(DynoTransferCog(bot))
    if (
        bot.get_command("transferstaffstats") is None
        and bot.get_cog("Staff Stats Transfer") is None
    ):
        await bot.add_cog(StaffStatsTransferCog(bot))
    if bot.get_command("transferdyno") is None:
        raise RuntimeError(
            "Staff Strike loaded without registering the transferdyno command."
        )
    if bot.get_command("transferstaffstats") is None:
        raise RuntimeError(
            "Staff Strike loaded without registering the transferstaffstats command."
        )
