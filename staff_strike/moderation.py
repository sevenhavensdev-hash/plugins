"""Staff Strike moderation cog for Modmail.

The cog deliberately uses Modmail's normal discord.py command and check APIs,
so it can be loaded with the same plugin loader as any other Modmail cog.
Moderation records are JSON-backed because Modmail plugins do not own a
database.  The stored schema is versioned and keeps enough information to
rebuild an audit trail after a restart.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import discord
from discord import ui
from discord.ext import commands, tasks

from core import checks
from core.models import PermissionLevel

from .staff_manager import (
    ACTION_COLORS,
    ACTION_ICONS,
    MOD_ACTIONS_FILE,
    _load,
    _save,
    format_duration,
    parse_duration,
    record_staff_action,
    remove_staff_action,
    ts,
)


BASE_DIR = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
HISTORY_FILE = __import__("os").path.join(BASE_DIR, "data", "moderation_history.json")
ACTIVE_BANS_FILE = __import__("os").path.join(BASE_DIR, "data", "active_bans.json")
CONFIG_FILE = __import__("os").path.join(BASE_DIR, "config.json")

MAX_TIMEOUT = timedelta(days=28)
SOFTBAN_DELETE_SECONDS = 7 * 24 * 60 * 60
HISTORY_ACTIONS = {"warn", "mute", "ban", "unmute", "unban"}
STAFF_MANAGEMENT_CONFIG_KEYS = (
    "STAFF_MANAGEMENT_ROLE_IDS",
    "HIGH_RANK_ROLE_IDS",
    "STAFF_MANAGEMENT_ROLE_ID",
    "HEAD_OF_STAFF_ROLE_ID",
    "ADMIN_ROLE_ID",
    "HEAD_ADMIN_ROLE_ID",
)
STAFF_ROLE_CONFIG_KEYS = (
    "STAFF_IDS",
    "TRIAL_MODERATOR_ROLE_ID",
    "MODERATOR_ROLE_ID",
    "SENIOR_MODERATOR_ROLE_ID",
    "STAFF_MANAGEMENT_ROLE_ID",
    "HEAD_OF_STAFF_ROLE_ID",
    "ADMIN_ROLE_ID",
    "HEAD_ADMIN_ROLE_ID",
)

MODERATION_COLORS = {
    "warn": 0xF1C40F,
    "mute": 0xE67E22,
    "softban": 0xC0392B,
    "ban": 0x992D22,
    "unmute": 0x2ECC71,
    "unban": 0x2ECC71,
}
MODERATION_ICONS = {
    "warn": "⚠️",
    "mute": "🔇",
    "softban": "🪃",
    "ban": "🔨",
    "unmute": "🔊",
    "unban": "🔓",
}
MODERATION_LABELS = {
    "warn": "Warning",
    "mute": "Timeout",
    "softban": "Softban",
    "ban": "Ban",
    "unmute": "Timeout Removed",
    "unban": "Ban Removed",
}


def _load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as file:
            import json

            value = json.load(file)
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _cfg_int(key: str, default: int = 0) -> int:
    import os

    value = _load_config().get(key)
    if isinstance(value, int) and value:
        return value
    environment_value = os.environ.get(key, "")
    return int(environment_value) if environment_value.isdigit() else default


def _cfg_ids(*keys: str) -> set[int]:
    import os

    result: set[int] = set()
    config = _load_config()
    for key in keys:
        value = config.get(key)
        if isinstance(value, list):
            result.update(int(item) for item in value if str(item).isdigit())
        elif isinstance(value, int) and value:
            result.add(value)
        else:
            result.update(
                int(item.strip())
                for item in os.environ.get(key, "").split(",")
                if item.strip().isdigit()
            )
    return result


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _display_name(user: discord.abc.User) -> str:
    return getattr(user, "display_name", str(user))


def _reason_text(reason: str) -> str:
    cleaned = " ".join(reason.split()).strip()
    return cleaned[:1000] if cleaned else "No reason provided."


async def staff_management_check(ctx: commands.Context) -> bool:
    """Modmail command check for Staff Management and higher."""
    author = ctx.author
    return isinstance(author, discord.Member) and bool(
        {role.id for role in author.roles}
        & _cfg_ids(*STAFF_MANAGEMENT_CONFIG_KEYS)
    )


class HistoryView(ui.View):
    """One moderation case per page, navigated with Discord buttons."""

    def __init__(
        self,
        cog: "ModerationCog",
        pages: List[discord.Embed],
        requester_id: int,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.pages = pages
        self.requester_id = requester_id
        self.current = 0

        self.previous = ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            custom_id=f"staffstrike:history:previous:{requester_id}",
            disabled=True,
        )
        self.next = ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            custom_id=f"staffstrike:history:next:{requester_id}",
            disabled=len(pages) <= 1,
        )
        self.page = ui.Button(
            label=f"1 / {len(pages)}",
            style=discord.ButtonStyle.secondary,
            custom_id=f"staffstrike:history:page:{requester_id}",
            disabled=True,
        )
        self.previous.callback = self._previous
        self.next.callback = self._next
        self.add_item(self.previous)
        self.add_item(self.page)
        self.add_item(self.next)

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        return isinstance(interaction.user, discord.Member) and bool(
            {role.id for role in interaction.user.roles}
            & _cfg_ids(*STAFF_ROLE_CONFIG_KEYS)
        )

    def _sync(self) -> None:
        self.previous.disabled = self.current == 0
        self.next.disabled = self.current == len(self.pages) - 1
        self.page.label = f"{self.current + 1} / {len(self.pages)}"

    async def _previous(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction):
            await interaction.response.send_message(
                "You cannot control another staff member's history view.",
                ephemeral=True,
            )
            return
        self.current = max(0, self.current - 1)
        self._sync()
        await interaction.response.edit_message(
            embed=self.pages[self.current], view=self
        )

    async def _next(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction):
            await interaction.response.send_message(
                "You cannot control another staff member's history view.",
                ephemeral=True,
            )
            return
        self.current = min(len(self.pages) - 1, self.current + 1)
        self._sync()
        await interaction.response.edit_message(
            embed=self.pages[self.current], view=self
        )


class WarningListView(ui.View):
    """Paged warning list with per-warning removal buttons."""

    PER_PAGE = 5

    def __init__(
        self,
        cog: "ModerationCog",
        target_id: int,
        warnings: List[dict],
        requester_id: int,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.target_id = target_id
        self.warnings = warnings
        self.requester_id = requester_id
        self.current = 0
        self._rebuild()

    @property
    def total_pages(self) -> int:
        return max(1, (len(self.warnings) + self.PER_PAGE - 1) // self.PER_PAGE)

    def _current_warnings(self) -> List[dict]:
        start = self.current * self.PER_PAGE
        return self.warnings[start : start + self.PER_PAGE]

    def _rebuild(self) -> None:
        self.clear_items()
        for record in self._current_warnings():
            case_id = str(record.get("case_id", "unknown"))
            button = ui.Button(
                label=f"Remove {case_id}",
                style=discord.ButtonStyle.danger,
                row=0,
                custom_id=f"staffstrike:warning:remove:{case_id}",
            )

            async def callback(
                interaction: discord.Interaction, case_id: str = case_id
            ) -> None:
                await self._remove_warning(interaction, case_id)

            button.callback = callback
            self.add_item(button)

        previous = ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            row=1,
            disabled=self.current == 0,
            custom_id=f"staffstrike:warnings:previous:{self.target_id}",
        )
        next_button = ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            row=1,
            disabled=self.current >= self.total_pages - 1,
            custom_id=f"staffstrike:warnings:next:{self.target_id}",
        )
        page = ui.Button(
            label=f"{self.current + 1} / {self.total_pages}",
            style=discord.ButtonStyle.secondary,
            row=1,
            disabled=True,
            custom_id=f"staffstrike:warnings:page:{self.target_id}",
        )
        previous.callback = self._previous
        next_button.callback = self._next
        self.add_item(previous)
        self.add_item(page)
        self.add_item(next_button)

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        return isinstance(interaction.user, discord.Member) and bool(
            {role.id for role in interaction.user.roles}
            & _cfg_ids(*STAFF_ROLE_CONFIG_KEYS)
        )

    async def _remove_warning(
        self, interaction: discord.Interaction, case_id: str
    ) -> None:
        if not await self._allowed(interaction):
            await interaction.response.send_message(
                "You do not have permission to remove warnings.",
                ephemeral=True,
            )
            return
        removed = await self.cog._remove_case(case_id, delete_log=True)
        if not removed:
            await interaction.response.send_message(
                "That warning is already gone.", ephemeral=True
            )
            return
        self.warnings = self.cog._warnings_for(self.target_id)
        self.current = min(self.current, self.total_pages - 1)
        self._rebuild()
        if self.warnings:
            embed = self.cog._warnings_embed(self.target_id, self.warnings, self.current)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            embed = discord.Embed(
                title="Warnings",
                description="No active warnings remain.",
                color=0x2ECC71,
                timestamp=_now(),
            )
            await interaction.response.edit_message(embed=embed, view=self)

    async def _previous(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction):
            await interaction.response.send_message(
                "You cannot control another staff member's warning view.",
                ephemeral=True,
            )
            return
        self.current = max(0, self.current - 1)
        self._rebuild()
        await interaction.response.edit_message(
            embed=self.cog._warnings_embed(
                self.target_id, self.warnings, self.current
            ),
            view=self,
        )

    async def _next(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction):
            await interaction.response.send_message(
                "You cannot control another staff member's warning view.",
                ephemeral=True,
            )
            return
        self.current = min(self.total_pages - 1, self.current + 1)
        self._rebuild()
        await interaction.response.edit_message(
            embed=self.cog._warnings_embed(
                self.target_id, self.warnings, self.current
            ),
            view=self,
        )


class ModerationCog(commands.Cog, name="Staff Strike Moderation"):
    """Moderation actions, case history, warning management, and expiry jobs."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.expire_punishments.start()

    async def cog_unload(self) -> None:
        self.expire_punishments.cancel()

    def _history(self) -> dict:
        return _load(HISTORY_FILE)

    def _active_bans(self) -> dict:
        return _load(ACTIVE_BANS_FILE)

    def _next_case_id(self) -> str:
        highest = 0
        for records in self._history().values():
            for record in records:
                match = re.fullmatch(r"SS-(\d+)", str(record.get("case_id", "")))
                if match:
                    highest = max(highest, int(match.group(1)))
        return f"SS-{highest + 1:06d}"

    def _staff_role_ids(self) -> set[int]:
        return _cfg_ids(*STAFF_ROLE_CONFIG_KEYS)

    def _is_staff(self, member: discord.abc.User) -> bool:
        return isinstance(member, discord.Member) and bool(
            {role.id for role in member.roles} & self._staff_role_ids()
        )

    def _is_staff_management(self, member: discord.abc.User) -> bool:
        return isinstance(member, discord.Member) and bool(
            {role.id for role in member.roles}
            & _cfg_ids(*STAFF_MANAGEMENT_CONFIG_KEYS)
        )

    async def _resolve_user(
        self, ctx: commands.Context, argument: str
    ) -> Optional[discord.User | discord.Member]:
        cleaned = argument.strip()
        if ctx.guild:
            try:
                return await commands.MemberConverter().convert(ctx, cleaned)
            except commands.MemberNotFound:
                pass

            if cleaned.isdigit():
                cached = ctx.guild.get_member(int(cleaned))
                if cached:
                    return cached

            lowered = cleaned.casefold()
            for member in ctx.guild.members:
                if (
                    str(member).casefold() == lowered
                    or member.name.casefold() == lowered
                    or member.display_name.casefold() == lowered
                ):
                    return member

        user_id = self._extract_id(cleaned)
        if user_id:
            try:
                return await self.bot.fetch_user(user_id)
            except discord.NotFound:
                return None

        try:
            return await commands.UserConverter().convert(ctx, cleaned)
        except commands.UserNotFound:
            return None

    @staticmethod
    def _extract_id(value: str) -> Optional[int]:
        match = re.fullmatch(r"<@!?(\d{17,20})>", value)
        if match:
            return int(match.group(1))
        if value.isdigit() and 17 <= len(value) <= 20:
            return int(value)
        return None

    async def _target_member(
        self, ctx: commands.Context, argument: str
    ) -> Optional[discord.Member]:
        target = await self._resolve_user(ctx, argument)
        return target if isinstance(target, discord.Member) else None

    def _can_act_on(
        self, ctx: commands.Context, target: discord.Member
    ) -> Tuple[bool, str]:
        if target.id == ctx.author.id:
            return False, "You cannot use this moderation action on yourself."
        if target.bot:
            return False, "Bot accounts are not valid moderation targets."
        guild_me = ctx.guild.me if ctx.guild else None
        if guild_me and target.id == guild_me.id:
            return False, "I cannot moderate myself."
        if guild_me and target.top_role >= guild_me.top_role:
            return False, "My highest role must be above the target's highest role."
        if isinstance(ctx.author, discord.Member) and target.top_role >= ctx.author.top_role:
            return False, "Your highest role must be above the target's highest role."
        return True, ""

    def _append_record(self, target: discord.abc.User, record: dict) -> None:
        history = self._history()
        history.setdefault(str(target.id), []).append(record)
        _save(HISTORY_FILE, history)

    def _records_for(self, user_id: int) -> List[dict]:
        return list(self._history().get(str(user_id), []))

    def _warnings_for(self, user_id: int) -> List[dict]:
        return [
            record
            for record in self._records_for(user_id)
            if record.get("action") == "warn"
        ]

    def _base_record(
        self,
        target: discord.abc.User,
        moderator: discord.abc.User,
        action: str,
        reason: str,
        expires_at: Optional[datetime] = None,
    ) -> dict:
        now = _now()
        return {
            "schema_version": 1,
            "case_id": self._next_case_id(),
            "action": action,
            "user_id": str(target.id),
            "user_tag": str(target),
            "moderator_id": str(moderator.id),
            "moderator_tag": str(moderator),
            "reason": _reason_text(reason),
            "created_at": now.isoformat(),
            "expires_at": _iso(expires_at),
            "active": True,
            "guild_id": str(getattr(getattr(moderator, "guild", None), "id", "")),
            "log_channel_id": None,
            "log_message_id": None,
            "log_url": None,
        }

    def _history_embed(self, record: dict) -> discord.Embed:
        action = str(record.get("action", "unknown"))
        label = MODERATION_LABELS.get(action, action.title())
        embed = discord.Embed(
            title=f"{MODERATION_ICONS.get(action, '•')}  {label} — {record.get('case_id', 'Unknown')}",
            color=MODERATION_COLORS.get(action, 0x5865F2),
            timestamp=_parse_iso(record.get("created_at")) or _now(),
        )
        target_id = record.get("user_id", "unknown")
        moderator_id = record.get("moderator_id", "unknown")
        embed.add_field(
            name="Target",
            value=f"<@{target_id}>\n`{record.get('user_tag', target_id)}`",
            inline=True,
        )
        embed.add_field(
            name="Moderator",
            value=f"<@{moderator_id}>\n`{record.get('moderator_tag', moderator_id)}`",
            inline=True,
        )
        embed.add_field(name="Reason", value=record.get("reason", "No reason provided."), inline=False)
        expires = _parse_iso(record.get("expires_at"))
        if expires:
            status = "Active until " + ts(expires, "F") if record.get("active") else "Expired"
            embed.add_field(name="Duration", value=status, inline=False)
        elif action in {"mute", "ban"}:
            embed.add_field(name="Duration", value="Permanent", inline=False)
        state = "Active" if record.get("active", True) else "Removed"
        embed.set_footer(
            text=f"{state} • Log message: {record.get('log_message_id') or 'not posted'}"
        )
        return embed

    def _history_pages(self, target: discord.abc.User) -> List[discord.Embed]:
        records = self._records_for(target.id)
        if not records:
            return [
                discord.Embed(
                    title=f"Moderation History — {_display_name(target)}",
                    description="No moderation history was found for this user.",
                    color=0x95A5A6,
                    timestamp=_now(),
                )
            ]
        pages: List[discord.Embed] = []
        for record in records:
            embed = self._history_embed(record)
            embed.set_author(name=str(target), icon_url=target.display_avatar.url)
            embed.set_thumbnail(url=target.display_avatar.url)
            pages.append(embed)
        return pages

    def _warnings_embed(
        self, target_id: int, warnings: List[dict], page: int = 0
    ) -> discord.Embed:
        target = self.bot.get_user(target_id)
        start = page * WarningListView.PER_PAGE
        current = warnings[start : start + WarningListView.PER_PAGE]
        embed = discord.Embed(
            title=f"Warnings — {target or target_id}",
            description=(
                f"Active warnings: **{len(warnings)}**\n"
                "Use the buttons below to remove a warning."
            ),
            color=0xF1C40F,
            timestamp=_now(),
        )
        if target:
            embed.set_thumbnail(url=target.display_avatar.url)
        for record in current:
            created = _parse_iso(record.get("created_at"))
            moderator = record.get("moderator_id", "unknown")
            embed.add_field(
                name=f"{record.get('case_id', 'Unknown')} • {ts(created, 'R') if created else 'unknown time'}",
                value=(
                    f"**Reason:** {record.get('reason', 'No reason provided.')}\n"
                    f"**Moderator:** <@{moderator}>"
                ),
                inline=False,
            )
        embed.set_footer(
            text=f"User ID: {target_id} • Page {page + 1}/{max(1, (len(warnings) + 4) // 5)}"
        )
        return embed

    async def _post_log(self, record: dict, ctx: Optional[commands.Context]) -> None:
        channel_id = _cfg_int("MOD_ACTION_LOG_CHANNEL")
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            moderator_id = int(record["moderator_id"])
            if record["action"] in {"warn", "mute", "ban", "softban"}:
                record_staff_action(moderator_id, record["action"])
            return

        action = str(record["action"])
        embed = self._history_embed(record)
        embed.title = (
            f"{MODERATION_ICONS.get(action, '•')}  Moderation Action — "
            f"{MODERATION_LABELS.get(action, action.title())}"
        )
        embed.add_field(
            name="Source",
            value=ctx.message.jump_url if ctx and getattr(ctx, "message", None) else "Staff Strike command",
            inline=False,
        )
        embed.set_footer(
            text=(
                f"case:{record['case_id']} • mod:{record['moderator_id']} • "
                f"act:{action} • target:{record['user_id']}"
            ),
            icon_url=self.bot.user.display_avatar.url if self.bot.user else None,
        )
        message = await channel.send(embed=embed)
        record["log_channel_id"] = str(channel.id)
        record["log_message_id"] = str(message.id)
        record["log_url"] = message.jump_url
        history = self._history()
        for existing in history.get(str(record["user_id"]), []):
            if existing.get("case_id") == record.get("case_id"):
                existing.update(record)
                break
        _save(HISTORY_FILE, history)

        if action in {"warn", "mute", "ban", "softban"}:
            record_staff_action(int(record["moderator_id"]), action, message.jump_url)

        try:
            await message.create_thread(
                name=f"Evidence — {MODERATION_LABELS.get(action, action.title())}"[:100],
                auto_archive_duration=10080,
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _confirmation(
        self,
        ctx: commands.Context,
        record: dict,
        description: Optional[str] = None,
    ) -> None:
        embed = discord.Embed(
            title=f"{MODERATION_ICONS.get(record['action'], '•')}  {MODERATION_LABELS.get(record['action'], record['action'].title())}",
            description=description
            or f"Case `{record['case_id']}` has been recorded.",
            color=MODERATION_COLORS.get(record["action"], 0x5865F2),
            timestamp=_now(),
        )
        embed.add_field(name="Target", value=f"<@{record['user_id']}>", inline=True)
        embed.add_field(name="Case", value=f"`{record['case_id']}`", inline=True)
        embed.add_field(name="Reason", value=record["reason"], inline=False)
        await ctx.send(embed=embed)

    async def _remove_case(
        self, case_id: str, *, delete_log: bool = False
    ) -> Optional[dict]:
        history = self._history()
        for user_id, records in history.items():
            for index, record in enumerate(records):
                if str(record.get("case_id")) != str(case_id):
                    continue
                removed = records.pop(index)
                _save(HISTORY_FILE, history)
                if delete_log and record.get("log_channel_id") and record.get("log_message_id"):
                    channel = self.bot.get_channel(int(record["log_channel_id"]))
                    if isinstance(channel, discord.TextChannel):
                        try:
                            message = await channel.fetch_message(int(record["log_message_id"]))
                            await message.delete()
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            pass
                if record.get("action") in {"warn", "mute", "ban", "softban"}:
                    remove_staff_action(
                        int(record.get("moderator_id", 0)),
                        str(record["action"]),
                        record.get("log_url"),
                    )
                return removed
        return None

    async def _record_reversal(
        self,
        ctx: commands.Context,
        target: discord.abc.User,
        action: str,
        reason: str,
        related_case: Optional[str] = None,
    ) -> dict:
        record = self._base_record(target, ctx.author, action, reason)
        record["active"] = False
        record["related_case_id"] = related_case
        self._append_record(target, record)
        await self._post_log(record, ctx)
        return record

    @tasks.loop(minutes=1)
    async def expire_punishments(self) -> None:
        now = _now()
        active_bans = self._active_bans()
        changed_bans = False
        history = self._history()
        changed_history = False

        for key, ban in list(active_bans.items()):
            expires = _parse_iso(ban.get("expires_at"))
            if not expires or expires > now:
                continue
            guild = self.bot.get_guild(int(ban.get("guild_id", 0)))
            if not guild:
                continue
            user_id = int(ban["user_id"])
            try:
                await guild.unban(
                    discord.Object(id=user_id),
                    reason="Temporary Staff Strike ban expired",
                )
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                continue
            active_bans.pop(key, None)
            changed_bans = True
            for record in history.get(str(user_id), []):
                if record.get("case_id") == ban.get("case_id"):
                    record["active"] = False
                    record["expired_at"] = now.isoformat()
                    changed_history = True
                    break

        for user_id, records in history.items():
            for record in records:
                mute_expires = _parse_iso(record.get("expires_at"))
                if (
                    record.get("action") == "mute"
                    and record.get("active")
                    and mute_expires is not None
                    and mute_expires <= now
                ):
                    record["active"] = False
                    record["expired_at"] = now.isoformat()
                    changed_history = True

        if changed_bans:
            _save(ACTIVE_BANS_FILE, active_bans)
        if changed_history:
            _save(HISTORY_FILE, history)

    @expire_punishments.before_loop
    async def _before_expiry(self) -> None:
        await self.bot.wait_until_ready()

    @commands.command(name="history")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def history(self, ctx: commands.Context, target: str) -> None:
        """Show one moderation case per page for a user."""
        if not ctx.guild:
            return
        user = await self._resolve_user(ctx, target)
        if not user:
            await ctx.send("I could not find that user.", delete_after=10)
            return
        pages = self._history_pages(user)
        await ctx.send(
            embed=pages[0],
            view=HistoryView(self, pages, ctx.author.id) if len(pages) > 1 else None,
        )

    @commands.command(name="warnings", aliases=["warns"])
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def warnings(self, ctx: commands.Context, target: str) -> None:
        """Show active warnings with per-warning remove buttons."""
        user = await self._resolve_user(ctx, target)
        if not user:
            await ctx.send("I could not find that user.", delete_after=10)
            return
        warning_records = self._warnings_for(user.id)
        if not warning_records:
            await ctx.send(
                embed=discord.Embed(
                    title=f"Warnings — {_display_name(user)}",
                    description="No active warnings were found.",
                    color=0x2ECC71,
                    timestamp=_now(),
                )
            )
            return
        view = WarningListView(self, user.id, warning_records, ctx.author.id)
        await ctx.send(
            embed=self._warnings_embed(user.id, warning_records),
            view=view,
        )

    @commands.command(name="warn")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def warn(self, ctx: commands.Context, target: str, *, reason: str) -> None:
        """Warn a member and create a warning case."""
        if not ctx.guild:
            return
        user = await self._resolve_user(ctx, target)
        if not user:
            await ctx.send("I could not find that user.", delete_after=10)
            return
        if isinstance(user, discord.Member):
            allowed, error = self._can_act_on(ctx, user)
            if not allowed:
                await ctx.send(error, delete_after=10)
                return
        record = self._base_record(user, ctx.author, "warn", reason)
        self._append_record(user, record)
        await self._post_log(record, ctx)
        await self._confirmation(ctx, record, f"{user.mention} has been warned.")

    @commands.command(name="mute", aliases=["timeout"])
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def mute(self, ctx: commands.Context, target: str, *, details: str) -> None:
        """Timeout a member. The final token is the duration."""
        member = await self._target_member(ctx, target)
        if not member:
            await ctx.send("Mute requires a current server member.", delete_after=10)
            return
        allowed, error = self._can_act_on(ctx, member)
        if not allowed:
            await ctx.send(error, delete_after=10)
            return
        pieces = details.rsplit(None, 1)
        if len(pieces) != 2:
            await ctx.send(
                "Usage: `mute <user> <reason> <duration>` — examples: `30m`, `2h`, `7d`.",
                delete_after=12,
            )
            return
        reason, duration_text = pieces
        duration = parse_duration(duration_text)
        if not duration or duration > MAX_TIMEOUT:
            await ctx.send(
                "Timeout duration must be between 1 second and 28 days.",
                delete_after=10,
            )
            return
        expires = _now() + duration
        try:
            await member.timeout(expires, reason=_reason_text(reason))
        except discord.Forbidden:
            await ctx.send("I do not have permission to timeout that member.", delete_after=10)
            return
        record = self._base_record(member, ctx.author, "mute", reason, expires)
        self._append_record(member, record)
        await self._post_log(record, ctx)
        await self._confirmation(
            ctx, record, f"{member.mention} has been timed out for **{format_duration(duration)}**."
        )

    @commands.command(name="softban")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def softban(self, ctx: commands.Context, target: str, *, reason: str) -> None:
        """Ban and immediately unban a member, deleting up to Discord's seven-day message limit."""
        if not ctx.guild:
            return
        member = await self._target_member(ctx, target)
        if not member:
            await ctx.send("Softban requires a current server member.", delete_after=10)
            return
        allowed, error = self._can_act_on(ctx, member)
        if not allowed:
            await ctx.send(error, delete_after=10)
            return
        try:
            await ctx.guild.ban(
                member,
                delete_message_seconds=SOFTBAN_DELETE_SECONDS,
                reason=_reason_text(reason),
            )
            await ctx.guild.unban(member, reason="Staff Strike softban completed")
        except discord.Forbidden:
            await ctx.send("I do not have permission to softban that member.", delete_after=10)
            return
        # Softban is intentionally not added to the target's moderation
        # history, matching the requested Dyno-style behavior.  It is still
        # posted to the action log and counted for staff activity.
        record = self._base_record(member, ctx.author, "softban", reason)
        record["active"] = False
        await self._post_log(record, ctx)
        await self._confirmation(
            ctx,
            record,
            f"{member.mention} was softbanned. Discord deleted messages from the last seven days.",
        )

    @commands.command(name="ban")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def ban(self, ctx: commands.Context, target: str, *, details: str) -> None:
        """Ban a user permanently or temporarily; an optional final token is the duration."""
        if not ctx.guild:
            return
        user = await self._resolve_user(ctx, target)
        if not user:
            await ctx.send("I could not find that user.", delete_after=10)
            return
        member = user if isinstance(user, discord.Member) else None
        if member:
            allowed, error = self._can_act_on(ctx, member)
            if not allowed:
                await ctx.send(error, delete_after=10)
                return
        pieces = details.rsplit(None, 1) if details.strip() else []
        duration = parse_duration(pieces[-1]) if len(pieces) == 2 else None
        reason = pieces[0] if duration else details
        if not reason.strip():
            await ctx.send(
                "Usage: `ban <user> <reason> [duration]`.",
                delete_after=10,
            )
            return
        expires = _now() + duration if duration else None
        try:
            await ctx.guild.ban(user, delete_message_seconds=0, reason=_reason_text(reason))
        except discord.Forbidden:
            await ctx.send("I do not have permission to ban that user.", delete_after=10)
            return
        record = self._base_record(user, ctx.author, "ban", reason, expires)
        self._append_record(user, record)
        if expires:
            active_bans = self._active_bans()
            active_bans[f"{ctx.guild.id}:{user.id}"] = {
                "guild_id": str(ctx.guild.id),
                "user_id": str(user.id),
                "case_id": record["case_id"],
                "expires_at": expires.isoformat(),
            }
            _save(ACTIVE_BANS_FILE, active_bans)
        await self._post_log(record, ctx)
        duration_description = (
            f" for **{format_duration(duration)}**" if duration else " permanently"
        )
        await self._confirmation(
            ctx, record, f"{user.mention if hasattr(user, 'mention') else user} was banned{duration_description}."
        )

    @commands.command(name="unmute", aliases=["untimeout"])
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def unmute(self, ctx: commands.Context, target: str) -> None:
        """Remove a member's timeout and log the reversal."""
        member = await self._target_member(ctx, target)
        if not member:
            await ctx.send("Unmute requires a current server member.", delete_after=10)
            return
        allowed, error = self._can_act_on(ctx, member)
        if not allowed:
            await ctx.send(error, delete_after=10)
            return
        try:
            await member.timeout(None, reason=f"Timeout removed by {ctx.author}")
        except discord.Forbidden:
            await ctx.send("I do not have permission to remove that timeout.", delete_after=10)
            return
        history = self._history()
        related = None
        for record in reversed(history.get(str(member.id), [])):
            if record.get("action") == "mute" and record.get("active"):
                record["active"] = False
                related = record.get("case_id")
                break
        _save(HISTORY_FILE, history)
        record = await self._record_reversal(
            ctx, member, "unmute", "Timeout removed by staff.", related
        )
        await self._confirmation(ctx, record, f"{member.mention} is no longer timed out.")

    @commands.command(name="unban")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def unban(self, ctx: commands.Context, target: str) -> None:
        """Remove a server ban and log the reversal."""
        if not ctx.guild:
            return
        user = await self._resolve_user(ctx, target)
        if not user:
            # A banned member is not in Guild.members, so search the ban list
            # to keep username/display-name lookup useful for unban too.
            lowered = target.casefold()
            try:
                async for entry in ctx.guild.bans(limit=None):
                    candidate = entry.user
                    if (
                        str(candidate).casefold() == lowered
                        or candidate.name.casefold() == lowered
                    ):
                        user = candidate
                        break
            except (discord.Forbidden, discord.HTTPException):
                pass
        if not user:
            await ctx.send("I could not find that user.", delete_after=10)
            return
        try:
            await ctx.guild.unban(
                discord.Object(id=user.id),
                reason=f"Ban removed by {ctx.author}",
            )
        except discord.NotFound:
            await ctx.send("That user is not currently banned.", delete_after=10)
            return
        except discord.Forbidden:
            await ctx.send("I do not have permission to unban that user.", delete_after=10)
            return
        history = self._history()
        related = None
        for record in reversed(history.get(str(user.id), [])):
            if record.get("action") == "ban" and record.get("active"):
                record["active"] = False
                related = record.get("case_id")
                break
        _save(HISTORY_FILE, history)
        active_bans = self._active_bans()
        active_bans.pop(f"{ctx.guild.id}:{user.id}", None)
        _save(ACTIVE_BANS_FILE, active_bans)
        record = await self._record_reversal(
            ctx, user, "unban", "Ban removed by staff.", related
        )
        await self._confirmation(ctx, record, f"{user} has been unbanned.")

    @commands.command(name="unwarn", aliases=["delwarn", "removewarn"])
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def unwarn(
        self, ctx: commands.Context, target: str, warning_id: str
    ) -> None:
        """Remove a warning by case ID, or by its moderation-log message ID."""
        user = await self._resolve_user(ctx, target)
        if not user:
            await ctx.send("I could not find that user.", delete_after=10)
            return
        records = self._warnings_for(user.id)
        match = next(
            (
                record
                for record in records
                if str(record.get("case_id")) == warning_id
                or str(record.get("log_message_id")) == warning_id
            ),
            None,
        )
        if not match:
            await ctx.send("That warning could not be found.", delete_after=10)
            return
        await self._remove_case(str(match["case_id"]), delete_log=True)
        await ctx.send(
            embed=discord.Embed(
                title="Warning Removed",
                description=f"Warning `{match['case_id']}` was removed from {user.mention}.",
                color=0x2ECC71,
                timestamp=_now(),
            )
        )

    @commands.command(name="delhistory")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def delete_history(self, ctx: commands.Context, history_message_id: int) -> None:
        """Delete a history case using the message ID of its moderation log."""
        history = self._history()
        match = None
        for records in history.values():
            match = next(
                (
                    record
                    for record in records
                    if str(record.get("log_message_id")) == str(history_message_id)
                ),
                None,
            )
            if match:
                break
        if not match:
            await ctx.send("No Staff Strike history entry uses that message ID.", delete_after=10)
            return
        await self._remove_case(str(match["case_id"]), delete_log=True)
        await ctx.send(
            embed=discord.Embed(
                title="History Entry Deleted",
                description=f"Case `{match['case_id']}` was deleted.",
                color=0x95A5A6,
                timestamp=_now(),
            )
        )

    @commands.command(name="clearhistory")
    @commands.check(staff_management_check)
    async def clear_history(self, ctx: commands.Context, target: str) -> None:
        """Permanently clear every history entry for a user (Staff Management+)."""
        user = await self._resolve_user(ctx, target)
        if not user:
            await ctx.send("I could not find that user.", delete_after=10)
            return
        history = self._history()
        records = list(history.pop(str(user.id), []))
        _save(HISTORY_FILE, history)
        deleted_logs = 0
        for record in records:
            if record.get("log_channel_id") and record.get("log_message_id"):
                channel = self.bot.get_channel(int(record["log_channel_id"]))
                if isinstance(channel, discord.TextChannel):
                    try:
                        message = await channel.fetch_message(int(record["log_message_id"]))
                        await message.delete()
                        deleted_logs += 1
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
            if record.get("action") in {"warn", "mute", "ban", "softban"}:
                remove_staff_action(
                    int(record.get("moderator_id", 0)),
                    str(record["action"]),
                    record.get("log_url"),
                )
        await ctx.send(
            embed=discord.Embed(
                title="Moderation History Cleared",
                description=(
                    f"All stored history for {user.mention} was permanently removed.\n"
                    f"Cases removed: **{len(records)}** • Log messages deleted: **{deleted_logs}**"
                ),
                color=0xE74C3C,
                timestamp=_now(),
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModerationCog(bot))