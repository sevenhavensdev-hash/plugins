"""
Moderation Plugin for ModMail
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Warns, mutes, bans, softbans, kicks, mod logging,
staff statistics, leaderboard, promotions and demotions.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

COLORS = {
    "warn":    0xF0A500,
    "mute":    0x5865F2,
    "unmute":  0x57F287,
    "kick":    0xFB8C00,
    "ban":     0xED4245,
    "softban": 0xFF6B35,
    "unban":   0x57F287,
    "note":    0x95A5A6,
}

ACTION_LABELS = {
    "warn":    "Warning",
    "mute":    "Mute",
    "unmute":  "Unmute",
    "kick":    "Kick",
    "ban":     "Ban",
    "softban": "Softban",
    "unban":   "Unban",
    "note":    "Note",
}

# Full match: 1w2d3h4m5s — any combination, any order isn't supported,
# but all components are optional so "2h30m" or "7d" both work fine.
_TIME_RE = re.compile(
    r"^(?:(?P<weeks>\d+)\s*w(?:eeks?)?)?\s*"
    r"(?:(?P<days>\d+)\s*d(?:ays?)?)?\s*"
    r"(?:(?P<hours>\d+)\s*h(?:ours?)?)?\s*"
    r"(?:(?P<minutes>\d+)\s*m(?:in(?:utes?)?)?)?\s*"
    r"(?:(?P<seconds>\d+)\s*s(?:econds?)?)?$",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def parse_duration(s: str) -> Optional[timedelta]:
    m = _TIME_RE.match(s.strip())
    if not m or not any(m.group(k) for k in ("weeks", "days", "hours", "minutes", "seconds")):
        return None
    w  = int(m.group("weeks")   or 0)
    d  = int(m.group("days")    or 0)
    h  = int(m.group("hours")   or 0)
    mi = int(m.group("minutes") or 0)
    se = int(m.group("seconds") or 0)
    td = timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=se)
    return td if td.total_seconds() > 0 else None


def format_duration(td: timedelta) -> str:
    total = int(td.total_seconds())
    parts = []
    for unit, secs in (("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
        val, total = divmod(total, secs)
        if val:
            parts.append(f"{val} {unit}{'s' if val != 1 else ''}")
    return ", ".join(parts) or "0 seconds"


def err(msg: str) -> discord.Embed:
    return discord.Embed(description=f"❌ {msg}", color=0xED4245)


def ok(msg: str) -> discord.Embed:
    return discord.Embed(description=f"✅ {msg}", color=0x57F287)


# ──────────────────────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────────────────────

class Moderation(commands.Cog):
    """Moderation commands, logging, and staff tracking."""

    def __init__(self, bot):
        self.bot = bot
        self.db  = bot.plugin_db.get_partition(self)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _next_case(self) -> int:
        doc = await self.db.find_one({"_id": "meta"})
        n   = ((doc or {}).get("case_count", 0)) + 1
        await self.db.update_one(
            {"_id": "meta"},
            {"$set": {"case_count": n}},
            upsert=True,
        )
        return n

    async def _config(self) -> dict:
        return (await self.db.find_one({"_id": "config"})) or {}

    async def _bump_stats(self, mod_id: int, action: str) -> None:
        await self.db.update_one(
            {"_id": str(mod_id), "type": "stats"},
            {
                "$inc": {f"actions.{action}": 1, "total": 1},
                "$set": {"last_active": datetime.now(timezone.utc).isoformat()},
            },
            upsert=True,
        )

    async def _add_record(
        self,
        user_id: int,
        action: str,
        reason: str,
        mod_id: int,
        case: int,
        *,
        duration: Optional[timedelta] = None,
        extra: Optional[str] = None,
    ) -> None:
        entry = {
            "case":      case,
            "action":    action,
            "reason":    reason,
            "mod":       str(mod_id),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if duration:
            entry["duration"] = int(duration.total_seconds())
        if extra:
            entry["extra"] = extra

        await self.db.update_one(
            {"_id": str(user_id), "type": "history"},
            {"$push": {"records": entry}},
            upsert=True,
        )

    # Warn-specific helpers
    async def _add_warning(self, user_id: int, reason: str, mod_id: int, case: int) -> int:
        doc  = await self.db.find_one({"_id": str(user_id), "type": "warnings"})
        warns = (doc or {}).get("warnings", [])
        wid   = len(warns) + 1
        warns.append({
            "id":        wid,
            "case":      case,
            "reason":    reason,
            "mod":       str(mod_id),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "active":    True,
        })
        await self.db.update_one(
            {"_id": str(user_id), "type": "warnings"},
            {"$set": {"warnings": warns}},
            upsert=True,
        )
        return wid

    async def _get_warnings(self, user_id: int) -> list:
        doc = await self.db.find_one({"_id": str(user_id), "type": "warnings"})
        return (doc or {}).get("warnings", [])

    async def _get_history(self, user_id: int) -> list:
        doc = await self.db.find_one({"_id": str(user_id), "type": "history"})
        return (doc or {}).get("records", [])

    # Evidence prompt
    async def _evidence_prompt(self, ctx: commands.Context) -> Optional[str]:
        prompt = await ctx.send(
            embed=discord.Embed(
                description=(
                    "**Evidence** — Reply with an image link or paste a URL.\n"
                    "Type `skip` to leave this blank. *(60 second timeout)*"
                ),
                color=0x2B2D31,
            )
        )

        def check(m: discord.Message) -> bool:
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            reply = await self.bot.wait_for("message", check=check, timeout=60.0)
            await prompt.delete()
            content = reply.content.strip()
            return None if content.lower() in {"skip", "none", "-", "n/a"} else content
        except asyncio.TimeoutError:
            await prompt.delete()
            return None

    # Mod log embed
    async def _post_log(
        self,
        ctx: commands.Context,
        action: str,
        target: Union[discord.Member, discord.User],
        reason: str,
        case: int,
        *,
        duration: Optional[timedelta] = None,
        evidence: Optional[str]       = None,
        appealable: bool              = False,
        appeal_link: Optional[str]    = None,
    ) -> None:
        cfg = await self._config()
        ch_id = cfg.get("log_channel")
        if not ch_id:
            return
        channel = ctx.guild.get_channel(int(ch_id))
        if not channel:
            return

        color = COLORS.get(action, 0x2B2D31)
        label = ACTION_LABELS.get(action, action.title())

        issued = f"{label} ({format_duration(duration)})" if duration else label

        if appealable and appeal_link:
            appeal_val = f"[Submit Appeal]({appeal_link})"
        elif appealable:
            appeal_val = "Yes"
        else:
            appeal_val = "No"

        embed = discord.Embed(color=color, timestamp=datetime.now(timezone.utc))
        embed.set_author(
            name=f"Case #{case}  ·  {label}",
            icon_url=(ctx.guild.icon.url if ctx.guild.icon else discord.utils.MISSING),
        )
        embed.set_thumbnail(url=target.display_avatar.url)

        embed.add_field(name="User ID & Username",  value=f"`{target.id}` / {target}",   inline=False)
        embed.add_field(name="Moderation Issued",   value=issued,                          inline=False)
        embed.add_field(name="Reason",              value=reason or "No reason provided.", inline=False)
        embed.add_field(name="Appealability",       value=appeal_val,                      inline=False)
        embed.add_field(name="Evidence",            value=evidence or "None provided.",    inline=False)

        embed.set_footer(text=f"Issued by {ctx.author}  ·  ID {ctx.author.id}")

        await channel.send(embed=embed)

    # Hierarchy check (reusable)
    def _can_act_on(self, ctx: commands.Context, target: discord.Member) -> bool:
        if ctx.author == ctx.guild.owner:
            return True
        return ctx.author.top_role > target.top_role

    # ── Configuration ─────────────────────────────────────────────────────────

    @commands.group(name="modset", invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def modset(self, ctx: commands.Context):
        """Show current moderation plugin settings."""
        cfg = await self._config()

        log_ch = cfg.get("log_channel")
        appeal = cfg.get("appeal_link", "Not set")
        default_appealable = cfg.get("default_appealable", False)

        embed = discord.Embed(
            title="Moderation — Configuration",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Log Channel",
            value=f"<#{log_ch}>" if log_ch else "Not configured",
            inline=True,
        )
        embed.add_field(
            name="Appeal Link",
            value=appeal,
            inline=True,
        )
        embed.add_field(
            name="Default Appealability",
            value="Enabled" if default_appealable else "Disabled",
            inline=True,
        )
        embed.set_footer(text="Use subcommands to update settings.")
        await ctx.send(embed=embed)

    @modset.command(name="logchannel", aliases=["log"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def modset_logchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the channel where moderation logs are posted."""
        await self.db.update_one(
            {"_id": "config"},
            {"$set": {"log_channel": str(channel.id)}},
            upsert=True,
        )
        await ctx.send(embed=ok(f"Moderation logs will be posted in {channel.mention}."))

    @modset.command(name="appeal")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def modset_appeal(self, ctx: commands.Context, url: str):
        """Set the appeal form URL attached to bans and mutes."""
        await self.db.update_one(
            {"_id": "config"},
            {"$set": {"appeal_link": url}},
            upsert=True,
        )
        await ctx.send(embed=ok(f"Appeal link set: <{url}>"))

    @modset.command(name="appealable")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def modset_appealable(self, ctx: commands.Context, enabled: bool):
        """Set whether actions are appealable by default. (true/false)"""
        await self.db.update_one(
            {"_id": "config"},
            {"$set": {"default_appealable": enabled}},
            upsert=True,
        )
        await ctx.send(embed=ok(f"Default appealability set to **{'Yes' if enabled else 'No'}**."))

    # ── Warn ──────────────────────────────────────────────────────────────────

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str):
        """Issue a warning to a member."""
        if member.bot:
            return await ctx.send(embed=err("You cannot warn a bot."))
        if not self._can_act_on(ctx, member):
            return await ctx.send(embed=err("You cannot moderate someone with an equal or higher role."))

        case   = await self._next_case()
        wid    = await self._add_warning(member.id, reason, ctx.author.id, case)
        warns  = await self._get_warnings(member.id)
        active = sum(1 for w in warns if w.get("active"))

        await self._record_action_and_stats(ctx, "warn", member, reason, case)

        embed = discord.Embed(
            description=f"**{member}** has been warned — warning **#{wid}** ({active} active).",
            color=COLORS["warn"],
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Reason", value=reason)
        await ctx.send(embed=embed)

        evidence = await self._evidence_prompt(ctx)
        cfg      = await self._config()
        await self._post_log(
            ctx, "warn", member, reason, case,
            evidence=evidence,
            appealable=cfg.get("default_appealable", False),
            appeal_link=cfg.get("appeal_link"),
        )

        try:
            dm = discord.Embed(
                title=f"Warning — {ctx.guild.name}",
                description=f"You received a warning in **{ctx.guild.name}**.",
                color=COLORS["warn"],
            )
            dm.add_field(name="Reason",           value=reason,       inline=False)
            dm.add_field(name="Active Warnings",  value=str(active),  inline=True)
            dm.add_field(name="Warning Number",   value=f"#{wid}",    inline=True)
            await member.send(embed=dm)
        except discord.Forbidden:
            pass

    @commands.command(aliases=["warnings"])
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def warns(self, ctx: commands.Context, member: discord.Member):
        """View all warnings on record for a member."""
        warns  = await self._get_warnings(member.id)
        active = [w for w in warns if w.get("active")]

        embed = discord.Embed(
            title=f"Warnings — {member}",
            color=COLORS["warn"] if active else 0x57F287,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        if not warns:
            embed.description = "No warnings on record."
        else:
            for w in warns[-10:]:
                ts     = w.get("timestamp", "")[:10]
                status = "⚠️" if w.get("active") else "~~pardoned~~"
                mod    = ctx.guild.get_member(int(w["mod"]))
                by     = str(mod) if mod else f"ID {w['mod']}"
                embed.add_field(
                    name  = f"#{w['id']} {status} · Case #{w.get('case', '?')} · {ts}",
                    value = f"**Reason:** {w['reason']}\n**By:** {by}",
                    inline=False,
                )
            embed.set_footer(text=f"{len(active)} active · {len(warns)} total")

        await ctx.send(embed=embed)

    @commands.command(aliases=["delwarn"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def clearwarn(self, ctx: commands.Context, member: discord.Member, warn_id: int):
        """Pardon a specific warning by its number."""
        warns = await self._get_warnings(member.id)
        for w in warns:
            if w["id"] == warn_id:
                if not w.get("active"):
                    return await ctx.send(embed=err(f"Warning #{warn_id} is already pardoned."))
                w["active"] = False
                break
        else:
            return await ctx.send(embed=err(f"Warning #{warn_id} not found for **{member}**."))

        await self.db.update_one(
            {"_id": str(member.id), "type": "warnings"},
            {"$set": {"warnings": warns}},
        )
        await ctx.send(embed=ok(f"Warning #{warn_id} pardoned for **{member}**."))

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def clearwarns(self, ctx: commands.Context, member: discord.Member):
        """Clear all warnings for a member."""
        await self.db.update_one(
            {"_id": str(member.id), "type": "warnings"},
            {"$set": {"warnings": []}},
            upsert=True,
        )
        await ctx.send(embed=ok(f"All warnings cleared for **{member}**."))

    # ── Mute ──────────────────────────────────────────────────────────────────

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def mute(
        self,
        ctx: commands.Context,
        member: discord.Member,
        duration: str,
        *,
        reason: str = "No reason provided.",
    ):
        """Timeout a member. Duration examples: 10m · 2h · 7d (max 28d)."""
        if member.bot:
            return await ctx.send(embed=err("You cannot mute a bot."))
        if not self._can_act_on(ctx, member):
            return await ctx.send(embed=err("You cannot moderate someone with an equal or higher role."))

        td = parse_duration(duration)
        if not td:
            return await ctx.send(
                embed=err(f"`{duration}` is not a valid duration. Try `10m`, `2h`, `3d`, etc.")
            )
        if td.total_seconds() > 2419200:
            return await ctx.send(embed=err("Duration cannot exceed 28 days (Discord limit)."))
        if member.is_timed_out():
            return await ctx.send(embed=err(f"**{member}** is already muted."))

        until = datetime.now(timezone.utc) + td
        await member.timeout(until, reason=f"[{ctx.author}] {reason}")

        case = await self._next_case()
        await self._record_action_and_stats(ctx, "mute", member, reason, case, duration=td)

        embed = discord.Embed(
            description=f"**{member}** has been muted for **{format_duration(td)}**.",
            color=COLORS["mute"],
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Reason", value=reason)
        await ctx.send(embed=embed)

        evidence = await self._evidence_prompt(ctx)
        cfg      = await self._config()
        await self._post_log(
            ctx, "mute", member, reason, case,
            duration=td,
            evidence=evidence,
            appealable=cfg.get("default_appealable", False),
            appeal_link=cfg.get("appeal_link"),
        )

        try:
            dm = discord.Embed(
                title=f"Muted — {ctx.guild.name}",
                description=f"You have been muted in **{ctx.guild.name}** for **{format_duration(td)}**.",
                color=COLORS["mute"],
            )
            dm.add_field(name="Reason", value=reason)
            await member.send(embed=dm)
        except discord.Forbidden:
            pass

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def unmute(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided."):
        """Remove a timeout from a member."""
        if not member.is_timed_out():
            return await ctx.send(embed=err(f"**{member}** is not currently muted."))

        await member.timeout(None, reason=f"[{ctx.author}] {reason}")
        await self._bump_stats(ctx.author.id, "unmute")

        await ctx.send(embed=ok(f"**{member}** has been unmuted."))

    # ── Kick ──────────────────────────────────────────────────────────────────

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided."):
        """Kick a member from the server."""
        if member.bot:
            return await ctx.send(embed=err("You cannot kick a bot."))
        if not self._can_act_on(ctx, member):
            return await ctx.send(embed=err("You cannot moderate someone with an equal or higher role."))

        try:
            dm = discord.Embed(
                title=f"Kicked — {ctx.guild.name}",
                description=f"You have been kicked from **{ctx.guild.name}**.",
                color=COLORS["kick"],
            )
            dm.add_field(name="Reason", value=reason)
            await member.send(embed=dm)
        except discord.Forbidden:
            pass

        await member.kick(reason=f"[{ctx.author}] {reason}")
        case = await self._next_case()
        await self._record_action_and_stats(ctx, "kick", member, reason, case)

        embed = discord.Embed(
            description=f"**{member}** has been kicked.",
            color=COLORS["kick"],
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Reason", value=reason)
        await ctx.send(embed=embed)

        evidence = await self._evidence_prompt(ctx)
        cfg      = await self._config()
        await self._post_log(
            ctx, "kick", member, reason, case,
            evidence=evidence,
            appealable=cfg.get("default_appealable", False),
            appeal_link=cfg.get("appeal_link"),
        )

    # ── Ban ───────────────────────────────────────────────────────────────────

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def ban(
        self,
        ctx: commands.Context,
        target: Union[discord.Member, discord.User],
        delete_days: Optional[int] = 0,
        *,
        reason: str = "No reason provided.",
    ):
        """Ban a member. Optionally purge message history: `ban @user 7 reason`."""
        if isinstance(target, discord.Member):
            if not self._can_act_on(ctx, target):
                return await ctx.send(embed=err("You cannot moderate someone with an equal or higher role."))
            try:
                dm = discord.Embed(
                    title=f"Banned — {ctx.guild.name}",
                    description=f"You have been banned from **{ctx.guild.name}**.",
                    color=COLORS["ban"],
                )
                dm.add_field(name="Reason", value=reason)
                await target.send(embed=dm)
            except discord.Forbidden:
                pass

        delete_days = max(0, min(delete_days or 0, 7))
        await ctx.guild.ban(target, reason=f"[{ctx.author}] {reason}", delete_message_days=delete_days)

        case = await self._next_case()
        await self._record_action_and_stats(ctx, "ban", target, reason, case)

        embed = discord.Embed(
            description=f"**{target}** has been banned.",
            color=COLORS["ban"],
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Reason", value=reason)
        if delete_days:
            embed.add_field(name="Messages Deleted", value=f"{delete_days} day(s)", inline=True)
        await ctx.send(embed=embed)

        evidence = await self._evidence_prompt(ctx)
        cfg      = await self._config()
        await self._post_log(
            ctx, "ban", target, reason, case,
            evidence=evidence,
            appealable=True,
            appeal_link=cfg.get("appeal_link"),
        )

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def softban(
        self,
        ctx: commands.Context,
        member: discord.Member,
        delete_days: Optional[int] = 7,
        *,
        reason: str = "No reason provided.",
    ):
        """Softban: ban then immediately unban to purge recent messages."""
        if not self._can_act_on(ctx, member):
            return await ctx.send(embed=err("You cannot moderate someone with an equal or higher role."))

        delete_days = max(1, min(delete_days or 7, 7))

        try:
            dm = discord.Embed(
                title=f"Softbanned — {ctx.guild.name}",
                description=(
                    f"You have been softbanned from **{ctx.guild.name}**.\n"
                    "This is not a permanent ban — you may rejoin with an invite link."
                ),
                color=COLORS["softban"],
            )
            dm.add_field(name="Reason", value=reason)
            await member.send(embed=dm)
        except discord.Forbidden:
            pass

        await ctx.guild.ban(
            member,
            reason=f"[Softban] [{ctx.author}] {reason}",
            delete_message_days=delete_days,
        )
        await ctx.guild.unban(member, reason="Softban — automatic unban")

        case = await self._next_case()
        await self._record_action_and_stats(ctx, "softban", member, reason, case)

        embed = discord.Embed(
            description=(
                f"**{member}** has been softbanned "
                f"({delete_days} day{'s' if delete_days != 1 else ''} of messages purged)."
            ),
            color=COLORS["softban"],
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Reason", value=reason)
        await ctx.send(embed=embed)

        evidence = await self._evidence_prompt(ctx)
        cfg      = await self._config()
        await self._post_log(
            ctx, "softban", member, reason, case,
            evidence=evidence,
            appealable=True,
            appeal_link=cfg.get("appeal_link"),
        )

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def unban(self, ctx: commands.Context, user_id: int, *, reason: str = "No reason provided."):
        """Unban a user by their ID."""
        try:
            user = await self.bot.fetch_user(user_id)
        except discord.NotFound:
            return await ctx.send(embed=err(f"No user found with ID `{user_id}`."))

        try:
            await ctx.guild.unban(user, reason=f"[{ctx.author}] {reason}")
        except discord.NotFound:
            return await ctx.send(embed=err(f"**{user}** is not currently banned in this server."))

        await self._bump_stats(ctx.author.id, "unban")

        embed = discord.Embed(
            description=f"**{user}** has been unbanned.",
            color=COLORS["unban"],
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Reason", value=reason)
        await ctx.send(embed=embed)

    # ── Note ──────────────────────────────────────────────────────────────────

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def note(self, ctx: commands.Context, member: discord.Member, *, text: str):
        """Attach a private staff note to a member's record (not visible to user)."""
        case = await self._next_case()
        await self._add_record(member.id, "note", text, ctx.author.id, case)

        await ctx.send(
            embed=discord.Embed(
                description=f"Note added to **{member}**'s record (Case #{case}).",
                color=COLORS["note"],
                timestamp=datetime.now(timezone.utc),
            ).add_field(name="Note", value=text)
        )

    # ── History ───────────────────────────────────────────────────────────────

    @commands.command(aliases=["modlogs"])
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def history(self, ctx: commands.Context, member: discord.Member):
        """View the full moderation history for a member."""
        records = await self._get_history(member.id)
        warns   = await self._get_warnings(member.id)
        active  = sum(1 for w in warns if w.get("active"))

        embed = discord.Embed(
            title=f"Moderation History — {member}",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        counts = {}
        for r in records:
            counts[r["action"]] = counts.get(r["action"], 0) + 1

        summary_parts = [f"**{ACTION_LABELS.get(k, k)}s:** {v}" for k, v in counts.items()]
        embed.description = "  ·  ".join(summary_parts) if summary_parts else "No moderation history."

        if active:
            embed.add_field(name="Active Warnings", value=str(active), inline=True)

        for r in records[-8:]:
            ts    = r.get("timestamp", "")[:10]
            label = ACTION_LABELS.get(r["action"], r["action"].title())
            mod   = ctx.guild.get_member(int(r["mod"]))
            by    = str(mod) if mod else f"ID {r['mod']}"
            val   = f"**Reason:** {r['reason']}\n**By:** {by}"
            if r.get("duration"):
                val += f"\n**Duration:** {format_duration(timedelta(seconds=r['duration']))}"
            embed.add_field(
                name  = f"Case #{r['case']} — {label} · {ts}",
                value = val,
                inline=False,
            )

        embed.set_footer(text=f"{len(records)} total entries")
        await ctx.send(embed=embed)

    # ── Staff Stats ───────────────────────────────────────────────────────────

    @commands.command(aliases=["modstats"])
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def staffstats(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """View moderation statistics for a staff member."""
        member = member or ctx.author
        doc    = await self.db.find_one({"_id": str(member.id), "type": "stats"})

        embed = discord.Embed(
            title=f"Staff Statistics — {member}",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        if not doc or not doc.get("actions"):
            embed.description = "No actions on record for this staff member."
        else:
            actions = doc["actions"]
            total   = doc.get("total", 0)
            last    = doc.get("last_active", "")[:10] or "Never"

            rows = []
            for key in ("warn", "mute", "unmute", "kick", "ban", "softban", "unban"):
                count = actions.get(key, 0)
                if count:
                    bar   = "█" * min(count, 15) + ("+" if count > 15 else "")
                    rows.append(f"`{ACTION_LABELS[key]:<8}` {bar} **{count}**")

            embed.description = "\n".join(rows) if rows else "No actions recorded."
            embed.add_field(name="Total Actions", value=str(total), inline=True)
            embed.add_field(name="Last Active",   value=last,        inline=True)

        await ctx.send(embed=embed)

    @commands.command(aliases=["staffleaderboard", "modlb"])
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def stafflb(self, ctx: commands.Context):
        """Show the top active staff members ranked by moderation actions."""
        cursor = self.db.find({"type": "stats"})
        docs   = await cursor.to_list(length=100)
        docs.sort(key=lambda d: d.get("total", 0), reverse=True)
        docs   = [d for d in docs if d.get("total", 0) > 0]

        embed = discord.Embed(
            title="Staff Leaderboard",
            color=0xF0A500,
            timestamp=datetime.now(timezone.utc),
        )

        if not docs:
            embed.description = "No moderation actions have been recorded yet."
            return await ctx.send(embed=embed)

        medals = {0: "🥇", 1: "🥈", 2: "🥉"}
        lines  = []
        for i, doc in enumerate(docs[:10]):
            try:
                member = ctx.guild.get_member(int(doc["_id"])) or await self.bot.fetch_user(int(doc["_id"]))
                name   = str(member)
            except Exception:
                name = f"Unknown ({doc['_id']})"

            rank  = medals.get(i, f"`{i + 1:>2}.`")
            total = doc.get("total", 0)
            lines.append(f"{rank}  **{name}** — {total} action{'s' if total != 1 else ''}")

        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Showing top {len(lines)} of {len(docs)} active staff")
        await ctx.send(embed=embed)

    # ── Promotion / Demotion ──────────────────────────────────────────────────

    async def _post_staff_log(
        self,
        ctx: commands.Context,
        action: str,
        member: discord.Member,
        role: discord.Role,
        reason: str,
    ) -> None:
        """Post a promotion or demotion entry to the log channel."""
        cfg = await self._config()
        ch_id = cfg.get("log_channel")
        if not ch_id:
            return
        channel = ctx.guild.get_channel(int(ch_id))
        if not channel:
            return

        is_promote = action == "promote"
        color = 0x57F287 if is_promote else 0xED4245
        label = "Promotion" if is_promote else "Demotion"
        arrow = "⬆️" if is_promote else "⬇️"
        verb  = "promoted to" if is_promote else "demoted from"

        embed = discord.Embed(
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(
            name=f"{arrow} Staff {label}",
            icon_url=(ctx.guild.icon.url if ctx.guild.icon else discord.utils.MISSING),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="User ID & Username", value=f"`{member.id}` / {member}", inline=False)
        embed.add_field(name=label,                value=f"{arrow} {verb} **{role.name}**",  inline=False)
        embed.add_field(name="Reason",             value=reason,                              inline=False)
        embed.set_footer(text=f"Issued by {ctx.author}  ·  ID {ctx.author.id}")

        await channel.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def promote(
        self,
        ctx: commands.Context,
        member: discord.Member,
        role: discord.Role,
        *,
        reason: str = "Staff promotion.",
    ):
        """Add a role to a member as a promotion."""
        if role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send(embed=err("You cannot assign a role equal to or higher than your own."))
        if role in member.roles:
            return await ctx.send(embed=err(f"**{member}** already has **{role.name}**."))

        await member.add_roles(role, reason=f"[Promote] [{ctx.author}] {reason}")

        embed = discord.Embed(
            title="Staff Promotion",
            description=f"**{member}** has been promoted to **{role.name}**.",
            color=0x57F287,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Promoted By", value=str(ctx.author), inline=True)
        embed.add_field(name="Role",        value=role.name,       inline=True)
        embed.add_field(name="Reason",      value=reason,          inline=False)
        await ctx.send(embed=embed)

        await self._post_staff_log(ctx, "promote", member, role, reason)

        try:
            dm = discord.Embed(
                title=f"Promotion — {ctx.guild.name}",
                description=f"Congratulations! You have been promoted to **{role.name}** in **{ctx.guild.name}**.",
                color=0x57F287,
            )
            dm.add_field(name="Reason", value=reason)
            await member.send(embed=dm)
        except discord.Forbidden:
            pass

    @commands.command()
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def demote(
        self,
        ctx: commands.Context,
        member: discord.Member,
        role: discord.Role,
        *,
        reason: str = "Staff demotion.",
    ):
        """Remove a role from a member as a demotion."""
        if role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send(embed=err("You cannot remove a role equal to or higher than your own."))
        if role not in member.roles:
            return await ctx.send(embed=err(f"**{member}** does not have **{role.name}**."))

        await member.remove_roles(role, reason=f"[Demote] [{ctx.author}] {reason}")

        embed = discord.Embed(
            title="Staff Demotion",
            description=f"**{member}** has been demoted from **{role.name}**.",
            color=0xED4245,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Demoted By", value=str(ctx.author), inline=True)
        embed.add_field(name="Role",       value=role.name,       inline=True)
        embed.add_field(name="Reason",     value=reason,          inline=False)
        await ctx.send(embed=embed)

        await self._post_staff_log(ctx, "demote", member, role, reason)

        try:
            dm = discord.Embed(
                title=f"Demotion — {ctx.guild.name}",
                description=f"You have been demoted from **{role.name}** in **{ctx.guild.name}**.",
                color=0xED4245,
            )
            dm.add_field(name="Reason", value=reason)
            await member.send(embed=dm)
        except discord.Forbidden:
            pass

    # ── Internal stat helper (DRY wrapper) ────────────────────────────────────

    async def _record_action_and_stats(
        self,
        ctx: commands.Context,
        action: str,
        target: Union[discord.Member, discord.User],
        reason: str,
        case: int,
        *,
        duration: Optional[timedelta] = None,
    ) -> None:
        await asyncio.gather(
            self._bump_stats(ctx.author.id, action),
            self._add_record(target.id, action, reason, ctx.author.id, case, duration=duration),
        )


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

async def setup(bot) -> None:
    await bot.add_cog(Moderation(bot))
