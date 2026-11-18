"""
Музыкальный Discord-бот с командами через "!": играет треки с YouTube
по ссылке, названию или ключевым словам.

Установка:
    pip install -U "discord.py[voice]" yt-dlp
    + установить ffmpeg

ВАЖНО: в Discord Developer Portal -> Bot -> Privileged Gateway Intents
включите "MESSAGE CONTENT INTENT", иначе бот не увидит текст команд.

Запуск:
    Windows (PowerShell):  $env:DISCORD_TOKEN="ваш_токен"; python music_bot.py
    Linux/macOS:           DISCORD_TOKEN=ваш_токен python music_bot.py

Команды: !kill <ссылка или название>, !skip, !pause, !resume, !queue, !stop
"""

import asyncio
import logging
import os
import subprocess
import sys
import threading
from collections import deque

import discord
import yt_dlp
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
if os.getenv("VOICE_DEBUG"):
    logging.getLogger("discord.voice_state").setLevel(logging.DEBUG)
    logging.getLogger("discord.gateway").setLevel(logging.DEBUG)
    logging.getLogger("discord.player").setLevel(logging.DEBUG)
PREFIX = "!"  # можно поменять на любой другой символ

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",  # можно писать просто название песни
}

# Куки YouTube: без них YouTube часто не пускает запросы с серверов хостинга
# ("Sign in to confirm you're not a bot").
# Содержимое файла cookies.txt кладётся в переменную окружения YT_COOKIES,
# а бот при запуске сам сохраняет его во временный файл.
# Надёжнее передавать куки в base64 через YT_COOKIES_B64: хостинги иногда
# портят табуляции и переносы строк в многострочных переменных.
_cookies = None
if os.getenv("YT_COOKIES_B64"):
    import base64
    _cookies = base64.b64decode(os.getenv("YT_COOKIES_B64")).decode("utf-8", "replace")
elif os.getenv("YT_COOKIES"):
    _cookies = os.getenv("YT_COOKIES").replace("\\n", "\n")

if _cookies:
    COOKIE_PATH = "/tmp/yt_cookies.txt"
    _cookies = _cookies.replace("\r\n", "\n").strip() + "\n"
    with open(COOKIE_PATH, "w", encoding="utf-8") as f:
        f.write(_cookies)
    YDL_OPTS["cookiefile"] = COOKIE_PATH
    _lines = [l for l in _cookies.splitlines() if l and not l.startswith("#")]
    _valid = [l for l in _lines if l.count("\t") >= 6]
    _yt = [l for l in _valid if "youtube.com" in l]
    print(f"Куки YouTube: строк {len(_lines)}, корректных {len(_valid)}, для youtube.com {len(_yt)}")
    if _lines and not _valid:
        print("⚠️ Куки повреждены (нет табуляций) — используйте YT_COOKIES_B64")
elif os.path.exists("cookies.txt"):
    YDL_OPTS["cookiefile"] = "cookies.txt"
    print("Куки YouTube загружены из cookies.txt")
FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

intents = discord.Intents.default()
intents.message_content = True  # нужно для команд через "!"
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# Очередь треков для каждого сервера: guild_id -> deque[{"title", "page"}]
queues: dict[int, deque] = {}


def get_queue(guild_id: int) -> deque:
    return queues.setdefault(guild_id, deque())


def _stream_headers(ydl, info: dict) -> str:
    """Заголовки для ffmpeg: googlevideo отдаёт 403, если они не совпадают с запросом yt-dlp."""
    headers = dict(info.get("http_headers") or {})
    try:
        cookie = ydl.cookiejar.get_cookie_header(info["url"])
    except Exception:
        cookie = ""
    if cookie:
        headers["Cookie"] = cookie
    return "".join(f"{k}: {v}\r\n" for k, v in headers.items())


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
                "headers": _stream_headers(ydl, info),
            }

    return await asyncio.get_running_loop().run_in_executor(None, _run)


class _YTDLPReader:
    def __init__(self, downloader, stopped: threading.Event, channel, title: str):
        self._downloader = downloader
        self._stopped = stopped
        self._channel = channel
        self._title = title
        self._stderr = bytearray()
        self._finished = False
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()

    def _read_stderr(self):
        for chunk in iter(lambda: self._downloader.stderr.read(4096), b""):
            self._stderr.extend(chunk)
            if len(self._stderr) > 8192:
                del self._stderr[:-8192]

    def read(self, size: int) -> bytes:
        if self._finished:
            return b""
        data = self._downloader.stdout.read(size)
        if data:
            return data

        self._finished = True
        return_code = self._downloader.wait()
        self._stderr_thread.join()
        if return_code and not self._stopped.is_set():
            error = self._stderr.decode("utf-8", "replace").strip()
            message = f"⚠️ Ошибка загрузки **{self._title}** (yt-dlp, код {return_code})."
            if error:
                message += f"\n```{error[-1500:]}```"
            asyncio.run_coroutine_threadsafe(self._channel.send(message), bot.loop)
        self._downloader.stdout.close()
        self._downloader.stderr.close()
        return b""


class YTDLPStream(discord.AudioSource):
    """Подаёт вывод yt-dlp в ffmpeg, не дожидаясь полной загрузки трека."""

    def __init__(self, url: str, channel, title: str):
        self._stopped = threading.Event()
        command = [
            sys.executable, "-m", "yt_dlp",
            "--ignore-config", "--no-playlist", "--quiet", "--no-warnings",
        ]
        if "cookiefile" in YDL_OPTS:
            command.extend(["--cookies", YDL_OPTS["cookiefile"]])
        command.extend(["--format", YDL_OPTS["format"], "--output", "-", url])
        downloader = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._downloader = downloader
        reader = _YTDLPReader(downloader, self._stopped, channel, title)
        try:
            self._ffmpeg = discord.FFmpegOpusAudio(reader, pipe=True, options="-vn")
        except Exception:
            self._stopped.set()
            if downloader.poll() is None:
                downloader.terminate()
            downloader.wait()
            reader._stderr_thread.join()
            raise

    def read(self) -> bytes:
        return self._ffmpeg.read()

    def is_opus(self) -> bool:
        return self._ffmpeg.is_opus()

    def cleanup(self):
        self._stopped.set()
        if self._downloader.poll() is None:
            self._downloader.terminate()
        self._ffmpeg.cleanup()


async def _log_voice_state(vc: discord.VoiceClient):
    """Диагностика: шифрование/DAVE и состояние голосового соединения."""
    await asyncio.sleep(3)
    conn = getattr(vc, "_connection", None)
    dave = getattr(conn, "dave_session", None)
    print(
        "VOICE:",
        f"discord.py={discord.__version__}",
        f"connected={vc.is_connected()} playing={vc.is_playing()}",
        f"mode={getattr(vc, 'mode', None)}",
        f"dave_version={getattr(conn, 'dave_protocol_version', None)}",
        f"dave_ready={getattr(dave, 'ready', None)}",
        f"dave_downgraded={getattr(conn, 'dave_downgraded', None)}",
        f"latency={vc.latency:.3f}s",
        flush=True,
    )


async def play_next(vc: discord.VoiceClient, guild_id: int, channel):
    """Берёт следующий трек из очереди и запускает его."""
    q = get_queue(guild_id)
    if not q or not vc.is_connected():
        return

    track = q.popleft()
    try:
        source = YTDLPStream(track["page"], channel, track["title"])
    except Exception as e:
        await channel.send(f"⚠️ Не удалось воспроизвести **{track['title']}**: {e}")
        await play_next(vc, guild_id, channel)
        return

    def after(error):
        if error:
            print(f"Ошибка воспроизведения: {error}")
        asyncio.run_coroutine_threadsafe(play_next(vc, guild_id, channel), bot.loop)

    vc.play(source, after=after)
    asyncio.create_task(_log_voice_state(vc))
    await channel.send(f"▶️ Сейчас играет: **{track['title']}**")


@bot.command(name="kill")
async def kill(ctx: commands.Context, *, query: str = None):
    """!kill <ссылка или название> — включить трек."""
    if not query:
        await ctx.send(f"Напиши, что включить: `{PREFIX}kill название или ссылка`")
        return
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("Сначала зайди в голосовой канал.")
        return

    channel = ctx.author.voice.channel
    vc = ctx.voice_client
    if vc is None:
        vc = await channel.connect()
    elif vc.channel != channel:
        await vc.move_to(channel)

    async with ctx.typing():  # показывает «бот печатает...», пока идёт поиск
        try:
            info = await extract(query)
        except Exception as e:
            await ctx.send(f"⚠️ Ничего не нашёл: {e}")
            return

    q = get_queue(ctx.guild.id)
    q.append({"title": info["title"], "page": info["page"]})

    if vc.is_playing() or vc.is_paused():
        await ctx.send(f"➕ Добавлено в очередь (#{len(q)}): **{info['title']}**")
    else:
        await play_next(vc, ctx.guild.id, ctx.channel)


@bot.command(name="skip")
async def skip(ctx: commands.Context):
    vc = ctx.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()  # вызовет after() -> запустится следующий трек
        await ctx.send("⏭️ Пропущено.")
    else:
        await ctx.send("Сейчас ничего не играет.")


@bot.command(name="pause")
async def pause(ctx: commands.Context):
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("⏸️ Пауза.")
    else:
        await ctx.send("Сейчас ничего не играет.")


@bot.command(name="resume")
async def resume(ctx: commands.Context):
    vc = ctx.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("▶️ Продолжаю.")
    else:
        await ctx.send("Нечего продолжать.")


@bot.command(name="queue")
async def show_queue(ctx: commands.Context):
    q = get_queue(ctx.guild.id)
    if not q:
        await ctx.send("Очередь пуста.")
        return
    lines = [f"{i}. {t['title']}" for i, t in enumerate(list(q)[:15], start=1)]
    if len(q) > 15:
        lines.append(f"…и ещё {len(q) - 15}")
    await ctx.send("🎶 **Очередь:**\n" + "\n".join(lines))


@bot.command(name="stop")
async def stop(ctx: commands.Context):
    get_queue(ctx.guild.id).clear()
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
    await ctx.send("⏹️ Остановлено, пока!")


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound):
        return  # молча игнорируем неизвестные команды
    print(f"Ошибка в команде {ctx.command}: {error}")
    await ctx.send(f"⚠️ Ошибка: {error}")


@bot.event
async def on_ready():
    print(f"Бот запущен как {bot.user}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Укажи токен в переменной окружения DISCORD_TOKEN")
    bot.run(TOKEN)
