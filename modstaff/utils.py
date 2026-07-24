"""
Utility helpers for the modstaff plugin.

Provides embed builders, time formatting, case numbering, and
other shared helpers used across views.py and modstaff.py.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

import discord


# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------

COLORS = {
    "warn":        0xF1C40F,  # yellow
    "mute":        0xE67E22,  # orange
    "timeout":     0xE67E22,  # orange
    "kick":        0xE74C3C,  # red
    "ban":         0x992D22,  # dark red
    "softban":     0xC0392B,  # red-ish
    "unban":       0x2ECC71,  # green
    "note":        0x3498DB,  # blue
    "promote":     0x1ABC9C,  # teal
    "demote":      0xE74C3C,  # red
    "history":     0x7289DA,  # blurple
    "stats":       0x5865F2,  # discord blurple
    "leaderboard": 0xF39C12,  # gold
    "error":       0xE74C3C,  # red
    "success":     0x2ECC71,  # green
    "info":        0x3498DB,  # blue
    "config":      0x95A5A6,  # grey
}

ACTION_EMOJIS = {
    "warn":        "⚠️",
    "mute":        "🔇",
    "timeout":     "⏱️",
    "kick":        "👢",
    "ban":         "🔨",
    "softban":     "🪃",
    "unban":       "✅",
    "note":        "📝",
    "promote":     "📈",
    "demote":      "📉",
}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def format_duration(seconds: int) -> str:
    """Return a human-readable duration string for a number of seconds."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return (f"{hours}h {minutes}m" if minutes else f"{hours}h")
    days, hours = divmod(hours, 24)
    return (f"{days}d {hours}h" if hours else f"{days}d")


def parse_duration(text: str) -> Optional[int]:
    """
    Parse a duration string such as '10m', '2h', '1d' into seconds.
    Returns None if the string cannot be parsed.
    """
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    m = re.fullmatch(r"(\d+)([smhdw])", text.strip().lower())
    if not m:
        return None
    return int(m.group(1)) * units[m.group(2)]


def time_in_rank(since_ts: Optional[float]) -> str:
    """
    Return a human-readable 'time in current rank' string.

    If since_ts is None (promotion predates plugin) returns the standard
    'Unknown (Promotion predates plugin)' message.
    """
    if since_ts is None:
        return "Unknown (Promotion predates plugin)"

    since = datetime.fromtimestamp(since_ts, tz=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    delta = now - since

    total_days = delta.days
    years, rem_days = divmod(total_days, 365)
    months, days = divmod(rem_days, 30)

    parts = []
    if years:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    if months:
        parts.append(f"{months} month{'s' if months != 1 else ''}")
    if days and not years:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if not parts:
        hours = delta.seconds // 3600
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")

    return ", ".join(parts)


def format_dt(ts: float) -> str:
    """Return a formatted UTC date string from a Unix timestamp."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%b %d, %Y")


def format_dt_long(ts: float) -> str:
    """Return a verbose formatted UTC datetime string from a Unix timestamp."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%b %d, %Y at %H:%M UTC")


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def error_embed(title: str, description: str) -> discord.Embed:
    """Build a standardised error embed."""
    return discord.Embed(
        title=f"❌ {title}",
        description=description,
        color=COLORS["error"],
    )


def success_embed(title: str, description: str) -> discord.Embed:
    """Build a standardised success embed."""
    return discord.Embed(
        title=f"✅ {title}",
        description=description,
        color=COLORS["success"],
    )


def action_embed(
    action: str,
    moderator: discord.Member,
    target: discord.abc.User,
    reason: str,
    case_id: int,
    *,
    extra_fields: Optional[list] = None,
    guild_name: str = "",
) -> discord.Embed:
    """
    Build the rich embed for a moderation action.

    Parameters
    ----------
    action      : the action name (e.g. 'ban')
    moderator   : the staff member who performed the action
    target      : the user the action was performed on
    reason      : the reason for the action
    case_id     : the sequential case number
    extra_fields: list of (name, value, inline) tuples
    guild_name  : the guild name for the footer
    """
    emoji = ACTION_EMOJIS.get(action, "🔧")
    color = COLORS.get(action, 0x7289DA)
    title = f"{emoji} {action.capitalize()} | Case #{case_id}"

    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(tz=timezone.utc))
    embed.add_field(name="👤 Target", value=f"{target.mention} (`{target.id}`)", inline=True)
    embed.add_field(name="🛡️ Moderator", value=f"{moderator.mention} (`{moderator.id}`)", inline=True)
    embed.add_field(name="📋 Reason", value=reason or "No reason provided.", inline=False)

    if extra_fields:
        for name, value, inline in extra_fields:
            embed.add_field(name=name, value=value, inline=inline)

    embed.set_thumbnail(url=getattr(target.display_avatar, "url", None))
    embed.set_footer(text=guild_name or "Modstaff Plugin")
    return embed


def log_embed(
    action: str,
    moderator: discord.Member,
    target: discord.abc.User,
    reason: str,
    case_id: int,
    guild_name: str = "",
) -> discord.Embed:
    """Build a log-channel embed (slightly more compact than the DM embed)."""
    return action_embed(
        action, moderator, target, reason, case_id, guild_name=guild_name
    )


def history_embed(
    target: discord.abc.User,
    cases: list[dict],
    page: int,
    total_pages: int,
) -> discord.Embed:
    """Build an embed for a single page of moderation history."""
    embed = discord.Embed(
        title=f"📋 Moderation History — {target.display_name}",
        description=f"User: {target.mention} (`{target.id}`)",
        color=COLORS["history"],
        timestamp=datetime.now(tz=timezone.utc),
    )
    if not cases:
        embed.add_field(name="No Records", value="This user has a clean record.", inline=False)
    else:
        for case in cases:
            emoji = ACTION_EMOJIS.get(case.get("action", ""), "🔧")
            ts = case.get("timestamp", 0)
            date_str = format_dt(ts) if ts else "Unknown date"
            reason = case.get("reason", "No reason provided.")
            if len(reason) > 100:
                reason = reason[:97] + "..."
            mod_id = case.get("moderator_id", "Unknown")
            embed.add_field(
                name=f"{emoji} Case #{case.get('case_id', '?')} — {case.get('action', 'unknown').capitalize()} on {date_str}",
                value=f"**Reason:** {reason}\n**Moderator:** <@{mod_id}>",
                inline=False,
            )
    embed.set_thumbnail(url=getattr(target.display_avatar, "url", None))
    embed.set_footer(text=f"Page {page}/{total_pages} • {target.id}")
    return embed


def stats_embed(
    member: discord.abc.User,
    guild: discord.Guild,
    staff_doc: dict,
    stats_doc: dict,
) -> discord.Embed:
    """Build the /staffstats embed."""
    color = COLORS["stats"]
    embed = discord.Embed(
        title=f"📊 Staff Statistics — {member.display_name}",
        color=color,
        timestamp=datetime.now(tz=timezone.utc),
    )
    embed.set_thumbnail(url=getattr(member.display_avatar, "url", None))

    # Staff information
    rank_since_ts = staff_doc.get("rank_since")
    staff_since_ts = staff_doc.get("staff_since")
    current_rank = staff_doc.get("current_rank", "Unknown")
    last_active_ts = staff_doc.get("last_active")

    staff_since_str = format_dt(staff_since_ts) if staff_since_ts else "Unknown"
    rank_since_str = format_dt(rank_since_ts) if rank_since_ts else "Unknown"
    last_active_str = format_dt_long(last_active_ts) if last_active_ts else "Never recorded"

    embed.add_field(
        name="👤 Staff Information",
        value=(
            f"**Current Rank:** {current_rank}\n"
            f"**Time in Rank:** {time_in_rank(rank_since_ts)}\n"
            f"**Staff Since:** {staff_since_str}\n"
            f"**Last Active:** {last_active_str}"
        ),
        inline=False,
    )

    # Ticket statistics
    t = stats_doc.get("tickets", {})
    total_tickets = t.get("total", 0)
    claimed = t.get("claimed", 0)
    messages_sent = t.get("messages_sent", 0)
    avg_response = t.get("avg_response_seconds", 0)
    avg_str = format_duration(int(avg_response)) if avg_response else "N/A"

    embed.add_field(
        name="🎫 Ticket Statistics",
        value=(
            f"**Total Handled:** {total_tickets:,}\n"
            f"**Tickets Claimed:** {claimed:,}\n"
            f"**Messages Sent:** {messages_sent:,}\n"
            f"**Avg Response Time:** {avg_str}"
        ),
        inline=True,
    )

    # Moderation statistics
    m = stats_doc.get("moderation", {})
    warns     = m.get("warn", 0)
    mutes     = m.get("mute", 0)
    timeouts  = m.get("timeout", 0)
    kicks     = m.get("kick", 0)
    bans      = m.get("ban", 0)
    softbans  = m.get("softban", 0)
    unbans    = m.get("unban", 0)
    notes     = m.get("note", 0)
    promotes  = m.get("promote", 0)
    demotes   = m.get("demote", 0)

    total_mod = warns + mutes + timeouts + kicks + bans + softbans + unbans + notes + promotes + demotes

    embed.add_field(
        name="🛡️ Moderation Statistics",
        value=(
            f"**Total Actions:** {total_mod:,}\n"
            f"⚠️ Warns: {warns:,}\n"
            f"🔇 Mutes: {mutes:,}\n"
            f"⏱️ Timeouts: {timeouts:,}\n"
            f"👢 Kicks: {kicks:,}\n"
            f"🔨 Bans: {bans:,}\n"
            f"🪃 Softbans: {softbans:,}\n"
            f"✅ Unbans: {unbans:,}\n"
            f"📝 Notes: {notes:,}\n"
            f"📈 Promotes: {promotes:,}\n"
            f"📉 Demotes: {demotes:,}"
        ),
        inline=True,
    )

    embed.set_footer(
        text=f"Staff Since: {staff_since_str} • Current Rank Since: {rank_since_str}"
    )
    return embed


def leaderboard_embed(
    category: str,
    entries: list[dict],
    guild: discord.Guild,
    page: int,
    total_pages: int,
) -> discord.Embed:
    """Build a leaderboard embed for a given category."""
    category_labels = {
        "overall":    "🏆 Overall Leaderboard",
        "tickets":    "🎫 Ticket Leaderboard",
        "moderation": "🛡️ Moderation Leaderboard",
        "messages":   "💬 Messages Leaderboard",
        "monthly":    "📅 Monthly Leaderboard",
    }
    title = category_labels.get(category, "🏆 Staff Leaderboard")
    embed = discord.Embed(
        title=title,
        color=COLORS["leaderboard"],
        timestamp=datetime.now(tz=timezone.utc),
    )

    if not entries:
        embed.description = "No staff data available yet."
    else:
        lines = []
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        start_rank = (page - 1) * 10 + 1
        for i, entry in enumerate(entries):
            rank = start_rank + i
            medal = medals.get(rank, f"**#{rank}**")
            user_id = entry.get("user_id", "?")
            score = entry.get("score", 0)
            lines.append(f"{medal} <@{user_id}> — `{score:,}`")
        embed.description = "\n".join(lines)

    embed.set_footer(text=f"Page {page}/{total_pages}")
    return embed
