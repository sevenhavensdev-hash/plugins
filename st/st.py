"""ModMail plugin for enforcing formatted server nicknames.

The plugin intentionally changes Discord server nicknames, not global Discord
usernames. Role IDs are configured through environment variables so the plugin
can be used without hard-coding a server's IDs into the repository.
"""

import logging
import os
import unicodedata
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands


log = logging.getLogger(__name__)


class StaffTitles(commands.Cog):
    """Let members choose safe names and apply staff rank prefixes."""

    # Ordered from highest rank to lowest rank. If a member has more than one
    # configured role, the first matching role wins.
    ROLE_LEVELS = (
        ("ST_HEAD_OF_STAFF_ROLE_ID", "✦✦✦ | "),
        ("ST_OVERSEER_ROLE_ID", "★★★ | "),
        ("ST_STAFF_MANAGEMENT_ROLE_ID", "★★ | "),
        ("ST_SENIOR_MODERATOR_ROLE_ID", "★ | "),
    )
    MANAGED_PREFIXES = tuple(prefix for _, prefix in ROLE_LEVELS)
    MAX_NICKNAME_LENGTH = 32

    def __init__(self, bot):
        self.bot = bot
        self.role_prefixes = self._load_role_prefixes()

        if not self.role_prefixes:
            log.warning(
                "StaffTitles is loaded but no role IDs are configured. "
                "Set at least one ST_*_ROLE_ID environment variable."
            )

    @staticmethod
    def _read_plugin_env() -> dict[str, str]:
        """Read simple KEY=value entries from the plugin's optional .env file."""
        env_path = Path(__file__).with_name(".env")
        if not env_path.exists():
            return {}

        values: dict[str, str] = {}
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            log.exception("Unable to read the StaffTitles .env file.")
            return {}

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            if key:
                values[key] = value

        return values

    @classmethod
    def _load_role_prefixes(cls) -> dict[int, str]:
        """Read one or more comma-separated Discord role IDs per rank."""
        configured: dict[int, str] = {}
        plugin_env = cls._read_plugin_env()

        for env_name, prefix in cls.ROLE_LEVELS:
            # The bot's real environment wins; the plugin-local .env is a
            # convenient fallback for GitHub-hosted plugins.
            raw_value = os.getenv(env_name) or plugin_env.get(env_name, "")
            for raw_role_id in raw_value.split(","):
                raw_role_id = raw_role_id.strip()
                if not raw_role_id:
                    continue

                try:
                    role_id = int(raw_role_id)
                except ValueError:
                    log.warning(
                        "Ignoring invalid role ID %r in %s.",
                        raw_role_id,
                        env_name,
                    )
                    continue

                # ROLE_LEVELS is high-to-low, so don't overwrite a higher
                # priority prefix if the same role ID was listed twice.
                configured.setdefault(role_id, prefix)

        return configured

    @classmethod
    def _strip_managed_prefix(cls, name: str) -> str:
        """Remove a prefix previously added by this plugin."""
        for prefix in sorted(cls.MANAGED_PREFIXES, key=len, reverse=True):
            if name.startswith(prefix):
                return name[len(prefix) :]
        return name

    @staticmethod
    def _is_name_character(character: str) -> bool:
        """Allow letters, numbers, periods, and commas, but no decorative symbols."""
        category = unicodedata.category(character)
        return category.startswith(("L", "N"))

    @classmethod
    def _clean_for_enforcement(cls, name: str) -> str:
        """Remove disallowed characters when fixing a nickname automatically."""
        cleaned: list[str] = []
        pending_space = False

        for character in cls._strip_managed_prefix(name):
            if cls._is_name_character(character) or character in {".", ","}:
                if pending_space and cleaned:
                    cleaned.append(" ")
                cleaned.append(character)
                pending_space = False
            elif character.isspace():
                pending_space = bool(cleaned)

        return "".join(cleaned).strip()

    @classmethod
    def _validate_requested_name(cls, name: str) -> Optional[str]:
        """Return an error message, or None when a command name is valid."""
        if not name:
            return "Please provide a name."

        if name != name.strip() or any(
            character.isspace() and character != " " for character in name
        ):
            return "Use letters, numbers, periods, commas, and single spaces only."

        if "  " in name:
            return "Use only one space between words."

        if not all(
            cls._is_name_character(character)
            or character in {" ", ".", ","}
            for character in name
        ):
            return (
                "Decorative symbols such as ★, @, and # are not allowed. "
                "Periods and commas are allowed."
            )

        if len(name) > cls.MAX_NICKNAME_LENGTH:
            return f"Your name must be {cls.MAX_NICKNAME_LENGTH} characters or fewer."

        return None

    def _prefix_for_member(self, member: discord.Member) -> str:
        """Return the highest-priority configured prefix for a member."""
        member_role_ids = {role.id for role in member.roles}
        for _, prefix in self.ROLE_LEVELS:
            for role_id, configured_prefix in self.role_prefixes.items():
                if role_id in member_role_ids and configured_prefix == prefix:
                    return prefix
        return ""

    def _base_name_for_member(self, member: discord.Member) -> str:
        current_name = member.nick or member.name
        return self._clean_for_enforcement(current_name)

    async def _apply_name(
        self,
        member: discord.Member,
        base_name: str,
        *,
        reason: str,
    ) -> tuple[bool, Optional[str]]:
        """Apply the correct nickname and return success plus an error."""
        prefix = self._prefix_for_member(member)
        nickname = f"{prefix}{base_name}"

        if len(nickname) > self.MAX_NICKNAME_LENGTH:
            return (
                False,
                "That name is too long once your staff prefix is included.",
            )

        if member.nick == nickname:
            return True, None

        try:
            await member.edit(nick=nickname, reason=reason)
        except discord.Forbidden:
            return (
                False,
                "I cannot change your server nickname. Give me Manage Nicknames "
                "and move my bot role above the member roles.",
            )
        except discord.HTTPException:
            log.exception("Discord rejected a nickname update for %s.", member.id)
            return False, "Discord rejected the nickname update. Please try again."

        return True, None

    @commands.guild_only()
    @commands.command(name="setname", aliases=("username", "name"))
    async def setname(self, ctx, *, requested_name: str):
        """Set your server nickname without symbols."""
        error = self._validate_requested_name(requested_name)
        if error:
            await ctx.send(error)
            return

        prefix = self._prefix_for_member(ctx.author)
        if len(prefix) + len(requested_name) > self.MAX_NICKNAME_LENGTH:
            await ctx.send(
                "That name is too long once your staff prefix is included. "
                f"Use {self.MAX_NICKNAME_LENGTH - len(prefix)} characters or fewer."
            )
            return

        success, error = await self._apply_name(
            ctx.author,
            requested_name,
            reason=f"{ctx.author} used the setname command",
        )
        if not success:
            await ctx.send(error or "I could not update your server nickname.")
            return

        await ctx.send(f"Your server name is now `{discord.utils.escape_markdown(prefix + requested_name)}`.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Apply a configured staff prefix when a member joins."""
        if member.bot:
            return

        base_name = self._base_name_for_member(member)
        if base_name:
            await self._apply_name(
                member,
                base_name,
                reason="Applying the StaffTitles nickname format",
            )

    @commands.Cog.listener()
    async def on_member_update(
        self,
        before: discord.Member,
        after: discord.Member,
    ):
        """Keep prefixes and symbol-free names correct after edits or role changes."""
        if after.bot:
            return

        roles_changed = {role.id for role in before.roles} != {
            role.id for role in after.roles
        }
        name_changed = before.name != after.name or before.nick != after.nick
        if not roles_changed and not name_changed:
            return

        base_name = self._base_name_for_member(after)
        if not base_name:
            return

        await self._apply_name(
            after,
            base_name,
            reason="Enforcing the StaffTitles nickname format",
        )


async def setup(bot):
    await bot.add_cog(StaffTitles(bot))
