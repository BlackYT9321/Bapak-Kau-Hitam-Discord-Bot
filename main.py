import os
import re
import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone
import aiohttp
from openai import AsyncOpenAI

URL_RE = re.compile(r'^https?://\S+$', re.IGNORECASE)

ALLOWED_IDS = {1125309705552670750, 1152093058066829325, 1341785621525692601, 1371790959406219316, 1362370052875096196}
API_BASE = "http://localhost:80/api"

current_mode = "manual"

openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

def split_message_and_image(text: str):
    words = text.split()
    if words and URL_RE.match(words[-1]):
        return ' '.join(words[:-1]), words[-1]
    return text, None

async def send_with_optional_image(channel, message: str, image_url: str = None):
    if image_url:
        embed = discord.Embed(description=message if message.strip() else None)
        embed.set_image(url=image_url)
        await channel.send(embed=embed)
    else:
        await channel.send(message)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
start_time = datetime.now(timezone.utc)

def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_IDS

async def post_log(event: str, type: str = "info"):
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{API_BASE}/bot/logs",
                json={"event": event, "type": type},
                timeout=aiohttp.ClientTimeout(total=5)
            )
    except Exception:
        pass

async def post_stats():
    try:
        latency = round(bot.latency * 1000)
        servers = [
            {"id": str(g.id), "name": g.name, "memberCount": g.member_count or 0}
            for g in bot.guilds
        ]
        payload = {
            "botName": bot.user.name,
            "botId": str(bot.user.id),
            "latency": latency,
            "serverCount": len(bot.guilds),
            "servers": servers,
            "startedAt": start_time.isoformat(),
        }
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{API_BASE}/bot/stats",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5)
            )
    except Exception:
        pass

@tasks.loop(seconds=15)
async def heartbeat():
    await post_stats()

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Bot berjaya online sebagai {bot.user.name} ({bot.user.id})")
    print("Slash commands synced! Sedia untuk menerima arahan!")
    await post_log("Bot connected to Gateway", "success")
    await post_log("Slash commands synced", "info")
    await post_stats()
    heartbeat.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await bot.process_commands(message)
    if current_mode == "ai" and bot.user in message.mentions:
        content = message.content \
            .replace(f"<@{bot.user.id}>", "") \
            .replace(f"<@!{bot.user.id}>", "") \
            .strip()
        if not content:
            content = "Hello!"
        async with message.channel.typing():
            try:
                response = await openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "You are a helpful and friendly Discord bot. Keep replies concise."},
                        {"role": "user", "content": content},
                    ],
                    max_tokens=500,
                )
                reply = response.choices[0].message.content
                await message.reply(reply)
                await post_log(f"AI replied in #{message.channel.name}", "command")
            except Exception as e:
                print(f"[AI ERROR] {type(e).__name__}: {e}")
                await message.reply("Maaf, ada masalah dengan AI. Cuba lagi.")
                await post_log(f"AI error: {e}", "error")

# !mode <ai|manual>
@bot.command(name="mode")
async def mode_prefix(ctx: commands.Context, new_mode: str):
    global current_mode
    if not is_allowed(ctx.author.id):
        await ctx.message.delete()
        return
    new_mode = new_mode.lower()
    if new_mode not in ("ai", "manual"):
        await ctx.send("❌ Mode mesti `ai` atau `manual`.", delete_after=5)
        return
    current_mode = new_mode
    emoji = "🤖" if new_mode == "ai" else "🕹️"
    await ctx.send(f"{emoji} Mode ditukar ke **{new_mode.upper()}**.", delete_after=10)
    await ctx.message.delete()
    await post_log(f"Mode switched to {new_mode}", "info")

# /mode
@bot.tree.command(name="mode", description="Toggle antara AI mode dan Manual mode")
@app_commands.describe(new_mode="Pilih mode: ai atau manual")
@app_commands.choices(new_mode=[
    app_commands.Choice(name="🤖 AI Mode", value="ai"),
    app_commands.Choice(name="🕹️ Manual Mode", value="manual"),
])
async def mode_slash(interaction: discord.Interaction, new_mode: app_commands.Choice[str]):
    global current_mode
    if not is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Kau tiada kebenaran untuk ini!", ephemeral=True)
        return
    current_mode = new_mode.value
    emoji = "🤖" if new_mode.value == "ai" else "🕹️"
    await interaction.response.send_message(f"{emoji} Mode ditukar ke **{new_mode.name}**.", ephemeral=True)
    await post_log(f"Mode switched to {new_mode.value}", "info")

# !speak <channel_id> <message> [image_url]
@bot.command(name="speak")
async def speak_prefix(ctx: commands.Context, channel_id: str, *, message: str):
    if not is_allowed(ctx.author.id):
        await ctx.message.delete()
        return
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        await ctx.send("❌ Channel tidak dijumpai. Pastikan ID betul.", delete_after=5)
        return
    text, image_url = split_message_and_image(message)
    await send_with_optional_image(channel, text, image_url)
    await ctx.message.delete()
    await post_log(f"!speak used in #{channel.name} → {channel.guild.name}", "command")

# /speak
@bot.tree.command(name="speak", description="Hantar mesej ke channel tertentu")
@app_commands.describe(
    channel="Pilih channel untuk hantar mesej",
    message="Mesej yang nak dihantar",
    image_url="(Optional) Link gambar untuk dihantar"
)
async def speak(interaction: discord.Interaction, channel: discord.TextChannel, message: str, image_url: str = None):
    if not is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Kau tiada kebenaran untuk ini!", ephemeral=True)
        return
    await send_with_optional_image(channel, message, image_url)
    await interaction.response.send_message(f"✅ Mesej berjaya dihantar ke {channel.mention}", ephemeral=True)
    await post_log(f"/speak used in #{channel.name} → {channel.guild.name}", "command")

# /leaveserver
@bot.tree.command(name="leaveserver", description="Buat bot keluar dari server tertentu")
@app_commands.describe(server_id="ID server yang nak ditinggalkan")
async def leaveserver(interaction: discord.Interaction, server_id: str):
    if not is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Kau tiada kebenaran untuk ini!", ephemeral=True)
        return
    guild = bot.get_guild(int(server_id))
    if guild:
        name = guild.name
        await guild.leave()
        await interaction.response.send_message(f"✅ Berjaya keluar dari server: **{name}**", ephemeral=True)
        await post_log(f"/leaveserver used → left {name}", "command")
    else:
        await interaction.response.send_message("❌ Server tidak dijumpai. Pastikan ID betul.", ephemeral=True)

# /servers
@bot.tree.command(name="servers", description="Tengok senarai server yang bot join")
async def servers(interaction: discord.Interaction):
    if not is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Kau tiada kebenaran untuk ini!", ephemeral=True)
        return
    if not bot.guilds:
        await interaction.response.send_message("Bot tiada dalam mana-mana server.", ephemeral=True)
        return
    lines = [f"**{g.name}** — `{g.id}` ({g.member_count} members)" for g in bot.guilds]
    await interaction.response.send_message("📋 **Server list:**\n" + "\n".join(lines), ephemeral=True)
    await post_log("/servers used", "command")

TOKEN = os.environ['DISCORD_TOKEN']
bot.run(TOKEN)
