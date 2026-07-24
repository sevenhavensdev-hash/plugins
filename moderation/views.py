"""
Discord UI Views for the modstaff plugin.

Contains reusable View classes for:
  - ConfirmView       (Confirm / Cancel buttons)
  - HistoryView       (paginated moderation history)
  - LeaderboardView   (category tabs + paginated leaderboard)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord

if TYPE_CHECKING:
    from discord import Interaction


# ---------------------------------------------------------------------------
# Confirm / Cancel view
# ---------------------------------------------------------------------------

class ConfirmView(discord.ui.View):
    """
    A simple two-button view that yields a boolean result.

    Usage::

        view = ConfirmView(author_id=ctx.author.id, timeout=30)
        msg  = await ctx.send(embed=..., view=view)
        await view.wait()

        if view.value is None:
            # timed out
        elif view.value:
            # confirmed
        else:
            # cancelled
    """

    def __init__(self, author_id: int, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value: Optional[bool] = None
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the command author can use these buttons.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: Interaction, button: discord.ui.Button):
        self.value = True
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel(self, interaction: Interaction, button: discord.ui.Button):
        self.value = False
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------------------
# Paginated history view
# ---------------------------------------------------------------------------

class HistoryView(discord.ui.View):
    """
    A paginated view for moderation history.

    The caller provides the full list of cases split into pages of embeds,
    and this view handles Previous / Next navigation.
    """

    def __init__(
        self,
        author_id: int,
        embeds: list[discord.Embed],
        timeout: float = 120.0,
    ):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.embeds = embeds
        self.current = 0
        self.message: Optional[discord.Message] = None
        self._update_buttons()

    def _update_buttons(self):
        self.prev_button.disabled = self.current == 0
        self.next_button.disabled = self.current >= len(self.embeds) - 1

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the command author can use these buttons.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: Interaction, button: discord.ui.Button):
        self.current -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current], view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: Interaction, button: discord.ui.Button):
        self.current += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current], view=self)

    @discord.ui.button(label="✖ Close", style=discord.ButtonStyle.danger)
    async def close_button(self, interaction: Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------------------
# Leaderboard view
# ---------------------------------------------------------------------------

LEADERBOARD_CATEGORIES = ["overall", "tickets", "moderation", "messages", "monthly"]
CATEGORY_LABELS = {
    "overall":    "🏆 Overall",
    "tickets":    "🎫 Tickets",
    "moderation": "🛡️ Moderation",
    "messages":   "💬 Messages",
    "monthly":    "📅 Monthly",
}


class LeaderboardView(discord.ui.View):
    """
    A view for the staff leaderboard with category tabs and pagination.

    The parent Cog provides a `get_leaderboard_page(category, page)` coroutine
    that returns (embed, total_pages).  This view calls it whenever the user
    switches category or page.
    """

    def __init__(
        self,
        author_id: int,
        get_page_func,          # async (category: str, page: int) -> (Embed, int)
        initial_embed: discord.Embed,
        initial_total_pages: int,
        timeout: float = 120.0,
    ):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.get_page = get_page_func
        self.category = "overall"
        self.page = 1
        self.total_pages = initial_total_pages
        self.embed = initial_embed
        self.message: Optional[discord.Message] = None
        self._update_nav_buttons()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _update_nav_buttons(self):
        self.prev_btn.disabled = self.page <= 1
        self.next_btn.disabled = self.page >= self.total_pages

    async def _refresh(self, interaction: Interaction):
        self.embed, self.total_pages = await self.get_page(self.category, self.page)
        self._update_nav_buttons()
        await interaction.response.edit_message(embed=self.embed, view=self)

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the command author can use these buttons.", ephemeral=True
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Category buttons
    # ------------------------------------------------------------------

    @discord.ui.button(label="🏆 Overall", style=discord.ButtonStyle.primary, row=0)
    async def btn_overall(self, interaction: Interaction, button: discord.ui.Button):
        self.category = "overall"
        self.page = 1
        await self._refresh(interaction)

    @discord.ui.button(label="🎫 Tickets", style=discord.ButtonStyle.primary, row=0)
    async def btn_tickets(self, interaction: Interaction, button: discord.ui.Button):
        self.category = "tickets"
        self.page = 1
        await self._refresh(interaction)

    @discord.ui.button(label="🛡️ Moderation", style=discord.ButtonStyle.primary, row=0)
    async def btn_moderation(self, interaction: Interaction, button: discord.ui.Button):
        self.category = "moderation"
        self.page = 1
        await self._refresh(interaction)

    @discord.ui.button(label="📅 Monthly", style=discord.ButtonStyle.primary, row=0)
    async def btn_monthly(self, interaction: Interaction, button: discord.ui.Button):
        self.category = "monthly"
        self.page = 1
        await self._refresh(interaction)

    # ------------------------------------------------------------------
    # Navigation buttons
    # ------------------------------------------------------------------

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, interaction: Interaction, button: discord.ui.Button):
        self.page -= 1
        await self._refresh(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(self, interaction: Interaction, button: discord.ui.Button):
        self.page += 1
        await self._refresh(interaction)

    @discord.ui.button(label="✖ Close", style=discord.ButtonStyle.danger, row=1)
    async def close_btn(self, interaction: Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
