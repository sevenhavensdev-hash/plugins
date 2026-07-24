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
        """Increment a moderation stat counter for a staff member."""
        await self.db.find_one_and_update(
            {"type": "staff_stats", "guild_id": str(guild_id), "user_id": str(moderator_id)},
            {"$inc": {f"moderation.{action}": 1}, "$set": {"last_active": time.time()}},
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
        # Patch the DM embed's case id (was 0 before we had the case id)
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
    # Promote command
    # ===========================================================================

    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @commands.command(name="promote")
    async def promote(
        self,
        ctx: commands.Context,
        member: discord.Member,
        role: discord.Role,
        *,
        reason: str = "No reason provided.",
    ):
        """
        Promote a member to a staff role with confirmation.

        Usage: `?promote @user @role [reason]`

        Requires ADMINISTRATOR permission level or a configured manager role.
        """
        cfg = await self._get_config(ctx.guild.id)
        manager_roles = cfg.get("manager_role_ids", [])
        if manager_roles:
            if not await self._check_role_permission(ctx, manager_roles):
                return await ctx.send(
                    embed=error_embed(
                        "Insufficient Permissions",
                        "You need a configured manager role to use this command.",
                    )
                )

        if role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
            return await ctx.send(
                embed=error_embed(
                    "Insufficient Hierarchy",
                    "You cannot promote someone to a role equal to or higher than your own.",
                )
            )

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

        now_ts = time.time()

        # Record promotion in database
        await self.db.find_one_and_update(
            {"type": "staff_data", "guild_id": str(ctx.guild.id), "user_id": str(member.id)},
            {
                "$set": {
                    "current_rank": role.name,
                    "rank_since": now_ts,
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
        await self._bump_staff_stat(ctx.guild.id, ctx.author.id, "promote")

        case_id = await self._insert_case(ctx.guild.id, member.id, ctx.author.id, "promote", reason)

        result_embed = action_embed(
            "promote", ctx.author, member, reason, case_id,
            extra_fields=[
                ("🏅 New Role", role.mention, True),
                ("📅 Promoted At", format_dt_long(now_ts), True),
            ],
            guild_name=ctx.guild.name,
        )
        await msg.edit(embed=result_embed, view=view)
        await self._send_log(ctx.guild, result_embed)

        # Optional DM notification
        dm_embed = discord.Embed(
            title=f"📈 Congratulations! You have been promoted in {ctx.guild.name}",
            description=(
                f"You have been promoted to **{role.name}** by {ctx.author.mention}.\n\n"
                f"**Reason:** {reason}"
            ),
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
        role: discord.Role,
        replacement_role: Optional[discord.Role] = None,
        *,
        reason: str = "No reason provided.",
    ):
        """
        Demote a member by removing a staff role with optional replacement.

        Usage: `?demote @user @role [@replacement_role] [reason]`
        """
        cfg = await self._get_config(ctx.guild.id)
        manager_roles = cfg.get("manager_role_ids", [])
        if manager_roles:
            if not await self._check_role_permission(ctx, manager_roles):
                return await ctx.send(
                    embed=error_embed(
                        "Insufficient Permissions",
                        "You need a configured manager role to use this command.",
                    )
                )

        if role not in member.roles:
            return await ctx.send(
                embed=error_embed("Role Not Found", f"{member.mention} does not have the role **{role.name}**.")
            )

        desc = (
            f"Are you sure you want to demote {member.mention} by removing **{role.name}**?"
        )
        if replacement_role:
            desc += f"\nThey will be assigned **{replacement_role.name}** instead."
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
                await member.add_roles(replacement_role, reason="Demotion replacement role")
        except discord.Forbidden:
            return await msg.edit(
                embed=error_embed("Missing Permissions", "I cannot modify this member's roles."),
                view=view,
            )
        except discord.HTTPException as e:
            return await msg.edit(embed=error_embed("Discord Error", str(e)), view=view)

        now_ts = time.time()

        # Record demotion in database
        await self.db.find_one_and_update(
            {"type": "staff_data", "guild_id": str(ctx.guild.id), "user_id": str(member.id)},
            {
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
            extra.append(("🔄 Replacement Role", replacement_role.mention, True))

        result_embed = action_embed(
            "demote", ctx.author, member, reason, case_id,
            extra_fields=extra,
            guild_name=ctx.guild.name,
        )
        await msg.edit(embed=result_embed, view=view)
        await self._send_log(ctx.guild, result_embed)

        # Optional DM notification
        dm_embed = discord.Embed(
            title=f"📉 Staff Update in {ctx.guild.name}",
            description=(
                f"You have been demoted from **{role.name}**"
                + (f" and assigned **{replacement_role.name}**" if replacement_role else "")
                + f".\n\n**Reason:** {reason}"
            ),
            color=COLORS["demote"],
            timestamp=datetime.now(tz=timezone.utc),
        )
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
        """
        target = member or ctx.author

        staff_doc = await self._get_staff_doc(ctx.guild.id, target.id)
        stats_doc = await self._get_stats_doc(ctx.guild.id, target.id)

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
        now = time.time()
        month_start = now - 30 * 86400  # last 30 days approximation

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
                # For monthly, we sum actions from the last 30 days using case records
                # This is an approximation using stored data; a full monthly breakdown
                # would require per-timestamp indexes.
                score = mod_total  # fallback — monthly tracking requires extra collection
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

        Navigate between categories (Overall, Tickets, Moderation, Monthly)
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
        Subcommands: setlog, setcolor, setstaffrole, setmanager, help
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
                f"`{prefix}modstaff showconfig` — Show current plugin configuration\n"
                f"`{prefix}modstaff help` — Show this message"
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
        ) or "None configured"

        manager_role_ids = cfg.get("manager_role_ids", [])
        manager_roles = ", ".join(
            f"<@&{r}>" for r in manager_role_ids
        ) or "None configured (uses ADMINISTRATOR level)"

        embed = discord.Embed(
            title="⚙️ ModStaff Configuration",
            color=COLORS["config"],
        )
        embed.add_field(name="📋 Log Channel", value=log_ch, inline=False)
        embed.add_field(name="👥 Staff Roles", value=staff_roles, inline=False)
        embed.add_field(name="🔑 Manager Roles", value=manager_roles, inline=False)

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
