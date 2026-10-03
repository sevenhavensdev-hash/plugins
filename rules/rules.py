"""Modmail plugin that posts the Age of Civilisations rules as Discord embeds."""

from discord.ext import commands
import discord


RULE_PAGE_LABELS = (
    ("Overview", "🌷"),
    ("Minor", "🍃"),
    ("Medium", "🌼"),
    ("Extreme", "🚨"),
    ("Voice", "🎧"),
    ("In-game", "🎮"),
)


class RulesPageButton(discord.ui.Button):
    def __init__(self, page_index, label, emoji, row):
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.secondary,
            custom_id=f"age_civilisations_rules:page:{page_index}",
            row=row,
        )
        self.page_index = page_index

    async def callback(self, interaction):
        view = RulesView(current_page=self.page_index)
        embed = AgeOfCivilisationsRules._build_embeds()[self.page_index]
        await interaction.response.edit_message(embed=embed, view=view)


class RulesView(discord.ui.View):
    """Persistent section buttons for a member's private rules view."""

    def __init__(self, current_page=0):
        super().__init__(timeout=None)
        for page_index, (label, emoji) in enumerate(RULE_PAGE_LABELS):
            button = RulesPageButton(
                page_index=page_index,
                label=label,
                emoji=emoji,
                row=0 if page_index < 3 else 1,
            )
            if page_index == current_page:
                button.style = discord.ButtonStyle.primary
            self.add_item(button)


class OpenRulesButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Open my rules",
            emoji="📖",
            style=discord.ButtonStyle.primary,
            custom_id="age_civilisations_rules:open",
        )

    async def callback(self, interaction):
        await interaction.response.send_message(
            embed=AgeOfCivilisationsRules._build_embeds()[0],
            view=RulesView(),
            ephemeral=True,
        )


class RulesLauncher(discord.ui.View):
    """Persistent public button that opens a private rules browser."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(OpenRulesButton())


class AgeOfCivilisationsRules(commands.Cog):
    """Publish the server rules in a channel configured by a server manager."""

    def __init__(self, bot):
        self.bot = bot
        self.collection = bot.api.get_plugin_partition(self)

    async def cog_load(self):
        # Re-register persistent custom IDs so old posts still work after restarts.
        self.bot.add_view(RulesLauncher())
        self.bot.add_view(RulesView())

    @commands.command(name="ruleschannel")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def rules_channel(self, ctx, channel: discord.TextChannel):
        """Choose where the rules announcement will be posted."""
        await self.collection.update_one(
            {"_id": str(ctx.guild.id)},
            {"$set": {"channel_id": channel.id}},
            upsert=True,
        )
        await ctx.send(
            f"Rules will be posted in {channel.mention} when someone uses "
            f"`{ctx.prefix}rules`."
        )

    @commands.command(name="rules")
    @commands.guild_only()
    @commands.cooldown(1, 30, commands.BucketType.guild)
    async def rules(self, ctx):
        """Post the formatted rules to this server's configured rules channel."""
        settings = await self.collection.find_one({"_id": str(ctx.guild.id)})
        channel_id = settings.get("channel_id") if settings else None
        channel = ctx.guild.get_channel(channel_id) if channel_id else None

        if not isinstance(channel, discord.TextChannel):
            await ctx.send(
                "No rules channel is configured (or the saved channel no longer "
                "exists). A server manager can set one with "
                f"`{ctx.prefix}ruleschannel #channel`."
            )
            return

        await channel.send(embed=self._build_landing_embed(), view=RulesLauncher())

        await ctx.send(f"Rules posted in {channel.mention}.", delete_after=8)

    @staticmethod
    def _build_landing_embed():
        embed = discord.Embed(
            title="🌍  Age of Civilisations 🌍 ",
            description=(
                "Welcome to the comunnity of civilization! 🌍\n\n"
                "These guidelines help keep the game fair and the community "
                "friendly. Press **Open my rules** for your own private, "
                "button-controlled guide—your browsing won’t change anyone "
                "else’s view."
            ),
            color=0xF7C6D0,
        )
        embed.set_author(name=" COMMUNITY GUIDE ")
        embed.add_field(
            name="Guidelines",
            value=(
                "🍃 Minor issues　 ·　 🌼 Medium violations　 ·　 🚨 Serious violations\n"
                "🎧 Voice chat　 ·　 🎮 Roblox in-game"
            ),
            inline=False,
        )
        embed.set_footer(text="Be kind • Play fair • Have fun ✨")
        return embed

    @staticmethod
    def _build_embeds():
        embeds = [
            discord.Embed(
                title="🌷 Start Here • Community Basics",
                description=(
                    "Welcome to the community! These guidelines help keep the game "
                    "fair and this server a lovely place to hang out. Joining and "
                    "playing means you agree to follow them. 🌍\n\n"
                    "🌿 **Use common sense:** We can’t write a rule for every "
                    "scenario. If you’re unsure whether something is against the "
                    "rules, it probably is—please don’t do it.\n\n"
                    "🛡️ **Staff discretion:** Moderators may step in when behavior "
                    "harms the community or disrupts the game, even if that exact "
                    "situation isn’t listed here."
                ),
                color=0xFFC8DD,
            ),
            discord.Embed(
                title="🍃 Minor Issues • Usually a warning",
                description=(
                    "A quick reminder or short mute is usually enough. 🌱\n\n"
                    "• **Use the right channels:** Keep in-game nation discussions, "
                    "roleplay, and lore in their proper channels. Out-of-character "
                    "(OOC) talk belongs in general chat.\n"
                    "• **No spam:** Don’t flood channels with rapid messages, "
                    "repeated short phrases, or huge walls of emojis.\n"
                    "• **No baiting:** Don’t start arguments, provoke people, or "
                    "create out-of-character drama just to get a reaction.\n"
                    "• **Be respectful:** Don’t be rude or insult other members; "
                    "treat everyone with basic decency.\n"
                    "• **No rules lawyering:** Don’t exploit technical loopholes "
                    "or twist wording to excuse bad behavior. Intent matters.\n"
                    "• **No mass pinging:** Don’t repeatedly ping staff or mention "
                    "users without a valid, urgent reason.\n"
                    "• **Keep profiles appropriate:** Nicknames, statuses, and "
                    "avatars must be clean. Disruptive or offensive profile layouts "
                    "must be changed."
                ),
                color=0xCDEAC0,
            ),
            discord.Embed(
                title="🌼 Medium Violations • Kick or softban",
                description=(
                    "These cause bigger disruptions and can earn a kick or softban. 🌼\n\n"
                    "• **No threats:** Threats are taken seriously, even as a joke; "
                    "more serious threats bring harsher punishments.\n"
                    "• **Keep it SFW:** No explicit, suggestive, or age-inappropriate "
                    "content. Don’t post anything that makes people uncomfortable "
                    "or isn’t safe for work.\n"
                    "• **No discrimination:** Racism, sexism, transphobia, or "
                    "targeting people for who they are is not tolerated.\n"
                    "• **Don’t bypass the filter:** Don’t alter blocked words with "
                    "symbols or substituted letters to evade Automod.\n"
                    "• **No public arguments:** Take fights to DMs. Staff will step "
                    "in if an argument spills into public channels.\n"
                    "• **Avoid controversial debates:** Don’t argue about real-world "
                    "politics, religion, or other sensitive topics here.\n"
                    "• **No unsolicited ads:** Don’t advertise servers, groups, or "
                    "YouTube channels, including by randomly DMing links to members.\n"
                    "• **Don’t flood chat:** Massive blocks of empty lines or "
                    "copy-pasted text disrupt chat.\n"
                    "• **Don’t misuse tickets:** Troll tickets, spammed staff "
                    "reports, or lying to admins during an investigation can result "
                    "in punishment."
                ),
                color=0xFFE7A3,
            ),
            discord.Embed(
                title="🚨 Extreme Violations • Permanent ban",
                description=(
                    "These serious violations mean immediate, permanent removal. 🚫\n\n"
                    "• **No slurs:** Using any kind of slur results in an immediate, "
                    "permanent ban. No exceptions.\n"
                    "• **No phishing or scams:** Fake links, scams, or attempts to "
                    "steal personal information mean a permanent ban with no appeal.\n"
                    "• **No gore or NSFW:** Pornography, 18+ content, and graphic "
                    "gore are strictly forbidden.\n"
                    "• **No alt accounts:** Secondary profiles are not allowed. "
                    "Alts will be banned on sight; ban or mute evasion can worsen "
                    "the punishment on the main account.\n"
                    "• **No exploits or cheating:** Don’t share, discuss, or promote "
                    "hacks, script injectors, noclipping, or Roblox bugs that give "
                    "an unfair advantage.\n"
                    "• **No cross-trading or black markets:** Don’t trade "
                    "civilisations land, resources, gold, or custom setups for real "
                    "money, Robux, or items in other games.\n"
                    "• **No raiding:** Organizing or participating in server raids "
                    "or nukes means an immediate permanent ban.\n"
                    "• **Follow platform rules:** Follow Discord’s Terms of Service "
                    "and Community Guidelines, and Roblox’s Terms of Service and "
                    "Community Standards. Using Vencord is the only exception."
                ),
                color=0xFFB4A2,
            ),
            discord.Embed(
                title="🎧 Voice Chat Rules",
                description=(
                    "All text rules apply in voice channels too. Keep voice chat safe "
                    "and appropriate.\n\n"
                    "• **No earrape:** Don’t scream into your mic, blast loud music, "
                    "or make loud noises to shock people.\n"
                    "• **Keep audio appropriate:** Don’t play explicit, racist, "
                    "hateful, or NSFW audio over your mic or through a bot.\n"
                    "• **Use soundboards moderately:** Don’t spam sound effects or "
                    "disrupt ongoing conversations.\n"
                    "• **No harassment:** Don’t target, mock, or harass people in "
                    "voice channels.\n"
                    "• **No harmful streams:** Streaming NSFW content, gore, or "
                    "anything harmful results in severe punishment.\n"
                    "• **No recording without permission:** Don’t record or share "
                    "anyone’s voice unless everyone in the channel explicitly agrees.\n"
                    "• **Don’t evade voice mutes:** Using alts to evade a voice mute "
                    "or kick will get all your accounts punished."
                ),
                color=0xDCC6F5,
            ),
            discord.Embed(
                title="🎮 In-Game Rules • Roblox",
                description=(
                    "These rules apply directly in the Roblox game. Follow them to "
                    "avoid being kicked or banned in-game.\n\n"
                    "• **No exploiting or hacking:** Third-party clients, script "
                    "executors, fly hacks, speed hacks, and aimbots mean an immediate "
                    "permanent ban.\n"
                    "• **Don’t abuse bugs:** Report glitches, glitch-builds, or "
                    "texture flaws that reveal walls, duplicate items, or give an "
                    "unfair advantage. Intentionally exploiting bugs is punishable.\n"
                    "• **No chat toxicity:** Roblox in-game chat follows the same "
                    "respect rules as Discord. Don’t bypass the dynamic filter, "
                    "trash-talk beyond friendly limits, or bully anyone.\n"
                    "• **Respect staff:** Follow a moderator’s in-game instructions. "
                    "Arguing with staff can get you removed."
                ),
                color=0xBDE0FE,
            ),
        ]
        for page_number, embed in enumerate(embeds, start=1):
            embed.set_author(name="🌸 AGE OF CIVILISATIONS • COMMUNITY GUIDE")
            embed.set_footer(
                text=f"Page {page_number}/{len(embeds)}  ✿  Pick a button to wander"
            )
        return embeds


async def setup(bot):
    await bot.add_cog(AgeOfCivilisationsRules(bot))
