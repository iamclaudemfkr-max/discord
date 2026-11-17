"""
Музыкальный Discord-бот: играет треки с YouTube (и других сайтов, которые понимает yt-dlp).

Установка:
    pip install -U "discord.py[voice]" yt-dlp
    + установить ffmpeg (Windows: https://ffmpeg.org, Linux: sudo apt install ffmpeg)

Запуск:
    Windows (PowerShell):  $env:DISCORD_TOKEN="ваш_токен"; python music_bot.py
    Linux/macOS:           DISCORD_TOKEN=ваш_токен python music_bot.py

Команды: /play, /skip, /pause, /resume, /queue, /stop
"""

import asyncio
import os
from collections import deque

import discord
import yt_dlp
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",  # можно писать просто название песни
}
FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Очередь треков для каждого сервера: guild_id -> deque[{"title", "page"}]
queues: dict[int, deque] = {}


def get_queue(guild_id: int) -> deque:
    return queues.setdefault(guild_id, deque())


async def extract(query: str) -> dict:
    """Находит трек и возвращает название, ссылку на страницу и прямую ссылку на аудио."""
    def _run():
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:  # результат поиска
                info = info["entries"][0]
            return {
                "title": info.get("title", "Без названия"),
                "page": info.get("webpage_url", query),
                "stream": info["url"],
            }

    # yt-dlp блокирующий, поэтому выносим в отдельный поток
    return await asyncio.get_running_loop().run_in_executor(None, _run)


async def play_next(vc: discord.VoiceClient, guild_id: int, channel):
    """Берёт следующий трек из очереди и запускает его."""
    q = get_queue(guild_id)
    if not q or not vc.is_connected():
        return

    track = q.popleft()
    try:
        # Прямые ссылки на аудио быстро протухают, поэтому получаем их прямо перед проигрыванием
        info = await extract(track["page"])
        source = discord.FFmpegOpusAudio(info["stream"], **FFMPEG_OPTS)
    except Exception as e:
        await channel.send(f"⚠️ Не удалось воспроизвести **{track['title']}**: {e}")
        await play_next(vc, guild_id, channel)
        return

    def after(error):
        if error:
            print(f"Ошибка воспроизведения: {error}")
        asyncio.run_coroutine_threadsafe(play_next(vc, guild_id, channel), client.loop)

    vc.play(source, after=after)
    await channel.send(f"▶️ Сейчас играет: **{info['title']}**")


@tree.command(name="play", description="Включить трек по ссылке или названию")
@app_commands.describe(query="Ссылка на YouTube или название песни")
async def play(interaction: discord.Interaction, query: str):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("Сначала зайди в голосовой канал.", ephemeral=True)
        return

    await interaction.response.defer()  # поиск может занять несколько секунд

    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    if vc is None:
        vc = await channel.connect()
    elif vc.channel != channel:
        await vc.move_to(channel)

    try:
        info = await extract(query)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Ничего не нашёл: {e}")
        return

    q = get_queue(interaction.guild_id)
    q.append({"title": info["title"], "page": info["page"]})

    if vc.is_playing() or vc.is_paused():
        await interaction.followup.send(f"➕ Добавлено в очередь (#{len(q)}): **{info['title']}**")
    else:
        await interaction.followup.send(f"🔎 Нашёл: **{info['title']}**")
        await play_next(vc, interaction.guild_id, interaction.channel)


@tree.command(name="skip", description="Пропустить текущий трек")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()  # вызовет after() -> запустится следующий трек
        await interaction.response.send_message("⏭️ Пропущено.")
    else:
        await interaction.response.send_message("Сейчас ничего не играет.", ephemeral=True)


@tree.command(name="pause", description="Поставить на паузу")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Пауза.")
    else:
        await interaction.response.send_message("Сейчас ничего не играет.", ephemeral=True)


@tree.command(name="resume", description="Продолжить воспроизведение")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Продолжаю.")
    else:
        await interaction.response.send_message("Нечего продолжать.", ephemeral=True)


@tree.command(name="queue", description="Показать очередь")
async def show_queue(interaction: discord.Interaction):
    q = get_queue(interaction.guild_id)
    if not q:
        await interaction.response.send_message("Очередь пуста.")
        return
    lines = [f"{i}. {t['title']}" for i, t in enumerate(list(q)[:15], start=1)]
    if len(q) > 15:
        lines.append(f"…и ещё {len(q) - 15}")
    await interaction.response.send_message("🎶 **Очередь:**\n" + "\n".join(lines))


@tree.command(name="stop", description="Остановить музыку и выйти из канала")
async def stop(interaction: discord.Interaction):
    get_queue(interaction.guild_id).clear()
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
    await interaction.response.send_message("⏹️ Остановлено, пока!")


@client.event
async def on_ready():
    await tree.sync()
    print(f"Бот запущен как {client.user}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Укажи токен в переменной окружения DISCORD_TOKEN")
    client.run(TOKEN)