"""
Staff Manager — Staff Strike Modmail Plugin
============================================
Tracks moderation actions created by the Staff Strike moderation cog, posts weekly activity reports, handles inactivity
requests, promotion requests with Accept/Decline buttons, and auto-logs
role changes.

Environment variables (set with ?config set KEY VALUE in Modmail):
  MOD_ACTION_LOG_CHANNEL        Channel ID for mod action logs
  MOD_ACTIVITY_LOG_CHANNEL      Channel ID for weekly activity report
  INACTIVITY_CHANNEL            Channel ID for inactivity requests
  PROMOTION_REQUEST_CHANNEL     Channel ID for promotion requests
  PROMOTION_LOG_CHANNEL         Channel ID for promotion logs
  TICKET_LOG_CHANNEL            Channel ID where Modmail posts thread-close embeds (for ticket close tracking)
  STAFF_IDS                     Comma-separated staff user IDs
  STAFF_MANAGEMENT_ROLE_IDS     Role IDs that can approve LOA requests
  HIGH_RANK_ROLE_IDS            Role IDs that can approve promotions
  TRIAL_MODERATOR_ROLE_ID
  MODERATOR_ROLE_ID
  SENIOR_MODERATOR_ROLE_ID
  STAFF_MANAGEMENT_ROLE_ID
  HEAD_OF_STAFF_ROLE_ID
  ADMIN_ROLE_ID
  HEAD_ADMIN_ROLE_ID
  WEEKLY_REPORT_HOUR            (unused) Report now fires Sunday 00:00 GMT+8 (Saturday 16:00 UTC)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import discord
from discord import ui
from discord.ext import commands, tasks

from core import checks
from core.models import PermissionLevel

# ---------------------------------------------------------------------------
# Constants & file paths
# ---------------------------------------------------------------------------

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MOD_ACTIONS_FILE    = os.path.join(DATA_DIR, "mod_actions.json")
INACTIVITY_FILE     = os.path.join(DATA_DIR, "inactivity.json")
ROLE_HISTORY_FILE   = os.path.join(DATA_DIR, "role_history.json")
PROMOTIONS_FILE     = os.path.join(DATA_DIR, "pending_promotions.json")

os.makedirs(DATA_DIR, exist_ok=True)

# Rank hierarchy — lowest to highest
RANK_HIERARCHY: List[str] = [
    "Trial Moderator",
    "Moderator",
    "Senior Moderator",
    "Staff Management",
    "Head of Staff",
    "Admin",
    "Head Admin",
]

RANK_EMOJIS: Dict[str, str] = {
    "Trial Moderator":  "🛡️",
    "Moderator":        "⚔️",
    "Senior Moderator": "🔱",
    "Staff Management": "👑",
    "Head of Staff":    "🌟",
    "Admin":            "💫",
    "Head Admin":       "🏆",
}

ACTION_COLORS: Dict[str, int] = {
    "warn":         0xF1C40F,
    "mute":         0xE67E22,
    "kick":         0xE74C3C,
    "ban":          0x992D22,
    "softban":      0xC0392B,
    "ticket_close": 0x1ABC9C,
}

ACTION_ICONS: Dict[str, str] = {
    "warn":         "⚠️",
    "mute":         "🔇",
    "kick":         "👟",
    "ban":          "🔨",
    "softban":      "🪃",
    "ticket_close": "🎫",
}

# Keywords that appear in Dyno embed text (title / author / description) mapped
# to a normalised action key.  Longer phrases are checked first to prevent
# partial matches.  Only *punitive* actions are tracked — no "un-" reversal
# actions and no VC actions.
DYNO_TITLE_MAP: Dict[str, str] = {
    # --- warn ---
    "member warned":     "warn",
    "user warned":       "warn",
    "warned":            "warn",
    "warn":              "warn",
    # --- mute ---
    "member muted":      "mute",
    "user muted":        "mute",
    "muted":             "mute",
    "mute":              "mute",
    # --- kick ---
    "member kicked":     "kick",
    "user kicked":       "kick",
    "kicked":            "kick",
    "kick":              "kick",
    # --- ban ---
    "member banned":     "ban",
    "user banned":       "ban",
    "banned":            "ban",
    "ban":               "ban",
    # --- softban ---
    "member softbanned": "softban",
    "user softbanned":   "softban",
    "softbanned":        "softban",
    "softban":           "softban",
    # --- reverse actions (decrement stats) ---
    "member unbanned":      "unban",
    "user unbanned":        "unban",
    "unbanned":             "unban",
    "unban":                "unban",
    "member unmuted":       "unmute",
    "user unmuted":         "unmute",
    "unmuted":              "unmute",
    "unmute":               "unmute",
    "mute removed":         "unmute",
    "mute expired":         "unmute",
    "tempmute expired":     "unmute",
    "temp mute expired":    "unmute",
    "ban removed":          "unban",
    "ban expired":          "unban",
    "tempban expired":      "unban",
    "temp ban expired":     "unban",
    "warning deleted":      "delcase",
    "warning removed":      "delcase",
    "warn deleted":         "delcase",
    "warn removed":         "delcase",
    "note deleted":         "delnote",
    "note removed":         "delnote",
    "case deleted":         "delcase",
    "case removed":         "delcase",
}

# Maps a reverse action to the tracked action it should decrement.
# "delcase" is a placeholder — _parse_dyno_embed replaces it with the
# specific action type detected from the embed fields at parse time.
REVERSE_ACTION_MAP: Dict[str, str] = {
    "unban":      "ban",
    "unmute":     "mute",
    "delwarn":    "warn",   # kept for safety
    "delcase":    "warn",   # default; overridden per-embed in _parse_dyno_embed
    "delmute":    "mute",
    "delkick":    "kick",
    "delban":     "ban",
    "delsoftban": "softban",
}

# Frozenset of all reversal-action keys — used for priority matching in the parser.
_REVERSAL_KEYS: frozenset = frozenset(REVERSE_ACTION_MAP.keys())

# Matches "Case #123 Deleted / Removed" patterns where a case number sits between
# the noun and the verb, making plain substring keywords unable to match.
_NUMBERED_DEL_RE = re.compile(
    r"\bcase\s*#?\d+\s+(?:delet|remov)ed?\b"
    r"|\bwarn(?:ing)?\s*#?\d+\s+(?:delet|remov)ed?\b"
    r"|\binfraction\s*#?\d+\s+(?:delet|remov)ed?\b",
    re.IGNORECASE,
)

# Embed text patterns that indicate a *lookup/list* command, not a new action.
# These are checked before DYNO_TITLE_MAP and will cause the embed to be skipped.
_LOOKUP_PATTERNS = re.compile(
    r"infractions?\s+for\b"
    r"|warnings?\s+for\b"
    r"|warnings?\s+list"
    r"|modlogs?\s+for\b"
    r"|cases?\s+for\b",
    re.IGNORECASE,
)

ACTION_LABELS: Dict[str, str] = {
    "warn":         "Warnings",
    "mute":         "Mutes",
    "kick":         "Kicks",
    "ban":          "Bans",
    "softban":      "Softbans",
    "ticket_close": "Tickets Closed",
}

ALL_ACTIONS: List[str] = list(ACTION_LABELS.keys())

# ---------------------------------------------------------------------------
# JSON persistence helpers
# ---------------------------------------------------------------------------

def _load(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def record_staff_action(
    moderator_id: int, action: str, link: Optional[str] = None
) -> None:
    """Record an action for staffstats/leaderboard.

    This module-level helper is shared by the moderation cog so actions made
    by this bot use exactly the same counters and evidence links as the staff
    manager.  Ticket-close tracking also uses this store.
    """
    data = _load(MOD_ACTIONS_FILE)
    week = datetime.now(timezone.utc).strftime("%Y-W%W")
    uid = str(moderator_id)
    data.setdefault(week, {}).setdefault(uid, {})
    data[week][uid][action] = data[week][uid].get(action, 0) + 1
    if link:
        links_key = f"{action}_links"
        data[week][uid].setdefault(links_key, [])
        data[week][uid][links_key].append(link)
    _save(MOD_ACTIONS_FILE, data)


def remove_staff_action(
    moderator_id: int, action: str, link: Optional[str] = None
) -> None:
    """Reverse one stored action, optionally matching its evidence link."""
    data = _load(MOD_ACTIONS_FILE)
    uid = str(moderator_id)
    links_key = f"{action}_links"

    for week_data in data.values():
        user_data = week_data.get(uid, {})
        links = user_data.get(links_key, [])
        if link:
            if link not in links:
                continue
            links.remove(link)
            if not links:
                user_data.pop(links_key, None)
        elif action not in user_data:
            continue
        elif links:
            links.pop()
            if not links:
                user_data.pop(links_key, None)

        if action in user_data:
            user_data[action] = max(0, user_data[action] - 1)
            if user_data[action] == 0:
                user_data.pop(action, None)
        _save(MOD_ACTIONS_FILE, data)
        return

    _save(MOD_ACTIONS_FILE, data)


# ---------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------

DURATION_RE = re.compile(
    r"^(?:(\d+)\s*w(?:eeks?)?)?"
    r"(?:(\d+)\s*d(?:ays?)?)?"
    r"(?:(\d+)\s*h(?:ours?)?)?"
    r"(?:(\d+)\s*m(?:inutes?|ins?)?)?"
    r"(?:(\d+)\s*s(?:econds?|ecs?)?)?$",
    re.IGNORECASE,
)


def parse_duration(text: str) -> Optional[timedelta]:
    """Parse '5d', '2h30m', '1w3d' etc. into a timedelta. Returns None on failure."""
    text = text.strip()
    m = DURATION_RE.fullmatch(text)
    if not m or not any(m.groups()):
        return None
    weeks, days, hours, minutes, seconds = (int(v or 0) for v in m.groups())
    td = timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    return td if td.total_seconds() > 0 else None


def format_duration(td: timedelta) -> str:
    """Return a human-readable string like '2 weeks, 3 days, 4 hours'."""
    total = int(td.total_seconds())
    weeks, r    = divmod(total, 604800)
    days, r     = divmod(r, 86400)
    hours, r    = divmod(r, 3600)
    minutes, s  = divmod(r, 60)
    parts = []
    if weeks:   parts.append(f"{weeks} week{'s' if weeks   != 1 else ''}")
    if days:    parts.append(f"{days} day{'s' if days     != 1 else ''}")
    if hours:   parts.append(f"{hours} hour{'s' if hours   != 1 else ''}")
    if minutes: parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if s and not parts: parts.append(f"{s} second{'s' if s != 1 else ''}")
    return ", ".join(parts) if parts else "0 seconds"


def ts(dt: datetime, style: str = "F") -> str:
    """Return a Discord timestamp string e.g. <t:1234567890:F>."""
    return f"<t:{int(dt.timestamp())}:{style}>"


# ---------------------------------------------------------------------------
# Config — reads from config.json in the same folder as this file
# ---------------------------------------------------------------------------

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_PACKAGE_DIR, "config.json")
if not os.path.exists(CONFIG_FILE):
    # The fallback also supports the timestamped config filename used when
    # source files are uploaded as attachments.
    _config_snapshots = sorted(
        name
        for name in os.listdir(_PACKAGE_DIR)
        if name.startswith("config") and name.endswith(".json")
    )
    if _config_snapshots:
        CONFIG_FILE = os.path.join(_PACKAGE_DIR, _config_snapshots[0])

def _load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


STAFF_ROLE_KEYS = (
    "TRIAL_MODERATOR_ROLE_ID",
    "MODERATOR_ROLE_ID",
    "SENIOR_MODERATOR_ROLE_ID",
    "STAFF_MANAGEMENT_ROLE_ID",
    "HEAD_OF_STAFF_ROLE_ID",
    "ADMIN_ROLE_ID",
    "HEAD_ADMIN_ROLE_ID",
)
STAFF_ADMIN_ROLE_KEYS = (
    "STAFF_MANAGEMENT_ROLE_IDS",
    "HIGH_RANK_ROLE_IDS",
    "STAFF_MANAGEMENT_ROLE_ID",
    "HEAD_OF_STAFF_ROLE_ID",
    "ADMIN_ROLE_ID",
    "HEAD_ADMIN_ROLE_ID",
)


def _configured_ids(*keys: str) -> set[int]:
    """Read configured user or role IDs for standalone bot permission checks."""
    config = _load_config()
    result: set[int] = set()
    for key in keys:
        value = config.get(key)
        if isinstance(value, list):
            result.update(int(item) for item in value if str(item).isdigit())
        elif isinstance(value, int) and value:
            result.add(value)
    return result


def staff_moderator_check(ctx: commands.Context) -> bool:
    """Allow configured staff members to use staff-management commands."""
    if not ctx.guild or not isinstance(ctx.author, discord.Member):
        return False
    user_ids = _configured_ids("STAFF_IDS")
    role_ids = _configured_ids(*STAFF_ROLE_KEYS)
    return ctx.author.id in user_ids or bool(
        {role.id for role in ctx.author.roles} & role_ids
    )


def staff_admin_check(ctx: commands.Context) -> bool:
    """Allow Staff Management and higher to use administrative commands."""
    if not ctx.guild or not isinstance(ctx.author, discord.Member):
        return False
    role_ids = _configured_ids(*STAFF_ADMIN_ROLE_KEYS)
    return bool({role.id for role in ctx.author.roles} & role_ids)


# ---------------------------------------------------------------------------
# Dyno embed parser
# ---------------------------------------------------------------------------

def _parse_dyno_embed(embed: discord.Embed) -> Optional[dict]:
    """
    Return a dict with action/user_id/user_tag/moderator/moderator_id/reason, or None.

    Searches the embed title, author name, description, and explicitly named
    action/type fields so Dyno embeds are matched across common log layouts.
    Returns None for lookup/list embeds (e.g. ?warnings, ?modlogs).
    """
    # Collect all text that might carry the action keyword
    title_str  = (embed.title or "").lower()
    author_str = (getattr(embed.author, "name", "") or "").lower()
    desc       = embed.description or ""
    desc_lower = desc.lower()
    action_field_text = " ".join(
        f"{field.name or ''} {field.value or ''}"
        for field in embed.fields
        if any(
            keyword in (field.name or "").lower()
            for keyword in ("action", "type", "punishment", "case type")
        )
    ).lower()

    # Combined searchable text — checked for keywords
    full_text = f"{title_str} {author_str} {desc_lower} {action_field_text}"

    # --- Exclude lookup / list embeds BEFORE action matching ---
    if _LOOKUP_PATTERNS.search(full_text):
        return None

    # --- Match action keyword ---
    # Deletion/reversal keywords get an explicit first pass so that forward-action
    # phrases inside a case-deletion embed's description (e.g. "Member warned" shown
    # as the original action) can never shadow the true deletion intent.
    #
    # Additionally, "Case #123 Deleted" / "Warn #5 Removed" contain a case number
    # that breaks plain substring matching — catch those with a regex pre-check.
    action: Optional[str] = None

    if _NUMBERED_DEL_RE.search(title_str) or _NUMBERED_DEL_RE.search(author_str):
        # Title explicitly says "Case #N deleted/removed" → it is always a deletion
        action = "delcase"
    else:
        # Pass 1 — deletion/reversal keywords (highest priority, scans full text)
        for keyword in sorted(DYNO_TITLE_MAP, key=len, reverse=True):
            if DYNO_TITLE_MAP[keyword] in _REVERSAL_KEYS and keyword in full_text:
                action = DYNO_TITLE_MAP[keyword]
                break
        # Pass 2 — forward action keywords (only reached if no reversal was found)
        if action is None:
            for keyword in sorted(DYNO_TITLE_MAP, key=len, reverse=True):
                if keyword in full_text:
                    action = DYNO_TITLE_MAP[keyword]
                    break

    if action is None:
        return None

    # --- Refine generic "delcase" to the specific action that was deleted ---
    # Dyno's "case deleted" embed usually has an "Action" or "Type" field
    # telling us what kind of case it was (e.g. "Ban", "Mute", "Warn").
    # We map that to the correct del* key so the right stat is decremented.
    if action == "delcase":
        _CASE_TYPE_MAP = {
            "softban": "delsoftban",
            "ban":     "delban",
            "mute":    "delmute",
            "kick":    "delkick",
            "warn":    "delwarn",
            "warning": "delwarn",
        }
        # Check embed fields first (most reliable)
        original_action: Optional[str] = None
        for f in embed.fields:
            fname = (f.name or "").lower()
            if any(kw in fname for kw in ("action", "type", "punishment", "case type")):
                fval = (f.value or "").lower()
                # longest keys first so "softban" beats "ban"
                for key in sorted(_CASE_TYPE_MAP, key=len, reverse=True):
                    if key in fval:
                        original_action = _CASE_TYPE_MAP[key]
                        break
                if original_action:
                    break
        # Fallback: scan the whole embed text for the first action type mentioned
        if not original_action:
            combined = f"{title_str} {author_str} {desc_lower}"
            for key in sorted(_CASE_TYPE_MAP, key=len, reverse=True):
                if re.search(rf"\b{re.escape(key)}\b", combined):
                    original_action = _CASE_TYPE_MAP[key]
                    break
        if original_action:
            action = original_action

    result: Dict[str, object] = {
        "action":       action,
        "user_id":      None,
        "user_tag":     None,
        "moderator":    None,
        "moderator_id": None,
        "reason":       "No reason provided.",
    }

    def field_value(*names: str) -> str:
        """Return the value of the first embed field whose name contains any of the given strings."""
        for name in names:
            nl = name.lower()
            for f in embed.fields:
                if nl in f.name.lower():
                    return f.value or ""
        return ""

    # Resolve an explicitly identified moderator before using description
    # mentions as a fallback for the target.
    mod_field_raw = field_value(
        "responsible moderator", "moderator", "mod", "staff", "by", "executor"
    )
    if mod_field_raw:
        moderator_mention = re.search(r"<@!?(\d{17,20})>", mod_field_raw)
        if moderator_mention:
            result["moderator_id"] = int(moderator_mention.group(1))
    if not result["moderator_id"]:
        moderator_line = re.search(
            r"(?:Responsible\s+)?Moderator[:\s]+<@!?(\d{17,20})>",
            desc,
            re.IGNORECASE,
        )
        if moderator_line:
            result["moderator_id"] = int(moderator_line.group(1))

    # --- User ---
    user_raw = field_value("user", "member", "target")
    if user_raw:
        # Field may contain "Username (123456789)" or just a mention
        mention_in_field = re.search(r"<@!?(\d{17,20})>", user_raw)
        if mention_in_field:
            result["user_id"] = int(mention_in_field.group(1))
        else:
            cleaned = re.sub(r"[<@!>]", "", user_raw).strip()
            id_paren = re.search(r"\((\d{17,20})\)", cleaned)
            if id_paren:
                result["user_id"] = int(id_paren.group(1))
            elif re.fullmatch(r"\d{17,20}", cleaned):
                result["user_id"] = int(cleaned)
            tag_part = re.sub(r"\s*\(\d+\)\s*$", "", cleaned).strip()
            if tag_part:
                result["user_tag"] = tag_part
    else:
        # Prefer an explicitly labelled target in the description over a loose
        # mention. Dyno descriptions may mention the moderator before the user.
        for pat in [
            r"User:\s*<@!?(\d{17,20})>",
            r"\*\*User:\*\*\s*<@!?(\d{17,20})>",
            r"User:\s*(\d{17,20})",
            r"(?:Member|Target):\s*<@!?(\d{17,20})>",
            r"\*\*(?:Member|Target):\*\*\s*<@!?(\d{17,20})>",
            r"(?:Member|Target):\s*(\d{17,20})",
        ]:
            m2 = re.search(pat, desc, re.IGNORECASE)
            if m2:
                result["user_id"] = int(m2.group(1))
                break

        # Use a description mention only if it is not the identified moderator.
        # If several candidates remain, leave the target unmatched rather than
        # assigning a case to the wrong person.
        if not result["user_id"]:
            all_mentions = list(dict.fromkeys(re.findall(r"<@!?(\d{17,20})>", desc)))
            candidates = [
                mention
                for mention in all_mentions
                if mention != str(result.get("moderator_id") or "")
            ]
            if len(candidates) == 1:
                result["user_id"] = int(candidates[0])
            elif not result["moderator_id"] and len(all_mentions) == 1:
                result["user_id"] = int(all_mentions[0])

    # --- Moderator ---
    # Try every plausible Dyno field name for the moderator.
    # Priority 1: mention in a dedicated moderator field
    if mod_field_raw:
        m_mention = re.search(r"<@!?(\d{17,20})>", mod_field_raw)
        if m_mention:
            result["moderator_id"] = int(m_mention.group(1))
        else:
            cleaned_mod = re.sub(r"[<@!>]", "", mod_field_raw).strip()
            if re.fullmatch(r"\d{17,20}", cleaned_mod):
                result["moderator_id"] = int(cleaned_mod)
            elif cleaned_mod:
                result["moderator"] = cleaned_mod

    # Priority 2: mention in description on a "Moderator: <@id>" line
    if not result["moderator_id"]:
        m3 = re.search(
            r"(?:Responsible\s+)?Moderator[:\s]+<@!?(\d{17,20})>",
            desc,
            re.IGNORECASE,
        )
        if m3:
            result["moderator_id"] = int(m3.group(1))

    # Priority 3: any remaining mention in description (last resort — may be imprecise)
    if not result["moderator_id"] and not result["moderator"]:
        # Find all mentions in description; skip the user one if we know it
        all_mentions = re.findall(r"<@!?(\d{17,20})>", desc)
        user_id_str  = str(result.get("user_id") or "")
        for mid in all_mentions:
            if mid != user_id_str:
                result["moderator_id"] = int(mid)
                break

    # --- Reason ---
    reason_raw = field_value("reason")
    if not reason_raw:
        # Try parsing from description
        m_reason = re.search(r"\*?\*?Reason\*?\*?[:\s]+(.+)", desc, re.IGNORECASE)
        if m_reason:
            reason_raw = m_reason.group(1).strip()
    if reason_raw:
        result["reason"] = reason_raw.strip() or "No reason provided."

    # Reject confirmation / summary embeds that have no user AND no moderator info.
    # These are simple one-line confirmations (e.g. "r3nj1_k has been warned.") that
    # happen to contain an action keyword but carry no case data.
    no_user = not result["user_id"] and not result["user_tag"]
    no_mod  = not result["moderator_id"] and not result["moderator"]
    if no_user and no_mod:
        return None

    return result


# ---------------------------------------------------------------------------
# Button Views — using add_item for Python 3.8+ compatibility
# ---------------------------------------------------------------------------


class InactivityView(ui.View):
    """Accept / Decline buttons for a Leave of Absence request."""

    def __init__(self, cog: StaffManagerCog, request_id: str) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.request_id = request_id

        accept = ui.Button(
            label="✅  Accept",
            style=discord.ButtonStyle.success,
            custom_id=f"loa_accept_{request_id}",
        )
        accept.callback = self._accept
        self.add_item(accept)

        decline = ui.Button(
            label="❌  Decline",
            style=discord.ButtonStyle.danger,
            custom_id=f"loa_decline_{request_id}",
        )
        decline.callback = self._decline
        self.add_item(decline)

    async def _accept(self, interaction: discord.Interaction) -> None:
        await self.cog._handle_loa_decision(interaction, self.request_id, accepted=True)

    async def _decline(self, interaction: discord.Interaction) -> None:
        await self.cog._handle_loa_decision(interaction, self.request_id, accepted=False)


class PromotionView(ui.View):
    """Accept / Decline buttons for a promotion request."""

    def __init__(self, cog: StaffManagerCog, request_id: str) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.request_id = request_id

        accept = ui.Button(
            label="✅  Accept Promotion",
            style=discord.ButtonStyle.success,
            custom_id=f"promo_accept_{request_id}",
        )
        accept.callback = self._accept
        self.add_item(accept)

        decline = ui.Button(
            label="❌  Decline Promotion",
            style=discord.ButtonStyle.danger,
            custom_id=f"promo_decline_{request_id}",
        )
        decline.callback = self._decline
        self.add_item(decline)

    async def _accept(self, interaction: discord.Interaction) -> None:
        await self.cog._handle_promotion_decision(interaction, self.request_id, accepted=True)

    async def _decline(self, interaction: discord.Interaction) -> None:
        await self.cog._handle_promotion_decision(interaction, self.request_id, accepted=False)


class PageView(ui.View):
    """
    Generic embed paginator — ◀  page / total  ▶ buttons.
    Buttons disable automatically when at the first or last page.
    The view expires after 5 minutes of inactivity.
    """

    def __init__(self, pages: List[discord.Embed], timeout: float = 300.0) -> None:
        super().__init__(timeout=timeout)
        self.pages   = pages
        self.current = 0

        self._btn_prev = ui.Button(
            label="◀",
            style=discord.ButtonStyle.secondary,
            custom_id="pgv:prev",
            disabled=True,
        )
        self._btn_prev.callback = self._go_prev
        self.add_item(self._btn_prev)

        self._btn_label = ui.Button(
            label=f"1 / {len(pages)}",
            style=discord.ButtonStyle.grey,
            custom_id="pgv:label",
            disabled=True,
        )
        self.add_item(self._btn_label)

        self._btn_next = ui.Button(
            label="▶",
            style=discord.ButtonStyle.secondary,
            custom_id="pgv:next",
            disabled=len(pages) <= 1,
        )
        self._btn_next.callback = self._go_next
        self.add_item(self._btn_next)

    def _sync(self) -> None:
        self._btn_prev.disabled = self.current == 0
        self._btn_next.disabled = self.current >= len(self.pages) - 1
        self._btn_label.label   = f"{self.current + 1} / {len(self.pages)}"

    async def _go_prev(self, interaction: discord.Interaction) -> None:
        self.current = max(0, self.current - 1)
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    async def _go_next(self, interaction: discord.Interaction) -> None:
        self.current = min(len(self.pages) - 1, self.current + 1)
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)


# ---------------------------------------------------------------------------
# Main Cog
# ---------------------------------------------------------------------------

class StaffManagerCog(commands.Cog, name="Staff Manager"):
    """Comprehensive staff management and mod logging for Modmail servers."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._mod_actions: dict         = _load(MOD_ACTIONS_FILE)
        self._inactivity: dict          = _load(INACTIVITY_FILE)
        self._role_history: dict        = _load(ROLE_HISTORY_FILE)
        self._pending_promotions: dict  = _load(PROMOTIONS_FILE)
        self._last_report_date: str     = ""

    async def cog_load(self) -> None:
        self.weekly_report_task.start()
        self.check_loa_expiry.start()
        # Initialize role tracking for existing staff after the bot is ready
        asyncio.create_task(self._init_role_tracking())

    async def cog_unload(self) -> None:
        self.weekly_report_task.cancel()
        self.check_loa_expiry.cancel()

    # ------------------------------------------------------------------ #
    # Role tracking initialisation                                         #
    # ------------------------------------------------------------------ #

    async def _init_role_tracking(self) -> None:
        """
        Called once on startup. For every staff member who already has a rank
        role but has no entry in role_history, record today as the start date.

        Persistence guarantee:
        - We always re-read the JSON from disk right before writing so that any
          data written by a previous bot instance is never overwritten.
        - Existing timestamps are NEVER replaced — the clock keeps running from
          the original assignment date even across restarts or crashes.
        """
        await self.bot.wait_until_ready()
        role_map = self._staff_role_map()
        if not role_map:
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        changed = False

        for guild in self.bot.guilds:
            for member in guild.members:
                uid = str(member.id)
                member_role_ids = {r.id for r in member.roles}
                for rank, rid in role_map.items():
                    if rid in member_role_ids:
                        # Re-read from disk each time to merge safely
                        on_disk  = _load(ROLE_HISTORY_FILE)
                        existing = on_disk.get(uid, {}).get(rank)
                        if not existing:
                            on_disk.setdefault(uid, {})[rank] = now_iso
                            _save(ROLE_HISTORY_FILE, on_disk)
                            changed = True
                        # Keep the in-memory copy up to date
                        self._role_history = on_disk

    # ------------------------------------------------------------------ #
    # Config helpers                                                       #
    # ------------------------------------------------------------------ #

    def _cfg(self, key: str, default: str = "") -> str:
        cfg = _load_config()
        v = cfg.get(key)
        if isinstance(v, list):
            return ",".join(str(x) for x in v)
        if v is not None and str(v) != "0":
            return str(v)
        return os.environ.get(key, default)

    def _cfg_int(self, key: str, default: int = 0) -> int:
        cfg = _load_config()
        v = cfg.get(key)
        if isinstance(v, int) and v != 0:
            return v
        env = os.environ.get(key, "")
        return int(env) if env.isdigit() else default

    def _cfg_list(self, key: str) -> List[int]:
        cfg = _load_config()
        v = cfg.get(key)
        if isinstance(v, list):
            return [int(x) for x in v if str(x).isdigit() or isinstance(x, int)]
        s = os.environ.get(key, "")
        return [int(x.strip()) for x in s.split(",") if x.strip().isdigit()]

    def _channel(self, key: str) -> Optional[discord.TextChannel]:
        cid = self._cfg_int(key)
        if not cid:
            return None
        return self.bot.get_channel(cid)  # type: ignore[return-value]

    def _staff_role_map(self) -> Dict[str, int]:
        keys = {
            "Trial Moderator":  "TRIAL_MODERATOR_ROLE_ID",
            "Moderator":        "MODERATOR_ROLE_ID",
            "Senior Moderator": "SENIOR_MODERATOR_ROLE_ID",
            "Staff Management": "STAFF_MANAGEMENT_ROLE_ID",
            "Head of Staff":    "HEAD_OF_STAFF_ROLE_ID",
            "Admin":            "ADMIN_ROLE_ID",
            "Head Admin":       "HEAD_ADMIN_ROLE_ID",
        }
        return {rank: self._cfg_int(env) for rank, env in keys.items() if self._cfg_int(env)}

    def _member_rank(self, member: discord.Member) -> Optional[str]:
        role_map = self._staff_role_map()
        member_role_ids = {r.id for r in member.roles}
        for rank in reversed(RANK_HIERARCHY):
            rid = role_map.get(rank)
            if rid and rid in member_role_ids:
                return rank
        return None

    def _next_rank(self, current: str) -> Optional[str]:
        try:
            idx = RANK_HIERARCHY.index(current)
            return RANK_HIERARCHY[idx + 1] if idx + 1 < len(RANK_HIERARCHY) else None
        except ValueError:
            return None

    def _time_in_rank(self, user_id: int, rank: str) -> Optional[timedelta]:
        record = self._role_history.get(str(user_id), {}).get(rank)
        if not record:
            return None
        try:
            since = datetime.fromisoformat(record)
            return datetime.now(timezone.utc) - since
        except ValueError:
            return None

    def _lifetime_totals(self, user_id: int) -> Dict[str, int]:
        totals: Dict[str, int] = {}
        for week_data in self._mod_actions.values():
            for act, count in week_data.get(str(user_id), {}).items():
                if act.endswith("_links"):  # skip evidence link lists
                    continue
                if isinstance(count, int):
                    totals[act] = totals.get(act, 0) + count
        return totals

    def _week_actions(self, user_id: int, week_key: str) -> Dict[str, int]:
        """Return action counts for a single week (no _links keys)."""
        raw = self._mod_actions.get(week_key, {}).get(str(user_id), {})
        return {k: v for k, v in raw.items() if not k.endswith("_links") and isinstance(v, int)}

    def _week_links(self, user_id: int, week_key: str, action: str) -> List[str]:
        """Return stored evidence links for a specific action in a given week."""
        raw = self._mod_actions.get(week_key, {}).get(str(user_id), {})
        return list(raw.get(f"{action}_links", []))

    def _week_key(self, dt: Optional[datetime] = None) -> str:
        return (dt or datetime.now(timezone.utc)).strftime("%Y-W%W")

    def _record_action(self, moderator_id: int, action: str, link: Optional[str] = None) -> None:
        """Increment the action counter and optionally store an evidence link."""
        record_staff_action(moderator_id, action, link)
        self._mod_actions = _load(MOD_ACTIONS_FILE)

    def _remove_action(self, moderator_id: int, action: str, link: Optional[str] = None) -> None:
        """
        Decrement the action counter and remove one evidence link (for deletions).

        If a specific link URL is supplied (e.g. from !modlogdelete), that exact URL is
        removed from the links list.  If no link is supplied (Dyno case-deleted flow,
        where we only know the action type), the most-recently-added link is popped so
        the visible link count stays in sync with the action count.
        """
        self._mod_actions = _load(MOD_ACTIONS_FILE)
        uid       = str(moderator_id)
        links_key = f"{action}_links"

        for week_data in self._mod_actions.values():
            uid_data = week_data.get(uid, {})

            if link:
                # Exact-link removal — find the week that has this URL
                stored_links = uid_data.get(links_key, [])
                if link not in stored_links:
                    continue
                stored_links.remove(link)
                if not stored_links:
                    uid_data.pop(links_key, None)
            elif action not in uid_data:
                # No count for this action in this week — skip
                continue
            else:
                # No specific link: pop the most-recently-added one (keeps count ↔ links in sync)
                stored_links = uid_data.get(links_key, [])
                if stored_links:
                    stored_links.pop()
                    if not stored_links:
                        uid_data.pop(links_key, None)

            # Decrement the counter
            if action in uid_data:
                uid_data[action] = max(0, uid_data[action] - 1)
                if uid_data[action] == 0:
                    uid_data.pop(action, None)

            _save(MOD_ACTIONS_FILE, self._mod_actions)
            return

        _save(MOD_ACTIONS_FILE, self._mod_actions)

    # ------------------------------------------------------------------ #
    # Message listener (ticket tracking only)                              #
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # --- Ticket close tracking (Modmail log channel) ---
        ticket_log_ch = self._channel("TICKET_LOG_CHANNEL")
        if ticket_log_ch and message.channel.id == ticket_log_ch.id and message.embeds:
            await self._handle_ticket_close(message)

    async def _handle_ticket_close(self, message: discord.Message) -> None:
        """
        Parse a Modmail thread-close log embed and record a ticket_close stat.

        Strategy (most → least reliable):
          1. "Closed by" / "closer" / "moderator" field containing a mention or ID
          2. "Closed by …" anywhere in description
          3. Footer text containing "Closed by" + name / ID
          4. Any mention in the embed that belongs to a known staff member
          5. Any bare 17–20 digit ID in the whole embed that is a staff member

        We accept any embed that contains "close" or "ticket" or "thread" anywhere
        in the full embed text so we're not tripped up by title variations.
        """
        for embed in message.embeds:
            # Build one big searchable string from every part of the embed
            parts: List[str] = [
                embed.title or "",
                embed.description or "",
                getattr(embed.author, "name", "") or "",
                embed.footer.text or "",
            ]
            for f in embed.fields:
                parts.append(f.name or "")
                parts.append(f.value or "")
            full_text = "\n".join(parts)
            full_lower = full_text.lower()

            # Gate: must look like a close/ticket/thread embed
            if not any(kw in full_lower for kw in ("close", "ticket", "thread")):
                continue

            closer_id: Optional[int] = None
            guild = message.guild
            staff_role_ids = set(self._staff_role_map().values())

            def _is_staff(uid: int) -> bool:
                if not guild:
                    return False
                member = guild.get_member(uid)
                if not member:
                    return False
                return bool({r.id for r in member.roles} & staff_role_ids)

            # ── Priority 1: "Closed by / Closer / Moderator" FIELD ───────
            for field in embed.fields:
                fname = (field.name or "").lower()
                if any(kw in fname for kw in ("closed", "closer", "moderator", "mod", "by")):
                    fval = field.value or ""
                    # mention
                    m = re.search(r"<@!?(\d{17,20})>", fval)
                    if m:
                        closer_id = int(m.group(1))
                        break
                    # bare ID
                    m = re.search(r"\b(\d{17,20})\b", fval)
                    if m:
                        closer_id = int(m.group(1))
                        break
                    # name lookup
                    name = re.sub(r"[<@!>]", "", fval).strip()
                    if name and guild:
                        found = discord.utils.find(
                            lambda mem, n=name: mem.display_name == n or str(mem) == n,
                            guild.members,
                        )
                        if found:
                            closer_id = found.id
                            break

            # ── Priority 2: "Closed by …" anywhere in description ────────
            if not closer_id:
                desc = embed.description or ""
                # mention
                m = re.search(
                    r"closed\s+by[:\s]+<@!?(\d{17,20})>",
                    desc, re.IGNORECASE,
                )
                if m:
                    closer_id = int(m.group(1))
                # bare ID
                if not closer_id:
                    m = re.search(
                        r"closed\s+by[:\s]+(\d{17,20})",
                        desc, re.IGNORECASE,
                    )
                    if m:
                        closer_id = int(m.group(1))
                # name
                if not closer_id:
                    m = re.search(
                        r"closed\s+by[:\s]+(.+?)(?:\n|$)",
                        desc, re.IGNORECASE,
                    )
                    if m and guild:
                        name = m.group(1).strip()
                        found = discord.utils.find(
                            lambda mem, n=name: mem.display_name == n or str(mem) == n,
                            guild.members,
                        )
                        if found:
                            closer_id = found.id

            # ── Priority 3: footer "Closed by …" / "moderator ID: …" ─────
            if not closer_id:
                footer = embed.footer.text or ""
                for pat in [
                    r"closed\s+by[:\s]+<@!?(\d{17,20})>",
                    r"closed\s+by[:\s]+(\d{17,20})",
                    r"moderator\s*id[:\s]+(\d{17,20})",
                    r"mod\s*id[:\s]+(\d{17,20})",
                ]:
                    m = re.search(pat, footer, re.IGNORECASE)
                    if m:
                        closer_id = int(m.group(1))
                        break
                if not closer_id:
                    # name fallback in footer
                    m = re.search(
                        r"closed\s+by[:\s]+(.+?)(?:\s*\||\s*·|$)",
                        footer, re.IGNORECASE,
                    )
                    if m and guild:
                        name = m.group(1).strip()
                        found = discord.utils.find(
                            lambda mem, n=name: mem.display_name == n or str(mem) == n,
                            guild.members,
                        )
                        if found:
                            closer_id = found.id

            # ── Priority 4: any mention in the embed → pick the staff one ─
            if not closer_id and staff_role_ids:
                for raw_id in re.findall(r"<@!?(\d{17,20})>", full_text):
                    uid = int(raw_id)
                    if _is_staff(uid):
                        closer_id = uid
                        break

            # ── Priority 5: any bare ID in the embed → pick the staff one ─
            if not closer_id and staff_role_ids:
                for raw_id in re.findall(r"\b(\d{17,20})\b", full_text):
                    uid = int(raw_id)
                    if _is_staff(uid):
                        closer_id = uid
                        break

            if closer_id:
                log_ch = self._channel("MOD_ACTION_LOG_CHANNEL")
                if log_ch:
                    # Post a deletable log embed (just like Dyno actions) so
                    # !modlogdelete can be used to reverse this stat.
                    data = {
                        "action":       "ticket_close",
                        "user_id":      None,
                        "user_tag":     "Ticket",
                        "moderator_id": closer_id,
                        "moderator":    None,
                        "reason":       "Modmail ticket closed",
                    }
                    await self._post_mod_action(log_ch, data, message)
                else:
                    # No log channel configured — record stat directly
                    self._record_action(closer_id, "ticket_close", link=message.jump_url)
            break  # only process the first matching embed per message

    async def _post_mod_action(
        self,
        log_ch: discord.TextChannel,
        data: dict,
        src: discord.Message,
    ) -> None:
        action   = str(data["action"])
        color    = ACTION_COLORS.get(action, 0x2F3136)
        icon     = ACTION_ICONS.get(action, "🔹")
        label    = ACTION_LABELS.get(action, action.capitalize())
        user_id  = data.get("user_id")
        user_tag = data.get("user_tag") or (f"ID: {user_id}" if user_id else "Unknown")
        now      = datetime.now(timezone.utc)

        user_mention = f"<@{user_id}>" if user_id else "Unknown"

        # --- Resolve moderator ---
        # Priority: moderator_id (from a mention in the Dyno embed) →
        #           name lookup in guild members → plain display string.
        mod_id: Optional[int]    = data.get("moderator_id")   # type: ignore[assignment]
        mod_display: str         = str(data.get("moderator") or "Unknown")
        guild                    = log_ch.guild

        resolved_member: Optional[discord.Member] = None

        if mod_id:
            # Direct ID match — most reliable
            resolved_member = guild.get_member(mod_id) if guild else None
        elif guild and mod_display and mod_display != "Unknown":
            # Fallback: search by display name or username
            # Also handle case where mod_display is a bare numeric string (older Dyno format)
            if re.fullmatch(r"\d{17,20}", mod_display):
                resolved_member = guild.get_member(int(mod_display))
                if resolved_member:
                    mod_id = resolved_member.id
            else:
                resolved_member = discord.utils.find(
                    lambda m: m.display_name == mod_display or str(m) == mod_display,
                    guild.members,
                )
                if resolved_member:
                    mod_id = resolved_member.id

        # Build the moderator display value for the embed
        if resolved_member:
            mod_embed_value = resolved_member.mention
        elif mod_id:
            mod_embed_value = f"<@{mod_id}>"
        else:
            mod_embed_value = f"**{mod_display}**"

        embed = discord.Embed(
            title=f"{icon}  Moderation Action — {label}",
            color=color,
            timestamp=now,
        )
        embed.add_field(name="👤  Target User",  value=f"{user_mention}\n`{user_tag}`", inline=True)
        embed.add_field(name="⚖️  Action",        value=f"`{label}`",                   inline=True)
        embed.add_field(name="🛡️  Moderator",     value=mod_embed_value,                inline=True)
        embed.add_field(name="📋  Reason",        value=str(data.get("reason") or "No reason provided."), inline=False)
        embed.add_field(name="🔗  Source",        value=f"[Jump to original log]({src.jump_url})", inline=False)

        # Embed moderator ID + action key in footer so modlogdelete can reverse the stat
        final_mod_id = mod_id or (resolved_member.id if resolved_member else 0)
        embed.set_footer(
            text=(
                f"Logged from #{src.channel.name}  •  {now.strftime('%d %b %Y, %H:%M UTC')}"
                f"  ║  mod:{final_mod_id}  ║  act:{action}"
            ),
            icon_url=self.bot.user.display_avatar.url,
        )
        log_msg = await log_ch.send(embed=embed)

        # Open a thread on the log message so staff can drop screenshots/links directly
        try:
            action_part = (embed.title or "").split("—")[-1].strip() or label
            await log_msg.create_thread(
                name=f"Evidence — {action_part}"[:100],
                auto_archive_duration=10080,  # 7 days
            )
        except (discord.Forbidden, discord.HTTPException):
            pass  # threads not supported or bot lacks permission — carry on

        # Record the action against the moderator's stat counter, storing the log link as evidence
        record_id = mod_id or (resolved_member.id if resolved_member else None)
        if record_id:
            self._record_action(record_id, action, link=log_msg.jump_url)

    # ------------------------------------------------------------------ #
    # Role-change listener (promotion / demotion auto-log)                 #
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.roles == after.roles:
            return
        role_map = self._staff_role_map()
        if not role_map:
            return

        id_to_rank = {v: k for k, v in role_map.items()}
        staff_ids  = set(role_map.values())

        before_ids = {r.id for r in before.roles}
        after_ids  = {r.id for r in after.roles}
        added      = after_ids - before_ids
        removed    = before_ids - after_ids

        # Track when the role was assigned.
        # Re-read from disk before each write so a bot restart or crash never
        # causes one member's entry to silently overwrite another's.
        uid = str(after.id)
        now_iso = datetime.now(timezone.utc).isoformat()
        for rank, rid in role_map.items():
            if rid in added:
                self._role_history = _load(ROLE_HISTORY_FILE)
                self._role_history.setdefault(uid, {})[rank] = now_iso
                _save(ROLE_HISTORY_FILE, self._role_history)

        # Log promotions / demotions
        for rid in added & staff_ids:
            new_rank = id_to_rank.get(rid)
            if not new_rank:
                continue
            old_rank: Optional[str] = None
            for oid in removed & staff_ids:
                candidate = id_to_rank.get(oid)
                if candidate and RANK_HIERARCHY.index(candidate) < RANK_HIERARCHY.index(new_rank):
                    old_rank = candidate
                    break
            await self._log_rank_change(after, old_rank, new_rank)

    async def _log_rank_change(
        self,
        member: discord.Member,
        old_rank: Optional[str],
        new_rank: str,
    ) -> None:
        ch = self._channel("PROMOTION_LOG_CHANNEL")
        if not ch:
            return
        is_promo = (
            old_rank is None
            or RANK_HIERARCHY.index(new_rank) > RANK_HIERARCHY.index(old_rank)
        )
        color = 0x2ECC71 if is_promo else 0xE74C3C
        title = "📈  Promotion Logged" if is_promo else "📉  Role Update Logged"
        now   = datetime.now(timezone.utc)

        embed = discord.Embed(title=title, color=color, timestamp=now)
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        if old_rank:
            embed.add_field(name="Previous Rank", value=f"{RANK_EMOJIS.get(old_rank, '')} {old_rank}", inline=True)
        embed.add_field(name="New Rank",  value=f"{RANK_EMOJIS.get(new_rank, '')} {new_rank}", inline=True)
        embed.add_field(name="Member",    value=member.mention, inline=True)
        embed.set_footer(text=f"User ID: {member.id}", icon_url=self.bot.user.display_avatar.url)
        await ch.send(embed=embed)

    # ------------------------------------------------------------------ #
    # Weekly report task                                                   #
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=5)
    async def weekly_report_task(self) -> None:
        # Fires every Sunday at 16:00 UTC = Monday 00:00 AM GMT+8
        now = datetime.now(timezone.utc)
        if now.weekday() != 6:   # 6 = Sunday UTC
            return
        if now.hour != 16:
            return
        today = now.strftime("%Y-%m-%d")
        if self._last_report_date == today:
            return
        self._last_report_date = today
        # Report covers the week that just ended (Mon–Sun GMT+8).
        # The task fires on Sunday 16:00 UTC = Monday 00:00 GMT+8, so 'now' is
        # still Sunday UTC — the final day of the week being summarised.
        # Using 'now' for the week key correctly matches how _record_action stores data.
        week        = self._week_key(now)
        this_monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        this_sunday = this_monday + timedelta(days=6, hours=23, minutes=59, seconds=59)
        await self._post_activity_report(
            period_label=f"{ts(this_monday, 'D')} — {ts(this_sunday, 'D')}",
            week_key=week,
            week_dt=now,
            title_suffix="(Automatic — Weekly Report)",
        )

    @weekly_report_task.before_loop
    async def _before_weekly(self) -> None:
        await self.bot.wait_until_ready()

    def _action_field_chunks(
        self,
        a: str,
        count: int,
        links: List[str],
    ) -> List[Tuple[str, str]]:
        """
        Convert one action + all its evidence links into one or more (name, value) tuples.
        Splits at 1000 chars so each field stays inside Discord's 1024-char limit.
        Extra chunks get a "↳ (cont.)" name so the paginator can spill them to new pages.
        """
        base_name = f"{ACTION_ICONS[a]}  {ACTION_LABELS[a]}"
        if not links:
            return [(base_name, f"**{count}**")]

        result: List[Tuple[str, str]] = []
        chunk_lines: List[str] = [f"**{count}**"]
        chunk_chars = len(chunk_lines[0]) + 1
        first_field = True

        for i, url in enumerate(links):
            line     = f"[Log {i + 1}]({url})"
            line_len = len(line) + 1
            if chunk_chars + line_len > 1000 and len(chunk_lines) > 1:
                name = base_name if first_field else f"↳ {ACTION_LABELS[a]} (cont.)"
                result.append((name, "\n".join(chunk_lines)))
                chunk_lines = []
                chunk_chars = 0
                first_field = False
            chunk_lines.append(line)
            chunk_chars += line_len

        if chunk_lines:
            name = base_name if first_field else f"↳ {ACTION_LABELS[a]} (cont.)"
            result.append((name, "\n".join(chunk_lines)))

        return result

    def _fields_to_pages(
        self,
        field_tuples: List[Tuple[str, str]],
        embed_factory: Callable[[], discord.Embed],
        first_embed: Optional[discord.Embed] = None,
    ) -> List[discord.Embed]:
        """
        Pack (name, value) field tuples into as many discord.Embeds as needed.
        Starts filling first_embed (if given) before creating new embeds via embed_factory.
        A new embed is created when the current one would exceed 5 800 chars or 24 fields.
        """
        MAX_CHARS  = 5800
        MAX_FIELDS = 24

        def _used(emb: discord.Embed) -> int:
            chars = 0
            for s in (emb.title, emb.description,
                      emb.author.name if emb.author else None,
                      emb.footer.text if emb.footer else None):
                if s:
                    chars += len(s)
            for f in emb.fields:
                chars += len(f.name or "") + len(f.value or "")
            return chars

        pages: List[discord.Embed] = []
        current = first_embed if first_embed is not None else embed_factory()
        used    = _used(current)

        for name, value in field_tuples:
            fc = len(name) + len(value)
            if (len(current.fields) >= MAX_FIELDS or used + fc > MAX_CHARS) and current.fields:
                pages.append(current)
                current = embed_factory()
                used    = _used(current)
            current.add_field(name=name, value=value, inline=True)
            used += fc

        pages.append(current)
        return pages

    def _build_activity_embeds(
        self,
        *,
        guild: discord.Guild,
        period_label: str,
        week_key: str,
        week_dt: datetime,
        title_suffix: str = "",
    ) -> List[discord.Embed]:
        """
        Build and return all embed pages for a staff activity report.
        Page 0 is the header; subsequent pages are per-member breakdowns.
        Each member may span multiple pages when they have many evidence links.
        """
        self._mod_actions = _load(MOD_ACTIONS_FILE)
        now = datetime.now(timezone.utc)

        ACTIVITY_RANKS = {"Trial Moderator", "Moderator", "Senior Moderator"}
        role_map       = self._staff_role_map()
        staff_role_ids = set(role_map.values())
        staff_members: List[discord.Member] = [
            m for m in guild.members
            if {r.id for r in m.roles} & staff_role_ids
            and self._member_rank(m) in ACTIVITY_RANKS
        ]

        header = discord.Embed(
            title=f"📊  Weekly Moderation Activity Report  {title_suffix}".strip(),
            description=(
                f"**Period:** {period_label}\n"
                f"*Covers all mod actions recorded during the selected week.*"
            ),
            color=0x5865F2,
            timestamp=now,
        )
        header.set_footer(
            text=f"Week {week_dt.strftime('%W')} · {week_dt.year}  ·  {len(staff_members)} staff member(s)",
            icon_url=self.bot.user.display_avatar.url,
        )

        if not staff_members:
            no_staff = discord.Embed(
                description="No staff members found with configured roles.",
                color=0x95A5A6,
            )
            return [header, no_staff]

        pages: List[discord.Embed] = [header]
        for member in staff_members:
            actions      = self._week_actions(member.id, week_key)
            total        = sum(actions.values())
            rank         = self._member_rank(member)
            rank_display = f"{RANK_EMOJIS.get(rank, '')} {rank}" if rank else "Staff"
            tir          = self._time_in_rank(member.id, rank) if rank else None
            color        = 0x5865F2 if total > 0 else 0x95A5A6

            # First embed for this member — pre-populate summary header fields
            first_embed = discord.Embed(
                title=f"Staff Activity — {member.display_name}",
                color=color,
                timestamp=now,
            )
            first_embed.set_author(
                name=f"{member.display_name}  ·  {rank_display}",
                icon_url=member.display_avatar.url,
            )
            first_embed.add_field(name="👤  Staff Member",  value=member.mention,   inline=True)
            first_embed.add_field(name="🏅  Rank",          value=rank_display,     inline=True)
            first_embed.add_field(name="📈  Total Actions", value=str(total),       inline=True)
            if rank and tir:
                first_embed.add_field(name=f"⏳  Time as {rank}", value=format_duration(tir), inline=True)
            first_embed.set_footer(
                text=f"User ID: {member.id}  ·  Period: {period_label}",
                icon_url=self.bot.user.display_avatar.url,
            )

            # Gather action fields — each action may produce multiple tuples if links overflow
            action_fields: List[Tuple[str, str]] = []
            for a in ALL_ACTIONS:
                action_fields.extend(
                    self._action_field_chunks(
                        a, actions.get(a, 0), self._week_links(member.id, week_key, a)
                    )
                )

            # Continuation embed factory (used only when action fields overflow the first embed)
            def _cont(m=member, rd=rank_display, pl=period_label, c=color) -> discord.Embed:
                e = discord.Embed(
                    title=f"Staff Activity — {m.display_name}  (cont.)",
                    color=c,
                    timestamp=now,
                )
                e.set_author(name=f"{m.display_name}  ·  {rd}", icon_url=m.display_avatar.url)
                e.set_footer(
                    text=f"User ID: {m.id}  ·  Period: {pl}",
                    icon_url=self.bot.user.display_avatar.url,
                )
                return e

            pages.extend(self._fields_to_pages(action_fields, _cont, first_embed=first_embed))

        return pages

    async def _post_activity_report(
        self,
        *,
        period_label: str,
        week_key: str,
        week_dt: datetime,
        title_suffix: str = "",
    ) -> None:
        """Post the activity report to MOD_ACTIVITY_LOG_CHANNEL (used by the weekly task)."""
        ch = self._channel("MOD_ACTIVITY_LOG_CHANNEL")
        if not ch:
            return
        pages = self._build_activity_embeds(
            guild=ch.guild,
            period_label=period_label,
            week_key=week_key,
            week_dt=week_dt,
            title_suffix=title_suffix,
        )
        # Post the report as separate Discord messages:
        #   1. One summary/header message.
        #   2. One message for each staff member.
        #
        # _fields_to_pages() already splits a staff member's activity across
        # continuation embeds when Discord's embed limits are reached. Sending
        # every page separately means those continuation pages become their own
        # messages instead of being hidden behind a paginator/editing one
        # shared message.
        for page in pages:
            await ch.send(embed=page)

    # ------------------------------------------------------------------ #
    # LOA expiry checker                                                   #
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=5)
    async def check_loa_expiry(self) -> None:
        now = datetime.now(timezone.utc)
        changed = False
        for req_id, req in self._inactivity.items():
            if req.get("status") != "accepted" or req.get("notified"):
                continue
            ends_str = req.get("ends_at")
            if not ends_str:
                continue
            try:
                ends_at = datetime.fromisoformat(ends_str)
            except ValueError:
                continue
            if now < ends_at:
                continue

            uid = req.get("user_id")
            if uid:
                try:
                    user = await self.bot.fetch_user(int(uid))
                    dm = discord.Embed(
                        title="🔔  Leave of Absence Ended",
                        description=(
                            "Your approved Leave of Absence has now ended. Welcome back! "
                            "Please resume your staff duties as soon as possible."
                        ),
                        color=0x2ECC71,
                        timestamp=now,
                    )
                    dm.add_field(name="Duration", value=req.get("duration_str", "N/A"), inline=True)
                    dm.add_field(name="Reason",   value=req.get("reason", "N/A"),       inline=True)
                    dm.set_footer(text="Staff Manager · Automated Notification")
                    await user.send(embed=dm)
                except Exception:
                    pass
            # Remove the inactivity role now that the LOA has ended
            inactivity_role_id = self._cfg_int("INACTIVITY_ROLE_ID")
            if inactivity_role_id and uid:
                for guild in self.bot.guilds:
                    member = guild.get_member(int(uid))
                    if member:
                        role = guild.get_role(inactivity_role_id)
                        if role:
                            try:
                                await member.remove_roles(role, reason="LOA expired")
                            except Exception:
                                pass
                        break

            req["notified"] = True
            changed = True

        if changed:
            _save(INACTIVITY_FILE, self._inactivity)

    @check_loa_expiry.before_loop
    async def _before_loa(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    # LOA decision handler (called by InactivityView buttons)              #
    # ------------------------------------------------------------------ #

    async def _handle_loa_decision(
        self,
        interaction: discord.Interaction,
        request_id: str,
        *,
        accepted: bool,
    ) -> None:
        allowed = self._cfg_list("STAFF_MANAGEMENT_ROLE_IDS")
        if allowed:
            member_rids = {r.id for r in getattr(interaction.user, "roles", [])}
            if not member_rids.intersection(set(allowed)):
                await interaction.response.send_message(
                    "❌ You don't have permission to handle LOA requests.", ephemeral=True
                )
                return

        req = self._inactivity.get(request_id)
        if not req:
            await interaction.response.send_message("❌ Request not found.", ephemeral=True)
            return
        if req.get("status") not in (None, "pending"):
            await interaction.response.send_message(
                f"ℹ️ This request was already **{req['status']}**.", ephemeral=True
            )
            return

        now = datetime.now(timezone.utc)
        req["status"]      = "accepted" if accepted else "declined"
        req["reviewed_by"] = str(interaction.user)
        req["reviewed_at"] = now.isoformat()

        if accepted:
            td = parse_duration(req.get("raw_duration", ""))
            ends_at = now + (td if td else timedelta(days=14))
            req["ends_at"] = ends_at.isoformat()

            # Assign the inactivity role to the member
            inactivity_role_id = self._cfg_int("INACTIVITY_ROLE_ID")
            uid_str = req.get("user_id")
            if inactivity_role_id and uid_str:
                for guild in self.bot.guilds:
                    member = guild.get_member(int(uid_str))
                    if member:
                        role = guild.get_role(inactivity_role_id)
                        if role:
                            try:
                                await member.add_roles(role, reason="LOA approved")
                            except Exception:
                                pass
                        break

        _save(INACTIVITY_FILE, self._inactivity)

        label = "✅ Accepted" if accepted else "❌ Declined"
        color = 0x2ECC71 if accepted else 0xE74C3C

        new_embed: Optional[discord.Embed] = None
        if interaction.message and interaction.message.embeds:
            old = interaction.message.embeds[0]
            new_embed = discord.Embed(
                title=old.title,
                description=old.description,
                color=color,
                timestamp=old.timestamp,
            )
            for f in old.fields:
                # Update the Status field in-place instead of copying it stale
                if "status" in (f.name or "").lower():
                    new_embed.add_field(name=f.name, value=label, inline=f.inline)
                else:
                    new_embed.add_field(name=f.name, value=f.value, inline=f.inline)
            new_embed.add_field(
                name="📋  Decision",
                value=f"{label} by {interaction.user.mention}\n{ts(now, 'F')}",
                inline=False,
            )
            if accepted and req.get("ends_at"):
                new_embed.add_field(
                    name="⏰  LOA Ends",
                    value=ts(datetime.fromisoformat(req["ends_at"]), "F"),
                    inline=False,
                )
            if old.footer.text:
                new_embed.set_footer(text=old.footer.text, icon_url=old.footer.icon_url)
            if old.author.name:
                new_embed.set_author(name=old.author.name, icon_url=old.author.icon_url)

        # Respond to the interaction by editing the message (must be first API call)
        if new_embed:
            await interaction.response.edit_message(embed=new_embed, view=None)
        else:
            await interaction.response.defer(ephemeral=True)

        await interaction.followup.send(
            f"{label} by {interaction.user.mention}.", ephemeral=True
        )

        uid = req.get("user_id")
        if uid:
            try:
                user = await self.bot.fetch_user(int(uid))
                dm = discord.Embed(
                    title="✅  LOA Approved" if accepted else "❌  LOA Declined",
                    color=color,
                    timestamp=now,
                )
                if not accepted:
                    dm.description = (
                        "Your Leave of Absence request was not approved at this time. "
                        "Please contact Staff Management if you have questions."
                    )
                dm.add_field(name="Reviewed by", value=str(interaction.user), inline=True)
                dm.add_field(name="Duration",    value=req.get("duration_str", "N/A"), inline=True)
                dm.add_field(name="Reason",      value=req.get("reason", "N/A"), inline=False)
                if accepted and req.get("ends_at"):
                    dm.add_field(
                        name="Your LOA ends",
                        value=ts(datetime.fromisoformat(req["ends_at"]), "F"),
                        inline=False,
                    )
                dm.set_footer(text="Staff Manager · Automated Notification")
                await user.send(embed=dm)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Promotion decision handler (called by PromotionView buttons)         #
    # ------------------------------------------------------------------ #

    async def _handle_promotion_decision(
        self,
        interaction: discord.Interaction,
        request_id: str,
        *,
        accepted: bool,
    ) -> None:
        allowed = self._cfg_list("HIGH_RANK_ROLE_IDS")
        if allowed:
            member_rids = {r.id for r in getattr(interaction.user, "roles", [])}
            if not member_rids.intersection(set(allowed)):
                await interaction.response.send_message(
                    "❌ You don't have permission to handle promotion requests.", ephemeral=True
                )
                return

        req = self._pending_promotions.get(request_id)
        if not req:
            await interaction.response.send_message("❌ Request not found.", ephemeral=True)
            return
        if req.get("status") not in (None, "pending"):
            await interaction.response.send_message(
                f"ℹ️ This request was already **{req['status']}**.", ephemeral=True
            )
            return

        # Staff Management cannot accept a request for Staff Management rank —
        # only Head of Staff and above may do so.
        if accepted and req.get("desired_rank") == "Staff Management":
            higher_ids = {
                self._cfg_int("HEAD_OF_STAFF_ROLE_ID"),
                self._cfg_int("ADMIN_ROLE_ID"),
                self._cfg_int("HEAD_ADMIN_ROLE_ID"),
            }
            reviewer_rids = {r.id for r in getattr(interaction.user, "roles", [])}
            if not reviewer_rids.intersection(higher_ids):
                await interaction.response.send_message(
                    "❌ Only **Head of Staff** and above can accept Staff Management promotion requests.",
                    ephemeral=True,
                )
                return

        now = datetime.now(timezone.utc)
        req["status"]      = "accepted" if accepted else "declined"
        req["reviewed_by"] = str(interaction.user)
        req["reviewed_at"] = now.isoformat()
        _save(PROMOTIONS_FILE, self._pending_promotions)

        label = "✅ Accepted" if accepted else "❌ Declined"
        color = 0x2ECC71 if accepted else 0xE74C3C

        new_embed: Optional[discord.Embed] = None
        if interaction.message and interaction.message.embeds:
            old = interaction.message.embeds[0]
            new_embed = discord.Embed(
                title=old.title,
                description=old.description,
                color=color,
                timestamp=old.timestamp,
            )
            for f in old.fields:
                # Update the Status field in-place instead of copying it stale
                if "status" in (f.name or "").lower():
                    new_embed.add_field(name=f.name, value=label, inline=f.inline)
                else:
                    new_embed.add_field(name=f.name, value=f.value, inline=f.inline)
            new_embed.add_field(
                name="📋  Decision",
                value=f"{label} by {interaction.user.mention}\n{ts(now, 'F')}",
                inline=False,
            )
            if old.footer.text:
                new_embed.set_footer(text=old.footer.text, icon_url=old.footer.icon_url)
            if old.author.name:
                new_embed.set_author(name=old.author.name, icon_url=old.author.icon_url)
            if old.thumbnail.url:
                new_embed.set_thumbnail(url=old.thumbnail.url)

        # Respond to the interaction by editing the message (must be first API call)
        if new_embed:
            await interaction.response.edit_message(embed=new_embed, view=None)
        else:
            await interaction.response.defer(ephemeral=True)

        await interaction.followup.send(
            f"{label} by {interaction.user.mention}.", ephemeral=True
        )

        uid           = req.get("user_id")
        current_rank  = req.get("current_rank", "")
        desired_rank  = req.get("desired_rank", "")

        if uid:
            try:
                user = await self.bot.fetch_user(int(uid))
                dm = discord.Embed(
                    title="🎉  Promotion Approved!" if accepted else "📋  Promotion Declined",
                    color=color,
                    timestamp=now,
                )
                if accepted:
                    dm.description = (
                        f"Congratulations! Your promotion to "
                        f"**{RANK_EMOJIS.get(desired_rank, '')} {desired_rank}** has been approved. "
                        f"Keep up the excellent work!"
                    )
                else:
                    dm.description = (
                        f"Your promotion request from **{current_rank}** to **{desired_rank}** "
                        f"was not approved at this time. "
                        f"Please continue working hard and feel free to reach out to Staff Management."
                    )
                dm.add_field(name="Reviewed by",  value=str(interaction.user), inline=True)
                dm.add_field(name="Current Rank", value=current_rank,          inline=True)
                if accepted:
                    dm.add_field(
                        name="New Rank",
                        value=f"{RANK_EMOJIS.get(desired_rank, '')} {desired_rank}",
                        inline=True,
                    )
                dm.set_footer(text="Staff Manager · Automated Notification")
                await user.send(embed=dm)
            except Exception:
                pass

        if accepted:
            log_ch = self._channel("PROMOTION_LOG_CHANNEL")
            if log_ch:
                target = log_ch.guild.get_member(int(uid)) if uid else None
                promo  = discord.Embed(title="🎊  Staff Promotion", color=0x2ECC71, timestamp=now)
                if target:
                    promo.set_author(name=str(target), icon_url=target.display_avatar.url)
                promo.add_field(name="Staff Member",  value=f"<@{uid}>" if uid else "Unknown", inline=True)
                promo.add_field(name="Previous Rank", value=f"{RANK_EMOJIS.get(current_rank, '')} {current_rank}", inline=True)
                promo.add_field(name="New Rank",      value=f"{RANK_EMOJIS.get(desired_rank, '')} {desired_rank}", inline=True)
                promo.add_field(name="Approved by",   value=interaction.user.mention, inline=True)
                promo.add_field(name="Date",          value=ts(now, "F"),             inline=True)
                promo.set_footer(text=f"User ID: {uid}", icon_url=self.bot.user.display_avatar.url)
                await log_ch.send(embed=promo)

            # Swap rank roles on the member
            if uid:
                role_map = self._staff_role_map()
                old_role_id = role_map.get(current_rank)
                new_role_id = role_map.get(desired_rank)
                for guild in self.bot.guilds:
                    member = guild.get_member(int(uid))
                    if member:
                        try:
                            if new_role_id:
                                new_role = guild.get_role(new_role_id)
                                if new_role:
                                    await member.add_roles(new_role, reason=f"Promoted to {desired_rank}")
                            if old_role_id:
                                old_role = guild.get_role(old_role_id)
                                if old_role:
                                    await member.remove_roles(old_role, reason=f"Promoted from {current_rank}")
                        except Exception:
                            pass
                        break

            # Reset the promoted staff member's stats across all weeks
            if uid:
                self._mod_actions = _load(MOD_ACTIONS_FILE)
                uid_str = str(uid)
                for week_data in self._mod_actions.values():
                    week_data.pop(uid_str, None)
                _save(MOD_ACTIONS_FILE, self._mod_actions)

    # ------------------------------------------------------------------ #
    # Commands                                                             #
    # ------------------------------------------------------------------ #

    @commands.command(name="inactivityreq", aliases=["loa", "loareq"])
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def inactivity_request(
        self, ctx: commands.Context, duration: str, *, reason: str
    ) -> None:
        """
        Submit a Leave of Absence request.
        Usage: !inactivityreq <duration> <reason>
        Examples:
          !inactivityreq 5d I'm sick
          !inactivityreq 2w Family emergency
          !inactivityreq 1d12h Mental health break
        """
        rank = self._member_rank(ctx.author) or "Modmail Moderator+"

        td = parse_duration(duration)
        if td is None:
            await ctx.send(
                embed=discord.Embed(
                    description=(
                        "❌ Invalid duration. Examples: `5d`, `2h`, `30m`, `1w`, `1d12h`\n"
                        "**w** = weeks · **d** = days · **h** = hours · **m** = minutes"
                    ),
                    color=0xE74C3C,
                ),
                delete_after=15,
            )
            return

        if td > timedelta(days=14):
            await ctx.send(
                embed=discord.Embed(description="❌ Maximum LOA duration is **14 days**.", color=0xE74C3C),
                delete_after=10,
            )
            return

        ch = self._channel("INACTIVITY_CHANNEL")
        if not ch:
            await ctx.send(
                embed=discord.Embed(description="❌ Inactivity channel is not configured.", color=0xE74C3C),
                delete_after=10,
            )
            return

        now          = datetime.now(timezone.utc)
        duration_str = format_duration(td)
        request_id   = f"{ctx.author.id}_{int(now.timestamp())}"

        embed = discord.Embed(title="📋  Leave of Absence Request", color=0xF1C40F, timestamp=now)
        embed.set_author(
            name=f"{ctx.author.display_name}  ·  {rank}",
            icon_url=ctx.author.display_avatar.url,
        )
        embed.add_field(name="🛡️  Staff Member",       value=f"{ctx.author.mention}\n`{ctx.author}`", inline=True)
        embed.add_field(name="🏅  Current Rank",        value=f"{RANK_EMOJIS.get(rank, '')} {rank}",  inline=True)
        embed.add_field(name="⏱️  Requested Duration",  value=f"**{duration_str}**",                  inline=True)
        embed.add_field(name="📄  Reason",              value=reason,                                  inline=False)
        embed.add_field(name="🕐  Submitted",           value=ts(now, "F"),                            inline=True)
        embed.add_field(name="📌  Status",              value="⏳ Pending Review",                     inline=True)
        embed.set_footer(
            text=f"User ID: {ctx.author.id}  ·  Request ID: {request_id}",
            icon_url=self.bot.user.display_avatar.url,
        )

        # Determine who to ping based on the requester's rank
        LOWER_RANKS = {"Trial Moderator", "Moderator", "Senior Moderator"}
        if rank in LOWER_RANKS:
            # Trial Mod / Mod / Senior Mod → ping Staff Management
            ping_ids = [self._cfg_int("STAFF_MANAGEMENT_ROLE_ID")]
        elif rank == "Staff Management":
            # Staff Management → ping Head of Staff
            ping_ids = [self._cfg_int("HEAD_OF_STAFF_ROLE_ID")]
        else:
            ping_ids = []
        ping_text = " ".join(f"<@&{rid}>" for rid in ping_ids if rid) or None

        view = InactivityView(self, request_id)
        msg  = await ch.send(content=ping_text, embed=embed, view=view)

        self._inactivity[request_id] = {
            "user_id":      str(ctx.author.id),
            "raw_duration": duration,
            "duration_str": duration_str,
            "reason":       reason,
            "status":       "pending",
            "message_id":   str(msg.id),
            "channel_id":   str(ch.id),
        }
        _save(INACTIVITY_FILE, self._inactivity)

        await ctx.message.add_reaction("✅")
        try:
            conf = discord.Embed(
                title="✅  LOA Request Submitted",
                description=(
                    f"Your request has been submitted to {ch.mention}.\n"
                    "You will receive a DM once it has been reviewed."
                ),
                color=0x2ECC71,
                timestamp=now,
            )
            conf.add_field(name="Duration", value=duration_str, inline=True)
            conf.add_field(name="Reason",   value=reason,       inline=True)
            conf.set_footer(text="Staff Manager · Automated Notification")
            await ctx.author.send(embed=conf)
        except discord.Forbidden:
            pass

    @commands.command(name="promotionreq", aliases=["promote", "promoapp"])
    @commands.check(staff_moderator_check)
    async def promotion_request(self, ctx: commands.Context, *, reason: str) -> None:
        """
        Submit a promotion request.
        Usage: !promotionreq <reason>
        Example: !promotionreq I have met all requirements and been active consistently.
        """
        rank = self._member_rank(ctx.author)  # type: ignore[arg-type]
        if not rank:
            await ctx.send(
                embed=discord.Embed(description="❌ You must be a staff member to use this command.", color=0xE74C3C),
                delete_after=10,
            )
            return

        ineligible = ["Staff Management", "Head of Staff", "Admin", "Head Admin"]
        if rank in ineligible:
            await ctx.send(
                embed=discord.Embed(
                    description=f"❌ **{rank}** members are not eligible to submit promotion requests.",
                    color=0xE74C3C,
                ),
                delete_after=10,
            )
            return

        desired_rank = self._next_rank(rank)
        if not desired_rank:
            await ctx.send(
                embed=discord.Embed(description="❌ You are already at the highest promotable rank.", color=0xE74C3C),
                delete_after=10,
            )
            return

        ch = self._channel("PROMOTION_REQUEST_CHANNEL")
        if not ch:
            await ctx.send(
                embed=discord.Embed(description="❌ Promotion request channel is not configured.", color=0xE74C3C),
                delete_after=10,
            )
            return

        totals        = self._lifetime_totals(ctx.author.id)
        time_in_rank  = self._time_in_rank(ctx.author.id, rank)
        time_str      = format_duration(time_in_rank) if time_in_rank else "Unknown (role not yet tracked)"
        now           = datetime.now(timezone.utc)
        request_id    = f"{ctx.author.id}_{int(now.timestamp())}"

        achievement_lines = [
            f"{ACTION_ICONS[a]} {ACTION_LABELS[a]}: **{totals[a]}**"
            for a in ALL_ACTIONS if totals.get(a, 0)
        ] or ["*No tracked mod actions yet.*"]

        # Only ping Staff Management and Head of Staff — not Admin/Head Admin
        ping_text = " ".join(f"<@&{rid}>" for rid in self._cfg_list("PROMOTION_PING_ROLE_IDS"))

        embed = discord.Embed(title="🌟  Promotion Request", color=0xF1C40F, timestamp=now)
        embed.set_author(
            name=f"{ctx.author.display_name}  ·  {rank}",
            icon_url=ctx.author.display_avatar.url,
        )
        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        embed.add_field(name="👤  Staff Name",            value=f"{ctx.author.mention}\n`{ctx.author}`",        inline=True)
        embed.add_field(name="🏅  Current Rank",           value=f"{RANK_EMOJIS.get(rank, '')} {rank}",         inline=True)
        embed.add_field(name="🎯  Desired Rank",           value=f"{RANK_EMOJIS.get(desired_rank, '')} {desired_rank}", inline=True)
        embed.add_field(name="⏳  Time in Current Rank",   value=time_str,                                       inline=False)
        embed.add_field(name="🏆  Achievements / Evidence",value="\n".join(achievement_lines),                  inline=False)
        embed.add_field(name="📝  Reason for Promotion",   value=reason,                                         inline=False)
        embed.add_field(name="🕐  Timestamp",              value=ts(now, "F"),                                   inline=True)
        embed.add_field(name="📌  Status",                 value="⏳ Pending Review",                            inline=True)
        embed.set_footer(
            text=f"User ID: {ctx.author.id}  ·  Request ID: {request_id}",
            icon_url=self.bot.user.display_avatar.url,
        )

        view    = PromotionView(self, request_id)
        content = ping_text or None
        msg     = await ch.send(content=content, embed=embed, view=view)

        self._pending_promotions[request_id] = {
            "user_id":      str(ctx.author.id),
            "current_rank": rank,
            "desired_rank": desired_rank,
            "reason":       reason,
            "status":       "pending",
            "message_id":   str(msg.id),
            "channel_id":   str(ch.id),
        }
        _save(PROMOTIONS_FILE, self._pending_promotions)

        await ctx.message.add_reaction("✅")
        try:
            conf = discord.Embed(
                title="✅  Promotion Request Submitted",
                description=(
                    f"Your request has been submitted to {ch.mention}.\n"
                    "You will receive a DM once it has been reviewed."
                ),
                color=0x2ECC71,
                timestamp=now,
            )
            conf.add_field(name="Current Rank", value=rank,         inline=True)
            conf.add_field(name="Desired Rank", value=desired_rank, inline=True)
            conf.set_footer(text="Staff Manager · Automated Notification")
            await ctx.author.send(embed=conf)
        except discord.Forbidden:
            pass

    @commands.command(name="modlog")
    @commands.check(staff_moderator_check)
    async def manual_mod_log(
        self,
        ctx: commands.Context,
        action: str,
        target: discord.User,
        moderator: Optional[discord.Member] = None,
        *,
        reason: str = "No reason provided.",
    ) -> None:
        """
        Manually log a mod action from an external workflow or correction.
        Usage: !modlog <action> <@target> [@moderator] [reason]
          action    — warn · mute · kick · ban · softban · note · ticket_close
          target    — the user being actioned / ticket opener (required)
          moderator — who gets the credit (optional, defaults to you)
          reason    — reason for the action (optional)

        Examples:
          !modlog ban @BadUser Spamming
          !modlog warn @BadUser @Will reason goes here
          !modlog ticket_close @User @Will closed manually
        """
        action = action.lower()
        if action not in ACTION_COLORS:
            valid = " · ".join(ACTION_COLORS.keys())
            await ctx.send(
                embed=discord.Embed(description=f"❌ Unknown action. Valid actions:\n`{valid}`", color=0xE74C3C),
                delete_after=10,
            )
            return
        ch = self._channel("MOD_ACTION_LOG_CHANNEL")
        if not ch:
            await ctx.send(
                embed=discord.Embed(description="❌ Mod action log channel not configured.", color=0xE74C3C),
                delete_after=10,
            )
            return

        mod = moderator or ctx.author
        data = {
            "action":       action,
            "user_id":      target.id,
            "user_tag":     str(target),
            "moderator":    str(mod),
            "moderator_id": mod.id,
            "reason":       reason,
        }
        await self._post_mod_action(ch, data, ctx.message)
        await ctx.message.add_reaction("✅")

    @commands.command(name="staffstats")
    @commands.check(staff_moderator_check)
    async def staff_stats(
        self,
        ctx: commands.Context,
        member: Optional[discord.Member] = None,
    ) -> None:
        """
        Show mod action stats for a staff member, one page per action type.
        Pages: Warns → Mutes → Kicks → Bans → Softbans → Notes → Tickets Closed.
        Each page shows this week's count + evidence links and the all-time total.
        Usage: !staffstats [@member]
        """
        target = member or ctx.author  # type: ignore[assignment]
        self._mod_actions = _load(MOD_ACTIONS_FILE)

        now         = datetime.now(timezone.utc)
        week_key    = self._week_key()
        rank        = self._member_rank(target)  # type: ignore[arg-type]
        rank_display = f"{RANK_EMOJIS.get(rank, '')} {rank}" if rank else "Staff"
        tir         = self._time_in_rank(target.id, rank) if rank else None
        week_actions = self._week_actions(target.id, week_key)
        totals       = self._lifetime_totals(target.id)
        footer_base  = f"User ID: {target.id}  ·  Week: {week_key}"

        pages: List[discord.Embed] = []

        for idx, a in enumerate(ALL_ACTIONS):
            icon    = ACTION_ICONS.get(a, "🔹")
            label   = ACTION_LABELS.get(a, a.capitalize())
            color   = ACTION_COLORS.get(a, 0x5865F2)
            count_w = week_actions.get(a, 0)
            count_t = totals.get(a, 0)
            links   = self._week_links(target.id, week_key, a)

            def _make(lbl=label, ic=icon, col=color, cont: bool = False) -> discord.Embed:
                title = f"{ic}  {lbl}" + ("  (cont.)" if cont else "")
                e = discord.Embed(title=title, color=col, timestamp=now)
                e.set_author(
                    name=f"{target.display_name}  ·  {rank_display}",
                    icon_url=target.display_avatar.url,
                )
                e.set_footer(
                    text=f"{footer_base}  ·  {lbl}",
                    icon_url=self.bot.user.display_avatar.url,
                )
                return e

            first_page = _make()
            first_page.add_field(name="📅  This Week", value=str(count_w), inline=True)
            first_page.add_field(name="📈  All-Time",  value=str(count_t), inline=True)
            # Show rank / time-in-rank once, on the first action's page
            if idx == 0 and rank and tir:
                first_page.add_field(
                    name=f"⏳  Time as {rank}",
                    value=format_duration(tir),
                    inline=True,
                )

            # Chunk evidence links (each chunk fits inside a 1000-char field)
            if links:
                link_chunks: List[List[str]] = []
                chunk_buf: List[str] = []
                chunk_len = 0
                for i, url in enumerate(links):
                    line = f"[Log {i + 1}]({url})"
                    if chunk_len + len(line) + 1 > 1000 and chunk_buf:
                        link_chunks.append(chunk_buf)
                        chunk_buf = []
                        chunk_len = 0
                    chunk_buf.append(line)
                    chunk_len += len(line) + 1
                if chunk_buf:
                    link_chunks.append(chunk_buf)

                first_page.add_field(
                    name="🔗  Evidence — This Week",
                    value="\n".join(link_chunks[0]),
                    inline=False,
                )
                pages.append(first_page)

                for lc in link_chunks[1:]:
                    cont_page = _make(cont=True)
                    cont_page.add_field(
                        name="🔗  Evidence (cont.)",
                        value="\n".join(lc),
                        inline=False,
                    )
                    pages.append(cont_page)
            else:
                pages.append(first_page)

        view = PageView(pages)
        await ctx.send(embed=pages[0], view=view)

    @commands.command(name="staffactivity", aliases=["activityreport", "weeklyreport"])
    @commands.check(staff_admin_check)
    async def staff_activity(
        self,
        ctx: commands.Context,
        week_offset: int = 0,
    ) -> None:
        """
        Show a staff activity report as separate messages.
        The summary header, each staff member, and any continuation pages are
        posted as individual messages instead of being placed behind buttons.
        Usage: !staffactivity [week_offset]
          week_offset: 0 = current week (default), 1 = last week, 2 = two weeks ago

        Examples:
          !staffactivity        — current week
          !staffactivity 1      — last week
          !staffactivity 2      — two weeks ago
        """
        ch = self._channel("MOD_ACTIVITY_LOG_CHANNEL")
        if not ch:
            await ctx.send(
                embed=discord.Embed(
                    description="❌ Activity log channel (`MOD_ACTIVITY_LOG_CHANNEL`) is not configured.",
                    color=0xE74C3C,
                ),
                delete_after=10,
            )
            return

        now       = datetime.now(timezone.utc)
        target_dt = now - timedelta(weeks=week_offset)
        week_key  = self._week_key(target_dt)

        monday = (target_dt - timedelta(days=target_dt.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc
        )
        sunday = monday + timedelta(days=6, hours=23, minutes=59, seconds=59)

        period_label = f"{ts(monday, 'D')} — {ts(sunday, 'D')}"
        suffix       = "(Manual)" if week_offset == 0 else f"(Manual — {week_offset} week{'s' if week_offset != 1 else ''} ago)"

        pages = self._build_activity_embeds(
            guild=ch.guild,
            period_label=period_label,
            week_key=week_key,
            week_dt=target_dt,
            title_suffix=suffix,
        )
        await ctx.message.add_reaction("✅")
        # Keep every report page as its own Discord message. This prevents the
        # report from becoming a button-controlled paginator and lets each
        # staff member's log remain visible independently.
        for page in pages:
            await ch.send(embed=page)

    @commands.command(name="modlogdelete", aliases=["delmodlog", "deletemodlog", "modlogdel"])
    @commands.check(staff_admin_check)
    async def modlog_delete(
        self,
        ctx: commands.Context,
        message_id: int,
    ) -> None:
        """
        Delete a mod log entry from the mod action log channel by message ID.
        Usage: !modlogdelete <message_id>
        Example: !modlogdelete 1234567890123456789

        Requires kick_members permission. Right-click the log message → Copy ID.
        """
        ch = self._channel("MOD_ACTION_LOG_CHANNEL")
        if not ch:
            await ctx.send(
                embed=discord.Embed(
                    description="❌ Mod action log channel is not configured.",
                    color=0xE74C3C,
                ),
                delete_after=10,
            )
            return

        try:
            target_msg = await ch.fetch_message(message_id)
        except discord.NotFound:
            await ctx.send(
                embed=discord.Embed(
                    description=(
                        f"❌ Message `{message_id}` not found in {ch.mention}.\n"
                        "Make sure you copied the ID from the correct channel."
                    ),
                    color=0xE74C3C,
                ),
                delete_after=12,
            )
            return
        except discord.Forbidden:
            await ctx.send(
                embed=discord.Embed(
                    description="❌ I don't have permission to read messages in that channel.",
                    color=0xE74C3C,
                ),
                delete_after=10,
            )
            return

        # Confirm it looks like one of our log embeds before deleting
        is_our_log = (
            target_msg.author.id == self.bot.user.id
            and target_msg.embeds
            and "Moderation Action" in (target_msg.embeds[0].title or "")
        )
        if not is_our_log:
            await ctx.send(
                embed=discord.Embed(
                    description=(
                        "❌ That message doesn't appear to be a mod log entry posted by me.\n"
                        "Deletion aborted."
                    ),
                    color=0xE74C3C,
                ),
                delete_after=12,
            )
            return

        # Extract moderator ID + action from the footer we embedded at log time
        log_embed   = target_msg.embeds[0]
        footer_text = log_embed.footer.text or ""
        # Staff Strike moderation logs use bullet separators while the
        # original staff-manager logs used the older double-bar format.
        mod_match   = re.search(r"(?:║|•)\s*mod:(\d+)", footer_text)
        act_match   = re.search(r"(?:║|•)\s*act:(\w+)", footer_text)
        stored_mod_id  = int(mod_match.group(1)) if mod_match else None
        stored_action  = act_match.group(1) if act_match else None
        log_jump_url   = target_msg.jump_url

        await target_msg.delete()

        # Reverse the stat — decrement count and remove evidence link
        if stored_mod_id and stored_action:
            self._remove_action(stored_mod_id, stored_action, link=log_jump_url)

        # Also try to delete the invoking command message for cleanliness
        try:
            await ctx.message.delete()
        except discord.Forbidden:
            pass

        now = datetime.now(timezone.utc)
        confirm = discord.Embed(
            title="🗑️  Mod Log Deleted",
            color=0x95A5A6,
            timestamp=now,
        )
        confirm.add_field(name="Channel",    value=ch.mention,          inline=True)
        confirm.add_field(name="Deleted by", value=ctx.author.mention,  inline=True)
        if stored_action:
            confirm.add_field(
                name="Stats updated",
                value=f"Removed 1 **{ACTION_LABELS.get(stored_action, stored_action)}** from <@{stored_mod_id}>'s count." if stored_mod_id else "Could not resolve moderator — stats unchanged.",
                inline=False,
            )
        confirm.set_footer(
            text=f"Deleted by {ctx.author}",
            icon_url=ctx.author.display_avatar.url,
        )
        await ctx.send(embed=confirm, delete_after=15)

    @commands.command(name="modlogreset", aliases=["resetmodlog", "clearmodlog", "modlogclear"])
    @commands.check(staff_admin_check)
    async def modlog_reset(
        self,
        ctx: commands.Context,
        member: discord.Member,
        scope: str = "week",
    ) -> None:
        """
        Reset a staff member's mod action stats.
        Usage: !modlogreset @member [week|alltime]
          week    — clears only the current week's stats (default)
          alltime — clears ALL weeks of stored data for this person

        Examples:
          !modlogreset @Will Serfort
          !modlogreset @Will Serfort alltime
        """
        scope = scope.lower().strip()
        if scope not in ("week", "alltime"):
            await ctx.send(
                embed=discord.Embed(
                    description="❌ Invalid scope. Use `week` (default) or `alltime`.",
                    color=0xE74C3C,
                ),
                delete_after=10,
            )
            return

        self._mod_actions = _load(MOD_ACTIONS_FILE)
        uid = str(member.id)
        now = datetime.now(timezone.utc)

        if scope == "week":
            week_key = self._week_key()
            week_data = self._mod_actions.get(week_key, {})
            cleared = week_data.pop(uid, {})
            if not any(k for k in cleared if not k.endswith("_links")):
                cleared = {}
            _save(MOD_ACTIONS_FILE, self._mod_actions)
            total_removed = sum(v for k, v in cleared.items() if not k.endswith("_links") and isinstance(v, int))
            scope_label = "this week"
        else:
            total_removed = 0
            for week_data in self._mod_actions.values():
                removed = week_data.pop(uid, {})
                total_removed += sum(v for k, v in removed.items() if not k.endswith("_links") and isinstance(v, int))
            _save(MOD_ACTIONS_FILE, self._mod_actions)
            scope_label = "all time"

        try:
            await ctx.message.delete()
        except discord.Forbidden:
            pass

        embed = discord.Embed(
            title="🔄  Mod Stats Reset",
            color=0xE67E22,
            timestamp=now,
        )
        embed.set_author(
            name=member.display_name,
            icon_url=member.display_avatar.url,
        )
        embed.add_field(name="👤  Staff Member", value=member.mention,       inline=True)
        embed.add_field(name="📅  Scope",        value=scope_label.title(),  inline=True)
        embed.add_field(name="🗑️  Actions Removed", value=str(total_removed), inline=True)
        embed.set_footer(
            text=f"Reset by {ctx.author}",
            icon_url=ctx.author.display_avatar.url,
        )
        await ctx.send(embed=embed, delete_after=20)

    @commands.command(name="modstatsreset", aliases=["resetstats", "clearstats", "statsreset"])
    @commands.check(staff_admin_check)
    async def modstats_reset(
        self,
        ctx: commands.Context,
        member: discord.Member,
    ) -> None:
        """
        Completely wipe ALL recorded mod stats for a staff member across every week.
        Usage: !modstatsreset @member

        Example:
          !modstatsreset @Will Serfort
        """
        self._mod_actions = _load(MOD_ACTIONS_FILE)
        uid = str(member.id)
        now = datetime.now(timezone.utc)

        total_removed = 0
        for week_data in self._mod_actions.values():
            removed = week_data.pop(uid, {})
            total_removed += sum(
                v for k, v in removed.items()
                if not k.endswith("_links") and isinstance(v, int)
            )
        _save(MOD_ACTIONS_FILE, self._mod_actions)

        try:
            await ctx.message.delete()
        except discord.Forbidden:
            pass

        embed = discord.Embed(
            title="🗑️  Staff Stats Wiped",
            description=f"All recorded mod actions for {member.mention} have been permanently removed.",
            color=0xE74C3C,
            timestamp=now,
        )
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.add_field(name="👤  Staff Member",     value=member.mention,       inline=True)
        embed.add_field(name="🗑️  Actions Removed", value=str(total_removed),   inline=True)
        embed.set_footer(
            text=f"Reset by {ctx.author}",
            icon_url=ctx.author.display_avatar.url,
        )
        await ctx.send(embed=embed, delete_after=20)

    @commands.command(name="staffleaderboard", aliases=["leaderboard", "lb", "topmods"])
    @commands.check(staff_moderator_check)
    async def staff_leaderboard(
        self,
        ctx: commands.Context,
        scope: str = "week",
        action: str = "total",
    ) -> None:
        """
        Show a ranked leaderboard of staff members by mod action count.
        Usage: !staffleaderboard [scope] [action]
          scope:  week (default) | alltime
          action: total (default) | warn | mute | kick | ban | softban | note

        Examples:
          !staffleaderboard                — this week, all action types combined
          !staffleaderboard alltime        — all-time totals
          !staffleaderboard week warn      — this week's warnings only
          !staffleaderboard alltime ban    — all-time bans only
        """
        scope  = scope.lower().strip()
        action = action.lower().strip()

        # Validate scope
        if scope not in ("week", "alltime"):
            await ctx.send(
                embed=discord.Embed(
                    description=(
                        "❌ Invalid scope. Use `week` or `alltime`.\n"
                        "Example: `!staffleaderboard alltime` or `!staffleaderboard week warn`"
                    ),
                    color=0xE74C3C,
                ),
                delete_after=12,
            )
            return

        # Validate action
        if action != "total" and action not in ACTION_LABELS:
            valid = "total · " + " · ".join(ACTION_LABELS.keys())
            await ctx.send(
                embed=discord.Embed(
                    description=f"❌ Unknown action type. Valid options:\n`{valid}`",
                    color=0xE74C3C,
                ),
                delete_after=12,
            )
            return

        now = datetime.now(timezone.utc)

        # Resolve staff members from roles (STAFF_IDS in this server's config
        # are actually role IDs, so we look up by roles, not STAFF_IDS)
        role_map       = self._staff_role_map()
        staff_role_ids = set(role_map.values())
        staff_members_lb: List[discord.Member] = []
        if ctx.guild:
            for m in ctx.guild.members:
                if {r.id for r in m.roles} & staff_role_ids:
                    staff_members_lb.append(m)

        # Build scores dict {member: count}
        scores: Dict[int, int] = {}

        if scope == "week":
            week_key = self._week_key()
            for m in staff_members_lb:
                acts = self._week_actions(m.id, week_key)
                scores[m.id] = acts.get(action, 0) if action != "total" else sum(acts.values())
        else:
            for m in staff_members_lb:
                totals = self._lifetime_totals(m.id)
                scores[m.id] = totals.get(action, 0) if action != "total" else sum(totals.values())

        # Sort descending; cap at 10 per page
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        medals       = ["🥇", "🥈", "🥉"]
        scope_label  = "This Week" if scope == "week" else "All-Time"
        action_label = "Total Actions" if action == "total" else ACTION_LABELS.get(action, action.capitalize())
        act_icon     = ACTION_ICONS.get(action, "📈") if action != "total" else "📈"
        member_map   = {m.id: m for m in staff_members_lb}

        PER_PAGE = 10
        lb_pages: List[discord.Embed] = []

        # Always produce at least one page (even if empty)
        for page_start in range(0, max(len(ranked), 1), PER_PAGE):
            slice_ = ranked[page_start : page_start + PER_PAGE]
            page_num   = page_start // PER_PAGE + 1
            total_pages = max(1, (len(ranked) + PER_PAGE - 1) // PER_PAGE)

            e = discord.Embed(
                title=f"🏆  Staff Leaderboard — {scope_label}",
                description=f"**Ranked by:** {action_label}",
                color=0xF1C40F,
                timestamp=now,
            )
            e.set_footer(
                text=f"Scope: {scope_label}  ·  {action_label}  ·  Page {page_num}/{total_pages}",
                icon_url=self.bot.user.display_avatar.url,
            )

            if not slice_:
                e.description = f"**Ranked by:** {action_label}\n\n*No data recorded for this period yet.*"
            else:
                lines: List[str] = []
                for pos, (uid, cnt) in enumerate(slice_, start=page_start + 1):
                    medal    = medals[pos - 1] if pos <= 3 else f"`#{pos}`"
                    m        = member_map.get(uid) or (ctx.guild.get_member(uid) if ctx.guild else None)
                    name     = m.display_name if m else f"<@{uid}>"
                    m_rank   = self._member_rank(m) if m else None
                    rank_str = f" {RANK_EMOJIS.get(m_rank, '')} {m_rank}" if m_rank else ""
                    lines.append(
                        f"{medal} **{name}**{rank_str}\n"
                        f"  {act_icon} {action_label}: **{cnt}**"
                    )
                e.add_field(name="Rankings", value="\n".join(lines), inline=False)

            lb_pages.append(e)

        view = PageView(lb_pages)
        await ctx.send(embed=lb_pages[0], view=view)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StaffManagerCog(bot))
