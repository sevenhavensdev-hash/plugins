import discord
import aiohttp
from discord.ext import commands
from datetime import datetime, timezone


ROBLOX_USERS_API = "https://users.roblox.com/v1"
ROBLOX_THUMBNAILS_API = "https://thumbnails.roblox.com/v1"
ROBLOX_BADGES_API = "https://badges.roblox.com/v1"
ROBLOX_OPEN_CLOUD = "https://apis.roblox.com/cloud/v2"
ROBLOX_MESSAGING = "https://apis.roblox.com/messaging-service/v1"

ACTION_COLORS = {
    "Ban": discord.Color.red(),
    "Unban": discord.Color.brand_green(),
    "Kick": discord.Color.orange(),
}

ACTION_ICONS = {
    "Ban": "🔨",
    "Unban": "✅",
    "Kick": "👢",
}


class RobloxMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)

    async def get_config(self):
        doc = await self.db.find_one({"_id": "config"})
        return doc or {}

    async def store_mod_action(self, roblox_user_id: int, roblox_username: str, action: str, moderator: discord.Member, **kwargs):
        entry = {
            "action": action,
            "moderator_id": str(moderator.id),
            "moderator_tag": str(moderator),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        entry.update(kwargs)
        await self.db.find_one_and_update(
            {"_id": f"history_{roblox_user_id}"},
            {
                "$push": {"actions": entry},
                "$set": {"roblox_username": roblox_username, "roblox_user_id": roblox_user_id},
            },
            upsert=True
        )

    async def send_mod_log(self, ctx, action: str, user: dict, color: discord.Color, **kwargs):
        config = await self.get_config()
        log_channel_id = config.get("log_channel_id")
        if not log_channel_id:
            return

        channel = ctx.guild.get_channel(int(log_channel_id))
        if not channel:
            return

        user_id = user["id"]
        embed = discord.Embed(title=f"Roblox Mod Log — {action}", color=color, timestamp=datetime.now(timezone.utc))
        embed.set_author(name=str(ctx.author), icon_url=ctx.author.display_avatar.url)
        embed.add_field(name="Roblox User", value=f"[@{user['name']}](https://www.roblox.com/users/{user_id}/profile) (`{user_id}`)", inline=True)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)

        for key, value in kwargs.items():
            embed.add_field(name=key, value=value, inline=False)

        embed.add_field(name="Channel", value=ctx.channel.mention, inline=True)
        await channel.send(embed=embed)

    async def resolve_user(self, session, identifier):
        if identifier.isdigit():
            async with session.get(f"{ROBLOX_USERS_API}/users/{identifier}") as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        payload = {"usernames": [identifier], "excludeBannedUsers": False}
        async with session.post(f"{ROBLOX_USERS_API}/usernames/users", json=payload) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            if not data.get("data"):
                return None
            user_id = data["data"][0]["id"]
            async with session.get(f"{ROBLOX_USERS_API}/users/{user_id}") as r2:
                if r2.status == 200:
                    return await r2.json()
                return None

    async def get_avatar_url(self, session, user_id):
        params = {"userIds": user_id, "size": "420x420", "format": "Png", "isCircular": "false"}
        async with session.get(f"{ROBLOX_THUMBNAILS_API}/users/avatar-headshot", params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("data"):
                    return data["data"][0].get("imageUrl")
        return None

    async def get_game_badges(self, session, user_id, universe_id):
        badges = []
        cursor = None
        while True:
            params = {"limit": 100, "sortOrder": "Asc"}
            if cursor:
                params["cursor"] = cursor
            async with session.get(f"{ROBLOX_BADGES_API}/users/{user_id}/badges", params=params) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
                for badge in data.get("data", []):
                    awarder = badge.get("awarder", {})
                    if str(awarder.get("id")) == str(universe_id):
                        badges.append(badge["name"])
                cursor = data.get("nextPageCursor")
                if not cursor:
                    break
        return badges

    async def get_ban_status(self, session, universe_id, user_id, api_key):
        headers = {"x-api-key": api_key}
        url = f"{ROBLOX_OPEN_CLOUD}/universes/{universe_id}/user-restrictions/{user_id}"
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("gameJoinRestriction", {}).get("active", False)
        return False

    def format_date(self, iso_string):
        try:
            dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
            return dt.strftime("%B %d, %Y")
        except Exception:
            return iso_string or "Unknown"

    def format_timestamp(self, iso_string):
        try:
            dt = datetime.fromisoformat(iso_string)
            return f"<t:{int(dt.timestamp())}:R>"
        except Exception:
            return iso_string or "Unknown"

    @commands.command(name="rlookup", usage="<username | id>")
    @commands.has_permissions(manage_messages=True)
    async def rlookup(self, ctx, identifier: str):
        """Look up a Roblox user's profile, avatar, game badges, and ban status. Accepts a username or numeric user ID."""
        config = await self.get_config()
        api_key = config.get("api_key")
        universe_id = config.get("universe_id")

        async with aiohttp.ClientSession() as session:
            user = await self.resolve_user(session, identifier)
            if not user:
                embed = discord.Embed(
                    description="No Roblox user was found with that username or ID.",
                    color=discord.Color.red()
                )
                return await ctx.send(embed=embed)

            user_id = user["id"]
            avatar_url = await self.get_avatar_url(session, user_id)
            is_banned = False
            game_badges = []

            if api_key and universe_id:
                is_banned = await self.get_ban_status(session, universe_id, user_id, api_key)
                game_badges = await self.get_game_badges(session, user_id, universe_id)

            description = user.get("description", "").strip() or "No description set."
            created = self.format_date(user.get("created", ""))

            embed = discord.Embed(
                title=user.get("displayName", user.get("name")),
                url=f"https://www.roblox.com/users/{user_id}/profile",
                color=discord.Color.red() if is_banned else discord.Color.brand_green()
            )
            if avatar_url:
                embed.set_thumbnail(url=avatar_url)

            embed.add_field(name="Username", value=f"@{user.get('name')}", inline=True)
            embed.add_field(name="User ID", value=str(user_id), inline=True)
            embed.add_field(name="Account Created", value=created, inline=True)
            embed.add_field(name="Description", value=description[:1024], inline=False)
            embed.add_field(
                name="Game Ban Status",
                value="🔴  Banned" if is_banned else "🟢  Not Banned",
                inline=True
            )

            if game_badges:
                badge_lines = "\n".join(f"• {b}" for b in game_badges[:15])
                if len(game_badges) > 15:
                    badge_lines += f"\n*+{len(game_badges) - 15} more*"
                embed.add_field(name=f"Game Badges ({len(game_badges)})", value=badge_lines, inline=False)
            else:
                embed.add_field(name="Game Badges", value="No badges earned in this game.", inline=False)

            embed.set_footer(text=f"Requested by {ctx.author} • Roblox ID: {user_id}")
            await ctx.send(embed=embed)

    @commands.command(name="rban", usage="<username | id> [duration] <reason>")
    @commands.has_permissions(manage_messages=True)
    async def rban(self, ctx, identifier: str, duration: str = None, *, reason: str = "No reason provided."):
        """Ban a player from your Roblox game. Duration is optional (e.g. 86400s for 24 hours) — omit it for a permanent ban."""
        config = await self.get_config()
        api_key = config.get("api_key")
        universe_id = config.get("universe_id")

        if not api_key or not universe_id:
            embed = discord.Embed(
                description="The Roblox API key or Universe ID has not been configured. Use `robloxmod setup` to get started.",
                color=discord.Color.red()
            )
            return await ctx.send(embed=embed)

        async with aiohttp.ClientSession() as session:
            user = await self.resolve_user(session, identifier)
            if not user:
                embed = discord.Embed(description="No Roblox user was found with that username or ID.", color=discord.Color.red())
                return await ctx.send(embed=embed)

            user_id = user["id"]
            headers = {"x-api-key": api_key, "Content-Type": "application/json"}
            url = f"{ROBLOX_OPEN_CLOUD}/universes/{universe_id}/user-restrictions/{user_id}"

            if duration:
                if duration.isdigit():
                    duration = duration + "s"
                duration_display = duration
            else:
                duration_display = "Permanent"

            restriction = {
                "active": True,
                "privateReason": reason,
                "displayReason": reason,
                "excludeAltAccounts": False,
            }
            if duration:
                restriction["duration"] = duration

            payload = {"gameJoinRestriction": restriction}

            async with session.patch(
                url, json=payload, headers=headers,
                params={"updateMask": "game_join_restriction"}
            ) as resp:
                if resp.status in (200, 204):
                    embed = discord.Embed(title="Roblox Ban Issued", color=discord.Color.red())
                    embed.add_field(name="User", value=f"@{user['name']} (`{user_id}`)", inline=True)
                    embed.add_field(name="Duration", value=duration_display, inline=True)
                    embed.add_field(name="Reason", value=reason, inline=False)
                    embed.set_footer(text=f"Actioned by {ctx.author}")
                    await ctx.send(embed=embed)
                    await self.send_mod_log(ctx, "Ban", user, discord.Color.red(), Reason=reason, Duration=duration_display)
                    await self.store_mod_action(user_id, user["name"], "Ban", ctx.author, reason=reason, duration=duration_display)
                else:
                    raw = await resp.text()
                    try:
                        import json as _json
                        error_data = _json.loads(raw)
                        msg = error_data.get("message") or error_data.get("error") or raw
                    except Exception:
                        msg = raw or f"HTTP {resp.status}"
                    embed = discord.Embed(
                        description=f"Failed to ban user (`HTTP {resp.status}`):\n```{msg[:1000]}```",
                        color=discord.Color.red()
                    )
                    await ctx.send(embed=embed)

    @commands.command(name="runban", usage="<username | id>")
    @commands.has_permissions(manage_messages=True)
    async def runban(self, ctx, identifier: str):
        """Remove an active game ban for a Roblox player. The player will be able to rejoin immediately."""
        config = await self.get_config()
        api_key = config.get("api_key")
        universe_id = config.get("universe_id")

        if not api_key or not universe_id:
            embed = discord.Embed(
                description="The Roblox API key or Universe ID has not been configured. Use `robloxmod setup` to get started.",
                color=discord.Color.red()
            )
            return await ctx.send(embed=embed)

        async with aiohttp.ClientSession() as session:
            user = await self.resolve_user(session, identifier)
            if not user:
                embed = discord.Embed(description="No Roblox user was found with that username or ID.", color=discord.Color.red())
                return await ctx.send(embed=embed)

            user_id = user["id"]
            headers = {"x-api-key": api_key, "Content-Type": "application/json"}
            url = f"{ROBLOX_OPEN_CLOUD}/universes/{universe_id}/user-restrictions/{user_id}"
            payload = {"gameJoinRestriction": {"active": False}}

            async with session.patch(
                url, json=payload, headers=headers,
                params={"updateMask": "game_join_restriction"}
            ) as resp:
                if resp.status in (200, 204):
                    embed = discord.Embed(title="Roblox Ban Removed", color=discord.Color.brand_green())
                    embed.add_field(name="User", value=f"@{user['name']} (`{user_id}`)", inline=True)
                    embed.set_footer(text=f"Actioned by {ctx.author}")
                    await ctx.send(embed=embed)
                    await self.send_mod_log(ctx, "Unban", user, discord.Color.brand_green())
                    await self.store_mod_action(user_id, user["name"], "Unban", ctx.author)
                else:
                    raw = await resp.text()
                    try:
                        import json as _json
                        error_data = _json.loads(raw)
                        msg = error_data.get("message") or error_data.get("error") or raw
                    except Exception:
                        msg = raw or f"HTTP {resp.status}"
                    embed = discord.Embed(
                        description=f"Failed to unban user (`HTTP {resp.status}`):\n```{msg[:1000]}```",
                        color=discord.Color.red()
                    )
                    await ctx.send(embed=embed)

    @commands.command(name="rkick", usage="<username | id> <reason>")
    @commands.has_permissions(manage_messages=True)
    async def rkick(self, ctx, identifier: str, *, reason: str = "No reason provided."):
        """Kick a player from all active servers in your Roblox game via the Messaging Service. Requires an in-game handler script."""
        config = await self.get_config()
        api_key = config.get("api_key")
        universe_id = config.get("universe_id")

        if not api_key or not universe_id:
            embed = discord.Embed(
                description="The Roblox API key or Universe ID has not been configured. Use `robloxmod setup` to get started.",
                color=discord.Color.red()
            )
            return await ctx.send(embed=embed)

        async with aiohttp.ClientSession() as session:
            user = await self.resolve_user(session, identifier)
            if not user:
                embed = discord.Embed(description="No Roblox user was found with that username or ID.", color=discord.Color.red())
                return await ctx.send(embed=embed)

            user_id = user["id"]
            headers = {"x-api-key": api_key, "Content-Type": "application/json"}
            topic = "RobloxModKick"
            url = f"{ROBLOX_MESSAGING}/universes/{universe_id}/topics/{topic}"
            message_payload = {"message": f'{{"userId": {user_id}, "reason": "{reason}"}}'}

            async with session.post(url, json=message_payload, headers=headers) as resp:
                if resp.status in (200, 204):
                    embed = discord.Embed(title="Roblox Kick Issued", color=discord.Color.orange())
                    embed.add_field(name="User", value=f"@{user['name']} (`{user_id}`)", inline=True)
                    embed.add_field(name="Reason", value=reason, inline=False)
                    embed.set_footer(text=f"Actioned by {ctx.author} • Requires in-game handler")
                    await ctx.send(embed=embed)
                    await self.send_mod_log(ctx, "Kick", user, discord.Color.orange(), Reason=reason)
                    await self.store_mod_action(user_id, user["name"], "Kick", ctx.author, reason=reason)
                else:
                    raw = await resp.text()
                    try:
                        import json as _json
                        error_data = _json.loads(raw)
                        msg = error_data.get("message") or error_data.get("error") or raw
                    except Exception:
                        msg = raw or f"HTTP {resp.status}"
                    embed = discord.Embed(
                        description=f"Failed to kick user (`HTTP {resp.status}`):\n```{msg[:1000]}```",
                        color=discord.Color.red()
                    )
                    await ctx.send(embed=embed)

    @commands.command(name="rhistory", usage="<username | id>")
    @commands.has_permissions(manage_messages=True)
    async def rhistory(self, ctx, identifier: str):
        """View the full moderation history for a Roblox user — all bans, unbans, and kicks logged by this plugin."""
        async with aiohttp.ClientSession() as session:
            user = await self.resolve_user(session, identifier)
            if not user:
                embed = discord.Embed(description="No Roblox user was found with that username or ID.", color=discord.Color.red())
                return await ctx.send(embed=embed)

            user_id = user["id"]
            avatar_url = await self.get_avatar_url(session, user_id)

        record = await self.db.find_one({"_id": f"history_{user_id}"})

        if not record or not record.get("actions"):
            embed = discord.Embed(
                description=f"No moderation history found for **@{user['name']}**.",
                color=discord.Color.greyple()
            )
            embed.set_footer(text=f"Roblox ID: {user_id}")
            return await ctx.send(embed=embed)

        actions = list(reversed(record["actions"]))
        total = len(actions)
        shown = actions[:10]

        ban_count = sum(1 for a in record["actions"] if a["action"] == "Ban")
        unban_count = sum(1 for a in record["actions"] if a["action"] == "Unban")
        kick_count = sum(1 for a in record["actions"] if a["action"] == "Kick")

        embed = discord.Embed(
            title=f"Moderation History — @{user['name']}",
            url=f"https://www.roblox.com/users/{user_id}/profile",
            color=discord.Color.blurple()
        )
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        embed.add_field(name="User ID", value=str(user_id), inline=True)
        embed.add_field(
            name="Summary",
            value=f"🔨 {ban_count} Ban{'s' if ban_count != 1 else ''}  •  ✅ {unban_count} Unban{'s' if unban_count != 1 else ''}  •  👢 {kick_count} Kick{'s' if kick_count != 1 else ''}",
            inline=False
        )

        for entry in shown:
            action = entry.get("action", "Unknown")
            icon = ACTION_ICONS.get(action, "•")
            moderator_tag = entry.get("moderator_tag", "Unknown")
            timestamp = self.format_timestamp(entry.get("timestamp", ""))
            reason = entry.get("reason", "")
            duration = entry.get("duration", "")

            lines = [f"**Moderator:** {moderator_tag}", f"**When:** {timestamp}"]
            if reason:
                lines.append(f"**Reason:** {reason}")
            if duration and action == "Ban":
                lines.append(f"**Duration:** {duration}")

            embed.add_field(name=f"{icon} {action}", value="\n".join(lines), inline=False)

        footer = f"Showing {len(shown)} of {total} total action{'s' if total != 1 else ''} • Roblox ID: {user_id}"
        if total > 10:
            footer = f"Showing 10 most recent of {total} total actions • Roblox ID: {user_id}"
        embed.set_footer(text=footer)
        await ctx.send(embed=embed)

    @commands.group(name="robloxmod", invoke_without_command=True, usage="<subcommand>")
    @commands.has_permissions(administrator=True)
    async def robloxmod(self, ctx):
        """Configure the Roblox Mod plugin — set your API key, Universe ID, and mod log channel."""
        embed = discord.Embed(title="Roblox Mod — Configuration", color=discord.Color.blurple())
        embed.add_field(
            name="Commands",
            value=(
                "`robloxmod setkey <api_key>` — Set your Roblox Open Cloud API key\n"
                "`robloxmod setuniverse <id>` — Set your Universe ID\n"
                "`robloxmod setlogchannel <#channel>` — Set the mod log channel\n"
                "`robloxmod removelogchannel` — Remove the mod log channel\n"
                "`robloxmod config` — View current configuration"
            ),
            inline=False
        )
        await ctx.send(embed=embed)

    @robloxmod.command(name="setkey")
    @commands.has_permissions(administrator=True)
    async def robloxmod_setkey(self, ctx, api_key: str):
        await self.db.find_one_and_update(
            {"_id": "config"},
            {"$set": {"api_key": api_key}},
            upsert=True
        )
        try:
            await ctx.message.delete()
        except discord.Forbidden:
            pass
        embed = discord.Embed(description="Roblox API key has been saved.", color=discord.Color.brand_green())
        await ctx.send(embed=embed)

    @robloxmod.command(name="setuniverse")
    @commands.has_permissions(administrator=True)
    async def robloxmod_setuniverse(self, ctx, universe_id: str):
        await self.db.find_one_and_update(
            {"_id": "config"},
            {"$set": {"universe_id": universe_id}},
            upsert=True
        )
        embed = discord.Embed(description=f"Universe ID set to `{universe_id}`.", color=discord.Color.brand_green())
        await ctx.send(embed=embed)

    @robloxmod.command(name="setlogchannel")
    @commands.has_permissions(administrator=True)
    async def robloxmod_setlogchannel(self, ctx, channel: discord.TextChannel):
        await self.db.find_one_and_update(
            {"_id": "config"},
            {"$set": {"log_channel_id": str(channel.id)}},
            upsert=True
        )
        embed = discord.Embed(
            description=f"Mod log channel set to {channel.mention}. All ban, unban, and kick actions will be logged there.",
            color=discord.Color.brand_green()
        )
        await ctx.send(embed=embed)

    @robloxmod.command(name="removelogchannel")
    @commands.has_permissions(administrator=True)
    async def robloxmod_removelogchannel(self, ctx):
        await self.db.find_one_and_update(
            {"_id": "config"},
            {"$unset": {"log_channel_id": ""}},
            upsert=True
        )
        embed = discord.Embed(description="Mod log channel has been removed.", color=discord.Color.orange())
        await ctx.send(embed=embed)

    @robloxmod.command(name="config")
    @commands.has_permissions(administrator=True)
    async def robloxmod_config(self, ctx):
        config = await self.get_config()
        api_key = config.get("api_key")
        universe_id = config.get("universe_id")
        log_channel_id = config.get("log_channel_id")

        log_channel_display = "Not set"
        if log_channel_id:
            channel = ctx.guild.get_channel(int(log_channel_id))
            log_channel_display = channel.mention if channel else f"Unknown channel (`{log_channel_id}`)"

        embed = discord.Embed(title="Roblox Mod — Current Configuration", color=discord.Color.blurple())
        embed.add_field(name="API Key", value=f"||`{'*' * 12 + api_key[-4:] if api_key else 'Not set'}`||", inline=False)
        embed.add_field(name="Universe ID", value=universe_id or "Not set", inline=True)
        embed.add_field(name="Log Channel", value=log_channel_display, inline=True)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(RobloxMod(bot))
