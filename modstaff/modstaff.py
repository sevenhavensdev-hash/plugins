"""
modstaff — Professional Moderation & Staff Management Plugin for Modmail
=========================================================================

A Dyno-inspired plugin providing full moderation history, staff statistics,
rank tracking, and a leaderboard — all using Discord embeds and buttons.

Plugin structure follows the official Modmail plugin API exactly:
  - commands.Cog subclass loaded via setup()
  - self.bot.plugin_db.get_partition(self) for persistent MongoDB storage
  - core.checks for permission levels
  - discord.ui.View / discord.ui.Button for interactive UI
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel, getLogger

from .utils import (
    COLORS,
    ACTION_EMOJIS,
    action_embed,
    error_embed,
    success_embed,
    history_embed,
    stats_embed,
    leaderboard_embed,
    format_dt,
    format_dt_long,
    format_duration,
    parse_duration,
    time_in_rank,
)
from .views import ConfirmView, HistoryView, LeaderboardView

logger = getLogger(__name__)

# Number of history cases displayed per page
HISTORY_PAGE_SIZE = 5
# Number of staff members per leaderboard page
LEADERBOARD_PAGE_SIZE = 10


class ModStaff(commands.Cog, name="ModStaff"):
    """
    Professional moderation and staff management plugin.

    Provides moderation commands (warn, mute, timeout, kick, ban, softban,
    unban, note, history), staff management (promote, demote), statistics
    (staffstats, staffleaderboard), and a full configuration system.
    """

    def __init__(self, bot):
        self.bot = bot
        # MongoDB partition for persistent storage (Motor/pymongo async)
        self.db = self.bot.plugin_db.get_partition(self)

    # ===========================================================================
    # Internal database helpers
    # ===========================================================================

    async def _next_case_id(self, guild_id: int) -> int:
        """Atomically increment and return the next case number for this guild."""
        result = await self.db.find_one_and_update(
            {"_id": f"case_counter_{guild_id}"},
            {"$inc": {"count": 1}},
            upsert=True,
            return_document=True,
        )
        return result.get("count", 1)

    async def _insert_case(
        self,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        action: str,
        reason: str,
    ) -> int:
        """Insert a moderation case and return its case_id."""
        case_id = await self._next_case_id(guild_id)
        await self.db.insert_one(
            {
                "type": "case",
                "case_id": case_id,
                "guild_id": str(guild_id),
                "user_id": str(user_id),
                "moderator_id": str(moderator_id),
                "action": action,
                "reason": reason or "No reason provided.",
                "timestamp": time.time(),
            }
        )
        return case_id

    async def _get_cases(self, guild_id: int, user_id: int) -> list[dict]:
        """Retrieve all moderation cases for a user in a guild, newest first."""
        cursor = self.db.find(
            {"type": "case", "guild_id": str(guild_id), "user_id": str(user_id)}
        ).sort("timestamp", -1)
        return await cursor.to_list(length=None)

    async def _bump_staff_stat(
        self, guild_id: int, moderator_id: int, action: str
    ):
        """
        Increment a moderation stat counter for a staff member.

        last_active is written to BOTH staff_stats and staff_data so that
        whichever document the stats embed reads from, the value is current.
        """
        now = time.time()
        # Primary stats document
        await self.db.find_one_and_update(
            {"type": "staff_stats", "guild_id": str(guild_id), "user_id": str(moderator_id)},
            {
                "$inc": {f"moderation.{action}": 1},
                "$set": {"last_active": now},
            },
            upsert=True,
        )
        # Mirror last_active into staff_data so the embed always has it
        await self.db.find_one_and_update(
            {"type": "staff_data", "guild_id": str(guild_id), "user_id": str(moderator_id)},
            {"$set": {"last_active": now}},
            upsert=True,
        )

    async def _get_staff_doc(self, guild_id: int, user_id: int) -> dict:
        """Retrieve or create the staff profile document for a user."""
        doc = await self.db.find_one(
            {"type": "staff_data", "guild_id": str(guild_id), "user_id": str(user_id)}
        )
        return doc or {}

    async def _get_stats_doc(self, guild_id: int, user_id: int) -> dict:
        """Retrieve or create the stats document for a staff member."""
        doc = await self.db.find_one(
            {"type": "staff_stats", "guild_id": str(guild_id), "user_id": str(user_id)}
        )
        return doc or {}

    async def _get_config(self, guild_id: int) -> dict:
        """Retrieve plugin configuration for this guild."""
        doc = await self.db.find_one(
            {"type": "config", "guild_id": str(guild_id)}
        )
        return doc or {}

    async def _get_rank_order(self, guild: discord.Guild):
        """Return the configured rank ladder (list of role id strings, low → high)."""
        cfg = await self._get_config(guild.id)
        return cfg.get("rank_order", [])

    async def _get_member_ladder_role(
        self, guild: discord.Guild, member: discord.Member
    ) -> Optional[discord.Role]:
        """
        Return the highest role the member currently holds that appears in the
        rank ladder, or None if they hold none.
        """
        rank_order = await self._get_rank_order(guild)
        if not rank_order:
            return None
        member_role_ids = {str(r.id) for r in member.roles}
        # Walk highest → lowest and return the first match
        for rid in reversed(rank_order):
            if rid in member_role_ids:
                return guild.get_role(int(rid))
        return None

    async def _get_next_lower_role(
        self, guild: discord.Guild, role: discord.Role
    ) -> Optional[discord.Role]:
        """
        Given a role being removed during a demotion, return the next lower
        role in the configured rank ladder, or None if not configured / at bottom.

        The rank_order list is stored lowest → highest.
        """
        rank_order = await self._get_rank_order(guild)
        if not rank_order:
            return None
        try:
            idx = rank_order.index(str(role.id))
        except ValueError:
            return None
        if idx == 0:
            return None  # already at the bottom
        lower_id = rank_order[idx - 1]
        return guild.get_role(int(lower_id))

    async def _get_next_higher_role(
        self, guild: discord.Guild, role: discord.Role
    ) -> Optional[discord.Role]:
        """
        Given the member's current highest ladder role, return the next role up,
        or None if already at the top.
        """
        rank_order = await self._get_rank_order(guild)
        if not rank_order:
            return None
        try:
            idx = rank_order.index(str(role.id))
        except ValueError:
            return None
        if idx >= len(rank_order) - 1:
            return None  # already at the top
        return guild.get_role(int(rank_order[idx + 1]))

    async def _apply_rank_perks(
        self,
        guild: discord.Guild,
        member: discord.Member,
        old_role: Optional[discord.Role],
        new_role: Optional[discord.Role],
    ):
        """
        Add/remove perk roles (LR, MR, department roles, etc.) when a member
        moves from old_role to new_role.

        rank_perks config shape: { "<rank_role_id>": ["<perk_role_id>", ...] }

        Only the *difference* is touched — perk roles shared by both ranks are
        left alone so members never briefly lose a shared role.

        Returns (added, removed) lists of discord.Role objects for display.
        """
        cfg = await self._get_config(guild.id)
        rank_perks: dict = cfg.get("rank_perks", {})

        old_perks = set(rank_perks.get(str(old_role.id), [])) if old_role else set()
        new_perks = set(rank_perks.get(str(new_role.id), [])) if new_role else set()

        to_add = [guild.get_role(int(rid)) for rid in new_perks - old_perks]
        to_remove = [guild.get_role(int(rid)) for rid in old_perks - new_perks]
        to_add = [r for r in to_add if r is not None]
        to_remove = [r for r in to_remove if r is not None]

        try:
            if to_remove:
                await member.remove_roles(*to_remove, reason="Rank perk update")
            if to_add:
                await member.add_roles(*to_add, reason="Rank perk update")
        except (discord.Forbidden, discord.HTTPException) as e:
            logger.warning("Failed to apply rank perks for %s: %s", member.id, e)

        return to_add, to_remove

    async def _save_config(self, guild_id: int, updates: dict):
        """Upsert plugin configuration for this guild."""
        await self.db.find_one_and_update(
            {"type": "config", "guild_id": str(guild_id)},
            {"$set": updates},
            upsert=True,
        )

    # ===========================================================================
    # Internal helpers
    # ===========================================================================

    async def _get_log_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        """Return the configured log channel, or None if not set."""
        cfg = await self._get_config(guild.id)
        ch_id = cfg.get("log_channel_id")
        if ch_id:
            return guild.get_channel(int(ch_id))
        return None

    async def _send_log(
        self,
        guild: discord.Guild,
        embed: discord.Embed,
    ):
        """Send a log embed to the configured log channel, if any."""
        channel = await self._get_log_channel(guild)
        if channel:
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                logger.warning("Missing permissions to send to log channel %s", channel.id)
            except discord.HTTPException as e:
                logger.error("Failed to send log embed: %s", e)

    def _get_embed_color(self, cfg: dict, action: str) -> int:
        """Return the embed color for an action, respecting custom branding."""
        custom = cfg.get("embed_colors", {})
        return custom.get(action, COLORS.get(action, 0x7289DA))

    async def _check_role_permission(
        self,
        ctx: commands.Context,
        required_roles: list[int],
    ) -> bool:
        """
        Return True if the command author has any of the required_roles.
        Falls back to standard Modmail permission checks if no roles configured.
        """
        if not required_roles:
            return True  # no role restriction — rely on @has_permissions decorator
        author_role_ids = {r.id for r in ctx.author.roles}
        return bool(author_role_ids.intersection(required_roles))

    async def _try_dm(
        self,
        user: discord.abc.User,
        embed: discord.Embed,
    ) -> bool:
        """Attempt to DM a user. Returns True on success."""
        try:
            await user.send(embed=embed)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _is_staff_member(self, guild_id: int, member: discord.Member) -> bool:
        """Return True if the member holds any configured staff role."""
        cfg = await self._get_config(guild_id)
        staff_role_ids = cfg.get("staff_role_ids", [])
        if not staff_role_ids:
            return False
        member_role_ids = {str(r.id) for r in member.roles}
        return bool(member_role_ids.intersection(staff_role_ids))

    # ===========================================================================
    # Message tracking listener
    # ===========================================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        Track messages sent by staff members in guild channels.

        Increments tickets.messages_sent in staff_stats for every non-bot
        message a staff member sends in a guild text channel or thread.
        This powers the 'Messages Sent' counter shown in ?staffstats.
        """
        # Ignore DMs, bots, and system messages
        if not message.guild or message.author.bot or not message.content:
            return

        member = message.guild.get_member(message.author.id)
        if member is None:
            return

        # Only track messages from configured staff members
        if not await self._is_staff_member(message.guild.id, member):
            return

        now = time.time()
        await self.db.find_one_and_update(
            {
                "type": "staff_stats",
                "guild_id": str(message.guild.id),
                "user_id": str(message.author.id),
            },
            {
                "$inc": {"tickets.messages_sent": 1},
                "$set": {"last_active": now},
            },
            upsert=True,
        )
        # Mirror last_active to staff_data too
        await self.db.find_one_and_update(
            {
                "type": "staff_data",
                "guild_id": str(message.guild.id),
                "user_id": str(message.author.id),
            },
            {"$set": {"last_active": now}},
            upsert=True,
        )

    # ===========================================================================
    # Moderation commands
    # ===========================================================================

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.command(name="warn")
    async def warn(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        reason: str = "No reason provided.",
    ):
        """
        Issue a formal warning to a guild member.

        Usage: `?warn @user [reason]`
        """
        if member == ctx.author:
            return await ctx.send(embed=error_embed("Cannot Warn", "You cannot warn yourself."))
        if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
            return await ctx.send(
                embed=error_embed("Insufficient Hierarchy", "You cannot warn a member with a higher or equal role.")
            )

        case_id = await self._insert_case(ctx.guild.id, member.id, ctx.author.id, "warn", reason)
        await self._bump_staff_stat(ctx.guild.id, ctx.author.id, "warn")

        embed = action_embed("warn", ctx.author, member, reason, case_id, guild_name=ctx.guild.name)
        await ctx.send(embed=embed)
        await self._send_log(ctx.guild, embed)

        dm_embed = action_embed("warn", ctx.author, member, reason, case_id, guild_name=ctx.guild.name)
        dm_embed.title = f"⚠️ You have been warned in {ctx.guild.name}"
        await self._try_dm(member, dm_embed)

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.command(name="mute")
    async def mute(
        self,
        ctx: commands.Context,
        member: discord.Member,
        duration: str = "10m",
        *,
        reason: str = "No reason provided.",
    ):
        """
        Mute a member using Discord timeout (up to 28 days).

        Usage: `?mute @user [duration] [reason]`
        Duration format: 10s, 5m, 2h, 1d, 1w (max 28d).
        """
        if member == ctx.author:
            return await ctx.send(embed=error_embed("Cannot Mute", "You cannot mute yourself."))
        if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
            return await ctx.send(
                embed=error_embed("Insufficient Hierarchy", "You cannot mute a member with a higher or equal role.")
            )

        seconds = parse_duration(duration)
        if seconds is None:
            return await ctx.send(
                embed=error_embed(
                    "Invalid Duration",
                    "Use a format like `10m`, `2h`, `1d`. Supported units: s, m, h, d, w.",
                )
            )
        if seconds > 28 * 86400:
            return await ctx.send(
                embed=error_embed("Duration Too Long", "Discord timeouts are limited to 28 days.")
            )

        until = discord.utils.utcnow() + timedelta(seconds=seconds)
        try:
            await member.timeout(until, reason=f"[Case] {reason} | Mod: {ctx.author}")
        except discord.Forbidden:
            return await ctx.send(
                embed=error_embed("Missing Permissions", "I do not have permission to timeout this member.")
            )
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed("Discord Error", str(e)))

        case_id = await self._insert_case(ctx.guild.id, member.id, ctx.author.id, "mute", reason)
        await self._bump_staff_stat(ctx.guild.id, ctx.author.id, "mute")

        embed = action_embed(
            "mute", ctx.author, member, reason, case_id,
            extra_fields=[("⏱️ Duration", format_duration(seconds), True)],
            guild_name=ctx.guild.name,
        )
        await ctx.send(embed=embed)
        await self._send_log(ctx.guild, embed)

        dm_embed = action_embed(
            "mute", ctx.author, member, reason, case_id,
            extra_fields=[("⏱️ Duration", format_duration(seconds), True)],
            guild_name=ctx.guild.name,
        )
        dm_embed.title = f"🔇 You have been muted in {ctx.guild.name}"
        await self._try_dm(member, dm_embed)

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.command(name="timeout")
    async def timeout_cmd(
        self,
        ctx: commands.Context,
        member: discord.Member,
        duration: str,
        *,
        reason: str = "No reason provided.",
    ):
        """
        Apply a Discord timeout to a member.

        Usage: `?timeout @user <duration> [reason]`
        Duration format: 10s, 5m, 2h, 1d, 1w (max 28d).
        """
        if member == ctx.author:
            return await ctx.send(embed=error_embed("Cannot Timeout", "You cannot timeout yourself."))
        if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
            return await ctx.send(
                embed=error_embed("Insufficient Hierarchy", "You cannot timeout a member with a higher or equal role.")
            )

        seconds = parse_duration(duration)
        if seconds is None:
            return await ctx.send(
                embed=error_embed("Invalid Duration", "Use a format like `10m`, `2h`, `1d`.")
            )
        if seconds > 28 * 86400:
            return await ctx.send(
                embed=error_embed("Duration Too Long", "Discord timeouts are limited to 28 days.")
            )

        until = discord.utils.utcnow() + timedelta(seconds=seconds)
        try:
            await member.timeout(until, reason=f"[Case] {reason} | Mod: {ctx.author}")
        except discord.Forbidden:
            return await ctx.send(
                embed=error_embed("Missing Permissions", "I do not have permission to timeout this member.")
            )
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed("Discord Error", str(e)))

        case_id = await self._insert_case(ctx.guild.id, member.id, ctx.author.id, "timeout", reason)
        await self._bump_staff_stat(ctx.guild.id, ctx.author.id, "timeout")

        embed = action_embed(
            "timeout", ctx.author, member, reason, case_id,
            extra_fields=[("⏱️ Duration", format_duration(seconds), True)],
            guild_name=ctx.guild.name,
        )
        await ctx.send(embed=embed)
        await self._send_log(ctx.guild, embed)

        dm_embed = action_embed(
            "timeout", ctx.author, member, reason, case_id,
            extra_fields=[("⏱️ Duration", format_duration(seconds), True)],
            guild_name=ctx.guild.name,
        )
        dm_embed.title = f"⏱️ You have been timed out in {ctx.guild.name}"
        await self._try_dm(member, dm_embed)

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.command(name="kick")
    async def kick(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        reason: str = "No reason provided.",
    ):
        """
        Kick a member from the guild.

        Usage: `?kick @user [reason]`
        """
        if member == ctx.author:
            return await ctx.send(embed=error_embed("Cannot Kick", "You cannot kick yourself."))
        if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
            return await ctx.send(
                embed=error_embed("Insufficient Hierarchy", "You cannot kick a member with a higher or equal role.")
            )

        dm_embed = action_embed("kick", ctx.author, member, reason, 0, guild_name=ctx.guild.name)
        dm_embed.title = f"👢 You have been kicked from {ctx.guild.name}"
        await self._try_dm(member, dm_embed)

        try:
            await member.kick(reason=f"[Case] {reason} | Mod: {ctx.author}")
        except discord.Forbidden:
            return await ctx.send(
                embed=error_embed("Missing Permissions", "I do not have permission to kick this member.")
            )
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed("Discord Error", str(e)))

        case_id = await self._insert_case(ctx.guild.id, member.id, ctx.author.id, "kick", reason)
        await self._bump_staff_stat(ctx.guild.id, ctx.author.id, "kick")

        embed = action_embed("kick", ctx.author, member, reason, case_id, guild_name=ctx.guild.name)
        await ctx.send(embed=embed)
        await self._send_log(ctx.guild, embed)

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.command(name="ban")
    async def ban(
        self,
        ctx: commands.Context,
        user: Union[discord.Member, discord.User],
        *,
        reason: str = "No reason provided.",
    ):
        """
        Ban a user from the guild.

        Usage: `?ban @user [reason]`
        Accepts user IDs for offline users.
        """
        if isinstance(user, discord.Member):
            if user == ctx.author:
                return await ctx.send(embed=error_embed("Cannot Ban", "You cannot ban yourself."))
            if user.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
                return await ctx.send(
                    embed=error_embed("Insufficient Hierarchy", "You cannot ban a member with a higher or equal role.")
                )
            dm_embed = action_embed("ban", ctx.author, user, reason, 0, guild_name=ctx.guild.name)
            dm_embed.title = f"🔨 You have been banned from {ctx.guild.name}"
            await self._try_dm(user, dm_embed)

        try:
            await ctx.guild.ban(user, reason=f"[Case] {reason} | Mod: {ctx.author}", delete_message_days=0)
        except discord.Forbidden:
            return await ctx.send(
                embed=error_embed("Missing Permissions", "I do not have permission to ban this user.")
            )
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed("Discord Error", str(e)))

        case_id = await self._insert_case(ctx.guild.id, user.id, ctx.author.id, "ban", reason)
        await self._bump_staff_stat(ctx.guild.id, ctx.author.id, "ban")

        embed = action_embed("ban", ctx.author, user, reason, case_id, guild_name=ctx.guild.name)
        await ctx.send(embed=embed)
        await self._send_log(ctx.guild, embed)

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.command(name="softban")
    async def softban(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        reason: str = "No reason provided.",
    ):
        """
        Softban a member (ban then immediately unban to delete recent messages).

        Usage: `?softban @user [reason]`
        """
        if member == ctx.author:
            return await ctx.send(embed=error_embed("Cannot Softban", "You cannot softban yourself."))
        if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
            return await ctx.send(
                embed=error_embed("Insufficient Hierarchy", "You cannot softban a member with a higher or equal role.")
            )

        dm_embed = action_embed("softban", ctx.author, member, reason, 0, guild_name=ctx.guild.name)
        dm_embed.title = f"🪃 You have been softbanned from {ctx.guild.name}"
        await self._try_dm(member, dm_embed)

        try:
            await ctx.guild.ban(member, reason=f"[Softban] {reason} | Mod: {ctx.author}", delete_message_days=7)
            await ctx.guild.unban(member, reason="Softban — auto unban")
        except discord.Forbidden:
            return await ctx.send(
                embed=error_embed("Missing Permissions", "I do not have permission to softban this member.")
            )
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed("Discord Error", str(e)))

        case_id = await self._insert_case(ctx.guild.id, member.id, ctx.author.id, "softban", reason)
        await self._bump_staff_stat(ctx.guild.id, ctx.author.id, "softban")

        embed = action_embed(
            "softban", ctx.author, member, reason, case_id,
            extra_fields=[("ℹ️ Note", "Member was banned then immediately unbanned (messages deleted).", False)],
            guild_name=ctx.guild.name,
        )
        await ctx.send(embed=embed)
        await self._send_log(ctx.guild, embed)

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.command(name="unban")
    async def unban(
        self,
        ctx: commands.Context,
        user_id: int,
        *,
        reason: str = "No reason provided.",
    ):
        """
        Unban a user by their Discord ID.

        Usage: `?unban <user_id> [reason]`
        """
        try:
            ban_entry = await ctx.guild.fetch_ban(discord.Object(id=user_id))
        except discord.NotFound:
            return await ctx.send(
                embed=error_embed("Not Banned", f"No ban was found for user ID `{user_id}`.")
            )
        except discord.Forbidden:
            return await ctx.send(
                embed=error_embed("Missing Permissions", "I do not have permission to view bans.")
            )

        user = ban_entry.user
        try:
            await ctx.guild.unban(user, reason=f"[Case] {reason} | Mod: {ctx.author}")
        except discord.Forbidden:
            return await ctx.send(
                embed=error_embed("Missing Permissions", "I do not have permission to unban this user.")
            )
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed("Discord Error", str(e)))

        case_id = await self._insert_case(ctx.guild.id, user.id, ctx.author.id, "unban", reason)
        await self._bump_staff_stat(ctx.guild.id, ctx.author.id, "unban")

        embed = action_embed("unban", ctx.author, user, reason, case_id, guild_name=ctx.guild.name)
        await ctx.send(embed=embed)
        await self._send_log(ctx.guild, embed)

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.command(name="addnote", aliases=["staffnote", "snote"])
    async def note(
        self,
        ctx: commands.Context,
        member: Union[discord.Member, discord.User],
        *,
        note: str,
    ):
        """
        Add a staff-only note to a user's moderation history.

        Usage: `?addnote @user <note text>`
        Aliases: staffnote, snote
        """
        case_id = await self._insert_case(ctx.guild.id, member.id, ctx.author.id, "note", note)
        await self._bump_staff_stat(ctx.guild.id, ctx.author.id, "note")

        embed = action_embed("note", ctx.author, member, note, case_id, guild_name=ctx.guild.name)
        embed.title = f"📝 Note Added | Case #{case_id}"
        await ctx.send(embed=embed)
        await self._send_log(ctx.guild, embed)

    # ===========================================================================
    # History command
    # ===========================================================================

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.command(name="history", aliases=["modlogs", "cases"])
    async def history(
        self,
        ctx: commands.Context,
        member: Union[discord.Member, discord.User],
    ):
        """
        Display paginated moderation history for a user.

        Usage: `?history @user`
        """
        cases = await self._get_cases(ctx.guild.id, member.id)

        if not cases:
            embed = discord.Embed(
                title=f"📋 Moderation History — {member.display_name}",
                description=f"{member.mention} has a clean record. ✅",
                color=COLORS["history"],
            )
            embed.set_thumbnail(url=getattr(member.display_avatar, "url", None))
            return await ctx.send(embed=embed)

        # Split into pages of HISTORY_PAGE_SIZE
        chunks = [cases[i:i + HISTORY_PAGE_SIZE] for i in range(0, len(cases), HISTORY_PAGE_SIZE)]
        total_pages = len(chunks)
        embeds = [
            history_embed(member, chunk, page + 1, total_pages)
            for page, chunk in enumerate(chunks)
        ]

        if total_pages == 1:
            return await ctx.send(embed=embeds[0])

        view = HistoryView(author_id=ctx.author.id, embeds=embeds)
        msg = await ctx.send(embed=embeds[0], view=view)
        view.message = msg

    # ===========================================================================
    # Delete case command
    # ===========================================================================

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.command(name="delcase", aliases=["deletecase", "removecase", "expunge"])
    async def delcase(self, ctx: commands.Context, case_id: int):
        """
        Remove a moderation case from the history by its case number.

        Usage: `?delcase <case_id>`
        Use `?history @user` to find the case number.
        """
        case = await self.db.find_one(
            {"type": "case", "guild_id": str(ctx.guild.id), "case_id": case_id}
        )
        if not case:
            return await ctx.send(
                embed=error_embed("Case Not Found", f"No case `#{case_id}` was found in this server.")
            )

        action = case.get("action", "unknown")
        user_id = case.get("user_id", "?")
        mod_id = case.get("moderator_id", "?")
        reason = case.get("reason", "No reason provided.")
        ts = case.get("timestamp", 0)
        date_str = format_dt(ts) if ts else "Unknown date"

        emoji = ACTION_EMOJIS.get(action, "🔧")
        confirm_embed = discord.Embed(
            title=f"🗑️ Delete Case #{case_id}?",
            description=(
                f"{emoji} **Action:** {action.capitalize()}\n"
                f"**Target:** <@{user_id}>\n"
                f"**Moderator:** <@{mod_id}>\n"
                f"**Reason:** {reason}\n"
                f"**Date:** {date_str}\n\n"
                "⚠️ This cannot be undone."
            ),
            color=COLORS.get("delcase", 0xFF4444),
        )
        confirm_embed.set_footer(text="This will permanently remove the case from all records.")

        view = ConfirmView(author_id=ctx.author.id, timeout=30.0)
        msg = await ctx.send(embed=confirm_embed, view=view)
        view.message = msg
        await view.wait()

        if view.value is None:
            return await msg.edit(embed=error_embed("Timed Out", "Case deletion cancelled."), view=view)
        if not view.value:
            return await msg.edit(embed=error_embed("Cancelled", "Case deletion cancelled."), view=view)

        await self.db.delete_one(
            {"type": "case", "guild_id": str(ctx.guild.id), "case_id": case_id}
        )

        result = discord.Embed(
            title=f"🗑️ Case #{case_id} Deleted",
            description=(
                f"Case **#{case_id}** ({action.capitalize()} on <@{user_id}>) "
                f"has been permanently removed.\n**Deleted by:** {ctx.author.mention}"
            ),
            color=COLORS["success"],
            timestamp=datetime.now(tz=timezone.utc),
        )
        await msg.edit(embed=result, view=view)

        log_embed = discord.Embed(
            title=f"🗑️ Case #{case_id} Deleted",
            description=(
                f"**Original Action:** {emoji} {action.capitalize()}\n"
                f"**Original Target:** <@{user_id}>\n"
                f"**Original Reason:** {reason}\n"
                f"**Deleted by:** {ctx.author.mention} (`{ctx.author.id}`)"
            ),
            color=COLORS.get("delcase", 0xFF4444),
            timestamp=datetime.now(tz=timezone.utc),
        )
        await self._send_log(ctx.guild, log_embed)

    # ===========================================================================
    # Promote command
    # ===========================================================================

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @commands.command(name="promote")
    async def promote(
        self,
        ctx: commands.Context,
        member: discord.Member,
        role: Optional[discord.Role] = None,
        *,
        reason: str = "No reason provided.",
    ):
        """
        Promote a member to a staff role with confirmation.

        Usage: `?promote <@user|ID> [@role] [reason]`

        If no role is given and a rank ladder is configured, the next role up
        from the member's current ladder rank is used automatically.
        If staff roles are configured, the target role must be one of them.
        Requires ADMINISTRATOR permission level or a configured manager role.
        """
        cfg = await self._get_config(ctx.guild.id)
        manager_roles = cfg.get("manager_role_ids", [])
        is_admin = ctx.author.id == ctx.guild.owner_id or ctx.author.guild_permissions.administrator
        if manager_roles and not is_admin:
            if not await self._check_role_permission(ctx, manager_roles):
                return await ctx.send(
                    embed=error_embed(
                        "Insufficient Permissions",
                        "You need a configured manager role to use this command.",
                    )
                )

        # Auto-detect role from rank ladder if none provided
        if role is None:
            current = await self._get_member_ladder_role(ctx.guild, member)
            if current is None:
                rank_order = await self._get_rank_order(ctx.guild)
                if not rank_order:
                    return await ctx.send(
                        embed=error_embed(
                            "No Role Specified",
                            f"No rank ladder is configured. Either specify a role: "
                            f"`{ctx.prefix}promote @user @role` or set up a ladder with "
                            f"`{ctx.prefix}modstaff setrankorder`.",
                        )
                    )
                # Member has no ladder role — start them at the bottom
                role = ctx.guild.get_role(int(rank_order[0]))
                if role is None:
                    return await ctx.send(embed=error_embed("Role Not Found", "The lowest ladder role no longer exists."))
            else:
                role = await self._get_next_higher_role(ctx.guild, current)
                if role is None:
                    return await ctx.send(
                        embed=error_embed(
                            "Already at Top",
                            f"{member.mention} is already at the highest configured rank (**{current.name}**).",
                        )
                    )

        # Only enforce the staff-role whitelist when it has been configured.
        staff_role_ids = cfg.get("staff_role_ids", [])
        if staff_role_ids and str(role.id) not in staff_role_ids:
            return await ctx.send(
                embed=error_embed(
                    "Not a Staff Role",
                    f"{role.mention} is not in the configured staff role list.\n"
                    f"Add it with `{ctx.prefix}modstaff setstaffrole {role.mention}` first, "
                    f"or leave staff roles unconfigured to allow any role.",
                )
            )

        if role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
            return await ctx.send(
                embed=error_embed(
                    "Insufficient Hierarchy",
                    "You cannot promote someone to a role equal to or higher than your own.",
                )
            )

        # Capture current ladder role before promoting (needed for perk diff)
        old_ladder_role = await self._get_member_ladder_role(ctx.guild, member)

        # Build confirmation embed
        confirm_embed = discord.Embed(
            title="📈 Confirm Promotion",
            description=(
                f"Are you sure you want to promote {member.mention} to **{role.name}**?\n\n"
                f"**Reason:** {reason}"
            ),
            color=COLORS["promote"],
        )
        confirm_embed.set_thumbnail(url=getattr(member.display_avatar, "url", None))
        confirm_embed.set_footer(text="This action will be logged and stored.")

        view = ConfirmView(author_id=ctx.author.id, timeout=30.0)
        msg = await ctx.send(embed=confirm_embed, view=view)
        view.message = msg
        await view.wait()

        if view.value is None:
            return await msg.edit(
                embed=error_embed("Timed Out", "Promotion was cancelled — no response received."),
                view=view,
            )
        if not view.value:
            return await msg.edit(
                embed=error_embed("Cancelled", "Promotion was cancelled."),
                view=view,
            )

        # Perform the promotion
        try:
            await member.add_roles(role, reason=f"[Promotion] {reason} | Mod: {ctx.author}")
        except discord.Forbidden:
            return await msg.edit(
                embed=error_embed("Missing Permissions", "I cannot assign that role."),
                view=view,
            )
        except discord.HTTPException as e:
            return await msg.edit(embed=error_embed("Discord Error", str(e)), view=view)

        # Apply rank perks (LR/MR/department roles etc.)
        perks_added, perks_removed = await self._apply_rank_perks(
            ctx.guild, member, old_ladder_role, role
        )

        now_ts = time.time()

        # Update staff_data.
        # IMPORTANT: $setOnInsert only fires when the document is first created.
        # This means staff_since is preserved for existing staff members (re-promotions,
        # rank changes) — only set on their very first promotion.
        # We also store last_active here so the embed always has it regardless of
        # which document the stats embed reads from.
        await self.db.find_one_and_update(
            {"type": "staff_data", "guild_id": str(ctx.guild.id), "user_id": str(member.id)},
            {
                "$set": {
                    "current_rank": role.name,
                    "current_rank_id": str(role.id),
                    "rank_since": now_ts,
                    "last_active": now_ts,
                },
                "$setOnInsert": {"staff_since": now_ts},
                "$push": {
                    "promotions": {
                        "role": role.name,
                        "role_id": str(role.id),
                        "promoted_by": str(ctx.author.id),
                        "reason": reason,
                        "timestamp": now_ts,
                    }
                },
            },
            upsert=True,
        )

        # Update stats for the PROMOTER (their "promotions performed" count).
        # The PROMOTEE's own moderation stats are untouched — nothing resets.
        await self._bump_staff_stat(ctx.guild.id, ctx.author.id, "promote")

        # Also touch the promotee's staff_stats last_active so their profile
        # is marked as recently active.
        await self.db.find_one_and_update(
            {"type": "staff_stats", "guild_id": str(ctx.guild.id), "user_id": str(member.id)},
            {"$set": {"last_active": now_ts}},
            upsert=True,
        )

        case_id = await self._insert_case(ctx.guild.id, member.id, ctx.author.id, "promote", reason)

        # Add staff team role if configured and member doesn't already have it
        team_role_id = cfg.get("staff_team_role_id")
        team_role_added = False
        if team_role_id:
            team_role = ctx.guild.get_role(int(team_role_id))
            if team_role and team_role not in member.roles:
                try:
                    await member.add_roles(team_role, reason="Staff team role — promotion")
                    team_role_added = True
                except (discord.Forbidden, discord.HTTPException) as e:
                    logger.warning("Could not add staff team role: %s", e)

        promote_extra = [
            ("🏅 New Role", role.mention, True),
            ("📅 Promoted At", format_dt_long(now_ts), True),
        ]
        if team_role_added and team_role:
            promote_extra.append(("👥 Team Role", team_role.mention, True))
        if perks_added:
            promote_extra.append(("➕ Perks Added", " ".join(r.mention for r in perks_added), False))
        if perks_removed:
            promote_extra.append(("➖ Perks Removed", " ".join(r.mention for r in perks_removed), False))

        result_embed = action_embed(
            "promote", ctx.author, member, reason, case_id,
            extra_fields=promote_extra,
            guild_name=ctx.guild.name,
        )
        await msg.edit(embed=result_embed, view=view)
        await self._send_log(ctx.guild, result_embed)

        # Optional DM notification
        dm_desc = f"You have been promoted to **{role.name}** by {ctx.author.mention}.\n\n**Reason:** {reason}"
        if perks_added:
            dm_desc += f"\n\n**Roles added:** {', '.join(r.name for r in perks_added)}"
        if perks_removed:
            dm_desc += f"\n**Roles removed:** {', '.join(r.name for r in perks_removed)}"
        dm_embed = discord.Embed(
            title=f"📈 Congratulations! You have been promoted in {ctx.guild.name}",
            description=dm_desc,
            color=COLORS["promote"],
            timestamp=datetime.now(tz=timezone.utc),
        )
        dm_embed.set_thumbnail(url=ctx.guild.icon.url if ctx.guild.icon else None)
        await self._try_dm(member, dm_embed)

    # ===========================================================================
    # Demote command
    # ===========================================================================

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @commands.command(name="demote")
    async def demote(
        self,
        ctx: commands.Context,
        member: discord.Member,
        role: Optional[discord.Role] = None,
        replacement_role: Optional[discord.Role] = None,
        *,
        reason: str = "No reason provided.",
    ):
        """
        Demote a member by removing a staff role.

        Usage: `?demote <@user|ID> [@role] [@replacement_role] [reason]`

        If no role is given and a rank ladder is configured, the member's
        current highest ladder role is detected automatically.
        The next lower role in the ladder is assigned automatically unless
        you specify a replacement.
        """
        cfg = await self._get_config(ctx.guild.id)
        manager_roles = cfg.get("manager_role_ids", [])
        is_admin = ctx.author.id == ctx.guild.owner_id or ctx.author.guild_permissions.administrator
        if manager_roles and not is_admin:
            if not await self._check_role_permission(ctx, manager_roles):
                return await ctx.send(
                    embed=error_embed(
                        "Insufficient Permissions",
                        "You need a configured manager role to use this command.",
                    )
                )

        # Auto-detect role from rank ladder if none provided
        if role is None:
            role = await self._get_member_ladder_role(ctx.guild, member)
            if role is None:
                rank_order = await self._get_rank_order(ctx.guild)
                if not rank_order:
                    return await ctx.send(
                        embed=error_embed(
                            "No Role Specified",
                            f"No rank ladder is configured. Either specify a role: "
                            f"`{ctx.prefix}demote @user @role` or set up a ladder with "
                            f"`{ctx.prefix}modstaff setrankorder`.",
                        )
                    )
                return await ctx.send(
                    embed=error_embed(
                        "No Ladder Role Found",
                        f"{member.mention} does not hold any role from the rank ladder.",
                    )
                )

        if role not in member.roles:
            return await ctx.send(
                embed=error_embed("Role Not Found", f"{member.mention} does not have the role **{role.name}**.")
            )

        # Auto-detect replacement from rank ladder if none provided
        if replacement_role is None:
            replacement_role = await self._get_next_lower_role(ctx.guild, role)

        desc = f"Are you sure you want to demote {member.mention} by removing **{role.name}**?"
        if replacement_role:
            desc += f"\nThey will automatically be assigned **{replacement_role.name}** (next rank down)."
        desc += f"\n\n**Reason:** {reason}"

        confirm_embed = discord.Embed(
            title="📉 Confirm Demotion",
            description=desc,
            color=COLORS["demote"],
        )
        confirm_embed.set_thumbnail(url=getattr(member.display_avatar, "url", None))
        confirm_embed.set_footer(text="This action will be logged and stored.")

        view = ConfirmView(author_id=ctx.author.id, timeout=30.0)
        msg = await ctx.send(embed=confirm_embed, view=view)
        view.message = msg
        await view.wait()

        if view.value is None:
            return await msg.edit(
                embed=error_embed("Timed Out", "Demotion was cancelled — no response received."),
                view=view,
            )
        if not view.value:
            return await msg.edit(embed=error_embed("Cancelled", "Demotion was cancelled."), view=view)

        try:
            await member.remove_roles(role, reason=f"[Demotion] {reason} | Mod: {ctx.author}")
            if replacement_role:
                await member.add_roles(replacement_role, reason="Demotion — next rank down")
        except discord.Forbidden:
            return await msg.edit(
                embed=error_embed("Missing Permissions", "I cannot modify this member's roles."),
                view=view,
            )
        except discord.HTTPException as e:
            return await msg.edit(embed=error_embed("Discord Error", str(e)), view=view)

        # Apply rank perks (LR/MR/department roles etc.)
        perks_added, perks_removed = await self._apply_rank_perks(
            ctx.guild, member, role, replacement_role
        )

        now_ts = time.time()

        # Record demotion in database
        await self.db.find_one_and_update(
            {"type": "staff_data", "guild_id": str(ctx.guild.id), "user_id": str(member.id)},
            {
                "$set": {
                    "current_rank": replacement_role.name if replacement_role else "None",
                    "current_rank_id": str(replacement_role.id) if replacement_role else None,
                    "rank_since": now_ts,
                    "last_active": now_ts,
                },
                "$push": {
                    "demotions": {
                        "role_removed": role.name,
                        "role_id": str(role.id),
                        "replacement": replacement_role.name if replacement_role else None,
                        "demoted_by": str(ctx.author.id),
                        "reason": reason,
                        "timestamp": now_ts,
                    }
                },
            },
            upsert=True,
        )
        await self._bump_staff_stat(ctx.guild.id, ctx.author.id, "demote")

        case_id = await self._insert_case(ctx.guild.id, member.id, ctx.author.id, "demote", reason)

        extra = [("📉 Role Removed", role.mention, True)]
        if replacement_role:
            extra.append(("🔄 New Role", replacement_role.mention, True))
        if perks_added:
            extra.append(("➕ Perks Added", " ".join(r.mention for r in perks_added), False))
        if perks_removed:
            extra.append(("➖ Perks Removed", " ".join(r.mention for r in perks_removed), False))

        result_embed = action_embed(
            "demote", ctx.author, member, reason, case_id,
            extra_fields=extra,
            guild_name=ctx.guild.name,
        )
        await msg.edit(embed=result_embed, view=view)
        await self._send_log(ctx.guild, result_embed)

        # DM notification
        dm_desc = (
            f"You have been demoted from **{role.name}**"
            + (f" and assigned **{replacement_role.name}**" if replacement_role else "")
            + f".\n\n**Reason:** {reason}"
        )
        if perks_added:
            dm_desc += f"\n\n**Roles added:** {', '.join(r.name for r in perks_added)}"
        if perks_removed:
            dm_desc += f"\n**Roles removed:** {', '.join(r.name for r in perks_removed)}"
        dm_embed = discord.Embed(
            title=f"📉 Staff Update in {ctx.guild.name}",
            description=dm_desc,
            color=COLORS["demote"],
            timestamp=datetime.now(tz=timezone.utc),
        )
        await self._try_dm(member, dm_embed)

    # ===========================================================================
    # Termination command
    # ===========================================================================

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @commands.command(name="termination", aliases=["terminate", "fire"])
    async def termination(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        reason: str = "No reason provided.",
    ):
        """
        Terminate a staff member — removes ALL configured staff roles at once.

        Usage: `?termination @user [reason]`

        Requires a configured manager role (if set) or ADMINISTRATOR level.
        All staff roles defined via `?modstaff setstaffrole` are removed.
        """
        cfg = await self._get_config(ctx.guild.id)
        manager_roles = cfg.get("manager_role_ids", [])
        is_admin = ctx.author.id == ctx.guild.owner_id or ctx.author.guild_permissions.administrator
        if manager_roles and not is_admin:
            if not await self._check_role_permission(ctx, manager_roles):
                return await ctx.send(
                    embed=error_embed(
                        "Insufficient Permissions",
                        "You need a configured manager role to use this command.",
                    )
                )

        # Gather all configured staff roles the member currently has
        staff_role_ids = cfg.get("staff_role_ids", [])
        if not staff_role_ids:
            return await ctx.send(
                embed=error_embed(
                    "No Staff Roles Configured",
                    "No staff roles have been configured yet.\n"
                    f"Use `{ctx.prefix}modstaff setstaffrole @role` to add them first.",
                )
            )

        roles_to_remove = [r for r in member.roles if str(r.id) in staff_role_ids]

        if not roles_to_remove:
            return await ctx.send(
                embed=error_embed(
                    "No Staff Roles Found",
                    f"{member.mention} does not hold any of the configured staff roles.",
                )
            )

        # Also collect team role and department role for removal
        extra_remove: list[discord.Role] = []

        team_role_id = cfg.get("staff_team_role_id")
        team_role = ctx.guild.get_role(int(team_role_id)) if team_role_id else None
        if team_role and team_role in member.roles:
            extra_remove.append(team_role)

        # Fetch stored department role from staff_data
        staff_doc = await self._get_staff_doc(ctx.guild.id, member.id)
        dept_role_id = staff_doc.get("dept_role_id")
        dept_role = ctx.guild.get_role(int(dept_role_id)) if dept_role_id else None
        if dept_role and dept_role in member.roles and dept_role not in extra_remove:
            extra_remove.append(dept_role)

        all_roles_to_remove = roles_to_remove + [r for r in extra_remove if r not in roles_to_remove]
        roles_list = ", ".join(f"**{r.name}**" for r in all_roles_to_remove)

        confirm_embed = discord.Embed(
            title="🚫 Confirm Staff Termination",
            description=(
                f"You are about to **terminate** {member.mention}.\n\n"
                f"**Roles to be removed:**\n{roles_list}\n\n"
                f"**Reason:** {reason}\n\n"
                "⚠️ This will remove all staff, team, and department roles."
            ),
            color=COLORS["termination"],
        )
        confirm_embed.set_thumbnail(url=getattr(member.display_avatar, "url", None))
        confirm_embed.set_footer(text="This action will be logged and stored.")

        view = ConfirmView(author_id=ctx.author.id, timeout=30.0)
        msg = await ctx.send(embed=confirm_embed, view=view)
        view.message = msg
        await view.wait()

        if view.value is None:
            return await msg.edit(embed=error_embed("Timed Out", "Termination cancelled."), view=view)
        if not view.value:
            return await msg.edit(embed=error_embed("Cancelled", "Termination cancelled."), view=view)

        try:
            await member.remove_roles(*all_roles_to_remove, reason=f"[Termination] {reason} | Mod: {ctx.author}")
        except discord.Forbidden:
            return await msg.edit(
                embed=error_embed("Missing Permissions", "I cannot remove roles from this member."),
                view=view,
            )
        except discord.HTTPException as e:
            return await msg.edit(embed=error_embed("Discord Error", str(e)), view=view)

        now_ts = time.time()

        # Clear stored dept role and update rank in staff_data
        await self.db.find_one_and_update(
            {"type": "staff_data", "guild_id": str(ctx.guild.id), "user_id": str(member.id)},
            {
                "$set": {
                    "current_rank": "Terminated",
                    "current_rank_id": None,
                    "dept_role_id": None,
                    "last_active": now_ts,
                },
                "$push": {
                    "demotions": {
                        "role_removed": "ALL STAFF ROLES",
                        "roles": [str(r.id) for r in all_roles_to_remove],
                        "replacement": None,
                        "demoted_by": str(ctx.author.id),
                        "reason": f"[TERMINATION] {reason}",
                        "timestamp": now_ts,
                    }
                },
            },
            upsert=True,
        )
        await self._bump_staff_stat(ctx.guild.id, ctx.author.id, "demote")

        case_id = await self._insert_case(
            ctx.guild.id, member.id, ctx.author.id, "termination", reason
        )

        result_embed = discord.Embed(
            title=f"🚫 Staff Terminated | Case #{case_id}",
            color=COLORS["termination"],
            timestamp=datetime.now(tz=timezone.utc),
        )
        result_embed.add_field(name="👤 Member", value=f"{member.mention} (`{member.id}`)", inline=True)
        result_embed.add_field(name="🛡️ Terminated By", value=f"{ctx.author.mention}", inline=True)
        result_embed.add_field(name="📋 Reason", value=reason, inline=False)
        result_embed.add_field(name="🗑️ Roles Removed", value=roles_list, inline=False)
        result_embed.set_thumbnail(url=getattr(member.display_avatar, "url", None))
        result_embed.set_footer(text=ctx.guild.name)

        await msg.edit(embed=result_embed, view=view)
        await self._send_log(ctx.guild, result_embed)

        # DM the terminated member
        dm_embed = discord.Embed(
            title=f"🚫 You have been terminated from {ctx.guild.name}",
            description=f"All of your staff roles have been removed.\n\n**Reason:** {reason}",
            color=COLORS["termination"],
            timestamp=datetime.now(tz=timezone.utc),
        )
        dm_embed.set_thumbnail(url=ctx.guild.icon.url if ctx.guild.icon else None)
        await self._try_dm(member, dm_embed)

    # ===========================================================================
    # Staff statistics command
    # ===========================================================================

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.command(name="staffstats", aliases=["ss", "mystats"])
    async def staffstats(
        self,
        ctx: commands.Context,
        member: Optional[Union[discord.Member, discord.User]] = None,
    ):
        """
        Display comprehensive staff statistics for a member.

        Usage: `?staffstats [@user]`
        Defaults to the command author if no user is specified.

        Shows current rank, time in rank, staff since, last active,
        tickets handled, messages sent, moderation action counts,
        and full role promotion/demotion history.
        """
        target = member or ctx.author

        staff_doc = await self._get_staff_doc(ctx.guild.id, target.id)
        stats_doc = await self._get_stats_doc(ctx.guild.id, target.id)

        # Merge last_active: prefer staff_stats (most up-to-date), fall back to staff_data
        if not stats_doc.get("last_active") and staff_doc.get("last_active"):
            stats_doc = dict(stats_doc)
            stats_doc["last_active"] = staff_doc["last_active"]

        embed = stats_embed(target, ctx.guild, staff_doc, stats_doc)
        await ctx.send(embed=embed)

    # ===========================================================================
    # Staff leaderboard command
    # ===========================================================================

    async def _build_leaderboard_scores(
        self, guild_id: int, category: str
    ) -> list[dict]:
        """
        Compute leaderboard entries for a given category.
        Returns a list of {"user_id": str, "score": int} dicts, sorted descending.
        """
        cursor = self.db.find({"type": "staff_stats", "guild_id": str(guild_id)})
        docs = await cursor.to_list(length=None)

        entries = []

        for doc in docs:
            m = doc.get("moderation", {})
            t = doc.get("tickets", {})

            mod_total = sum(
                m.get(k, 0)
                for k in ("warn", "mute", "timeout", "kick", "ban", "softban", "unban", "note", "promote", "demote")
            )

            if category == "overall":
                score = mod_total + t.get("total", 0) + t.get("messages_sent", 0)
            elif category == "tickets":
                score = t.get("total", 0)
            elif category == "moderation":
                score = mod_total
            elif category == "messages":
                score = t.get("messages_sent", 0)
            elif category == "monthly":
                score = mod_total  # fallback — full monthly tracking requires per-timestamp indexing
            else:
                score = 0

            if score > 0:
                entries.append({"user_id": doc.get("user_id", "?"), "score": score})

        entries.sort(key=lambda x: x["score"], reverse=True)
        return entries

    async def _get_leaderboard_page(
        self, guild_id: int, category: str, page: int
    ):
        """Return (embed, total_pages) for a leaderboard page request."""
        guild = self.bot.get_guild(guild_id)
        all_entries = await self._build_leaderboard_scores(guild_id, category)
        total = len(all_entries)
        total_pages = max(1, math.ceil(total / LEADERBOARD_PAGE_SIZE))
        page = max(1, min(page, total_pages))
        slice_start = (page - 1) * LEADERBOARD_PAGE_SIZE
        page_entries = all_entries[slice_start:slice_start + LEADERBOARD_PAGE_SIZE]
        embed = leaderboard_embed(category, page_entries, guild, page, total_pages)
        return embed, total_pages

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.command(name="staffleaderboard", aliases=["slb", "leaderboard"])
    async def staffleaderboard(self, ctx: commands.Context):
        """
        Display the interactive staff leaderboard.

        Navigate between categories (Overall, Tickets, Moderation, Messages)
        and pages using the buttons below the embed.

        Usage: `?staffleaderboard`
        """
        guild_id = ctx.guild.id

        embed, total_pages = await self._get_leaderboard_page(guild_id, "overall", 1)

        async def get_page(category: str, page: int):
            return await self._get_leaderboard_page(guild_id, category, page)

        view = LeaderboardView(
            author_id=ctx.author.id,
            get_page_func=get_page,
            initial_embed=embed,
            initial_total_pages=total_pages,
        )
        msg = await ctx.send(embed=embed, view=view)
        view.message = msg

    # ===========================================================================
    # Configuration commands
    # ===========================================================================

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @commands.group(name="modstaff", invoke_without_command=True)
    async def modstaff_group(self, ctx: commands.Context):
        """
        ModStaff plugin configuration.

        Run `?modstaff` to see this help message.
        Subcommands: setlog, setcolor, setstaffrole, setmanager, setrankorder, showconfig, help
        """
        prefix = ctx.prefix
        embed = discord.Embed(
            title="⚙️ ModStaff Configuration",
            description="Use the subcommands below to configure the plugin.",
            color=COLORS["config"],
        )
        embed.add_field(
            name="Available Subcommands",
            value=(
                f"`{prefix}modstaff setlog <#channel>` — Set the moderation log channel\n"
                f"`{prefix}modstaff setcolor <action> <hex>` — Set embed color for an action\n"
                f"`{prefix}modstaff setstaffrole <@role>` — Add/remove a staff role\n"
                f"`{prefix}modstaff setmanager <@role>` — Add/remove a manager role\n"
                f"`{prefix}modstaff setteamrole [@role]` — Set/clear the global staff team role\n"
                f"`{prefix}modstaff setrankorder [@role1 @role2 ...]` — Set/view rank ladder (low → high)\n"
                f"`{prefix}modstaff clearrankorder` — Remove the rank ladder\n"
                f"`{prefix}modstaff setranktier @rank [@perk1 ...]` — Attach perk roles (LR/MR/dept) to a rank\n"
                f"`{prefix}modstaff clearranktier @rank` — Remove perk roles from a rank\n"
                f"`{prefix}modstaff showranktiers` — Show all configured rank tier perks\n"
                f"`{prefix}modstaff showconfig` — Show current plugin configuration\n"
                f"`{prefix}modstaff help` — Show this message\n\n"
                f"**Per-member commands:**\n"
                f"`{prefix}setdept @user [@role] [reason]` — Assign or remove a member's department role"
            ),
            inline=False,
        )
        embed.set_footer(text="ModStaff Plugin | Requires Administrator")
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @modstaff_group.command(name="setlog")
    async def setlog(self, ctx: commands.Context, channel: discord.TextChannel):
        """
        Set the channel where moderation actions are logged.

        Usage: `?modstaff setlog #channel`
        """
        await self._save_config(ctx.guild.id, {"log_channel_id": str(channel.id)})
        await ctx.send(
            embed=success_embed("Log Channel Set", f"Moderation actions will now be logged in {channel.mention}.")
        )

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @modstaff_group.command(name="setcolor")
    async def setcolor(self, ctx: commands.Context, action: str, color_hex: str):
        """
        Set a custom embed color for a moderation action.

        Usage: `?modstaff setcolor ban #FF0000`
        Valid actions: warn, mute, timeout, kick, ban, softban, unban, note, promote, demote
        """
        action = action.lower()
        if action not in COLORS:
            return await ctx.send(
                embed=error_embed("Invalid Action", f"Unknown action `{action}`. Valid: {', '.join(COLORS.keys())}")
            )
        color_hex = color_hex.lstrip("#")
        try:
            color_int = int(color_hex, 16)
        except ValueError:
            return await ctx.send(embed=error_embed("Invalid Color", "Provide a valid hex color, e.g. `#FF5733`."))

        cfg = await self._get_config(ctx.guild.id)
        embed_colors = cfg.get("embed_colors", {})
        embed_colors[action] = color_int
        await self._save_config(ctx.guild.id, {"embed_colors": embed_colors})
        await ctx.send(embed=success_embed("Color Updated", f"Color for **{action}** set to `#{color_hex.upper()}`."))

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @modstaff_group.command(name="setstaffrole")
    async def setstaffrole(self, ctx: commands.Context, role: discord.Role):
        """
        Toggle a role as a staff role (adds if not present, removes if present).

        Usage: `?modstaff setstaffrole @role`
        """
        cfg = await self._get_config(ctx.guild.id)
        staff_roles = cfg.get("staff_role_ids", [])
        role_id = str(role.id)

        if role_id in staff_roles:
            staff_roles.remove(role_id)
            msg = f"**{role.name}** removed from staff roles."
        else:
            staff_roles.append(role_id)
            msg = f"**{role.name}** added to staff roles."

        await self._save_config(ctx.guild.id, {"staff_role_ids": staff_roles})
        await ctx.send(embed=success_embed("Staff Role Updated", msg))

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @modstaff_group.command(name="setmanager")
    async def setmanager(self, ctx: commands.Context, role: discord.Role):
        """
        Toggle a role as a manager role (can use promote/demote commands).

        Usage: `?modstaff setmanager @role`
        """
        cfg = await self._get_config(ctx.guild.id)
        manager_roles = cfg.get("manager_role_ids", [])
        role_id = str(role.id)

        if role_id in manager_roles:
            manager_roles.remove(role_id)
            msg = f"**{role.name}** removed from manager roles."
        else:
            manager_roles.append(role_id)
            msg = f"**{role.name}** added to manager roles."

        await self._save_config(ctx.guild.id, {"manager_role_ids": manager_roles})
        await ctx.send(embed=success_embed("Manager Role Updated", msg))

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @modstaff_group.command(name="setrankorder")
    async def setrankorder(self, ctx: commands.Context):
        """
        Set the staff rank ladder used for automatic demotion role assignment.

        @mention roles in the message in order from LOWEST to HIGHEST rank.

        Usage: `?modstaff setrankorder @TrialMod @Moderator @SeniorMod @Admin`

        Works with roles that have spaces in their names.
        When `?demote` is used and no replacement role is specified, the plugin
        will automatically assign the next lower role in this ladder.
        Example: demoting a Senior Moderator → automatically assigns Moderator.

        Run with no roles mentioned to view the current ladder.
        Use `?modstaff clearrankorder` to remove the ladder entirely.
        """
        # Parse role IDs from message content in the order they were typed.
        # ctx.message.role_mentions is unordered (sorted by hierarchy), so we
        # extract <@&ID> tokens ourselves to preserve the user's intended order.
        mention_ids = re.findall(r"<@&(\d+)>", ctx.message.content)
        role_map = {str(r.id): r for r in ctx.message.role_mentions}
        roles = [role_map[rid] for rid in mention_ids if rid in role_map]

        if not roles:
            # Show current rank order instead of erroring
            cfg = await self._get_config(ctx.guild.id)
            rank_order = cfg.get("rank_order", [])
            if not rank_order:
                return await ctx.send(
                    embed=discord.Embed(
                        title="🪜 Rank Ladder",
                        description=(
                            "No rank ladder is configured.\n\n"
                            f"Set one with `{ctx.prefix}modstaff setrankorder @role1 @role2 ...` (lowest → highest)."
                        ),
                        color=COLORS.get("config", 0x5865F2),
                    )
                )
            ladder_display = "\n".join(
                f"`{i + 1}.` <@&{r_id}>" for i, r_id in enumerate(rank_order)
            )
            embed = discord.Embed(
                title="🪜 Current Rank Ladder",
                description=f"**Order (lowest → highest):**\n{ladder_display}",
                color=COLORS.get("config", 0x5865F2),
            )
            embed.set_footer(
                text=f"To change: {ctx.prefix}modstaff setrankorder @role1 @role2 ... | To clear: {ctx.prefix}modstaff clearrankorder"
            )
            return await ctx.send(embed=embed)

        role_ids = [str(r.id) for r in roles]
        await self._save_config(ctx.guild.id, {"rank_order": role_ids})

        ladder_display = "\n".join(
            f"`{i + 1}.` {r.mention}" for i, r in enumerate(roles)
        )
        embed = discord.Embed(
            title="🪜 Rank Ladder Configured",
            description=(
                f"Demotion will now automatically assign the next role down.\n\n"
                f"**Order (lowest → highest):**\n{ladder_display}"
            ),
            color=COLORS.get("success", 0x57F287),
        )
        embed.set_footer(text=f"Use {ctx.prefix}demote @user @role — the lower role is assigned automatically.")
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @modstaff_group.command(name="clearrankorder")
    async def clearrankorder(self, ctx: commands.Context):
        """
        Remove the configured staff rank ladder entirely.

        Usage: `?modstaff clearrankorder`
        """
        cfg = await self._get_config(ctx.guild.id)
        if not cfg.get("rank_order"):
            return await ctx.send(embed=error_embed("Nothing to Clear", "No rank ladder is currently configured."))
        await self._save_config(ctx.guild.id, {"rank_order": []})
        await ctx.send(embed=success_embed("Rank Ladder Cleared", "The rank ladder has been removed."))

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @modstaff_group.command(name="setranktier")
    async def setranktier(self, ctx: commands.Context):
        """
        Attach extra roles (LR, MR, department, etc.) to a rank.

        The FIRST role mentioned is the rank. All remaining mentions are the
        perk roles that get added when someone reaches that rank (and removed
        when they leave it, unless the next rank shares the same perk).

        Usage:
          `?modstaff setranktier @TrialMod @LowRank`
          `?modstaff setranktier @SeniorMod @MidRank @StaffDept`
          `?modstaff setranktier @TrialMod` — view current perks for that rank
          `?modstaff clearranktier @TrialMod` — remove all perks for that rank

        Perk roles are applied automatically during ?promote and ?demote.
        Only the *difference* between old and new perks is touched, so shared
        roles (e.g. LR on both Trial and Mod) are never removed mid-ladder.
        """
        mention_ids = re.findall(r"<@&(\d+)>", ctx.message.content)
        role_map = {str(r.id): r for r in ctx.message.role_mentions}
        mentions = [role_map[rid] for rid in mention_ids if rid in role_map]

        if not mentions:
            return await ctx.send(
                embed=error_embed(
                    "No Rank Specified",
                    f"Mention the rank role first, then its perk roles.\n"
                    f"Example: `{ctx.prefix}modstaff setranktier @TrialMod @LowRank`",
                )
            )

        rank_role = mentions[0]
        perk_roles = mentions[1:]

        cfg = await self._get_config(ctx.guild.id)
        rank_perks: dict = cfg.get("rank_perks", {})

        # View mode — only rank mentioned, no perks
        if not perk_roles:
            current = rank_perks.get(str(rank_role.id), [])
            if not current:
                return await ctx.send(
                    embed=discord.Embed(
                        title=f"🎖️ Rank Tier: {rank_role.name}",
                        description=(
                            f"No perk roles are configured for **{rank_role.name}**.\n\n"
                            f"Add some: `{ctx.prefix}modstaff setranktier {rank_role.mention} @PerkRole1 @PerkRole2`"
                        ),
                        color=COLORS.get("config", 0x5865F2),
                    )
                )
            perk_display = "\n".join(f"<@&{rid}>" for rid in current)
            return await ctx.send(
                embed=discord.Embed(
                    title=f"🎖️ Rank Tier: {rank_role.name}",
                    description=f"**Perk roles (auto-applied on promotion/demotion):**\n{perk_display}",
                    color=COLORS.get("config", 0x5865F2),
                )
            )

        rank_perks[str(rank_role.id)] = [str(r.id) for r in perk_roles]
        await self._save_config(ctx.guild.id, {"rank_perks": rank_perks})

        perk_display = "\n".join(f"• {r.mention}" for r in perk_roles)
        embed = discord.Embed(
            title=f"🎖️ Rank Tier Set: {rank_role.name}",
            description=(
                f"The following perk roles will be **added** when someone is promoted to "
                f"**{rank_role.name}**, and **removed** when they leave it "
                f"(unless the next rank shares the perk):\n\n{perk_display}"
            ),
            color=COLORS.get("success", 0x57F287),
        )
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @modstaff_group.command(name="clearranktier")
    async def clearranktier(self, ctx: commands.Context):
        """
        Remove all perk roles from a rank tier.

        Usage: `?modstaff clearranktier @RankRole`
        """
        mention_ids = re.findall(r"<@&(\d+)>", ctx.message.content)
        role_map = {str(r.id): r for r in ctx.message.role_mentions}
        mentions = [role_map[rid] for rid in mention_ids if rid in role_map]

        if not mentions:
            return await ctx.send(
                embed=error_embed("No Rank Specified", f"Mention the rank role to clear. Example: `{ctx.prefix}modstaff clearranktier @TrialMod`")
            )

        rank_role = mentions[0]
        cfg = await self._get_config(ctx.guild.id)
        rank_perks: dict = cfg.get("rank_perks", {})

        if str(rank_role.id) not in rank_perks:
            return await ctx.send(embed=error_embed("Nothing to Clear", f"**{rank_role.name}** has no configured perk roles."))

        del rank_perks[str(rank_role.id)]
        await self._save_config(ctx.guild.id, {"rank_perks": rank_perks})
        await ctx.send(embed=success_embed("Rank Tier Cleared", f"Perk roles for **{rank_role.name}** have been removed."))

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @modstaff_group.command(name="setteamrole")
    async def setteamrole(self, ctx: commands.Context):
        """
        Set (or clear) the staff team role given to ALL staff members on promotion.

        The team role is:
          • Added automatically whenever ?promote is used
          • Removed automatically on ?termination (along with rank + dept roles)

        Usage:
          `?modstaff setteamrole @StaffTeam` — set the role
          `?modstaff setteamrole` (no mention) — clear the configured team role
        """
        mention_ids = re.findall(r"<@&(\d+)>", ctx.message.content)
        role_map = {str(r.id): r for r in ctx.message.role_mentions}
        mentions = [role_map[rid] for rid in mention_ids if rid in role_map]

        if not mentions:
            cfg = await self._get_config(ctx.guild.id)
            current = cfg.get("staff_team_role_id")
            if not current:
                return await ctx.send(
                    embed=discord.Embed(
                        title="👥 Staff Team Role",
                        description=(
                            "No staff team role is configured.\n\n"
                            f"Set one with `{ctx.prefix}modstaff setteamrole @RoleHere`"
                        ),
                        color=COLORS.get("config", 0x5865F2),
                    )
                )
            await self._save_config(ctx.guild.id, {"staff_team_role_id": None})
            return await ctx.send(embed=success_embed("Team Role Cleared", "The staff team role has been removed."))

        role = mentions[0]
        await self._save_config(ctx.guild.id, {"staff_team_role_id": str(role.id)})
        await ctx.send(
            embed=success_embed(
                "Staff Team Role Set",
                f"{role.mention} will now be added to every member on `?promote` "
                f"and removed on `?termination`.",
            )
        )

    # ===========================================================================
    # setdept — per-member department role command
    # ===========================================================================

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @commands.command(name="setdept")
    async def setdept(
        self,
        ctx: commands.Context,
        member: discord.Member,
        role: Optional[discord.Role] = None,
        *,
        reason: str = "No reason provided.",
    ):
        """
        Assign or remove a department role for a specific staff member.

        Usage:
          `?setdept @user @DeptRole [reason]` — assign a department role
          `?setdept @user` (no role) — remove their current department role

        The department role is stored per-person and is automatically removed
        on `?termination` along with all other staff roles.
        Requires ADMINISTRATOR permission level or a configured manager role.
        """
        cfg = await self._get_config(ctx.guild.id)
        manager_roles = cfg.get("manager_role_ids", [])
        is_admin = ctx.author.id == ctx.guild.owner_id or ctx.author.guild_permissions.administrator
        if manager_roles and not is_admin:
            if not await self._check_role_permission(ctx, manager_roles):
                return await ctx.send(
                    embed=error_embed(
                        "Insufficient Permissions",
                        "You need a configured manager role to use this command.",
                    )
                )

        staff_doc = await self._get_staff_doc(ctx.guild.id, member.id)
        current_dept_id = staff_doc.get("dept_role_id")
        current_dept = ctx.guild.get_role(int(current_dept_id)) if current_dept_id else None

        # No role provided — remove current department role
        if role is None:
            if not current_dept:
                return await ctx.send(
                    embed=error_embed(
                        "No Department Role",
                        f"{member.mention} does not have a department role assigned.",
                    )
                )

            try:
                await member.remove_roles(current_dept, reason=f"[Dept Removed] {reason} | Mod: {ctx.author}")
            except discord.Forbidden:
                return await ctx.send(
                    embed=error_embed("Missing Permissions", "I cannot remove roles from this member.")
                )
            except discord.HTTPException as e:
                return await ctx.send(embed=error_embed("Discord Error", str(e)))

            await self.db.find_one_and_update(
                {"type": "staff_data", "guild_id": str(ctx.guild.id), "user_id": str(member.id)},
                {"$set": {"dept_role_id": None}},
                upsert=True,
            )

            embed = success_embed(
                "Department Role Removed",
                f"Removed {current_dept.mention} from {member.mention}.\n**Reason:** {reason}",
            )
            await ctx.send(embed=embed)
            await self._send_log(ctx.guild, discord.Embed(
                title="🏢 Department Role Removed",
                description=(
                    f"**Member:** {member.mention} (`{member.id}`)\n"
                    f"**Role Removed:** {current_dept.mention}\n"
                    f"**By:** {ctx.author.mention}\n"
                    f"**Reason:** {reason}"
                ),
                color=COLORS.get("demote", 0xFF4444),
                timestamp=datetime.now(tz=timezone.utc),
            ))
            return

        # Role provided — assign new department role
        # Remove old dept role first if different
        if current_dept and current_dept != role:
            try:
                await member.remove_roles(current_dept, reason="Department role replaced")
            except (discord.Forbidden, discord.HTTPException) as e:
                logger.warning("Could not remove old dept role for %s: %s", member.id, e)

        try:
            await member.add_roles(role, reason=f"[Dept Assigned] {reason} | Mod: {ctx.author}")
        except discord.Forbidden:
            return await ctx.send(
                embed=error_embed("Missing Permissions", "I cannot assign that role to this member.")
            )
        except discord.HTTPException as e:
            return await ctx.send(embed=error_embed("Discord Error", str(e)))

        await self.db.find_one_and_update(
            {"type": "staff_data", "guild_id": str(ctx.guild.id), "user_id": str(member.id)},
            {"$set": {"dept_role_id": str(role.id)}},
            upsert=True,
        )

        desc = f"Assigned {role.mention} to {member.mention} as their department role.\n**Reason:** {reason}"
        if current_dept and current_dept != role:
            desc += f"\n*(Replaced previous: {current_dept.mention})*"

        embed = success_embed("Department Role Set", desc)
        await ctx.send(embed=embed)
        await self._send_log(ctx.guild, discord.Embed(
            title="🏢 Department Role Assigned",
            description=(
                f"**Member:** {member.mention} (`{member.id}`)\n"
                f"**Role:** {role.mention}\n"
                f"**By:** {ctx.author.mention}\n"
                f"**Reason:** {reason}"
            ),
            color=COLORS.get("promote", 0x57F287),
            timestamp=datetime.now(tz=timezone.utc),
        ))

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @modstaff_group.command(name="showranktiers")
    async def showranktiers(self, ctx: commands.Context):
        """
        Show all configured rank tier perk roles.

        Usage: `?modstaff showranktiers`
        """
        cfg = await self._get_config(ctx.guild.id)
        rank_perks: dict = cfg.get("rank_perks", {})
        rank_order: list = cfg.get("rank_order", [])

        if not rank_perks:
            return await ctx.send(
                embed=discord.Embed(
                    title="🎖️ Rank Tier Perks",
                    description=(
                        "No rank tiers are configured.\n\n"
                        f"Set one with `{ctx.prefix}modstaff setranktier @RankRole @PerkRole1 ...`"
                    ),
                    color=COLORS.get("config", 0x5865F2),
                )
            )

        # Display in ladder order if available, then any extras
        ordered_ids = [rid for rid in rank_order if rid in rank_perks]
        unordered_ids = [rid for rid in rank_perks if rid not in rank_order]
        display_ids = ordered_ids + unordered_ids

        lines = []
        for rid in display_ids:
            perks = rank_perks[rid]
            perk_str = " ".join(f"<@&{p}>" for p in perks)
            lines.append(f"<@&{rid}> → {perk_str}")

        embed = discord.Embed(
            title="🎖️ Rank Tier Perks",
            description="\n".join(lines),
            color=COLORS.get("config", 0x5865F2),
        )
        embed.set_footer(text=f"Use {ctx.prefix}modstaff setranktier @rank @perk1 @perk2 to edit")
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @modstaff_group.command(name="showconfig")
    async def showconfig(self, ctx: commands.Context):
        """
        Display current plugin configuration for this guild.

        Usage: `?modstaff showconfig`
        """
        cfg = await self._get_config(ctx.guild.id)

        log_ch_id = cfg.get("log_channel_id")
        log_ch = ctx.guild.get_channel(int(log_ch_id)).mention if log_ch_id else "Not set"

        staff_role_ids = cfg.get("staff_role_ids", [])
        staff_roles = ", ".join(
            f"<@&{r}>" for r in staff_role_ids
        ) or "None configured (any role can be used in ?promote)"

        manager_role_ids = cfg.get("manager_role_ids", [])
        manager_roles = ", ".join(
            f"<@&{r}>" for r in manager_role_ids
        ) or "None configured (uses ADMINISTRATOR level)"

        team_role_id = cfg.get("staff_team_role_id")
        team_role_display = f"<@&{team_role_id}>" if team_role_id else "Not set"

        embed = discord.Embed(
            title="⚙️ ModStaff Configuration",
            color=COLORS["config"],
        )
        embed.add_field(name="📋 Log Channel", value=log_ch, inline=False)
        embed.add_field(name="👥 Staff Roles", value=staff_roles, inline=False)
        embed.add_field(name="🔑 Manager Roles", value=manager_roles, inline=False)
        embed.add_field(name="🏷️ Staff Team Role", value=team_role_display, inline=False)

        rank_order = cfg.get("rank_order", [])
        if rank_order:
            ladder_lines = "\n".join(
                f"`{i + 1}.` <@&{r_id}>" for i, r_id in enumerate(rank_order)
            )
            embed.add_field(name="🪜 Rank Ladder (low → high)", value=ladder_lines, inline=False)
        else:
            embed.add_field(name="🪜 Rank Ladder", value="Not configured — set with `?modstaff setrankorder`", inline=False)

        rank_perks = cfg.get("rank_perks", {})
        if rank_perks:
            ordered_ids = [rid for rid in rank_order if rid in rank_perks]
            unordered_ids = [rid for rid in rank_perks if rid not in rank_order]
            tier_lines = []
            for rid in ordered_ids + unordered_ids:
                perk_str = " ".join(f"<@&{p}>" for p in rank_perks[rid])
                tier_lines.append(f"<@&{rid}> → {perk_str}")
            embed.add_field(name="🎖️ Rank Tier Perks", value="\n".join(tier_lines), inline=False)
        else:
            embed.add_field(name="🎖️ Rank Tier Perks", value="Not configured — set with `?modstaff setranktier`", inline=False)

        custom_colors = cfg.get("embed_colors", {})
        if custom_colors:
            color_lines = "\n".join(
                f"`{action}` → `#{hex(val)[2:].upper().zfill(6)}`"
                for action, val in custom_colors.items()
            )
            embed.add_field(name="🎨 Custom Embed Colors", value=color_lines, inline=False)

        embed.set_footer(text=f"Guild: {ctx.guild.name}")
        await ctx.send(embed=embed)

    # ===========================================================================
    # Error handler
    # ===========================================================================

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """
        Global error handler for this cog.
        Provides friendly embeds for common error types.
        """
        # Only handle errors from this cog's commands
        if ctx.cog is not self:
            return

        if isinstance(error, commands.MissingRequiredArgument):
            embed = error_embed(
                "Missing Argument",
                f"Missing required argument: `{error.param.name}`.\n"
                f"Run `{ctx.prefix}help {ctx.command.qualified_name}` for usage.",
            )
            await ctx.send(embed=embed)

        elif isinstance(error, commands.BadArgument):
            embed = error_embed(
                "Invalid Argument",
                f"{error}\nRun `{ctx.prefix}help {ctx.command.qualified_name}` for usage.",
            )
            await ctx.send(embed=embed)

        elif isinstance(error, commands.MemberNotFound):
            await ctx.send(embed=error_embed("Member Not Found", f"No member found for `{error.argument}`."))

        elif isinstance(error, commands.UserNotFound):
            await ctx.send(embed=error_embed("User Not Found", f"No user found for `{error.argument}`."))

        elif isinstance(error, commands.RoleNotFound):
            await ctx.send(embed=error_embed("Role Not Found", f"No role found for `{error.argument}`."))

        elif isinstance(error, commands.ChannelNotFound):
            await ctx.send(embed=error_embed("Channel Not Found", f"No channel found for `{error.argument}`."))

        elif isinstance(error, commands.CheckFailure):
            await ctx.send(
                embed=error_embed(
                    "Insufficient Permissions",
                    "You do not have permission to use this command.",
                )
            )

        elif isinstance(error, commands.CommandInvokeError):
            original = error.original
            logger.error("Command error in %s: %s", ctx.command, original, exc_info=original)
            await ctx.send(
                embed=error_embed(
                    "Unexpected Error",
                    "An unexpected error occurred. It has been logged.\n"
                    f"```{type(original).__name__}: {original}```",
                )
            )

        else:
            logger.error("Unhandled command error: %s", error, exc_info=error)


# ===========================================================================
# Plugin setup — required entry point
# ===========================================================================

async def setup(bot):
    """Register the ModStaff cog with the Modmail bot."""
    await bot.add_cog(ModStaff(bot))
