import shlex

combos = {
    "nightcore": ["nightcore"],
    "nightcore + bassboost": ["nightcore", "bassboost"],
    "slushed + nightcore": ["slowed", "nightcore"],
    "всё вместе": ["bassboost", "slowed", "reverb", "nightcore"],
}

FX_FILTERS = {
    "bassboost": "bass=f=60:g=15",
    "slowed": "asetrate=38098,aresample=48000,atempo=0.85",
    "reverb": "aecho=0.8:0.9:60|60:0.4|0.35",
    "nightcore": "asetrate=60000,aresample=48000,atempo=1.25",
}

for name, modes in combos.items():
    filters = [FX_FILTERS[m] for m in modes if m in FX_FILTERS]
    ffmpeg_options = "-vn"
    if filters:
        ffmpeg_options += f' -af "{",".join(filters)}"'
    print(f"{name:25} -> {shlex.split(ffmpeg_options)}")

print("OK")
