#!/usr/bin/env python3
"""
Briefing matinal : météo Évian + actus -> texte façon radio -> mp3 (OpenAI TTS).

Sorties (dossier public/, publié sur GitHub Pages) :
  - briefing.mp3  : l'audio lu par le raccourci iPhone
  - briefing.txt  : le texte (pratique pour débugger)
  - index.html    : petite page de test avec un lecteur audio

Test en local :
  pip install -r requirements.txt
  OPENAI_API_KEY=sk-... python briefing.py
"""
from __future__ import annotations

import datetime as dt
import html
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
from openai import OpenAI

# ------------------------------------------------------------------
# Réglages (tous modifiables via variables d'environnement du workflow)
# ------------------------------------------------------------------
CITY = os.getenv("CITY", "Evian")
LAT = float(os.getenv("LAT", "46.4008"))
LON = float(os.getenv("LON", "6.5897"))
TZ = ZoneInfo("Europe/Paris")
LANGUAGE = os.getenv("BRIEFING_LANGUAGE", "English")
TEXT_MODEL = os.getenv("TEXT_MODEL", "gpt-4o-mini")      # rédaction du texte
TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")    # voix
TTS_VOICE = os.getenv("TTS_VOICE", "ash")                # essaie aussi: onyx, coral, echo, verse
TARGET_WORDS = int(os.getenv("TARGET_WORDS", "150"))     # ~1 minute d'audio

FEEDS = [
    "https://feeds.bloomberg.com/markets/news.rss",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.lemonde.fr/rss/une.xml",
]

VOICE_STYLE = (
    "Voice: energetic, warm American morning-radio host. "
    "Tone: upbeat, confident, smiling. "
    "Pacing: brisk but clear, with short pauses between the greeting, the weather and each headline. "
    "Emphasis: put energy on the opening greeting and the city name."
)

OUT = Path("public")

# Codes météo WMO utilisés par Open-Meteo
WMO = {
    0: "clear skies", 1: "mostly clear skies", 2: "partly cloudy skies", 3: "overcast skies",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow",
    73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers",
    81: "rain showers", 82: "violent rain showers", 85: "snow showers",
    86: "heavy snow showers", 95: "thunderstorms", 96: "thunderstorms with hail",
    99: "thunderstorms with hail",
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ------------------------------------------------------------------
# 1. Météo
# ------------------------------------------------------------------
def get_weather() -> dict | None:
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": LAT,
                "longitude": LON,
                "timezone": "Europe/Paris",
                "current": "temperature_2m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,"
                         "precipitation_probability_max,weather_code",
                "forecast_days": 1,
            },
            timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        return {
            "now_temp_c": round(d["current"]["temperature_2m"]),
            "now_sky": WMO.get(d["current"]["weather_code"], "mixed conditions"),
            "min_c": round(d["daily"]["temperature_2m_min"][0]),
            "max_c": round(d["daily"]["temperature_2m_max"][0]),
            "rain_chance_pct": d["daily"]["precipitation_probability_max"][0],
            "today_sky": WMO.get(d["daily"]["weather_code"][0], "mixed conditions"),
        }
    except Exception as e:  # noqa: BLE001
        log(f"[warn] météo indisponible : {e}")
        return None


# ------------------------------------------------------------------
# 2. Actus (RSS)
# ------------------------------------------------------------------
def get_headlines(per_feed: int = 5) -> list[str]:
    items: list[str] = []
    for url in FEEDS:
        try:
            f = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0 morning-briefing"})
            if not f.entries:
                log(f"[warn] flux vide ou bloqué : {url}")
                continue
            source = f.feed.get("title", url)
            for e in f.entries[:per_feed]:
                title = (e.get("title") or "").strip()
                summary = html.unescape(e.get("summary") or "").strip()
                # on coupe le HTML éventuel et on limite la longueur
                summary = summary.split("<")[0][:220]
                if title:
                    items.append(f"- [{source}] {title} — {summary}")
        except Exception as e:  # noqa: BLE001
            log(f"[warn] flux KO {url} : {e}")
    log(f"[info] {len(items)} titres récupérés")
    return items


# ------------------------------------------------------------------
# 3. Rédaction du texte
# ------------------------------------------------------------------
def fallback_script(now: dt.datetime, w: dict | None, headlines: list[str]) -> str:
    """Texte de secours si le LLM plante : moins joli mais le réveil marche quand même."""
    parts = [f"Good morning, {CITY}! It's {now:%A, %B} {now.day}."]
    if w:
        parts.append(
            f"Right now it's {w['now_temp_c']} degrees with {w['now_sky']}. "
            f"Today, {w['today_sky']}, from {w['min_c']} to {w['max_c']} degrees, "
            f"with a {w['rain_chance_pct']} percent chance of rain."
        )
    for h in headlines[:3]:
        parts.append(h.split("] ", 1)[-1].split(" — ")[0] + ".")
    parts.append("Have a great day!")
    return " ".join(parts)


def write_script(client: OpenAI, now: dt.datetime, w: dict | None, headlines: list[str]) -> str:
    date_str = f"{now:%A, %B} {now.day}"
    system = (
        "You write the script for a short morning radio briefing, read aloud by a text-to-speech voice. "
        f"Write in {LANGUAGE}. Spoken style only: no markdown, no lists, no emojis, no stage directions. "
        f"About {TARGET_WORDS} words. Structure: "
        f"1) open exactly with 'Good morning, {CITY}!' and say the date; "
        "2) the weather, using it to set the mood for the day (temperatures in degrees Celsius, say 'degrees'); "
        "3) the three most important headlines, favouring markets/business and major world news, "
        "one or two plain sentences each; "
        "4) a short upbeat sign-off. "
        "Only use facts present in the data provided. Never invent numbers or details."
    )
    user = (
        f"Date: {date_str}\n"
        f"Weather data: {w if w else 'unavailable - skip the weather gracefully'}\n"
        "Headlines:\n" + ("\n".join(headlines) if headlines else "none available - keep it short")
    )
    try:
        resp = client.chat.completions.create(
            model=TEXT_MODEL,
            temperature=0.8,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        text = (resp.choices[0].message.content or "").strip()
        if len(text.split()) < 30:
            raise ValueError("texte trop court")
        return text
    except Exception as e:  # noqa: BLE001
        log(f"[warn] rédaction LLM KO, texte de secours : {e}")
        return fallback_script(now, w, headlines)


# ------------------------------------------------------------------
# 4. Voix
# ------------------------------------------------------------------
def synthesize(client: OpenAI, text: str, path: Path) -> None:
    # Garde-fou : l'API génère parfois de longs silences en fin de fichier.
    # ~2,6 mots/seconde et ~16 Ko/s en mp3 -> on refait une tentative si le fichier est anormal.
    expected_s = max(len(text.split()) / 2.6, 20)
    max_bytes = int(expected_s * 16_000 * 2.5)
    kwargs = dict(model=TTS_MODEL, voice=TTS_VOICE, input=text, response_format="mp3")
    if "gpt-4o" in TTS_MODEL:  # tts-1 ne gère pas les instructions de style
        kwargs["instructions"] = VOICE_STYLE

    for attempt in (1, 2):
        with client.audio.speech.with_streaming_response.create(**kwargs) as resp:
            resp.stream_to_file(path)
        size = path.stat().st_size
        log(f"[info] mp3 : {size // 1024} Ko (tentative {attempt})")
        if 10_000 < size < max_bytes:
            return
        log("[warn] taille suspecte, nouvelle tentative")
    if path.stat().st_size <= 10_000:
        raise RuntimeError("mp3 vide ou quasi vide")


# ------------------------------------------------------------------
# 5. Page de test
# ------------------------------------------------------------------
def write_index(now: dt.datetime, text: str) -> None:
    stamp = now.strftime("%Y%m%d%H%M")
    (OUT / "index.html").write_text(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Morning briefing</title>
<style>body{{font-family:system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem;line-height:1.5}}
audio{{width:100%}}</style></head><body>
<h1>Morning briefing</h1><p>Généré le {now:%d/%m/%Y à %H:%M}</p>
<audio controls src="briefing.mp3?v={stamp}"></audio>
<p>{html.escape(text)}</p></body></html>""",
        encoding="utf-8",
    )


def main() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY manquante")
    OUT.mkdir(exist_ok=True)
    client = OpenAI()
    now = dt.datetime.now(TZ)

    weather = get_weather()
    headlines = get_headlines()
    text = write_script(client, now, weather, headlines)
    log("----- TEXTE -----\n" + text + "\n-----------------")

    (OUT / "briefing.txt").write_text(text, encoding="utf-8")
    synthesize(client, text, OUT / "briefing.mp3")
    write_index(now, text)
    (OUT / ".nojekyll").touch()
    log("[ok] briefing prêt")


if __name__ == "__main__":
    main()
