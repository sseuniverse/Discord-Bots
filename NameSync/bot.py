import os
import asyncio
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timezone

import discord
from discord import app_commands
from dotenv import load_dotenv

# ==================================================
# CONFIGURATION
# ==================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError(
        f"DISCORD_TOKEN is missing. Create a .env file beside bot.py "
        f"and add: DISCORD_TOKEN=your_bot_token\nExpected file: {BASE_DIR / '.env'}"
    )

DB_CHANNEL_NAME = "nickname-formats"
NICKNAME_LIMIT = 32
EDIT_DELAY_SECONDS = 1.0
MAX_DIAGNOSTIC_LINES = 12
MAX_EMBED_FIELD = 1000

intents = discord.Intents.default()
intents.members = True


class TournamentClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print("Slash commands synced and ready!")


client = TournamentClient()


# ==================================================
# EMBED / UTILITY HELPERS
# ==================================================

def make_embed(title, description=None, color=discord.Color.blurple()):
    embed = discord.Embed(title=title, description=description, color=color)
    embed.timestamp = datetime.now(timezone.utc)
    embed.set_footer(text="Nickname Manager • Detailed diagnostics are best-effort")
    return embed


def clean(value, limit=900):
    value = discord.utils.escape_markdown(str(value))
    return value if len(value) <= limit else value[:limit - 3] + "..."


async def bot_member_for(guild: discord.Guild) -> discord.Member | None:
    """Resolve the bot's member object, refreshing from Discord if needed."""
    if guild.me is not None:
        return guild.me
    if client.user is None:
        return None
    member = guild.get_member(client.user.id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(client.user.id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


def format_error(exc: Exception) -> str:
    if isinstance(exc, discord.Forbidden):
        return "Discord denied the action. Check bot permissions and role hierarchy."
    if isinstance(exc, discord.NotFound):
        return "The target member, role, channel, or message no longer exists."
    if isinstance(exc, discord.HTTPException):
        return f"Discord API error (HTTP {exc.status}, code {getattr(exc, 'code', 'unknown')}): {exc.text or exc}"
    return f"{type(exc).__name__}: {exc}"


async def get_db_channel(guild: discord.Guild, create=True) -> discord.TextChannel | None:
    """Find the storage channel; optionally create it with safe permission overwrites."""
    channel = discord.utils.get(guild.text_channels, name=DB_CHANNEL_NAME)
    if channel is not None:
        return channel
    if not create:
        return None

    # In unusual cache states, guild.default_role can be unavailable. Refresh roles
    # and resolve @everyone by the guild ID before building permission overwrites.
    everyone = guild.default_role
    if everyone is None:
        try:
            roles = await guild.fetch_roles()
            everyone = discord.utils.get(roles, id=guild.id)
        except discord.HTTPException as exc:
            raise RuntimeError(
                "Could not fetch server roles. Check the bot's server access and Manage Channels permission."
            ) from exc

    if everyone is None:
        raise RuntimeError(
            "Could not resolve the @everyone role, so the storage channel cannot be created safely."
        )

    overwrites = {
        everyone: discord.PermissionOverwrite(view_channel=False),
    }

    bot_member = await bot_member_for(guild)
    if bot_member is not None:
        overwrites[bot_member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
        )
    else:
        raise RuntimeError(
            "Could not resolve the bot's server member. Please reinvite the bot or try again."
        )

    return await guild.create_text_channel(
        DB_CHANNEL_NAME,
        overwrites=overwrites,
        reason="Create private nickname-format storage",
    )


async def fetch_formats(guild: discord.Guild):
    """Return {role_id: {'format': str, 'message': Message}}. Newest duplicate wins."""
    channel = await get_db_channel(guild)
    formats = {}
    async for message in channel.history(limit=None, oldest_first=True):
        if "|" not in message.content:
            continue
        try:
            role_text, nickname_format = message.content.split("|", 1)
            role_id = int(role_text.strip())
            nickname_format = nickname_format.strip()
            if nickname_format:
                formats[role_id] = {"format": nickname_format, "message": message}
        except (ValueError, AttributeError):
            continue
    return formats


EXPERIMENTAL_SETTING_PREFIX = "BOT_SETTING | experimental |"


async def get_experimental(guild: discord.Guild) -> bool:
    """Read the guild's experimental logging flag from the storage channel."""
    channel = await get_db_channel(guild, create=False)
    if channel is None:
        return False
    async for message in channel.history(limit=None, oldest_first=False):
        content = message.content.strip()
        if content.lower().startswith(EXPERIMENTAL_SETTING_PREFIX.lower()):
            return content.split("|", 2)[-1].strip().lower() == "true"
    return False


async def set_experimental(guild: discord.Guild, enabled: bool) -> None:
    """Persist the experimental logging flag in the existing storage channel."""
    channel = await get_db_channel(guild, create=True)
    async for message in channel.history(limit=None, oldest_first=False):
        if message.content.strip().lower().startswith(EXPERIMENTAL_SETTING_PREFIX.lower()):
            await message.delete()
    await channel.send(f"{EXPERIMENTAL_SETTING_PREFIX} {str(enabled).lower()}")


def validate_format(name_format: str):
    if "{user}" not in name_format:
        return "The format must contain `{user}`. Example: `[Mod] {user}`."
    example = name_format.replace("{user}", "ExampleUser")
    if len(example) > NICKNAME_LIMIT:
        return (
            f"Format is too long ({len(example)} characters with `ExampleUser`). "
            f"Discord nicknames are limited to {NICKNAME_LIMIT} characters."
        )
    return None


def build_member_plan(member: discord.Member, formats: dict):
    """Return status, reason, matched role, and target nickname."""
    if member.bot:
        return "skip", "Bot account", None, None

    matches = [role for role in member.roles if role.id in formats]
    if not matches:
        return "skip", "No configured role", None, None

    # Highest role in the server's role hierarchy wins.
    selected = max(matches, key=lambda role: role.position)
    nickname_format = formats[selected.id]["format"]
    base_name = member.global_name or member.name
    new_nick = nickname_format.replace("{user}", base_name)

    if len(new_nick) > NICKNAME_LIMIT:
        return (
            "skip",
            f"Generated nickname is {len(new_nick)} characters (limit {NICKNAME_LIMIT})",
            selected,
            new_nick[:NICKNAME_LIMIT],
        )

    if member.nick == new_nick:
        return "skip", "Nickname already matches", selected, new_nick

    return "ready", "Ready to update", selected, new_nick


def add_diagnostic(bucket, member, reason, role=None, target=None):
    item = f"• {clean(member.display_name, 70)} (`{member.id}`): {clean(reason, 180)}"
    if role is not None:
        item += f" | role: {clean(role.name, 70)}"
    if target:
        item += f" | target: `{clean(target, 80)}`"
    bucket.append(item)


async def send_public_result(interaction: discord.Interaction, embed: discord.Embed):
    """Post results in the channel, not as a slash-command follow-up message."""
    channel = interaction.channel
    if channel is None or not hasattr(channel, "send"):
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        # Do not lose the results if the bot cannot post in the channel.
        await interaction.followup.send(
            "I couldn't post the result message here (missing Send Messages/Embed Links). "
            "Showing the result privately instead.",
            embed=embed,
            ephemeral=True,
        )


async def require_guild(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.", ephemeral=True
        )
        return None
    return interaction.guild


# ==================================================
# /SETUP
# ==================================================

@client.tree.command(name="setup", description="Set up and check the nickname bot's storage channel")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def setup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        return

    try:
        channel = await get_db_channel(guild, create=True)
        bot_member = await bot_member_for(guild)
        permissions = channel.permissions_for(bot_member) if bot_member else None

        embed = make_embed(
            "Nickname Manager Setup",
            "Storage setup completed. Review the checks below before transforming members.",
            discord.Color.green(),
        )
        embed.add_field(name="Storage channel", value=channel.mention, inline=False)
        embed.add_field(
            name="Can view channel",
            value="✅ Yes" if permissions and permissions.view_channel else "❌ No",
            inline=True,
        )
        embed.add_field(
            name="Can send messages",
            value="✅ Yes" if permissions and permissions.send_messages else "❌ No",
            inline=True,
        )
        embed.add_field(
            name="Can read history",
            value="✅ Yes" if permissions and permissions.read_message_history else "❌ No",
            inline=True,
        )
        embed.add_field(
            name="Manage Nicknames",
            value="✅ Yes" if bot_member and bot_member.guild_permissions.manage_nicknames else "❌ No",
            inline=True,
        )
        embed.add_field(
            name="Server Members Intent",
            value=(
                "Enabled in code. Also enable **Server Members Intent** in the Discord Developer Portal."
            ),
            inline=False,
        )
        embed.add_field(
            name="Next steps",
            value=(
                "1. Put the bot's highest role above the roles whose members it will rename.\n"
                "2. Run `/add_format` to save a format.\n"
                "3. Run `/diagnose_transform` for a dry run.\n"
                "4. Run `/transform` to apply changes."
            ),
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except (discord.Forbidden, discord.HTTPException) as exc:
        await interaction.followup.send(
            f"Setup failed: {format_error(exc)}", ephemeral=True
        )
    except Exception as exc:
        await interaction.followup.send(
            f"Setup failed: `{type(exc).__name__}: {clean(exc, 500)}`. "
            "Check that the bot has **Manage Channels**, **View Channels**, and **Manage Nicknames** permissions.",
            ephemeral=True,
        )


# ==================================================
# /ADD_FORMAT
# ==================================================

@client.tree.command(name="add_format", description="Save one nickname format for one or more roles")
@app_commands.describe(
    name_format="Nickname template containing {user}, e.g. [Mod] {user}",
    role_1="First role to configure",
    role_2="Optional second role",
    role_3="Optional third role",
    role_4="Optional fourth role",
    role_5="Optional fifth role",
    role_6="Optional sixth role",
    role_7="Optional seventh role",
    role_8="Optional eighth role",
    role_9="Optional ninth role",
    role_10="Optional tenth role",
)
@app_commands.default_permissions(manage_roles=True)
@app_commands.guild_only()
async def add_format(
    interaction: discord.Interaction,
    name_format: str,
    role_1: discord.Role,
    role_2: discord.Role = None,
    role_3: discord.Role = None,
    role_4: discord.Role = None,
    role_5: discord.Role = None,
    role_6: discord.Role = None,
    role_7: discord.Role = None,
    role_8: discord.Role = None,
    role_9: discord.Role = None,
    role_10: discord.Role = None,
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        return

    problem = validate_format(name_format)
    if problem:
        await interaction.followup.send(f"❌ {problem}", ephemeral=True)
        return

    roles = [
        role for role in (
            role_1, role_2, role_3, role_4, role_5,
            role_6, role_7, role_8, role_9, role_10,
        )
        if role is not None
    ]
    if len({role.id for role in roles}) != len(roles):
        await interaction.followup.send("❌ Select each role only once.", ephemeral=True)
        return

    bot_member = await bot_member_for(guild)
    if bot_member:
        too_high = [role for role in roles if role >= bot_member.top_role]
        if too_high:
            names = ", ".join(role.mention for role in too_high)
            await interaction.followup.send(
                f"⚠️ Saved formats can exist for these roles, but the bot may not be able to rename their members because the roles are at or above the bot's top role: {names}",
                ephemeral=True,
            )
            # Continue saving; role placement is a runtime permission issue.

    try:
        channel = await get_db_channel(guild)
        existing = await fetch_formats(guild)
        saved = []
        for role in roles:
            old_entry = existing.get(role.id)
            if old_entry:
                await old_entry["message"].delete()
            await channel.send(f"{role.id} | {name_format}")
            saved.append(role.mention)

        embed = make_embed("Nickname formats saved", color=discord.Color.green())
        embed.add_field(name="Format", value=f"`{clean(name_format, 200)}`", inline=False)
        embed.add_field(name="Roles", value=", ".join(saved), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except (discord.Forbidden, discord.HTTPException) as exc:
        await interaction.followup.send(
            f"❌ Could not save the format. {format_error(exc)}", ephemeral=True
        )


# Backwards-compatible alias for the previous command name.
@client.tree.command(name="add_roles", description="(Legacy) Save a nickname format for up to 10 roles")
@app_commands.describe(name_format="Format containing {user}, e.g. [Mod] {user}", role_1="Required role")
@app_commands.default_permissions(manage_roles=True)
@app_commands.guild_only()
async def add_roles_legacy(
    interaction: discord.Interaction,
    name_format: str,
    role_1: discord.Role,
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        return
    problem = validate_format(name_format)
    if problem:
        await interaction.followup.send(f"❌ {problem}", ephemeral=True)
        return
    try:
        channel = await get_db_channel(guild)
        formats = await fetch_formats(guild)
        old = formats.get(role_1.id)
        if old:
            await old["message"].delete()
        await channel.send(f"{role_1.id} | {name_format}")
        await interaction.followup.send(
            f"✅ Saved `{clean(name_format, 200)}` for {role_1.mention}. "
            "Use `/add_format` for multiple roles.",
            ephemeral=True,
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        await interaction.followup.send(f"❌ {format_error(exc)}", ephemeral=True)


# ==================================================
# /TRANSFORM
# ==================================================

@client.tree.command(name="transform", description="Apply saved nickname formats and show the result summary")
@app_commands.default_permissions(manage_roles=True)
@app_commands.guild_only()
async def transform(interaction: discord.Interaction):
    # The public channel message keeps the familiar, simple design. Detailed logs are
    # sent privately in the command interaction only when /experimental is true.
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    if guild is None:
        return

    try:
        experimental = await get_experimental(guild)
        bot_member = await bot_member_for(guild)
        if bot_member is None:
            await interaction.followup.send("Could not identify the bot's server member.", ephemeral=True)
            return
        if not bot_member.guild_permissions.manage_nicknames:
            await interaction.followup.send("❌ The bot needs the **Manage Nicknames** permission.", ephemeral=True)
            return

        formats = await fetch_formats(guild)
        if not formats:
            await interaction.followup.send("No formats are saved. Run `/setup`, then `/add_format` first.", ephemeral=True)
            return

        stats = Counter()
        reasons = Counter()
        failures, skips, successes = [], [], []
        total_seen = 0

        async for member in guild.fetch_members(limit=None):
            total_seen += 1
            status, reason, role, target = build_member_plan(member, formats)

            if status == "skip":
                stats["skipped"] += 1
                reasons[reason] += 1
                if len(skips) < MAX_DIAGNOSTIC_LINES:
                    add_diagnostic(skips, member, reason, role, target)
                continue

            # Avoid predictable failures and explain them in experimental logs.
            if member == guild.owner:
                stats["skipped"] += 1
                reason = "Server owner cannot be renamed by the bot"
                reasons[reason] += 1
                if len(skips) < MAX_DIAGNOSTIC_LINES:
                    add_diagnostic(skips, member, reason, role, target)
                continue
            if member.top_role >= bot_member.top_role:
                stats["skipped"] += 1
                reason = "Member's highest role is at/above the bot's highest role"
                reasons[reason] += 1
                if len(skips) < MAX_DIAGNOSTIC_LINES:
                    add_diagnostic(skips, member, reason, role, target)
                continue

            try:
                await member.edit(
                    nick=target,
                    reason=f"Nickname format transform initiated by {interaction.user} ({interaction.user.id})",
                )
                stats["updated"] += 1
                if len(successes) < MAX_DIAGNOSTIC_LINES:
                    add_diagnostic(successes, member, "Nickname updated", role, target)
            except discord.Forbidden as exc:
                stats["failed"] += 1
                reason_text = "Discord denied the nickname change (permissions or role hierarchy)"
                reasons[reason_text] += 1
                if len(failures) < MAX_DIAGNOSTIC_LINES:
                    add_diagnostic(failures, member, f"{reason_text}. {format_error(exc)}", role, target)
            except discord.HTTPException as exc:
                stats["failed"] += 1
                reason_text = f"Discord HTTP error {exc.status} / code {getattr(exc, 'code', 'unknown')}"
                reasons[reason_text] += 1
                if len(failures) < MAX_DIAGNOSTIC_LINES:
                    add_diagnostic(failures, member, f"{reason_text}: {exc.text or exc}", role, target)
            except Exception as exc:
                stats["failed"] += 1
                reason_text = f"Unexpected {type(exc).__name__}"
                reasons[reason_text] += 1
                if len(failures) < MAX_DIAGNOSTIC_LINES:
                    add_diagnostic(failures, member, f"{reason_text}: {exc}", role, target)
            finally:
                await asyncio.sleep(EDIT_DELAY_SECONDS)

        # Public result: preserve a clean summary with updated / failed / skipped.
        public_embed = make_embed(
            "Nickname Transformation Complete",
            "The nickname transformation has finished.",
            discord.Color.green() if stats["failed"] == 0 else discord.Color.orange(),
        )
        public_embed.add_field(name="✅ Updated", value=str(stats["updated"]), inline=True)
        public_embed.add_field(name="❌ Failed", value=str(stats["failed"]), inline=True)
        public_embed.add_field(name="⏭️ Skipped", value=str(stats["skipped"]), inline=True)
        public_embed.add_field(name="Members scanned", value=str(total_seen), inline=True)
        public_embed.add_field(name="Formats loaded", value=str(len(formats)), inline=True)
        await send_public_result(interaction, public_embed)

        if experimental:
            # Private interaction log: only visible to the person who ran /transform.
            log_embed = make_embed(
                "🧪 Experimental Transform Logs",
                "Detailed diagnostics are enabled by `/experimental enabled:true`.",
                discord.Color.orange(),
            )
            log_embed.add_field(name="Updated / Failed / Skipped", value=f"{stats['updated']} / {stats['failed']} / {stats['skipped']}", inline=False)
            reason_lines = [f"• **{clean(reason, 130)}** — {count}" for reason, count in reasons.most_common(12)]
            log_embed.add_field(name="Reason totals", value="\n".join(reason_lines)[:MAX_EMBED_FIELD] or "No skips or failures.", inline=False)
            for title, lines in (("Failure examples", failures), ("Skip examples", skips), ("Updated examples", successes)):
                if lines:
                    log_embed.add_field(name=title, value="\n".join(lines)[:MAX_EMBED_FIELD], inline=False)
            if stats["failed"] > MAX_DIAGNOSTIC_LINES or stats["skipped"] > MAX_DIAGNOSTIC_LINES or stats["updated"] > MAX_DIAGNOSTIC_LINES:
                log_embed.add_field(
                    name="Log limit",
                    value=f"Showing up to {MAX_DIAGNOSTIC_LINES} examples per category. Reason totals include every scanned member.",
                    inline=False,
                )
            await interaction.followup.send(embed=log_embed, ephemeral=True)
        else:
            await interaction.followup.send(
                "Transform finished. The public summary was posted in this channel. Detailed logs are off; use `/experimental enabled:true` to enable them.",
                ephemeral=True,
            )

    except discord.Forbidden as exc:
        await interaction.followup.send(f"❌ Could not read the storage channel or members. {format_error(exc)}", ephemeral=True)
    except discord.HTTPException as exc:
        await interaction.followup.send(f"❌ Discord API error during transform. {format_error(exc)}", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(
            f"❌ Transform stopped unexpectedly: `{type(exc).__name__}: {clean(exc, 500)}`",
            ephemeral=True,
        )


@client.tree.command(name="experimental", description="Enable or disable detailed transform logs")
@app_commands.describe(enabled="True shows detailed logs privately after /transform; false hides them")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def experimental(interaction: discord.Interaction, enabled: bool):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        return
    try:
        await set_experimental(guild, enabled)
        state = "enabled" if enabled else "disabled"
        await interaction.followup.send(
            f"🧪 Experimental transform logs are now **{state}**. "
            + ("After `/transform`, detailed logs will appear privately in your interaction; the public message will only show updated, failed, and skipped totals." if enabled else "After `/transform`, only the clean public summary will be posted; detailed logs will be hidden."),
            ephemeral=True,
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        await interaction.followup.send(f"Could not save the experimental setting. {format_error(exc)}", ephemeral=True)


# ==================================================
# EXPERIMENTAL / DRY-RUN DIAGNOSTICS
# ==================================================

@client.tree.command(name="diagnose_transform", description="[Experimental] Dry-run: explain what transform would do without changing nicknames")
@app_commands.default_permissions(manage_roles=True)
@app_commands.guild_only()
async def diagnose_transform(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    if guild is None:
        return

    try:
        channel = await get_db_channel(guild, create=False)
        if channel is None:
            await interaction.followup.send(
                "Storage channel not found. Run `/setup` first.", ephemeral=True
            )
            return
        formats = await fetch_formats(guild)
        if not formats:
            await interaction.followup.send("No saved formats found. Use `/add_format` first.", ephemeral=True)
            return

        stats = Counter()
        reasons = Counter()
        examples = defaultdict(list)
        bot_member = await bot_member_for(guild)

        async for member in guild.fetch_members(limit=None):
            status, reason, role, target = build_member_plan(member, formats)
            if status == "ready":
                # Dry-run permission checks can catch common hierarchy problems, but Discord
                # remains authoritative when the actual edit is attempted.
                if bot_member and member != guild.owner and member.top_role >= bot_member.top_role:
                    status = "skip"
                    reason = "Member's highest role is at/above the bot's highest role"
                elif member == guild.owner:
                    status = "skip"
                    reason = "Server owner cannot be renamed by the bot"
                else:
                    status = "would_update"
                    reason = "Would update"
            stats[status] += 1
            reasons[reason] += 1
            if len(examples[status]) < MAX_DIAGNOSTIC_LINES:
                add_diagnostic(examples[status], member, reason, role, target)

        embed = make_embed(
            "[Experimental] Transform Dry Run",
            "No nicknames were changed. Permission checks are estimates; the real edit can still fail.",
            discord.Color.orange(),
        )
        embed.add_field(name="Would update", value=str(stats["would_update"]), inline=True)
        embed.add_field(name="Would skip", value=str(stats["skip"]), inline=True)
        embed.add_field(name="Members scanned", value=str(sum(stats.values())), inline=True)
        embed.add_field(
            name="Reason totals",
            value="\n".join(f"• **{clean(reason, 140)}** — {count}" for reason, count in reasons.most_common(12))[:MAX_EMBED_FIELD],
            inline=False,
        )
        for status, title in (
            ("would_update", "Would update (sample)"),
            ("skip", "Would skip (sample)"),
        ):
            if examples[status]:
                embed.add_field(name=title, value="\n".join(examples[status])[:MAX_EMBED_FIELD], inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(
            f"Diagnostic run failed: `{type(exc).__name__}: {clean(exc, 500)}`",
            ephemeral=True,
        )


@client.tree.command(name="test_member", description="[Experimental] Diagnose a single member's nickname transformation")
@app_commands.describe(member="Member to inspect; this command does not change their nickname")
@app_commands.default_permissions(manage_roles=True)
@app_commands.guild_only()
async def test_member(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        return
    try:
        formats = await fetch_formats(guild)
        status, reason, role, target = build_member_plan(member, formats)
        bot_member = await bot_member_for(guild)
        if status == "ready":
            if member == guild.owner:
                status, reason = "skip", "Server owner cannot be renamed by the bot"
            elif bot_member and member.top_role >= bot_member.top_role:
                status, reason = "skip", "Member's highest role is at/above the bot's highest role"
            elif not bot_member or not bot_member.guild_permissions.manage_nicknames:
                status, reason = "fail", "Bot lacks Manage Nicknames permission"

        color = discord.Color.green() if status == "ready" else (
            discord.Color.red() if status == "fail" else discord.Color.orange()
        )
        embed = make_embed("[Experimental] Member Transform Test", color=color)
        embed.add_field(name="Member", value=f"{member.mention}\n`{member.id}`", inline=False)
        embed.add_field(name="Result", value=status.replace("_", " ").title(), inline=True)
        embed.add_field(name="Reason", value=clean(reason, 500), inline=True)
        embed.add_field(name="Matched role", value=role.mention if role else "None", inline=True)
        embed.add_field(name="Current nickname", value=clean(member.nick or "(no server nickname)", 300), inline=False)
        embed.add_field(name="Target nickname", value=f"`{clean(target, 200)}`" if target else "None", inline=False)
        embed.add_field(
            name="Important",
            value="This is a dry run. Discord's actual response is the final authority; no nickname was changed.",
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(
            f"Member test failed: `{type(exc).__name__}: {clean(exc, 500)}`",
            ephemeral=True,
        )


# ==================================================
# /REMOVE_FORMAT
# ==================================================

@client.tree.command(name="remove_format", description="Remove a saved format for a role or remove all formats")
@app_commands.describe(
    role="Role whose format should be removed (leave blank to use remove_all)",
    remove_all="Set true to delete every saved format",
)
@app_commands.default_permissions(manage_roles=True)
@app_commands.guild_only()
async def remove_format(
    interaction: discord.Interaction,
    role: discord.Role = None,
    remove_all: bool = False,
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        return
    if role is not None and remove_all:
        await interaction.followup.send("Choose a role OR set `remove_all` to true, not both.", ephemeral=True)
        return
    if role is None and not remove_all:
        await interaction.followup.send("Choose a role or set `remove_all` to true.", ephemeral=True)
        return

    try:
        formats = await fetch_formats(guild)
        if remove_all:
            removed = 0
            for entry in formats.values():
                try:
                    await entry["message"].delete()
                    removed += 1
                except discord.NotFound:
                    continue
            await interaction.followup.send(
                embed=make_embed("Formats removed", f"Deleted **{removed}** saved format(s).", discord.Color.green()),
                ephemeral=True,
            )
            return

        entry = formats.get(role.id)
        if entry is None:
            await interaction.followup.send(f"No format is saved for {role.mention}.", ephemeral=True)
            return
        await entry["message"].delete()
        await interaction.followup.send(
            embed=make_embed("Format removed", f"Removed the nickname format for {role.mention}.", discord.Color.green()),
            ephemeral=True,
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        await interaction.followup.send(f"Could not remove format. {format_error(exc)}", ephemeral=True)


# Legacy alias
@client.tree.command(name="remove", description="(Legacy) Remove a role format or clear all formats")
@app_commands.describe(role="Role whose format should be removed", remove_all="Set true to delete all formats")
@app_commands.default_permissions(manage_roles=True)
@app_commands.guild_only()
async def remove_legacy(
    interaction: discord.Interaction,
    role: discord.Role = None,
    remove_all: bool = False,
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        return
    if role is not None and remove_all:
        await interaction.followup.send("Choose a role OR set `remove_all` to true, not both.", ephemeral=True)
        return
    if role is None and not remove_all:
        await interaction.followup.send("Select a role or set `remove_all` to true.", ephemeral=True)
        return
    try:
        formats = await fetch_formats(guild)
        targets = list(formats.values()) if remove_all else ([formats[role.id]] if role.id in formats else [])
        for entry in targets:
            await entry["message"].delete()
        await interaction.followup.send(f"Removed {len(targets)} format(s).", ephemeral=True)
    except (discord.Forbidden, discord.HTTPException) as exc:
        await interaction.followup.send(f"Could not remove formats. {format_error(exc)}", ephemeral=True)


# ==================================================
# /VIEW_FORMATS
# ==================================================

@client.tree.command(name="view_formats", description="List all saved nickname formats")
@app_commands.default_permissions(manage_roles=True)
@app_commands.guild_only()
async def view_formats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        return
    try:
        formats = await fetch_formats(guild)
        if not formats:
            await interaction.followup.send("No nickname formats are saved yet. Run `/add_format`.", ephemeral=True)
            return

        lines = []
        for role_id, entry in sorted(formats.items(), key=lambda item: item[0]):
            role = guild.get_role(role_id)
            role_name = role.mention if role else f"Deleted role (`{role_id}`)"
            lines.append(f"• {role_name}: `{clean(entry['format'], 200)}`")

        # Keep messages under Discord's content limit.
        chunks, current = [], ""
        for line in lines:
            if len(current) + len(line) + 1 > 1800:
                chunks.append(current)
                current = ""
            current += line + "\n"
        if current:
            chunks.append(current)

        for index, chunk in enumerate(chunks, start=1):
            embed = make_embed(
                "Saved Nickname Formats" if len(chunks) == 1 else f"Saved Nickname Formats ({index}/{len(chunks)})",
                chunk,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
    except (discord.Forbidden, discord.HTTPException) as exc:
        await interaction.followup.send(f"Could not read saved formats. {format_error(exc)}", ephemeral=True)


# ==================================================
# /BOT_STATUS
# ==================================================

@client.tree.command(name="bot_status", description="Check the bot's permissions and nickname setup")
@app_commands.default_permissions(manage_roles=True)
@app_commands.guild_only()
async def bot_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        return
    bot_member = await bot_member_for(guild)
    channel = await get_db_channel(guild, create=False)
    formats_count = "Unknown"
    try:
        formats_count = str(len(await fetch_formats(guild))) if channel else "0 (storage channel missing)"
    except Exception as exc:
        formats_count = f"Error: {type(exc).__name__}"

    embed = make_embed("Nickname Bot Status")
    embed.add_field(name="Bot member found", value="Yes" if bot_member else "No", inline=True)
    embed.add_field(
        name="Manage Nicknames",
        value="✅ Yes" if bot_member and bot_member.guild_permissions.manage_nicknames else "❌ No",
        inline=True,
    )
    embed.add_field(
        name="Top role",
        value=(
            bot_member.top_role.mention
            if bot_member is not None and bot_member.top_role is not None
            else "Unknown"
        ),
        inline=True,
    )
    embed.add_field(name="Storage channel", value=channel.mention if channel else "Missing — run `/setup`", inline=False)
    embed.add_field(name="Saved formats", value=formats_count, inline=True)
    embed.add_field(
        name="Members intent",
        value=(
            "Code intent is enabled. Confirm **Server Members Intent** is enabled in the Discord Developer Portal."
        ),
        inline=False,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


# ==================================================
# STARTUP
# ==================================================

@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")
    print(f"discord.py version: {discord.__version__}")


client.run(TOKEN)
