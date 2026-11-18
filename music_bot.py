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

Команды: !kill <ссылка или название>, !skip, !pause, !resume, !queue, !stop, !bassboost, !slushed, !slushed+reverb, !reverb, !nightcore, !normal
"""

import asyncio
import glob
import logging
import os
import tempfile
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

# Аудио-эффекты (ffmpeg -af). Ключ — имя эффекта, значение — строка фильтров.
FX_FILTERS: dict[str, str] = {
    "bassboost": "bass=f=60:g=15",                                                                          # усиление низких частот (басбуст)
    "slowed": "asetrate=38098,aresample=48000,atempo=0.85",                                                 # slowed (без реверба), pitch −4 полутона
    "reverb": "aecho=0.8:0.9:60|60:0.4|0.35",                                                               # реверберация/эхо
    "nightcore": "asetrate=60000,aresample=48000,atempo=1.25",                                              # nightcore: speed ×1.25, pitch +4 полутона
}

# Активные эффекты для каждого сервера: guild_id -> set имён эффектов
fx_effects: dict[int, set[str]] = {}


def get_fx(guild_id: int) -> set[str]:
    return fx_effects.setdefault(guild_id, set())


def add_fx(guild_id: int, *modes: str) -> None:
    get_fx(guild_id).update(modes)


def remove_fx(guild_id: int, mode: str) -> None:
    fx = fx_effects.get(guild_id)
    if fx:
        fx.discard(mode)


def clear_fx(guild_id: int) -> None:
    fx_effects.pop(guild_id, None)

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


DOWNLOAD_DIR = tempfile.mkdtemp(prefix="music_")


def _download_sync(url: str) -> str:
    """Скачивает аудио средствами yt-dlp во временный файл.

    ffmpeg напрямую с googlevideo получает 403 (ссылки привязаны к клиенту
    yt-dlp, нужны чанки и т.д.), а встроенный загрузчик yt-dlp работает.
    Сначала пробуем без куки (клиенты ios/visionos отдают рабочие ссылки),
    затем с куки.
    """
    base = {k: v for k, v in YDL_OPTS.items() if k != "cookiefile"}
    attempts = [base]
    if "cookiefile" in YDL_OPTS:
        attempts.append(dict(base, cookiefile=YDL_OPTS["cookiefile"]))
    last = None
    for opts in attempts:
        name = os.urandom(6).hex()
        opts = dict(opts, outtmpl=os.path.join(DOWNLOAD_DIR, name + ".%(ext)s"))
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            files = glob.glob(os.path.join(DOWNLOAD_DIR, name + ".*"))
            if files:
                return files[0]
        except Exception as e:
            last = e
    raise last or RuntimeError("не удалось скачать аудио")


async def download(url: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(None, _download_sync, url)


async def _log_voice_state(vc: discord.VoiceClient, path: str):
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
        f"file={os.path.getsize(path) if os.path.exists(path) else 'gone'}B",
        flush=True,
    )


async def play_next(vc: discord.VoiceClient, guild_id: int, channel):
    """Берёт следующий трек из очереди и запускает его."""
    q = get_queue(guild_id)
    if not q or not vc.is_connected():
        return

    track = q.popleft()
    try:
        info = {"title": track["title"]}
        path = await download(track["page"])
        modes = get_fx(guild_id)
        filters = [FX_FILTERS[m] for m in modes if m in FX_FILTERS]
        ffmpeg_options = "-vn"
        if filters:
            ffmpeg_options += f' -af "{",".join(filters)}"'
        source = discord.FFmpegOpusAudio(path, options=ffmpeg_options)
    except Exception as e:
        await channel.send(f"⚠️ Не удалось воспроизвести **{track['title']}**: {e}")
        await play_next(vc, guild_id, channel)
        return

    def after(error):
        if error:
            print(f"Ошибка воспроизведения: {error}")
        try:
            os.remove(path)
        except OSError:
            pass
        asyncio.run_coroutine_threadsafe(play_next(vc, guild_id, channel), bot.loop)

    vc.play(source, after=after)
    asyncio.create_task(_log_voice_state(vc, path))
    fx = get_fx(guild_id)
    suffix = f" (эффекты: {', '.join(sorted(fx))})" if fx else ""
    await channel.send(f"▶️ Сейчас играет: **{info['title']}**{suffix}")


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


@bot.command(name="bassboost", aliases=["bb"])
async def bassboost(ctx: commands.Context):
    """!bassboost (!bb) — добавить эффект bassboost."""
    add_fx(ctx.guild.id, "bassboost")
    fx = get_fx(ctx.guild.id)
    await ctx.send(f"🔊 Эффект **bassboost** добавлен. Активные эффекты: {', '.join(sorted(fx)) or '—'}")


@bot.command(name="slushed")
async def slushed(ctx: commands.Context):
    """!slushed — добавить slowed (без реверба), pitch −4 полутона."""
    add_fx(ctx.guild.id, "slowed")
    fx = get_fx(ctx.guild.id)
    await ctx.send(f"🐌 Эффект **slushed** добавлен. Активные эффекты: {', '.join(sorted(fx)) or '—'}")


@bot.command(name="slushed+reverb", aliases=["sr"])
async def slushed_reverb(ctx: commands.Context):
    """!slushed+reverb (!sr) — добавить slowed + reverb."""
    add_fx(ctx.guild.id, "slowed", "reverb")
    fx = get_fx(ctx.guild.id)
    await ctx.send(f"🐌🎧 Эффекты **slushed + reverb** добавлены. Активные эффекты: {', '.join(sorted(fx)) or '—'}")


@bot.command(name="reverb", aliases=["rvb"])
async def reverb_cmd(ctx: commands.Context):
    """!reverb (!rvb) — добавить реверберацию."""
    add_fx(ctx.guild.id, "reverb")
    fx = get_fx(ctx.guild.id)
    await ctx.send(f"🎧 Эффект **reverb** добавлен. Активные эффекты: {', '.join(sorted(fx)) or '—'}")


@bot.command(name="nightcore", aliases=["nc"])
async def nightcore_cmd(ctx: commands.Context):
    """!nightcore (!nc) — добавить nightcore (ускорение ×1.25, pitch +4 полутона)."""
    add_fx(ctx.guild.id, "nightcore")
    fx = get_fx(ctx.guild.id)
    await ctx.send(f"⚡ Эффект **nightcore** добавлен. Активные эффекты: {', '.join(sorted(fx)) or '—'}")


@bot.command(name="normal", aliases=["off", "clearfx", "nofx"])
async def normal(ctx: commands.Context):
    """!normal (!off, !clearfx) — убрать все эффекты, обычное звучание."""
    clear_fx(ctx.guild.id)
    await ctx.send("✅ Все эффекты убраны — обычное звучание.")


@bot.command(name="fx")
async def fx(ctx: commands.Context):
    """!fx — показать текущие эффекты звучания."""
    mode = get_fx(ctx.guild.id)
    await ctx.send(f"Активные эффекты: {', '.join(sorted(mode)) or 'отсутствуют (обычное)'}")


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
