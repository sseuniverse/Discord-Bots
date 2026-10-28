from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError(
        f"DISCORD_TOKEN is missing. Create a .env file beside bot.py "
        f"and add: DISCORD_TOKEN=your_bot_token\nExpected file: {BASE_DIR / '.env'}"
    )

CATEGORY_NAME = "SSE giveaway"
CH_GAMES = "giveaway-games"
CH_TEMPLATES = "giveaway-templates"
CH_IDS = "player-ids"
CH_CONFIGS = "giveaway-configs"
CH_LOGS = "giveaway-logs"
CH_REPORTS = "giveaway-reports"
SERVER_ALLOWLIST_FILE = BASE_DIR / "server.txt"
STORAGE_CHANNELS = (CH_GAMES, CH_TEMPLATES, CH_IDS, CH_CONFIGS, CH_LOGS, CH_REPORTS)

# Storage is intentionally kept in Discord messages, as requested. Each record is
# one bot-authored message; JSON records are versioned so they can be migrated.
RECORD_PREFIX = "SSEGB1 "
MAX_DURATION_SECONDS = 365 * 24 * 60 * 60
MAX_PRIZE = 2_000_000_000
TEMPLATE_VARIABLES = {"giveaway_name", "game_name", "total_prize", "winners", "prize_per_person", "currency", "end_time", "joined"}

intents = discord.Intents.default()
intents.message_content = True
intents.members = True


def now_ts() -> int:
    return int(time.time())


def normalize_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def normalize_game_id(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def parse_duration(value: str) -> int:
    """Parse a complete duration such as 2d3h, 30m, or 45s; reject leftovers."""
    raw = value.strip().lower().replace(" ", "")
    if not raw:
        raise ValueError("Enter a duration such as `30m`, `2h`, or `1d6h`.")
    matches = list(re.finditer(r"(\d+)([dhms])", raw))
    if not matches or "".join(m.group(0) for m in matches) != raw:
        raise ValueError("Invalid duration. Use combinations such as `30m`, `2h`, or `1d6h`.")
    total = sum(int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)] for m in matches)
    if total <= 0:
        raise ValueError("Duration must be greater than zero.")
    if total > MAX_DURATION_SECONDS:
        raise ValueError("Duration cannot exceed 365 days.")
    return total


def record_payload(message: discord.Message) -> dict[str, Any] | None:
    if not message.content.startswith(RECORD_PREFIX):
        return None
    try:
        value = json.loads(message.content[len(RECORD_PREFIX):])
        return value if isinstance(value, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def encode_record(value: dict[str, Any]) -> str:
    return RECORD_PREFIX + json.dumps(value, separators=(",", ":"), ensure_ascii=False)


class GiveawayBot(commands.Bot):
    def __init__(self):
        # commands.Bot already creates and owns an app_commands.CommandTree.
        # Creating another tree here raises ClientException at startup.
        super().__init__(command_prefix="!", intents=intents)
        self.entry_locks: dict[int, asyncio.Lock] = {}
        self.finish_locks: dict[int, asyncio.Lock] = {}
        self.recovered = False

    async def setup_hook(self):
        self.add_view(PersistentJoinView())
        self.add_view(ReportReviewView())
        await self.tree.sync()
        print("Slash commands synced and ready!")
        if not self.recovery_loop.is_running():
            self.recovery_loop.start()

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print(f"discord.py version: {discord.__version__}")

    @tasks.loop(seconds=20)
    async def recovery_loop(self):
        # Re-check persistent records periodically. This restores timers after a
        # restart and retries records whose end time has passed.
        for guild in list(self.guilds):
            try:
                logs = await get_db_channel(guild, CH_CONFIGS)
                if not logs:
                    continue
                async for message in logs.history(limit=None):
                    data = record_payload(message)
                    if not data or data.get("kind") != "giveaway":
                        continue
                    # Refresh the visible heartbeat only every five minutes. If the
                    # process stops, this timestamp naturally becomes stale; the bot
                    # cannot edit Discord messages while it is offline.
                    if data.get("kind") == "giveaway" and data.get("status") in ("active", "scheduled"):
                        if now_ts() - int(data.get("bot_status_ts", 0)) >= 300:
                            try:
                                channel = guild.get_channel(int(data.get("channel_id", 0)))
                                if channel is None:
                                    channel = await client.fetch_channel(int(data.get("channel_id", 0)))
                                live = await channel.fetch_message(int(data.get("message_id", 0)))
                                await update_bot_online_status(live, now_ts())
                                data["bot_status_ts"] = now_ts()
                                await message.edit(content=encode_record(data))
                            except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError, TypeError, ValueError) as status_exc:
                                print(f"Could not refresh giveaway status for message {data.get('message_id')}: {status_exc!r}")
                    if data.get("status") in ("active", "drawing", "announcing") and (data.get("status") == "announcing" or int(data.get("end_ts", 0)) <= now_ts()):
                        await finish_giveaway(guild, message, data)
            except Exception as exc:
                print(f"Recovery scan failed for guild {guild.id}: {exc!r}")

    @recovery_loop.before_loop
    async def before_recovery_loop(self):
        await self.wait_until_ready()


client = GiveawayBot()

def load_allowed_server_ids() -> set[int]:
    """Load the server allowlist from server.txt, one Discord guild ID per line."""
    allowed: set[int] = set()
    try:
        with SERVER_ALLOWLIST_FILE.open("r", encoding="utf-8") as file:
            for raw_line in file:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    allowed.add(int(line))
                except ValueError:
                    print(f"Ignoring invalid server ID in {SERVER_ALLOWLIST_FILE.name}: {line!r}")
    except FileNotFoundError:
        print(f"WARNING: {SERVER_ALLOWLIST_FILE.name} was not found. All servers will be blocked except /serverinfo.")
    except OSError as exc:
        print(f"Could not read {SERVER_ALLOWLIST_FILE.name}: {exc!r}")
    return allowed


async def server_bound_interaction_check(interaction: discord.Interaction) -> bool:
    # /serverinfo must remain usable in every server, including unlicensed servers.
    command = interaction.command
    if command is not None and command.name == "serverinfo":
        return True
    if interaction.guild is None:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "This bot's commands can only be used inside an authorized server.", ephemeral=True
            )
        return False
    allowed_ids = load_allowed_server_ids()
    if interaction.guild.id in allowed_ids:
        return True
    message = (
        "🔒 **This server is not authorized to use SSE Giveaway.**\n\n"
        "The giveaway commands are disabled in this server. Contact the bot creator to request access.\n\n"
        f"Server ID: `{interaction.guild.id}`\n"
        "Use `/serverinfo` to check your server ID and authorization status."
    )
    if not interaction.response.is_done():
        await interaction.response.send_message(message, ephemeral=True)
    return False


# Apply the allowlist globally so every slash command is protected consistently.
client.tree.interaction_check = server_bound_interaction_check


async def get_db_channel(guild: discord.Guild, channel_name: str) -> discord.TextChannel | None:
    category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
    if not category:
        return None
    return discord.utils.get(category.text_channels, name=channel_name)


async def require_storage(guild: discord.Guild, channel_name: str) -> discord.TextChannel:
    channel = await get_db_channel(guild, channel_name)
    if channel is None:
        raise RuntimeError(f"Storage channel `#{channel_name}` is missing. Ask an administrator to run `/setup`.")
    return channel


async def iter_bot_records(channel: discord.TextChannel):
    async for msg in channel.history(limit=None, oldest_first=True):
        if msg.author == client.user:
            data = record_payload(msg)
            if data is not None:
                yield msg, data


async def get_config(guild: discord.Guild) -> dict[str, Any]:
    channel = await get_db_channel(guild, CH_CONFIGS)
    if not channel:
        return {"kind": "config", "managers": [], "blacklist": None}
    async for msg, data in iter_bot_records(channel):
        if data.get("kind") == "config":
            return data
    # Backward compatibility with the original plain-text configuration record.
    async for msg in channel.history(limit=None, oldest_first=True):
        if msg.author.bot and msg.content.startswith("Managers:") and "Blacklist:" in msg.content:
            try:
                parts = msg.content.split("|")
                managers_text = parts[0].split(":", 1)[1]
                managers = [int(x.strip()) for x in managers_text.split(",") if x.strip().isdigit()]
                blacklist_text = parts[1].split(":", 1)[1].strip()
                blacklist = int(blacklist_text) if blacklist_text.isdigit() else None
                return {"kind": "config", "managers": managers, "blacklist": blacklist}
            except (IndexError, ValueError):
                continue
    return {"kind": "config", "managers": [], "blacklist": None}


async def save_config(guild: discord.Guild, data: dict[str, Any]) -> None:
    channel = await require_storage(guild, CH_CONFIGS)
    async for msg, old in iter_bot_records(channel):
        if old.get("kind") == "config":
            await msg.edit(content=encode_record(data))
            return
    await channel.send(encode_record(data))


async def is_manager(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.id == interaction.guild.owner_id or interaction.user.guild_permissions.administrator:
        return True
    cfg = await get_config(interaction.guild)
    manager_ids = {int(x) for x in cfg.get("managers", []) if str(x).isdigit()}
    return any(role.id in manager_ids for role in interaction.user.roles)


async def ensure_manager(interaction: discord.Interaction) -> bool:
    if await is_manager(interaction):
        return True
    if not interaction.response.is_done():
        await interaction.response.send_message("❌ You are not allowed to manage giveaways.", ephemeral=True)
    else:
        await interaction.followup.send("❌ You are not allowed to manage giveaways.", ephemeral=True)
    return False


async def get_game(guild: discord.Guild, game_name: str) -> dict[str, Any] | None:
    channel = await get_db_channel(guild, CH_GAMES)
    if not channel:
        return None
    target = normalize_name(game_name)
    async for _, data in iter_bot_records(channel):
        if data.get("kind") == "game" and normalize_name(str(data.get("name", ""))) == target:
            return data
    # Read legacy messages from the original bot, so existing data is not lost.
    async for msg in channel.history(limit=None):
        if msg.author.bot and msg.content.startswith("Game:"):
            parts = [x.strip() for x in msg.content.split("|")]
            if len(parts) >= 3 and normalize_name(parts[0].split(":", 1)[1]) == target:
                req = parts[1].split(":", 1)[-1].strip().lower() == "true"
                currencies = [x.strip() for x in parts[2].split(":", 1)[-1].split(",") if x.strip()]
                return {"kind": "game", "name": parts[0].split(":", 1)[1].strip(), "requires_id": req, "currencies": currencies}
    return None


async def get_player_id(guild: discord.Guild, user_id: int, game_name: str) -> str | None:
    channel = await get_db_channel(guild, CH_IDS)
    if not channel:
        return None
    target = normalize_name(game_name)
    found = None
    async for _, data in iter_bot_records(channel):
        if data.get("kind") == "player_id" and int(data.get("user_id", 0)) == user_id and normalize_name(str(data.get("game", ""))) == target:
            found = str(data.get("in_game_id", "")).strip() or None
    if found:
        return found
    # Legacy record format: DiscordID | game name lowercase | in-game ID
    async for msg in channel.history(limit=None, oldest_first=True):
        if msg.author.bot:
            parts = [p.strip() for p in msg.content.split("|", 2)]
            if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) == user_id and normalize_name(parts[1]) == target:
                found = parts[2] or None
    return found


async def find_id_owner(guild: discord.Guild, game_name: str, in_game_id: str, exclude_user_id: int | None = None) -> int | None:
    """Find another Discord user already using this game ID (including legacy records)."""
    wanted = normalize_game_id(in_game_id)
    if not wanted:
        return None
    channel = await get_db_channel(guild, CH_IDS)
    if not channel:
        return None
    owners: dict[tuple[int, str], str] = {}
    async for msg in channel.history(limit=None, oldest_first=True):
        if msg.author != client.user:
            continue
        data = record_payload(msg)
        if data and data.get("kind") == "player_id":
            try:
                uid = int(data.get("user_id", 0))
            except (TypeError, ValueError):
                continue
            game = str(data.get("game", ""))
            value = str(data.get("in_game_id", ""))
            if uid and normalize_name(game) == normalize_name(game_name):
                owners[(uid, normalize_name(game))] = value
        elif not data:
            parts = [x.strip() for x in msg.content.split("|", 2)]
            if len(parts) == 3 and parts[0].isdigit() and normalize_name(parts[1]) == normalize_name(game_name):
                owners[(int(parts[0]), normalize_name(parts[1]))] = parts[2]
    for (uid, _), value in owners.items():
        if uid != exclude_user_id and normalize_game_id(value) == wanted:
            return uid
    return None


async def delete_player_id_record(guild: discord.Guild, user_id: int, game_name: str) -> bool:
    channel = await get_db_channel(guild, CH_IDS)
    if not channel:
        return False
    deleted = False
    async for msg in channel.history(limit=None):
        if msg.author != client.user:
            continue
        data = record_payload(msg)
        if data and data.get("kind") == "player_id" and int(data.get("user_id", 0)) == user_id and normalize_name(str(data.get("game", ""))) == normalize_name(game_name):
            await msg.delete(); deleted = True
        elif not data:
            parts = [x.strip() for x in msg.content.split("|", 2)]
            if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) == user_id and normalize_name(parts[1]) == normalize_name(game_name):
                await msg.delete(); deleted = True
    return deleted


async def validate_game_id(guild: discord.Guild, game_name: str, in_game_id: str) -> str:
    clean = in_game_id.strip()
    game = await get_game(guild, game_name)
    minimum = int((game or {}).get("min_id_length", 3))
    maximum = int((game or {}).get("max_id_length", 50))
    if not clean or len(clean) < minimum or len(clean) > maximum or "\n" in clean or "\r" in clean:
        raise ValueError(f"The ID for **{game_name}** must be {minimum}–{maximum} characters long.")
    return clean


async def save_player_id(guild: discord.Guild, user_id: int, game_name: str, in_game_id: str) -> None:
    clean = await validate_game_id(guild, game_name, in_game_id)
    owner = await find_id_owner(guild, game_name, clean, exclude_user_id=user_id)
    if owner is not None:
        raise ValueError(f"This in-game ID is already registered by <@{owner}> for **{game_name}**. If it belongs to you, use `/report_duplicate_id` and attach proof.")
    channel = await require_storage(guild, CH_IDS)
    target = normalize_name(game_name)
    found_msg = None
    # Remove duplicate records for this user/game, leaving exactly one canonical record.
    async for msg in channel.history(limit=None, oldest_first=True):
        if msg.author != client.user:
            continue
        data = record_payload(msg)
        if data and data.get("kind") == "player_id" and int(data.get("user_id", 0)) == user_id and normalize_name(str(data.get("game", ""))) == target:
            if found_msg is None:
                found_msg = msg
            else:
                await msg.delete()
        elif not data:
            parts = [p.strip() for p in msg.content.split("|", 2)]
            if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) == user_id and normalize_name(parts[1]) == target:
                await msg.delete()
    payload = {"kind": "player_id", "user_id": user_id, "game": game_name, "in_game_id": clean}
    if found_msg:
        await found_msg.edit(content=encode_record(payload))
    else:
        await channel.send(encode_record(payload))


async def get_giveaway_record(guild: discord.Guild, giveaway_id: int) -> tuple[discord.Message, dict[str, Any]] | None:
    records = await get_db_channel(guild, CH_CONFIGS)
    if records:
        async for msg, data in iter_bot_records(records):
            if data.get("kind") == "giveaway" and int(data.get("message_id", 0)) == giveaway_id:
                return msg, data
    # Backward compatibility: older versions stored machine records in giveaway-logs.
    logs = await get_db_channel(guild, CH_LOGS)
    if not logs:
        return None
    async for msg, data in iter_bot_records(logs):
        if data.get("kind") == "giveaway" and int(data.get("message_id", 0)) == giveaway_id:
            return msg, data
    async for msg in logs.history(limit=None):
        if msg.author.bot and f"GiveawayID: {giveaway_id} |" in msg.content:
            try:
                parts = dict(piece.strip().split(": ", 1) for piece in msg.content.split("|"))
                return msg, {"kind": "giveaway", "message_id": giveaway_id, "game": parts["Game"], "channel_id": int(parts["Channel"]), "end_ts": int(float(parts["EndTime"])), "winners": int(parts["Winners"]), "status": "active", "thread_id": msg.thread.id if msg.thread else None}
            except (KeyError, ValueError, AttributeError):
                return None
    return None


async def get_template(guild: discord.Guild, game_name: str, template_name: str) -> tuple[str, str] | None:
    channel = await get_db_channel(guild, CH_TEMPLATES)
    if not channel:
        return None
    game_target, template_target = normalize_name(game_name), normalize_name(template_name)
    async for msg in channel.history(limit=None, oldest_first=True):
        if msg.author != client.user:
            continue
        lines = msg.content.splitlines()
        if not lines:
            continue
        header = lines[0]
        match = re.fullmatch(r"\s*Game:\s*(.*?)\s*\|\s*Template:\s*(.*?)\s*\|\s*Currency:\s*(.*?)\s*", header, re.I)
        if match and normalize_name(match.group(1)) == game_target and normalize_name(match.group(2)) == template_target:
            if "---" not in msg.content:
                continue
            body = msg.content.split("---", 1)[1].strip()
            return body, match.group(3).strip()
    return None


def giveaway_embed_from_body(body: str, game_name: str, status: str) -> discord.Embed:
    """Render each giveaway as a clean, self-contained card instead of a wall of channel text."""
    lines = body.splitlines()
    title = lines[0].strip() if lines else f"🎉 {game_name} Giveaway"
    description = "\n".join(lines[1:]).strip() if len(lines) > 1 else body.strip()
    if len(description) > 4000:
        description = description[:3997] + "..."
    color = discord.Color.gold() if status == "scheduled" else discord.Color.green()
    embed = discord.Embed(title=title[:256], description=description or "Good luck!", color=color)
    if status == "scheduled":
        embed.set_footer(text=f"⏸️ Not started yet · 🤖 Bot online · Last check <t:{now_ts()}:R>")
    else:
        embed.set_footer(text=f"🤖 Bot online · Last check <t:{now_ts()}:R> · SSE Giveaway")
    return embed


async def update_bot_online_status(message: discord.Message, timestamp: int) -> None:
    """Refresh the visible bot heartbeat on a giveaway post while the bot is online."""
    if message.embeds:
        embed = message.embeds[0].copy()
        if "Not started yet" in (embed.footer.text or ""):
            embed.set_footer(text=f"⏸️ Not started yet · 🤖 Bot online · Last check <t:{timestamp}:R>")
        else:
            embed.set_footer(text=f"🤖 Bot online · Last check <t:{timestamp}:R> · SSE Giveaway")
        await message.edit(embed=embed)
        return
    content = message.content or ""
    status_line = re.compile(r"^🤖 \*\*Bot status:\*\* 🟢 Online · Last check <t:\d+:R>$", re.M)
    replacement = f"🤖 **Bot status:** 🟢 Online · Last check <t:{timestamp}:R>"
    if status_line.search(content):
        content = status_line.sub(replacement, content)
    else:
        content = content.rstrip() + "\n\n" + replacement
    await message.edit(content=content, allowed_mentions=discord.AllowedMentions.none())


async def update_joined_count(message: discord.Message, count: int) -> None:
    if message.embeds:
        embed = message.embeds[0].copy()
        description = embed.description or ""
        marker = re.compile(r"Players Joined:\s*(?:\*\*\s*)?\d+(?:\s*\*\*)?", re.I)
        if marker.search(description):
            description = marker.sub(f"Players Joined: **{count}**", description)
        else:
            description = description.rstrip() + f"\n\n👥 **Players Joined:** {count}"
        embed.description = description[:4096]
        await message.edit(embed=embed)
        return
    content = message.content
    marker = re.compile(r"Players Joined:\s*(?:\*\*\s*)?\d+(?:\s*\*\*)?", re.I)
    if marker.search(content):
        content = marker.sub(f"Players Joined: **{count}**", content)
    else:
        content = content.rstrip() + f"\n\n👥 Players Joined: **{count}**"
    await message.edit(content=content)


async def get_thread(guild: discord.Guild, data: dict[str, Any]) -> discord.Thread | None:
    thread_id = data.get("thread_id")
    if not thread_id:
        return None
    thread = guild.get_thread(int(thread_id))
    if thread:
        return thread
    try:
        fetched = await client.fetch_channel(int(thread_id))
        return fetched if isinstance(fetched, discord.Thread) else None
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
        return None


async def get_entry_ids(thread: discord.Thread) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    async for msg in thread.history(limit=None, oldest_first=True):
        # Supports both new structured entries and legacy raw Discord IDs.
        data = None
        if msg.content.startswith("ENTRY "):
            try:
                data = json.loads(msg.content[6:])
            except json.JSONDecodeError:
                data = None
        value = data.get("user_id") if isinstance(data, dict) else (msg.content.strip() if msg.content.strip().isdigit() else None)
        if value is not None:
            try:
                user_id = int(value)
                if user_id not in seen:
                    seen.add(user_id)
                    result.append(user_id)
            except (TypeError, ValueError):
                pass
    return result


class PersistentJoinView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Start Giveaway", style=discord.ButtonStyle.primary, custom_id="sse_giveaway:start:v1")
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not interaction.message:
            return await interaction.response.send_message("This button only works inside the server.", ephemeral=True)
        if not await is_manager(interaction):
            return await interaction.response.send_message("❌ Only a configured giveaway manager can start this giveaway.", ephemeral=True)
        found = await get_giveaway_record(interaction.guild, interaction.message.id)
        if not found:
            return await interaction.response.send_message("❌ Giveaway record not found.", ephemeral=True)
        log_msg, data = found
        if data.get("status") != "scheduled":
            return await interaction.response.send_message("This giveaway is not waiting for a manual start.", ephemeral=True)
        duration_seconds = int(data.get("duration_seconds", 0))
        if duration_seconds <= 0:
            return await interaction.response.send_message("❌ Saved duration is invalid. Please cancel and recreate this giveaway.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        data["status"] = "active"
        data["started_ts"] = now_ts()
        data["end_ts"] = data["started_ts"] + duration_seconds
        try:
            await log_msg.edit(content=encode_record(data))
            if interaction.message.embeds:
                embed = interaction.message.embeds[0].copy()
                description = (embed.description or "").replace(
                    "waiting for a manager to start this giveaway", f"<t:{data['end_ts']}:R> · Ends <t:{data['end_ts']}:F>"
                ).replace("{end_time}", f"<t:{data['end_ts']}:R> · Ends <t:{data['end_ts']}:F>")
                embed.description = description[:4096]
                embed.color = discord.Color.green()
                embed.set_footer(text="SSE Giveaway • Good luck to all participants")
                await interaction.message.edit(embed=embed, view=giveaway_view("active"))
            else:
                body = interaction.message.content
                body = body.replace("waiting for a manager to start this giveaway", f"<t:{data['end_ts']}:R> · Ends <t:{data['end_ts']}:F>")
                body = body.replace("{end_time}", f"<t:{data['end_ts']}:R>")
                await interaction.message.edit(content=body, view=giveaway_view("active"))
        except discord.HTTPException:
            return await interaction.followup.send("❌ Could not start this giveaway. Check the bot's message permissions.", ephemeral=True)
        await refresh_giveaway_log(interaction.guild, data)
        await interaction.followup.send(f"✅ Giveaway started! It ends <t:{data['end_ts']}:F>.", ephemeral=True)

    @discord.ui.button(label="Join Giveaway", style=discord.ButtonStyle.green, custom_id="sse_giveaway:join:v1")
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.message:
            return await interaction.response.send_message("This button only works inside the server.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            cfg = await get_config(interaction.guild)
            blacklist = cfg.get("blacklist")
            if blacklist and any(r.id == int(blacklist) for r in interaction.user.roles):
                return await interaction.followup.send("❌ You are not allowed to join this giveaway.", ephemeral=True)

            found = await get_giveaway_record(interaction.guild, interaction.message.id)
            if not found:
                return await interaction.followup.send("❌ This giveaway could not be found or has ended.", ephemeral=True)
            log_msg, data = found
            if data.get("status") == "scheduled":
                return await interaction.followup.send("⏳ **This giveaway has not started yet.** Please wait for a manager to press **Start Giveaway**. The Join button will appear when it starts.", ephemeral=True)
            if data.get("status") != "active" or int(data.get("end_ts", 0)) <= now_ts():
                return await interaction.followup.send("🏁 **This giveaway has ended.** You can check the announcement for the winners.", ephemeral=True)
            thread = await get_thread(interaction.guild, data)
            if not thread:
                return await interaction.followup.send("❌ The private tracking thread is unavailable. Please contact a manager.", ephemeral=True)

            lock = client.entry_locks.setdefault(interaction.message.id, asyncio.Lock())
            async with lock:
                entrants = await get_entry_ids(thread)
                if interaction.user.id in entrants:
                    return await interaction.followup.send("⚠️ You have already joined this giveaway.", ephemeral=True)
                game_name = str(data.get("game", ""))
                game = await get_game(interaction.guild, game_name)
                player_id = await get_player_id(interaction.guild, interaction.user.id, game_name)
                if game and game.get("requires_id") and player_id:
                    duplicate_owner = await find_id_owner(interaction.guild, game_name, player_id, exclude_user_id=interaction.user.id)
                    if duplicate_owner is not None:
                        return await interaction.followup.send(
                            f"❌ Your saved ID is already registered by <@{duplicate_owner}> for **{game_name}**. You cannot join until a moderator reviews it. If the ID belongs to you, use `/report_duplicate_id` and attach proof.",
                            ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
                        )
                if game and game.get("requires_id") and not player_id:
                    # Modal must be the initial interaction response; deferred responses cannot open a modal.
                    # Respond with a button to open the modal in a fresh interaction instead.
                    view = NeedIDView(game_name, interaction.message.id)
                    return await interaction.followup.send("Your in-game ID is required. Tap below to enter it.", view=view, ephemeral=True)
                await thread.send("ENTRY " + json.dumps({"user_id": interaction.user.id}, separators=(",", ":")))
                await update_joined_count(interaction.message, len(entrants) + 1)
            await interaction.followup.send("✅ You have successfully entered the giveaway!", ephemeral=True)
        except Exception as exc:
            print(f"Join handler error: {exc!r}")
            await interaction.followup.send("❌ Something went wrong while joining. Please try again or contact a manager.", ephemeral=True)


def giveaway_view(status: str) -> discord.ui.View:
    """Show only the controls appropriate for this giveaway state."""
    view = PersistentJoinView()
    for item in list(view.children):
        custom_id = getattr(item, "custom_id", "")
        if status == "scheduled" and custom_id == "sse_giveaway:join:v1":
            view.remove_item(item)
        elif status != "scheduled" and custom_id == "sse_giveaway:start:v1":
            view.remove_item(item)
    return view


def giveaway_log_embed(data: dict[str, Any]) -> discord.Embed:
    status = str(data.get("status", "unknown")).replace("_", " ").title()
    embed = discord.Embed(
        title=f"🎁 {data.get('giveaway_name') or data.get('game', 'Game')} · Giveaway Record",
        description=f"**Game:** {data.get('game', 'Game')}\n**Status:** {status}\n**Template used:** {data.get('template', 'Unknown')}",
        color=(discord.Color.green() if status == "Active" else discord.Color.blurple()),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Giveaway ID", value=f"`{data.get('message_id', 'unknown')}`", inline=True)
    embed.add_field(name="Prize pool", value=f"{data.get('prize_pool', 0)} {data.get('currency', '')}", inline=True)
    embed.add_field(name="Winners", value=str(data.get('winners', 1)), inline=True)
    embed.add_field(name="Duration", value=f"{int(data.get('duration_seconds', 0))} seconds", inline=True)
    embed.add_field(name="Posted in", value=f"<#{int(data.get('channel_id', 0))}>", inline=True)
    embed.add_field(name="Giveaway message", value=f"[Jump to giveaway](https://discord.com/channels/{int(data.get('guild_id', 0))}/{int(data.get('channel_id', 0))}/{int(data.get('message_id', 0))})", inline=True)
    preview = str(data.get("template_preview", "")).strip()
    if preview:
        embed.add_field(name="Template used", value=preview[:1024], inline=False)
    if data.get("winner_details"):
        embed.add_field(name="Winners announced", value="\n".join(f"<@{int(w['discord'])}> — {w.get('prize', '')} {data.get('currency', '')}" for w in data["winner_details"] if isinstance(w, dict))[:1024], inline=False)
    elif status == "Scheduled":
        embed.add_field(name="Next step", value="A manager must press **Start Giveaway**. The countdown begins after that.", inline=False)
    embed.set_footer(text=f"Giveaway ID: {data.get('message_id', 'unknown')} · Managed by SSE Giveaway")
    return embed


async def refresh_giveaway_log(guild: discord.Guild, data: dict[str, Any]) -> None:
    log_id = data.get("log_message_id")
    if not log_id:
        return
    logs = await get_db_channel(guild, CH_LOGS)
    if not logs:
        return
    try:
        msg = await logs.fetch_message(int(log_id))
        await msg.edit(embed=giveaway_log_embed(data))
    except (discord.HTTPException, ValueError, TypeError):
        pass


class NeedIDView(discord.ui.View):
    def __init__(self, game_name: str, giveaway_id: int):
        super().__init__(timeout=300)
        self.game_name = game_name
        self.giveaway_id = giveaway_id

    @discord.ui.button(label="Enter In-Game ID", style=discord.ButtonStyle.primary)
    async def enter_id(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = IDModal(self.game_name, self.giveaway_id)
        if interaction.guild:
            game = await get_game(interaction.guild, self.game_name)
            if game:
                modal.in_game_id.min_length = max(1, min(4000, int(game.get("min_id_length", 3))))
                modal.in_game_id.max_length = max(modal.in_game_id.min_length, min(4000, int(game.get("max_id_length", 50))))
                modal.in_game_id.placeholder = f"Enter {modal.in_game_id.min_length}-{modal.in_game_id.max_length} characters"
        await interaction.response.send_modal(modal)


class IDModal(discord.ui.Modal):
    def __init__(self, game_name: str, giveaway_id: int):
        super().__init__(title=f"Enter {game_name} ID", timeout=300)
        self.game_name = game_name
        self.giveaway_id = giveaway_id
        # TextInput length constraints are set from the game's saved configuration.
        game = None
        self.in_game_id = discord.ui.TextInput(label="Your In-Game ID", placeholder="Enter your exact game ID", min_length=3, max_length=50)
        self.add_item(self.in_game_id)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("This form only works inside the server.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            cfg = await get_config(interaction.guild)
            blacklist = cfg.get("blacklist")
            if blacklist and any(r.id == int(blacklist) for r in interaction.user.roles):
                return await interaction.followup.send("❌ You are not allowed to join this giveaway.", ephemeral=True)
            found = await get_giveaway_record(interaction.guild, self.giveaway_id)
            if not found or found[1].get("status") != "active" or int(found[1].get("end_ts", 0)) <= now_ts():
                return await interaction.followup.send("❌ This giveaway has ended or is closing.", ephemeral=True)
            game = await get_game(interaction.guild, self.game_name)
            if not game or not game.get("requires_id"):
                return await interaction.followup.send("❌ This game no longer requires an ID. Please join again using the giveaway button.", ephemeral=True)
            clean_id = self.in_game_id.value.strip()
            await save_player_id(interaction.guild, interaction.user.id, self.game_name, clean_id)
            thread = await get_thread(interaction.guild, found[1])
            if not thread:
                return await interaction.followup.send("❌ The giveaway tracking thread is unavailable. Your ID was saved, but you were not entered.", ephemeral=True)
            lock = client.entry_locks.setdefault(self.giveaway_id, asyncio.Lock())
            async with lock:
                entrants = await get_entry_ids(thread)
                if interaction.user.id not in entrants:
                    await thread.send("ENTRY " + json.dumps({"user_id": interaction.user.id}, separators=(",", ":")))
                    public_channel = interaction.guild.get_channel(int(found[1]["channel_id"]))
                    if isinstance(public_channel, discord.TextChannel):
                        try:
                            live = await public_channel.fetch_message(self.giveaway_id)
                            await update_joined_count(live, len(entrants) + 1)
                        except discord.HTTPException:
                            pass
            await interaction.followup.send("✅ Your ID has been saved and you are entered into the giveaway.", ephemeral=True)
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
        except Exception as exc:
            print(f"ID modal error: {exc!r}")
            await interaction.followup.send("❌ Something went wrong saving your ID. Please try again.", ephemeral=True)


async def apply_manager_channel_permissions(guild: discord.Guild, manager_ids: list[int]) -> None:
    """Give configured manager roles access to the private storage/report channels."""
    category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
    if not category:
        return
    bot_member = guild.me or await guild.fetch_member(client.user.id)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True, manage_threads=True, create_public_threads=True, send_messages_in_threads=True),
    }
    for role in guild.roles:
        if role.id in manager_ids:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, send_messages_in_threads=True)
    await category.edit(overwrites=overwrites, reason="Grant configured giveaway managers access to private giveaway channels")
    for channel in category.text_channels:
        await channel.edit(overwrites=overwrites, reason="Sync giveaway manager role access")


async def apply_template_permissions(guild: discord.Guild, manager_ids: list[int]) -> None:
    """Let configured managers use only the template drafting channel."""
    channel = await get_db_channel(guild, CH_TEMPLATES)
    if not channel:
        return
    overwrites = dict(channel.overwrites)
    overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
    me = guild.me or await guild.fetch_member(client.user.id)
    overwrites[me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True, manage_threads=True, create_public_threads=True, send_messages_in_threads=True)
    for role in guild.roles:
        if role.id in manager_ids:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, add_reactions=False, manage_messages=False)
    # This is a dedicated bot-managed channel: remove stale role grants so a
    # role removed from configuration cannot retain access to template drafts.
    for target in list(overwrites):
        if isinstance(target, discord.Role) and target.id != guild.default_role.id and target.id not in manager_ids:
            overwrites.pop(target, None)
    await channel.edit(overwrites=overwrites, reason="Apply giveaway template manager permissions")


class ResetStorageView(discord.ui.View):
    """Owner-only confirmation for deleting the bot's storage category."""
    def __init__(self, owner_id: int):
        super().__init__(timeout=60)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ Only the server owner who requested this reset can confirm it.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Delete storage channels", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None or guild.owner_id != interaction.user.id:
            return await interaction.response.send_message(
                "❌ Only the current server owner can reset giveaway storage.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
        try:
            # Deleting a Discord category does NOT delete its channels; it only
            # uncategorizes them. Delete all channels in the category first, plus
            # any orphaned storage channels left behind by older reset attempts.
            storage_channels = [
                ch for ch in guild.text_channels
                if ch.name in STORAGE_CHANNELS or (category is not None and ch.category_id == category.id)
            ]
            for channel in storage_channels:
                try:
                    await channel.delete(
                        reason=f"Giveaway storage reset by server owner {interaction.user} ({interaction.user.id})"
                    )
                except discord.NotFound:
                    pass
            if category is not None:
                await category.delete(
                    reason=f"Giveaway storage reset by server owner {interaction.user} ({interaction.user.id})"
                )
            for item in self.children:
                item.disabled = True
            await interaction.edit_original_response(
                content=(
                    "✅ **Giveaway storage reset complete.** Deleted the `SSE giveaway` category (if present) "
                    "and all six giveaway storage channels, including any orphaned channels left outside the category. "
                    "Your unrelated channels, such as `#nickname-formats`, were not touched. Run `/setup` to recreate storage."
                ),
                view=self,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I couldn't delete one or more storage channels. Give the bot **Manage Channels** permission, "
                "make sure its role is high enough, and try again. Any channels already deleted will remain deleted.",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            print(f"Storage reset failed in guild {guild.id}: {exc!r}")
            await interaction.followup.send(
                "❌ Discord couldn't delete one or more storage channels. Check bot permissions and try again.",
                ephemeral=True,
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Reset cancelled. No channels were deleted.", view=self)


@client.tree.command(name="serverinfo", description="Show this server's ID and giveaway bot authorization status")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message(
            "Use this command inside a server to see its server ID and authorization status.",
            ephemeral=True,
        )
    allowed = guild.id in load_allowed_server_ids()
    embed = discord.Embed(
        title="SSE Giveaway — Server Information",
        color=discord.Color.green() if allowed else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Server", value=guild.name, inline=False)
    embed.add_field(name="Server ID", value=f"`{guild.id}`", inline=False)
    embed.add_field(
        name="Authorization",
        value=(
            "✅ **Authorized** — giveaway commands are enabled."
            if allowed else
            "🔒 **Not authorized** — giveaway commands are disabled in this server. Contact the bot creator to request access."
        ),
        inline=False,
    )
    embed.set_footer(text="Copy the Server ID above to request access.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(name="reset_storage", description="OWNER ONLY: delete the giveaway storage category and all its channels")
@app_commands.default_permissions(administrator=True)
async def reset_storage(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Use this command inside your server.", ephemeral=True)
    if interaction.user.id != guild.owner_id:
        return await interaction.response.send_message(
            "🔒 **Server owner only.** Only the actual server owner can reset and delete giveaway storage channels. Administrator or manager roles are not sufficient.",
            ephemeral=True,
        )
    category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
    storage_channels = [
        ch for ch in guild.text_channels
        if ch.name in STORAGE_CHANNELS or (category is not None and ch.category_id == category.id)
    ]
    if category is None and not storage_channels:
        return await interaction.response.send_message(
            "ℹ️ No giveaway storage category or storage channels were found. Nothing needs to be deleted.",
            ephemeral=True,
        )
    channel_names = ", ".join(f"`#{ch.name}`" for ch in storage_channels) or "no text channels"
    view = ResetStorageView(guild.owner_id)
    await interaction.response.send_message(
        f"⚠️ **Danger: this cannot be undone.**\n\nThis will delete the **`{CATEGORY_NAME}`** category if present and these giveaway storage channels, including orphaned channels outside the category: {channel_names}. This removes saved game settings, templates, player IDs, giveaway records/history, and reports.\n\nUnrelated channels such as `#nickname-formats` will not be deleted. Only the server owner can confirm. Run `/setup` afterward to recreate storage.",
        view=view,
        ephemeral=True,
    )


@client.tree.command(name="setup", description="Create the hidden SSE giveaway storage category and channels")
@app_commands.default_permissions(administrator=True)
async def setup_system(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Run this command inside a server.", ephemeral=True)
    if not (interaction.user.id == interaction.guild.owner_id or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("Only the server owner or an administrator can run setup.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    bot_member = guild.me
    if bot_member is None:
        bot_member = await guild.fetch_member(client.user.id)
    category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
    cfg = await get_config(guild)
    manager_ids = {int(x) for x in cfg.get("managers", []) if str(x).isdigit()}
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True, manage_threads=True, create_public_threads=True, send_messages_in_threads=True),
    }
    for role in guild.roles:
        if role.id in manager_ids:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, send_messages_in_threads=True)
    if category is None:
        category = await guild.create_category(CATEGORY_NAME, overwrites=overwrites, reason="Initialize giveaway bot storage")
    else:
        await category.edit(overwrites=overwrites, reason="Secure giveaway bot storage permissions")
    for name in STORAGE_CHANNELS:
        channel = discord.utils.get(category.text_channels, name=name)
        if channel is None:
            channel = await guild.create_text_channel(name, category=category, overwrites=overwrites, reason="Initialize giveaway storage")
        else:
            await channel.edit(overwrites=overwrites, reason="Secure giveaway storage permissions")
        if name == CH_TEMPLATES:
            # Replace the previous bot-authored variable guide with the current one.
            # Normal user template messages are never deleted by this migration.
            async for msg in channel.history(limit=100):
                if msg.author == client.user and "GIVEAWAY TEMPLATES" in msg.content:
                    try:
                        await msg.delete()
                    except discord.HTTPException:
                        pass
            guide = ("🛠️ **GIVEAWAY TEMPLATES — VARIABLE GUIDE**\n\n"
                     "Create or update a template by sending your template message first, then running `/add_template` with its message link/ID, registered game, and template title. The bot saves it only if every variable is supported.\n\n"
                     "**Supported variables (case-sensitive):**\n"
                     "`{giveaway_name}` — optional custom giveaway name; blank if none was supplied\n"
                     "`{game_name}` — registered game name\n"
                     "`{total_prize}` — total prize pool\n"
                     "`{prize_per_person}` — base prize per winner\n"
                     "`{currency}` — registered game currency\n"
                     "`{winners}` — number of winners\n"
                     "`{end_time}` — Discord relative end time (or a waiting message before manual start)\n"
                     "`{joined}` — current number of joined players\n\n"
                     "**Important:** The bot no longer adds the giveaway name and game name above your template automatically. Add `{giveaway_name}` and/or `{game_name}` wherever you want them to appear. Unsupported variable names are rejected. Messages in this channel are not automatically saved as templates.")
            guide_msg = await channel.send(guide)
            try:
                await guide_msg.pin()
            except discord.HTTPException:
                pass
    await apply_manager_channel_permissions(guild, [int(x) for x in cfg.get("managers", []) if str(x).isdigit()])
    await apply_template_permissions(guild, [int(x) for x in cfg.get("managers", []) if str(x).isdigit()])
    await interaction.followup.send("✅ Storage channels are ready, including **#giveaway-reports**. Configured manager roles now have access to the giveaway channels and report review buttons.", ephemeral=True)


@client.tree.command(name="configure_roles", description="Configure up to two giveaway manager roles and an optional blacklist role")
@app_commands.default_permissions(administrator=True)
async def configure_roles(interaction: discord.Interaction, manager1: discord.Role, manager2: discord.Role | None = None, blacklist: discord.Role | None = None):
    if not interaction.guild or not await ensure_manager(interaction):
        return
    if manager2 and manager1.id == manager2.id:
        return await interaction.response.send_message("Choose two different manager roles.", ephemeral=True)
    try:
        cfg = {"kind": "config", "version": 1, "managers": [manager1.id] + ([manager2.id] if manager2 else []), "blacklist": blacklist.id if blacklist else None}
        await save_config(interaction.guild, cfg)
        await apply_manager_channel_permissions(interaction.guild, cfg["managers"])
        await apply_template_permissions(interaction.guild, cfg["managers"])
        await interaction.response.send_message("✅ Giveaway roles configured. The selected manager roles now have access to private giveaway channels, including #giveaway-reports, and can review reports using the buttons on each report.", ephemeral=True)
    except Exception as exc:
        print(f"configure_roles failed: {exc!r}")
        await interaction.response.send_message("❌ Configuration failed. Run `/setup` first and check the bot's permissions.", ephemeral=True)


@client.tree.command(name="create_game", description="Register or update a game and its currencies")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(game_name="Game name", requires_id="Whether participants must enter an ID", currencies="Comma-separated currencies", min_id_length="Minimum number of characters in this game's ID", max_id_length="Maximum number of characters in this game's ID")
async def create_game(interaction: discord.Interaction, game_name: str, requires_id: bool, currencies: str, min_id_length: app_commands.Range[int, 1, 100] = 3, max_id_length: app_commands.Range[int, 1, 100] = 50):
    if not interaction.guild or not await ensure_manager(interaction):
        return
    name = game_name.strip()
    currency_list = list(dict.fromkeys(x.strip() for x in currencies.split(",") if x.strip()))
    if not name or len(name) > 80 or not currency_list or any(len(x) > 40 for x in currency_list):
        return await interaction.response.send_message("Provide a game name (1–80 characters) and one or more comma-separated currencies (up to 40 characters each).", ephemeral=True)
    if requires_id and min_id_length > max_id_length:
        return await interaction.response.send_message("❌ Minimum ID length cannot be greater than maximum ID length.", ephemeral=True)
    if not requires_id:
        min_id_length, max_id_length = 1, 50
    try:
        channel = await require_storage(interaction.guild, CH_GAMES)
        existing = None
        async for msg, data in iter_bot_records(channel):
            if data.get("kind") == "game" and normalize_name(str(data.get("name", ""))) == normalize_name(name):
                existing = msg
        payload = {"kind": "game", "version": 1, "name": name, "requires_id": bool(requires_id), "currencies": currency_list, "min_id_length": int(min_id_length), "max_id_length": int(max_id_length)}
        if existing:
            await existing.edit(content=encode_record(payload))
        else:
            await channel.send(encode_record(payload))
        id_note = f" ID length: {min_id_length}–{max_id_length} characters." if requires_id else " No in-game ID required."
        await interaction.response.send_message(f"✅ Game `{name}` saved. Currencies: `{', '.join(currency_list)}`.{id_note}", ephemeral=True)
    except Exception as exc:
        print(f"create_game failed: {exc!r}")
        await interaction.response.send_message("❌ Could not save the game. Run `/setup` first and check bot permissions.", ephemeral=True)


@client.event
async def on_message(message: discord.Message):
    # Template messages are now imported explicitly with /add_template.
    # Do not delete, rewrite, or re-send ordinary messages in this channel.
    if message.author.bot or not message.guild or not isinstance(message.channel, discord.TextChannel):
        return
    if message.channel.name == CH_TEMPLATES and message.channel.category and message.channel.category.name == CATEGORY_NAME:
        if not await get_member_is_manager(message.author, message.guild):
            try:
                await message.delete()
            except discord.HTTPException:
                pass


async def get_member_is_manager(user: discord.User | discord.Member, guild: discord.Guild) -> bool:
    if not isinstance(user, discord.Member):
        try:
            user = await guild.fetch_member(user.id)
        except discord.HTTPException:
            return False
    if user.id == guild.owner_id or user.guild_permissions.administrator:
        return True
    cfg = await get_config(guild)
    manager_ids = {int(x) for x in cfg.get("managers", []) if str(x).isdigit()}
    return any(role.id in manager_ids for role in user.roles)


async def list_games(guild: discord.Guild) -> list[dict[str, Any]]:
    channel = await get_db_channel(guild, CH_GAMES)
    if not channel:
        return []
    games: dict[str, dict[str, Any]] = {}
    async for _, data in iter_bot_records(channel):
        if data.get("kind") == "game" and str(data.get("name", "")).strip():
            games[normalize_name(str(data["name"]))] = data
    # Include games saved by older versions of the bot.
    async for msg in channel.history(limit=None):
        if msg.author.bot and msg.content.startswith("Game:"):
            parts = [x.strip() for x in msg.content.split("|")]
            if len(parts) >= 3:
                name = parts[0].split(":", 1)[-1].strip()
                if name and normalize_name(name) not in games:
                    currencies = [x.strip() for x in parts[2].split(":", 1)[-1].split(",") if x.strip()]
                    games[normalize_name(name)] = {"kind": "game", "name": name, "currencies": currencies}
    return list(games.values())


async def game_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    if not interaction.guild:
        return []
    games = await list_games(interaction.guild)
    matches = [str(g["name"]) for g in games if current.casefold() in str(g.get("name", "")).casefold()]
    return [app_commands.Choice(name=name[:100], value=name[:100]) for name in matches[:25]]


async def template_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    if not interaction.guild:
        return []
    game_name = str(getattr(interaction.namespace, "game_name", "") or "")
    channel = await get_db_channel(interaction.guild, CH_TEMPLATES)
    if not channel:
        return []
    names: list[str] = []
    async for msg in channel.history(limit=None, oldest_first=True):
        if msg.author != client.user:
            continue
        lines = msg.content.splitlines()
        if not lines:
            continue
        m = re.fullmatch(r"\s*Game:\s*(.*?)\s*\|\s*Template:\s*(.*?)\s*\|\s*Currency:\s*(.*?)\s*", lines[0], re.I)
        if m and (not game_name or normalize_name(m.group(1)) == normalize_name(game_name)):
            title = m.group(2).strip()
            if title and current.casefold() in title.casefold() and title not in names:
                names.append(title)
    return [app_commands.Choice(name=name[:100], value=name[:100]) for name in names[:25]]


@client.tree.command(name="add_template", description="Save a template from an existing Discord message")
@app_commands.describe(message_link="Link or message ID of the message to use as the template", game_name="Choose a registered game", template_title="Name for this template (max 80 characters)")
async def add_template(interaction: discord.Interaction, message_link: str, game_name: str, template_title: str):
    if not interaction.guild:
        return await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
    if not await ensure_manager(interaction):
        return
    game = await get_game(interaction.guild, game_name)
    if not game:
        return await interaction.response.send_message("❌ That game is not registered. Use `/create_game` first.", ephemeral=True)
    title = template_title.strip()
    if len(title) > 80:
        return await interaction.response.send_message("❌ Template title must be 80 characters or fewer.", ephemeral=True)
    if not title:
        return await interaction.response.send_message("❌ Template title cannot be empty.", ephemeral=True)

    # Accept a message ID or a standard Discord message link.
    match = re.search(r"(?:https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/\d+/(\d+)/(\d+))", message_link.strip())
    source_channel_id = int(match.group(1)) if match else None
    source_message_id = int(match.group(2)) if match else None
    if not match and message_link.strip().isdigit():
        source_message_id = int(message_link.strip())
    if not source_message_id:
        return await interaction.response.send_message("❌ Provide a valid Discord message ID or message link.", ephemeral=True)
    source_channel = interaction.guild.get_channel(source_channel_id) if source_channel_id else interaction.channel
    if not isinstance(source_channel, discord.TextChannel):
        return await interaction.response.send_message("❌ I couldn't find the message's text channel in this server.", ephemeral=True)
    try:
        source_message = await source_channel.fetch_message(source_message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return await interaction.response.send_message("❌ I couldn't read that message. Check the link and the bot's channel permissions.", ephemeral=True)
    body = source_message.content.strip()
    if not body:
        return await interaction.response.send_message("❌ The source message has no text. Embed-only messages are not supported as templates.", ephemeral=True)
    if len(body) > 1700:
        return await interaction.response.send_message("❌ Template text is too long. Keep it under 1,700 characters.", ephemeral=True)

    found_variables = set(re.findall(r"\{([^{}]+)\}", body))
    unknown = sorted(found_variables - TEMPLATE_VARIABLES)
    malformed = re.findall(r"\{[^{}]*\}", body)
    if unknown:
        return await interaction.response.send_message("❌ Template not saved. Unknown variable(s): " + ", ".join(f"`{{{v}}}`" for v in unknown) + "\nSupported variables: " + ", ".join(f"`{{{v}}}`" for v in sorted(TEMPLATE_VARIABLES)), ephemeral=True)
    if re.search(r"\{[^{}]*\}", body) and not found_variables:
        return await interaction.response.send_message("❌ Template not saved. A variable is malformed. Use supported placeholders exactly, such as `{total_prize}`.", ephemeral=True)

    currencies = [str(x).strip() for x in game.get("currencies", []) if str(x).strip()]
    if not currencies:
        return await interaction.response.send_message("❌ This game has no currencies configured. Update it with `/create_game` first.", ephemeral=True)
    currency = currencies[0]
    templates_channel = await require_storage(interaction.guild, CH_TEMPLATES)
    header = f"Game: {game['name']} | Template: {title} | Currency: {currency}"
    saved_content = header + "\n---\n" + body
    if len(saved_content) > 1900:
        return await interaction.response.send_message("❌ The template plus its header exceeds Discord's message limit. Shorten the source message.", ephemeral=True)
    try:
        async for old in templates_channel.history(limit=None):
            if old.author == client.user and old.content.splitlines():
                hm = re.fullmatch(r"\s*Game:\s*(.*?)\s*\|\s*Template:\s*(.*?)\s*\|\s*Currency:\s*(.*?)\s*", old.content.splitlines()[0], re.I)
                if hm and normalize_name(hm.group(1)) == normalize_name(game["name"]) and normalize_name(hm.group(2)) == normalize_name(title):
                    await old.delete()
        await templates_channel.send(saved_content, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as exc:
        print(f"add_template failed: {exc!r}")
        return await interaction.response.send_message("❌ Could not save the template. Check the bot's storage-channel permissions.", ephemeral=True)
    await interaction.response.send_message(f"✅ Template **{title}** saved for **{game['name']}**. Currency set to `{currency}` (the game's first configured currency).", ephemeral=True)


@add_template.autocomplete("game_name")
async def add_template_game_autocomplete(interaction: discord.Interaction, current: str):
    return await game_autocomplete(interaction, current)


@client.tree.command(name="launch_giveaway", description="Launch a giveaway using a saved template")
@app_commands.describe(game_name="Choose a registered game", template_name="Choose a saved template", prize_pool="Total prize pool in the template currency", winners="Number of winners", duration="For example: 30m, 2h, or 1d6h", channel="Channel where the giveaway should be posted", start_mode="Start immediately or wait for a manager to press Start Giveaway", giveaway_name="Optional custom name to distinguish this giveaway", send_type="Post as a rich embed or a normal message")
@app_commands.choices(start_mode=[app_commands.Choice(name="Automatic (start immediately)", value="automatic"), app_commands.Choice(name="Manual (manager presses Start Giveaway)", value="manual")], send_type=[app_commands.Choice(name="Embed (rich card)", value="embed"), app_commands.Choice(name="Message (plain text)", value="message")])
async def launch_giveaway(interaction: discord.Interaction, game_name: str, template_name: str, prize_pool: app_commands.Range[int, 1, MAX_PRIZE], winners: app_commands.Range[int, 1, 100], duration: str, channel: discord.TextChannel, start_mode: str = "automatic", giveaway_name: str = "", send_type: str = "embed"):
    if not interaction.guild:
        return await interaction.response.send_message("Run this command inside a server.", ephemeral=True)
    if channel.guild.id != interaction.guild.id:
        return await interaction.response.send_message("Choose a channel from this server.", ephemeral=True)
    if not channel.permissions_for(interaction.guild.me).send_messages:
        return await interaction.response.send_message("❌ I cannot send messages in that channel.", ephemeral=True)
    if not await ensure_manager(interaction):
        return
    try:
        seconds = parse_duration(duration)
    except ValueError as exc:
        return await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
    game = await get_game(interaction.guild, game_name)
    if not game:
        return await interaction.response.send_message("❌ Game not found. Register it with `/create_game` first.", ephemeral=True)
    template = await get_template(interaction.guild, game_name, template_name)
    if not template:
        return await interaction.response.send_message("❌ Template not found. Check its header and make sure it was saved by the bot.", ephemeral=True)
    body_template, currency = template
    if normalize_name(currency) not in {normalize_name(x) for x in game.get("currencies", [])}:
        return await interaction.response.send_message("❌ The template currency is no longer registered for this game.", ephemeral=True)
    if winners > prize_pool:
        return await interaction.response.send_message("❌ Winner count cannot exceed the integer prize pool. Use a larger prize pool or fewer winners.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    started_ts = now_ts() if start_mode == "automatic" else 0
    end_ts = started_ts + seconds if started_ts else 0
    per_winner, remainder = divmod(prize_pool, winners)
    # Integer prizes: remainder goes to earliest listed winners; template shows base share.
    replacements = {
        "{giveaway_name}": giveaway_name.strip()[:100], "{game_name}": str(game["name"]),
        "{total_prize}": str(prize_pool), "{winners}": str(winners),
        "{prize_per_person}": str(per_winner), "{currency}": currency,
        "{end_time}": (f"<t:{end_ts}:R>" if end_ts else "waiting for a manager to start this giveaway"), "{joined}": "0",
    }
    body = body_template
    for key, value in replacements.items():
        body = body.replace(key, value)
    if "{end_time}" not in body_template:
        if end_ts:
            body += f"\n\n⏳ **Time remaining:** <t:{end_ts}:R> · Ends <t:{end_ts}:F>"
        else:
            body += "\n\n⏳ **Time remaining:** waiting for a manager to start this giveaway."
    if remainder:
        body += f"\n\nℹ️ Prize remainder: {remainder} {currency} will be distributed one extra unit to each of the first {remainder} winners."
    if len(body) > 1900:
        return await interaction.followup.send("❌ The rendered giveaway message is too long. Shorten the template.", ephemeral=True)
    display_name = giveaway_name.strip()[:100]
    state = "active" if start_mode == "automatic" else "scheduled"
    if send_type == "message":
        body += f"\n\n🤖 **Bot status:** 🟢 Online · Last check <t:{now_ts()}:R>"
        if len(body) > 2000:
            return await interaction.followup.send("❌ The rendered message exceeds Discord's 2,000-character limit after adding the status line. Shorten the template.", ephemeral=True)
        live = await channel.send(content=body, view=giveaway_view(state), allowed_mentions=discord.AllowedMentions.none())
    else:
        live_embed = giveaway_embed_from_body(body, display_name or str(game["name"]), state)
        live = await channel.send(embed=live_embed, view=giveaway_view(state), allowed_mentions=discord.AllowedMentions.none())
    logs = await require_storage(interaction.guild, CH_LOGS)
    records = await require_storage(interaction.guild, CH_CONFIGS)
    payload = {
        "kind": "giveaway", "version": 1, "message_id": live.id,
        "guild_id": interaction.guild.id, "channel_id": channel.id, "game": game["name"],
        "giveaway_name": display_name, "send_type": send_type, "template": template_name, "template_preview": body_template[:900], "currency": currency, "prize_pool": prize_pool,
        "winners": winners, "duration_seconds": seconds, "started_ts": started_ts,
        "end_ts": end_ts, "status": "active" if start_mode == "automatic" else "scheduled",
        "created_by": interaction.user.id, "created_ts": now_ts(), "bot_status_ts": now_ts(),
        "thread_id": None, "winner_ids": [], "winner_details": [], "remainder": remainder,
    }
    try:
        human_log = await logs.send(embed=giveaway_log_embed(payload), allowed_mentions=discord.AllowedMentions.none())
        payload["log_message_id"] = human_log.id
        thread = await logs.create_thread(name=f"{(display_name or game['name'])[:70]}-{live.id}"[:100], message=human_log, type=discord.ChannelType.public_thread, reason="Giveaway participant log in hidden storage channel")
        payload["thread_id"] = thread.id
        record_msg = await records.send(encode_record(payload))
    except Exception as exc:
        print(f"Giveaway logging failed: {exc!r}")
        try:
            await live.edit(view=None)
            await live.reply("❌ This giveaway could not be initialized safely, so joining has been disabled. Please contact a manager.")
        except discord.HTTPException:
            pass
        return await interaction.followup.send("❌ Could not initialize giveaway tracking. Check storage-channel permissions and try again.", ephemeral=True)
    if start_mode == "automatic":
        await interaction.followup.send(f"✅ Giveaway posted in {channel.mention} and started! It ends <t:{end_ts}:F>.", ephemeral=True)
    else:
        await interaction.followup.send(f"✅ Giveaway posted in {channel.mention}. It is waiting for a manager to press **Start Giveaway**; the countdown begins then.", ephemeral=True)


@launch_giveaway.autocomplete("game_name")
async def launch_game_autocomplete(interaction: discord.Interaction, current: str):
    return await game_autocomplete(interaction, current)


@launch_giveaway.autocomplete("template_name")
async def launch_template_autocomplete(interaction: discord.Interaction, current: str):
    return await template_autocomplete(interaction, current)


@client.tree.command(name="edit_giveaway", description="Edit prize, winner count, and duration for a running or scheduled giveaway")
@app_commands.describe(giveaway_id="Choose the giveaway to edit", prize_pool="Updated total prize pool", winners="Updated number of winners", duration="Updated duration, e.g. 30m, 2h, or 1d")
async def edit_giveaway(interaction: discord.Interaction, giveaway_id: str, prize_pool: app_commands.Range[int, 1, MAX_PRIZE], winners: app_commands.Range[int, 1, 100], duration: str):
    if not interaction.guild:
        return await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
    if not await ensure_manager(interaction):
        return
    if not giveaway_id.isdigit():
        return await interaction.response.send_message("Choose a giveaway from the suggestions.", ephemeral=True)
    found = await get_giveaway_record(interaction.guild, int(giveaway_id))
    if not found or found[1].get("status") not in ("active", "scheduled"):
        return await interaction.response.send_message("Only running or scheduled giveaways can be edited.", ephemeral=True)
    if winners > prize_pool:
        return await interaction.response.send_message("Winner count cannot exceed the integer prize pool.", ephemeral=True)
    try:
        seconds = parse_duration(duration)
    except ValueError as exc:
        return await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
    log_msg, data = found
    template = await get_template(interaction.guild, str(data.get("game", "")), str(data.get("template", "")))
    if not template:
        return await interaction.response.send_message("Saved template could not be found, so the giveaway was not changed.", ephemeral=True)
    body_template, currency = template
    status = str(data.get("status"))
    started_ts = now_ts() if status == "active" else 0
    end_ts = started_ts + seconds if started_ts else 0
    per_winner, remainder = divmod(prize_pool, winners)
    replacements = {"{game_name}": str(data.get("game", "")), "{total_prize}": str(prize_pool), "{winners}": str(winners), "{prize_per_person}": str(per_winner), "{currency}": currency, "{end_time}": (f"<t:{end_ts}:R>" if end_ts else "waiting for a manager to start this giveaway"), "{joined}": str(len(await get_entry_ids(await get_thread(interaction.guild, data)))) if await get_thread(interaction.guild, data) else "0"}
    body = body_template
    for key, value in replacements.items():
        body = body.replace(key, value)
    if data.get("giveaway_name"):
        body = f"🎁 **{data['giveaway_name']}**\n🎮 {data.get('game', 'Game')}\n\n" + body
    if "{end_time}" not in body_template:
        body += (f"\n\n⏳ **Time remaining:** <t:{end_ts}:R> · Ends <t:{end_ts}:F>" if end_ts else "\n\n⏳ **Time remaining:** waiting for a manager to start this giveaway.")
    if remainder:
        body += f"\n\nℹ️ Prize remainder: {remainder} {currency} will be distributed one extra unit to each of the first {remainder} winners."
    if len(body) > 1900:
        return await interaction.response.send_message("Rendered message is too long; shorten the template first.", ephemeral=True)
    channel = interaction.guild.get_channel(int(data.get("channel_id", 0)))
    if not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message("The giveaway channel no longer exists.", ephemeral=True)
    try:
        live = await channel.fetch_message(int(data["message_id"]))
        if data.get("send_type", "embed") == "message":
            await live.edit(content=body, embed=None, view=giveaway_view(status), allowed_mentions=discord.AllowedMentions.none())
        else:
            await live.edit(content=None, embed=giveaway_embed_from_body(body, str(data.get("giveaway_name") or data.get("game", "Giveaway")), status), view=giveaway_view(status), allowed_mentions=discord.AllowedMentions.none())
        data.update({"prize_pool": prize_pool, "winners": winners, "duration_seconds": seconds, "started_ts": started_ts, "end_ts": end_ts, "currency": currency, "remainder": remainder})
        await log_msg.edit(content=encode_record(data))
        await refresh_giveaway_log(interaction.guild, data)
    except discord.HTTPException as exc:
        print(f"edit_giveaway failed: {exc!r}")
        return await interaction.response.send_message("Could not update the giveaway message or record. Check bot permissions.", ephemeral=True)
    await interaction.response.send_message("✅ Giveaway settings updated.", ephemeral=True)


@edit_giveaway.autocomplete("giveaway_id")
async def edit_giveaway_ac(interaction: discord.Interaction, current: str):
    return await active_giveaway_choices(interaction, current)


@client.tree.command(name="end_giveaway", description="End an active giveaway immediately and draw winners")
@app_commands.describe(giveaway_id="Active giveaway message ID (choose from suggestions)")
async def end_giveaway(interaction: discord.Interaction, giveaway_id: str):
    if not interaction.guild:
        return await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
    if not await ensure_manager(interaction):
        return
    if not giveaway_id.isdigit():
        return await interaction.response.send_message("❌ Choose an active giveaway from the suggestions.", ephemeral=True)
    found = await get_giveaway_record(interaction.guild, int(giveaway_id))
    if not found:
        return await interaction.response.send_message("❌ I couldn't find that giveaway in the logs.", ephemeral=True)
    log_msg, data = found
    if data.get("status") != "active":
        return await interaction.response.send_message(f"❌ This giveaway is not active (status: `{data.get('status', 'unknown')}`).", ephemeral=True)
    data["end_ts"] = now_ts()
    try:
        await log_msg.edit(content=encode_record(data))
    except discord.HTTPException:
        return await interaction.response.send_message("❌ Could not update the giveaway record. Try again.", ephemeral=True)
    await interaction.response.send_message("⏳ Ending the giveaway and selecting winners…", ephemeral=True)
    await finish_giveaway(interaction.guild, log_msg, data)


@end_giveaway.autocomplete("giveaway_id")
async def end_giveaway_autocomplete(interaction: discord.Interaction, current: str):
    return await active_giveaway_choices(interaction, current)


@client.tree.command(name="edit_id", description="Save or update your in-game ID for a selected game")
@app_commands.describe(game_name="Choose a game registered by the giveaway managers", new_id="Your exact in-game player ID (3–50 characters)")
async def edit_id(interaction: discord.Interaction, game_name: str, new_id: str):
    if not interaction.guild:
        return await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
    game = await get_game(interaction.guild, game_name)
    if not game:
        return await interaction.response.send_message("❌ Game not found.", ephemeral=True)
    try:
        await save_player_id(interaction.guild, interaction.user.id, game["name"], new_id)
        await interaction.response.send_message(f"✅ Your in-game ID for `{game['name']}` has been updated.", ephemeral=True)
    except ValueError as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
    except Exception as exc:
        print(f"edit_id failed: {exc!r}")
        await interaction.response.send_message("❌ Could not update your ID. Check that setup is complete.", ephemeral=True)


@edit_id.autocomplete("game_name")
async def edit_id_game_autocomplete(interaction: discord.Interaction, current: str):
    return await game_autocomplete(interaction, current)


@client.tree.command(name="edit_template", description="Replace a saved template using a source message, game, and the same template title")
@app_commands.describe(message_link="Message ID or Discord message link containing the new template text", game_name="Registered game", template_title="Existing template title to replace")
async def edit_template(interaction: discord.Interaction, message_link: str, game_name: str, template_title: str):
    # The add-template flow validates variables and replaces a saved template with the same game/title.
    await add_template.callback(interaction, message_link, game_name, template_title)


@edit_template.autocomplete("game_name")
async def edit_template_game_ac(interaction: discord.Interaction, current: str):
    return await game_autocomplete(interaction, current)


@client.tree.command(name="report_duplicate_id", description="Report a duplicate game ID and upload proof for moderator review")
@app_commands.describe(game_name="Registered game", in_game_id="The exact ID you believe belongs to you", proof="Upload a screenshot or other proof", details="Optional explanation for the moderators")
async def report_duplicate_id(interaction: discord.Interaction, game_name: str, in_game_id: str, proof: discord.Attachment, details: str = ""):
    if not interaction.guild:
        return await interaction.response.send_message("Please use this command inside the server.", ephemeral=True)
    game = await get_game(interaction.guild, game_name)
    if not game:
        return await interaction.response.send_message("❌ I couldn't find that registered game. Choose a game from the suggestions.", ephemeral=True)
    try:
        clean_id = await validate_game_id(interaction.guild, game["name"], in_game_id)
    except ValueError as exc:
        return await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
    if proof.size > 10 * 1024 * 1024:
        return await interaction.response.send_message("❌ Proof must be 10 MB or smaller.", ephemeral=True)
    owner = await find_id_owner(interaction.guild, game["name"], clean_id, exclude_user_id=interaction.user.id)
    if owner is None:
        return await interaction.response.send_message("ℹ️ I couldn't find another account using that ID for this game. Check the game and ID, then try again if the issue continues.", ephemeral=True)
    channel = await get_db_channel(interaction.guild, CH_REPORTS)
    if not channel:
        return await interaction.response.send_message("❌ The report channel is missing. Ask an administrator to run `/setup`.", ephemeral=True)
    report = {"kind": "duplicate_id_report", "version": 1, "user_id": interaction.user.id, "claimed_owner_id": owner, "game": game["name"], "in_game_id": clean_id, "proof_url": proof.url, "proof_filename": proof.filename, "details": details[:800], "created_ts": now_ts(), "status": "open"}
    embed = discord.Embed(title="🛡️ Duplicate Game ID Report", description="A player says this game ID belongs to them. Review the proof before approving.", color=discord.Color.orange())
    embed.add_field(name="Game", value=game["name"], inline=True)
    embed.add_field(name="Reported ID", value=f"`{discord.utils.escape_markdown(clean_id)}`", inline=True)
    embed.add_field(name="Claimant", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
    embed.add_field(name="Currently registered to", value=f"<@{owner}> (`{owner}`)", inline=False)
    embed.add_field(name="Proof", value=f"[Open uploaded file]({proof.url}) · `{discord.utils.escape_markdown(proof.filename)}`", inline=False)
    if (proof.content_type or "").startswith("image/"):
        embed.set_image(url=proof.url)
    embed.add_field(name="Details", value=(discord.utils.escape_markdown(details[:900]) if details.strip() else "No additional details provided."), inline=False)
    embed.set_footer(text=encode_record(report))
    try:
        await channel.send(embed=embed, view=ReportReviewView(), allowed_mentions=discord.AllowedMentions.none())
        await interaction.response.send_message(f"✅ Your report for **{game['name']}** has been sent to {channel.mention}. A moderator will review your proof. Your ID won't be changed until the report is approved.", ephemeral=True)
    except Exception as exc:
        print(f"duplicate ID report failed: {exc!r}")
        await interaction.response.send_message("❌ I couldn't send your report. Please try again later or contact a moderator.", ephemeral=True)


@report_duplicate_id.autocomplete("game_name")
async def report_id_game_ac(interaction: discord.Interaction, current: str):
    return await game_autocomplete(interaction, current)


async def remove_user_from_active_game_giveaways(guild: discord.Guild, user_id: int, game_name: str) -> int:
    """Remove a disqualified user from active giveaway tracking threads for this game."""
    logs = await get_db_channel(guild, CH_CONFIGS)
    removed = 0
    if not logs:
        return removed
    async for log_msg, data in iter_bot_records(logs):
        if data.get("kind") != "giveaway" or data.get("status") not in ("active", "scheduled"):
            continue
        if normalize_name(str(data.get("game", ""))) != normalize_name(game_name):
            continue
        thread = await get_thread(guild, data)
        if not thread:
            continue
        async for entry in thread.history(limit=None):
            if not entry.content.startswith("ENTRY "):
                continue
            try:
                payload = json.loads(entry.content[6:])
                if int(payload.get("user_id", 0)) == user_id:
                    await entry.delete()
                    removed += 1
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        try:
            public_channel = guild.get_channel(int(data.get("channel_id", 0)))
            if isinstance(public_channel, discord.TextChannel):
                live = await public_channel.fetch_message(int(data.get("message_id", 0)))
                await update_joined_count(live, len(await get_entry_ids(thread)))
        except (discord.HTTPException, ValueError, TypeError):
            pass
    return removed


async def find_report_message_data(interaction: discord.Interaction) -> dict[str, Any] | None:
    if not interaction.message or not interaction.message.embeds:
        return None
    footer = interaction.message.embeds[0].footer.text or ""
    if not footer.startswith(RECORD_PREFIX):
        return None
    try:
        value = json.loads(footer[len(RECORD_PREFIX):])
        return value if isinstance(value, dict) and value.get("kind") == "duplicate_id_report" else None
    except json.JSONDecodeError:
        return None


class ReportReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Approve ID & Remove Duplicate", style=discord.ButtonStyle.success, custom_id="sse_giveaway:report:approve:v1")
    async def approve_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not await is_manager(interaction):
            return await interaction.response.send_message("❌ Only configured giveaway managers can review reports.", ephemeral=True)
        report = await find_report_message_data(interaction)
        if not report or report.get("status") != "open":
            return await interaction.response.send_message("This report is already handled or invalid.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        claimant = int(report.get("user_id", 0))
        game_name = str(report.get("game", ""))
        game_id = str(report.get("in_game_id", "")).strip()
        try:
            current_owner = await find_id_owner(interaction.guild, game_name, game_id, exclude_user_id=claimant)
            if current_owner is not None:
                await delete_player_id_record(interaction.guild, current_owner, game_name)
                removed_entries = await remove_user_from_active_game_giveaways(interaction.guild, current_owner, game_name)
                try:
                    old_user = interaction.guild.get_member(current_owner) or await client.fetch_user(current_owner)
                    await old_user.send(
                        f"⚠️ **You were removed from a {game_name} giveaway because your in-game ID was confirmed as a duplicate.**\n\n"
                        f"The ID `{discord.utils.escape_markdown(game_id)}` was approved for its verified owner after moderator review. Your saved ID has been removed and you were removed from {removed_entries} active giveaway entr(y/ies).\n\n"
                        "Please use `/edit_id` to enter your real in-game ID. If you believe this decision is incorrect, contact a server moderator."
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
            await save_player_id(interaction.guild, claimant, game_name, game_id)
            try:
                claimant_user = interaction.guild.get_member(claimant) or await client.fetch_user(claimant)
                await claimant_user.send(
                    f"✅ **Your {game_name} ID claim was approved.**\n\n"
                    f"A moderator verified that `{discord.utils.escape_markdown(game_id)}` belongs to you. "
                    "Your ID is now saved, and you can join eligible giveaways using this ID."
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            report["status"] = "approved"
            report["reviewed_by"] = interaction.user.id
            report["reviewed_ts"] = now_ts()
            embed = interaction.message.embeds[0].copy()
            embed.color = discord.Color.green()
            embed.title = "✅ Duplicate ID Report Approved"
            embed.add_field(name="Decision", value=f"Approved by {interaction.user.mention}. The previous registration was removed and the claimant's ID is now saved.", inline=False)
            embed.set_footer(text=encode_record(report))
            try:
                await interaction.message.edit(embed=embed, view=None, allowed_mentions=discord.AllowedMentions.none())
            except discord.NotFound:
                # The report message may have been deleted after the data action succeeded.
                print("Report approval completed, but the original report message no longer exists.")
            except discord.HTTPException as edit_exc:
                print(f"Report approved, but report message could not be updated: {edit_exc!r}")
            await interaction.followup.send("✅ Report approved. The previous owner's ID was removed, their active entries for this game were disqualified, and the claimant's ID was saved. DM notifications were attempted for both users.", ephemeral=True)
        except discord.NotFound as exc:
            print(f"Report approval encountered a missing Discord object: {exc!r}")
            await interaction.followup.send("⚠️ A Discord message/channel was missing during processing. Check the bot logs and saved ID records before retrying.", ephemeral=True)
        except Exception as exc:
            print(f"Report approval failed: {exc!r}")
            await interaction.followup.send("❌ Approval could not be completed. Check the bot logs and saved ID records before retrying.", ephemeral=True)

    @discord.ui.button(label="Reject Report", style=discord.ButtonStyle.danger, custom_id="sse_giveaway:report:reject:v1")
    async def reject_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not await is_manager(interaction):
            return await interaction.response.send_message("❌ Only configured giveaway managers can review reports.", ephemeral=True)
        report = await find_report_message_data(interaction)
        if not report or report.get("status") != "open":
            return await interaction.response.send_message("This report is already handled or invalid.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        report["status"] = "rejected"
        report["reviewed_by"] = interaction.user.id
        report["reviewed_ts"] = now_ts()
        embed = interaction.message.embeds[0].copy()
        embed.color = discord.Color.red()
        embed.title = "❌ Duplicate ID Report Rejected"
        embed.add_field(name="Decision", value=f"Rejected by {interaction.user.mention}. No saved IDs were changed.", inline=False)
        embed.set_footer(text=encode_record(report))
        try:
            await interaction.message.edit(embed=embed, view=None, allowed_mentions=discord.AllowedMentions.none())
        except discord.NotFound:
            print("Report rejected, but the original report message no longer exists.")
        except discord.HTTPException as edit_exc:
            print(f"Report rejected, but report message could not be updated: {edit_exc!r}")
        try:
            claimant = interaction.guild.get_member(int(report.get("user_id", 0))) or await client.fetch_user(int(report.get("user_id", 0)))
            await claimant.send(f"Your duplicate-ID report for **{report.get('game', 'the game')}** was reviewed and rejected. Your saved ID was not changed. If you have additional proof, please contact a moderator.")
        except (discord.Forbidden, discord.HTTPException):
            pass
        await interaction.followup.send("Report rejected. No IDs were changed. A DM notification was attempted for the claimant.", ephemeral=True)


@client.tree.command(name="delete_player_id", description="Moderator: remove a user's saved ID for a selected game")
@app_commands.describe(game_name="Registered game", user="Member whose saved ID should be removed")
async def delete_player_id(interaction: discord.Interaction, game_name: str, user: discord.Member):
    if not interaction.guild:
        return await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
    if not await ensure_manager(interaction):
        return
    game = await get_game(interaction.guild, game_name)
    if not game:
        return await interaction.response.send_message("Game not found.", ephemeral=True)
    deleted = await delete_player_id_record(interaction.guild, user.id, game["name"])
    if deleted:
        await interaction.response.send_message(f"🗑️ Removed {user.mention}'s saved ID for **{game['name']}**. This does not retroactively change any already-completed giveaway.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.response.send_message("No saved ID was found for that user and game.", ephemeral=True)


@delete_player_id.autocomplete("game_name")
async def delete_player_id_game_ac(interaction: discord.Interaction, current: str):
    return await game_autocomplete(interaction, current)


@client.tree.command(name="edit_game", description="Edit a game's ID requirement, ID length, and currencies")
@app_commands.describe(
    game_name="Registered game to edit",
    requires_id="Whether entrants must submit a player ID",
    currencies="Comma-separated currencies",
    min_id_length="Minimum ID length (leave empty to keep current setting)",
    max_id_length="Maximum ID length (leave empty to keep current setting)",
)
async def edit_game(
    interaction: discord.Interaction,
    game_name: str,
    requires_id: bool,
    currencies: str,
    min_id_length: app_commands.Range[int, 1, 100] | None = None,
    max_id_length: app_commands.Range[int, 1, 100] | None = None,
):
    if not interaction.guild:
        return await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
    if not await ensure_manager(interaction):
        return
    game = await get_game(interaction.guild, game_name)
    if not game:
        return await interaction.response.send_message("Game not found. Use `/create_game` first.", ephemeral=True)
    currency_list = list(dict.fromkeys(x.strip() for x in currencies.split(",") if x.strip()))
    if not currency_list or any(len(x) > 40 for x in currency_list):
        return await interaction.response.send_message("Provide one or more currencies separated by commas (max 40 characters each).", ephemeral=True)

    # Preserve the current values when the optional length fields are omitted.
    current_min = int(game.get("min_id_length", 3))
    current_max = int(game.get("max_id_length", 50))
    new_min = int(min_id_length) if min_id_length is not None else current_min
    new_max = int(max_id_length) if max_id_length is not None else current_max
    if requires_id and new_min > new_max:
        return await interaction.response.send_message("❌ Minimum ID length cannot be greater than maximum ID length.", ephemeral=True)

    channel = await require_storage(interaction.guild, CH_GAMES)
    async for msg, data in iter_bot_records(channel):
        if data.get("kind") == "game" and normalize_name(str(data.get("name", ""))) == normalize_name(game["name"]):
            updated = {
                "kind": "game", "version": 1, "name": game["name"],
                "requires_id": requires_id, "currencies": currency_list,
                "min_id_length": new_min, "max_id_length": new_max,
            }
            await msg.edit(content=encode_record(updated))
            length_note = f" ID length: `{new_min}–{new_max}` characters." if requires_id else " ID length validation is inactive because IDs are not required."
            return await interaction.response.send_message(
                f"✅ Updated **{game['name']}**. Requires ID: `{requires_id}`. "
                f"Currencies: `{', '.join(currency_list)}`.{length_note}",
                ephemeral=True,
            )
    await interaction.response.send_message("This game uses a legacy record. Run `/create_game` with the same name to migrate it, then use `/edit_game`.", ephemeral=True)


@edit_game.autocomplete("game_name")
async def edit_game_ac(interaction: discord.Interaction, current: str):
    return await game_autocomplete(interaction, current)


@client.tree.command(name="delete_template", description="Delete a saved template for a registered game")
@app_commands.describe(game_name="Choose the registered game", template_name="Choose the template to delete")
async def delete_template(interaction: discord.Interaction, game_name: str, template_name: str):
    if not interaction.guild:
        return await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
    if not await ensure_manager(interaction):
        return
    channel = await get_db_channel(interaction.guild, CH_TEMPLATES)
    if not channel:
        return await interaction.response.send_message("Run `/setup` first.", ephemeral=True)
    target = None
    async for msg in channel.history(limit=None):
        if msg.author != client.user or not msg.content.splitlines():
            continue
        m = re.fullmatch(r"\s*Game:\s*(.*?)\s*\|\s*Template:\s*(.*?)\s*\|\s*Currency:\s*(.*?)\s*", msg.content.splitlines()[0], re.I)
        if m and normalize_name(m.group(1)) == normalize_name(game_name) and normalize_name(m.group(2)) == normalize_name(template_name):
            target = msg
            break
    if not target:
        return await interaction.response.send_message("Template not found for that game.", ephemeral=True)
    await target.delete()
    await interaction.response.send_message(f"🗑️ Deleted template **{template_name}** for **{game_name}**.", ephemeral=True)


@delete_template.autocomplete("game_name")
async def delete_template_game_ac(interaction: discord.Interaction, current: str):
    return await game_autocomplete(interaction, current)


@delete_template.autocomplete("template_name")
async def delete_template_name_ac(interaction: discord.Interaction, current: str):
    return await template_autocomplete(interaction, current)


@client.tree.command(name="delete_game", description="Delete a registered game and its saved templates and player IDs")
@app_commands.describe(game_name="Choose the registered game to delete")
async def delete_game(interaction: discord.Interaction, game_name: str):
    if not interaction.guild:
        return await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
    if not await ensure_manager(interaction):
        return
    channel = await get_db_channel(interaction.guild, CH_GAMES)
    if not channel:
        return await interaction.response.send_message("Run `/setup` first.", ephemeral=True)
    deleted = False
    async for msg, data in iter_bot_records(channel):
        if data.get("kind") == "game" and normalize_name(str(data.get("name", ""))) == normalize_name(game_name):
            await msg.delete(); deleted = True
    templates = await get_db_channel(interaction.guild, CH_TEMPLATES)
    if templates:
        async for msg in templates.history(limit=None):
            if msg.author == client.user and msg.content.splitlines():
                m = re.fullmatch(r"\s*Game:\s*(.*?)\s*\|\s*Template:\s*(.*?)\s*\|\s*Currency:\s*(.*?)\s*", msg.content.splitlines()[0], re.I)
                if m and normalize_name(m.group(1)) == normalize_name(game_name):
                    await msg.delete()
    ids = await get_db_channel(interaction.guild, CH_IDS)
    if ids:
        async for msg, data in iter_bot_records(ids):
            if data.get("kind") == "player_id" and normalize_name(str(data.get("game", ""))) == normalize_name(game_name):
                await msg.delete()
    if deleted:
        await interaction.response.send_message(f"🗑️ Deleted **{game_name}** and its saved templates/player IDs. Existing giveaway history is preserved.", ephemeral=True)
    else:
        await interaction.response.send_message("Game not found.", ephemeral=True)


@delete_game.autocomplete("game_name")
async def delete_game_ac(interaction: discord.Interaction, current: str):
    return await game_autocomplete(interaction, current)


@client.tree.command(name="delete_giveaway", description="Cancel an active giveaway and disable joining")
@app_commands.describe(giveaway_id="Choose the active giveaway to cancel")
async def delete_giveaway(interaction: discord.Interaction, giveaway_id: str):
    if not interaction.guild:
        return await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
    if not await ensure_manager(interaction):
        return
    if not giveaway_id.isdigit():
        return await interaction.response.send_message("Choose an active giveaway from the suggestions.", ephemeral=True)
    found = await get_giveaway_record(interaction.guild, int(giveaway_id))
    if not found or found[1].get("status") not in ("active", "scheduled"):
        return await interaction.response.send_message("Active or scheduled giveaway not found.", ephemeral=True)
    log_msg, data = found
    channel = interaction.guild.get_channel(int(data.get("channel_id", 0)))
    if isinstance(channel, discord.TextChannel):
        try:
            live = await channel.fetch_message(int(data["message_id"]))
            await live.edit(view=None)
            await live.reply("🛑 This giveaway was cancelled by a manager.")
        except discord.HTTPException:
            pass
    data["status"] = "cancelled"
    data["finished_ts"] = now_ts()
    await log_msg.edit(content=encode_record(data))
    await interaction.response.send_message("🗑️ Giveaway cancelled. Its record remains in history.", ephemeral=True)


@delete_giveaway.autocomplete("giveaway_id")
async def delete_giveaway_ac(interaction: discord.Interaction, current: str):
    return await active_giveaway_choices(interaction, current)


async def active_giveaway_choices(interaction: discord.Interaction, current: str):
    if not interaction.guild:
        return []
    logs = await get_db_channel(interaction.guild, CH_CONFIGS)
    if not logs:
        return []
    choices = []
    async for _, data in iter_bot_records(logs):
        if data.get("kind") == "giveaway" and data.get("status") in ("active", "scheduled"):
            mid = str(data.get("message_id", "")); game = str(data.get("giveaway_name") or data.get("game", "Giveaway"))
            if current.casefold() in mid.casefold() or current.casefold() in game.casefold():
                choices.append(app_commands.Choice(name=f"{game} — ID {mid}"[:100], value=mid))
            if len(choices) >= 25: break
    return choices


class GiveawayHistorySelect(discord.ui.Select):
    def __init__(self, guild_id: int, records: list[dict[str, Any]]):
        self.guild_id = guild_id
        options = []
        for item in records[:25]:
            mid = int(item.get("message_id", 0))
            name = str(item.get("giveaway_name") or item.get("game") or "Giveaway")
            created = int(item.get("created_ts", 0))
            date_label = datetime.fromtimestamp(created, timezone.utc).strftime("%d %b %Y") if created else "Unknown date"
            status = str(item.get("status", "unknown")).replace("_", " ").title()
            label = f"{name[:55]} · {date_label}"[:100]
            description = f"{status} · {item.get('game', 'Game')} · ID {mid}"[:100]
            options.append(discord.SelectOption(label=label, value=str(mid), description=description))
        super().__init__(placeholder="Choose a giveaway to open…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        message_id = int(self.values[0])
        found = await get_giveaway_record(interaction.guild, message_id) if interaction.guild else None
        if not found:
            return await interaction.response.send_message("That giveaway record could not be found anymore.", ephemeral=True)
        data = found[1]
        url = f"https://discord.com/channels/{int(data.get('guild_id', self.guild_id))}/{int(data.get('channel_id', 0))}/{message_id}"
        name = data.get("giveaway_name") or data.get("game", "Giveaway")
        status = str(data.get("status", "unknown")).replace("_", " ").title()
        embed = discord.Embed(title=f"🎁 {name}", description=f"**Date:** <t:{int(data.get('created_ts', 0))}:F>\n**Status:** {status}\n**Game:** {data.get('game', 'Unknown')}\n**Template:** {data.get('template', 'Unknown')}\n**Giveaway ID:** `{message_id}`", color=discord.Color.blurple())
        winners = data.get("winner_details") or []
        if winners:
            embed.add_field(name="🏆 Winners", value="\n".join(f"<@{int(w['discord'])}> — ID `{w.get('game_id', 'N/A')}`" for w in winners if isinstance(w, dict))[:1024], inline=False)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open Giveaway Message", style=discord.ButtonStyle.link, url=url, emoji="🔗"))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class GiveawayHistoryView(discord.ui.View):
    def __init__(self, guild_id: int, records: list[dict[str, Any]]):
        super().__init__(timeout=180)
        self.add_item(GiveawayHistorySelect(guild_id, records))


@client.tree.command(name="show_giveaways", description="Browse giveaways by name and date, then open one")
@app_commands.describe(status="Choose scheduled, active, completed, cancelled, or all giveaways")
@app_commands.choices(status=[app_commands.Choice(name="Scheduled", value="scheduled"), app_commands.Choice(name="Active", value="active"), app_commands.Choice(name="Completed", value="completed"), app_commands.Choice(name="Cancelled", value="cancelled"), app_commands.Choice(name="All", value="all")])
async def show_giveaways(interaction: discord.Interaction, status: str = "all"):
    if not interaction.guild:
        return await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
    if not await ensure_manager(interaction):
        return
    logs = await get_db_channel(interaction.guild, CH_CONFIGS)
    if not logs:
        return await interaction.response.send_message("Run `/setup` first.", ephemeral=True)
    records = []
    async for _, data in iter_bot_records(logs):
        if data.get("kind") != "giveaway":
            continue
        if status != "all" and data.get("status") != status:
            continue
        records.append(data)
    records.sort(key=lambda x: int(x.get("created_ts", 0)), reverse=True)
    if not records:
        return await interaction.response.send_message("No giveaways found for that status.", ephemeral=True)
    # The list itself stays clean: just names and dates. Selecting an item reveals details and a direct link.
    description = "\n".join(f"• **{discord.utils.escape_markdown(str(x.get('giveaway_name') or x.get('game', 'Giveaway')))}** — <t:{int(x.get('created_ts', 0))}:d>" for x in records[:25])
    embed = discord.Embed(title="🎁 Giveaway History", description=description, color=discord.Color.blurple())
    embed.set_footer(text=f"Choose a giveaway below to open it · {min(len(records), 25)} of {len(records)} shown")
    await interaction.response.send_message(embed=embed, view=GiveawayHistoryView(interaction.guild.id, records), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def giveaway_results_embed(data: dict[str, Any], announcement: str) -> discord.Embed:
    winners = data.get("winner_details") or []
    title = "🏆 Giveaway Results"
    embed = discord.Embed(
        title=title,
        description=f"🎉 **{discord.utils.escape_markdown(str(data.get('giveaway_name') or data.get('game', 'Giveaway')))}** has ended!\n\n" +
                    ("Congratulations to our winner(s)!" if winners else "No eligible participants were available for the draw."),
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    if winners:
        lines = []
        for index, winner in enumerate(winners, start=1):
            if not isinstance(winner, dict):
                continue
            user = f"<@{int(winner['discord'])}>"
            game_id = winner.get("game_id")
            prize = winner.get("prize")
            line = f"**{index}.** {user}"
            if game_id:
                line += f"\n   🎮 Player ID: `{discord.utils.escape_markdown(str(game_id))}`"
            if prize is not None:
                line += f"\n   🎁 **Prize:** {prize} {discord.utils.escape_markdown(str(data.get('currency', '')))}"
            lines.append(line)
        embed.add_field(name="🥇 Winner(s)", value="\n\n".join(lines)[:1024] or "No winners", inline=False)
    else:
        embed.add_field(name="Result", value="Nobody eligible joined this giveaway. Better luck next time!", inline=False)
    embed.add_field(name="🎟️ Participants eligible", value=str(data.get("eligible_count", 0)), inline=True)
    embed.add_field(name="🏆 Winners selected", value=str(len(winners)), inline=True)
    if data.get("duplicate_count"):
        embed.add_field(name="⚠️ Duplicate IDs", value=f"{data['duplicate_count']} entries were excluded because their in-game IDs were duplicated.", inline=False)
    if len(winners) < int(data.get("winners", 1)) and winners:
        embed.add_field(name="Note", value=f"Only {len(winners)} eligible winner(s) could be selected from the available participants.", inline=False)
    embed.set_footer(text=f"SSE Giveaway • {data.get('template', 'Template')} template")
    return embed


async def finish_giveaway(guild: discord.Guild, log_msg: discord.Message, data: dict[str, Any]) -> None:
    giveaway_id = int(data.get("message_id", 0))
    lock = client.finish_locks.setdefault(giveaway_id, asyncio.Lock())
    async with lock:
        # Refresh record before proceeding so parallel recovery passes cannot draw twice.
        try:
            log_msg = await log_msg.channel.fetch_message(log_msg.id)
            current = record_payload(log_msg)
            if current:
                data = current
        except discord.HTTPException:
            return
        if data.get("status") == "completed" or data.get("status") == "cancelled":
            return
        thread = await get_thread(guild, data)
        if not thread:
            print(f"Cannot finish giveaway {giveaway_id}: tracking thread missing")
            return
        # Preserve an already-selected result during recovery. Resetting an
        # announcing record to drawing would cause a second, different draw.
        has_saved_announcement = data.get("status") == "announcing" and bool(data.get("announcement"))
        if not has_saved_announcement:
            data["status"] = "drawing"
            try:
                await log_msg.edit(content=encode_record(data))
                await refresh_giveaway_log(guild, data)
            except discord.HTTPException:
                return
        channel = guild.get_channel(int(data.get("channel_id", 0)))
        live = None
        if isinstance(channel, discord.TextChannel):
            try:
                live = await channel.fetch_message(giveaway_id)
                if live.embeds:
                    ended_embed = live.embeds[0].copy()
                    ended_embed.color = discord.Color.dark_grey()
                    ended_embed.set_footer(text="🏁 Giveaway ended · See the results message below")
                    await live.edit(embed=ended_embed, view=None)
                else:
                    await live.edit(view=None)
            except discord.HTTPException:
                live = None
        # If a previous attempt saved the draw but failed to announce it, reuse
        # the persisted announcement rather than selecting a new set of winners.
        if data.get("status") == "announcing" and data.get("announcement"):
            announcement = str(data["announcement"])
            winners = data.get("winner_ids", [])
        else:
            entrant_ids = await get_entry_ids(thread)
            game = await get_game(guild, str(data.get("game", "")))
            eligible: list[dict[str, Any]] = []
            if game and game.get("requires_id"):
                candidates: list[dict[str, Any]] = []
                id_counts: dict[str, int] = {}
                for user_id in entrant_ids:
                    player_id = await get_player_id(guild, user_id, str(data.get("game", "")))
                    normalized = normalize_game_id(player_id or "")
                    if not normalized:
                        continue
                    id_counts[normalized] = id_counts.get(normalized, 0) + 1
                    candidates.append({"discord": user_id, "game_id": player_id, "normalized_id": normalized})
                # If multiple Discord users submit the same game ID, disqualify ALL of them.
                eligible = [
                    {"discord": item["discord"], "game_id": item["game_id"]}
                    for item in candidates if id_counts.get(item["normalized_id"], 0) == 1
                ]
                duplicate_count = sum(1 for item in candidates if id_counts.get(item["normalized_id"], 0) > 1)
            else:
                eligible = [{"discord": user_id, "game_id": None} for user_id in entrant_ids]
                duplicate_count = 0
            if not eligible:
                announcement = "Nobody eligible joined this giveaway. Better luck next time!"
                winners = []
            else:
                actual_count = min(int(data.get("winners", 1)), len(eligible))
                selected = random.SystemRandom().sample(eligible, actual_count)
                winners = []
                winner_mentions = []
                remainder = int(data.get("remainder", 0))
                for index, winner in enumerate(selected):
                    extra = 1 if index < remainder else 0
                    amount = int(data.get("prize_pool", 0)) // max(1, int(data.get("winners", 1))) + extra
                    prize = f" — **{amount} {data.get('currency', '')}**" if data.get("prize_pool") is not None else ""
                    id_text = f" (In-game ID: `{discord.utils.escape_markdown(str(winner['game_id']))}`)" if winner["game_id"] else ""
                    winner_mentions.append(f"<@{winner['discord']}>{id_text}{prize}")
                    winners.append({"discord": int(winner["discord"]), "game_id": winner["game_id"], "prize": amount})
                announcement = "🎉 **GIVEAWAY ENDED!** 🎉\nCongratulations to the winner(s):\n" + "\n".join(winner_mentions)
                if duplicate_count:
                    announcement += f"\n\n⚠️ {duplicate_count} participant entr(y/ies) were disqualified because their in-game ID was duplicated. All users sharing a duplicate ID were excluded."
                if len(eligible) < int(data.get("winners", 1)):
                    announcement += f"\n\nOnly {len(eligible)} eligible participant(s) were available, so {len(eligible)} winner(s) were selected."
            # Persist the chosen result and announcement before sending anything public.
            data["status"] = "announcing"
            data["winner_ids"] = [int(w["discord"]) if isinstance(w, dict) else int(w) for w in winners]
            data["winner_details"] = winners
            data["announcement"] = announcement
            data["finished_ts"] = now_ts()
            data["eligible_count"] = len(eligible) if 'eligible' in locals() else 0
            data["duplicate_count"] = duplicate_count if 'duplicate_count' in locals() else 0
            try:
                await log_msg.edit(content=encode_record(data))
            except discord.HTTPException:
                return
        if channel and live and not data.get("announcement_message_id"):
            try:
                result_embed = giveaway_results_embed(data, announcement)
                result_message = await channel.send(
                    embed=result_embed,
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                )
                data["announcement_message_id"] = result_message.id
                await log_msg.edit(content=encode_record(data))
            except discord.HTTPException as exc:
                print(f"Winner announcement failed for {giveaway_id}: {exc!r}")
                return
        data["status"] = "completed"
        data["announcement_sent_ts"] = now_ts()
        try:
            await log_msg.edit(content=encode_record(data))
            await refresh_giveaway_log(guild, data)
        except discord.HTTPException:
            # If this save fails, recovery may retry the same saved announcement.
            return
        try:
            await thread.send("Giveaway ended. Winner selection has been saved.")
            await thread.edit(locked=True, archived=True)
        except discord.HTTPException as exc:
            print(f"Could not archive thread for {giveaway_id}: {exc!r}")


async def main():
    async with client:
        await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
