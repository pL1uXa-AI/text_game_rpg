# -*- coding: utf-8 -*-
"""
tts.py — озвучка ответов рассказчика (Text-to-Speech).

Движки (провайдеры):
- piper  — локальный Piper через sherpa-onnx (русские голоса ru_RU-ruslan/irina/dmitri/denis-medium).
           Голоса скачиваются из официального релиза k2-fsa/sherpa-onnx (tts-models) в data/tts/voices.
- kokoro — локальный Kokoro-82M через sherpa-onnx (нет русского; для англ. миров). Модель нужно
           положить в data/tts/kokoro/ (tts.onnx, tokens.txt, voices.json) — или скачать кнопкой.
- edge   — Microsoft Edge neural TTS (облако, бесплатно; лучшие русские голоса
           ru-RU-SvetlanaNeural/DmitryNeural). Нужен пакет edge-tts.
- none   — выключено.

Все синтезы — ФОНОВЫЕ (не блокируют ответ игроку). Аудио кэшируется по хешу
(текст+голос+скорость) в data/tts/cache и таблице tts_cache БД.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import threading
import time
import wave
from array import array
from pathlib import Path

from . import db
from .config import ROOT, get_config
from .logsetup import get_logger

log = get_logger(__name__)

# ═══════════ Пути и реестр голосов ═══════════

TTS_DIR = ROOT / "data" / "tts"
VOICES_DIR = TTS_DIR / "voices"        # data/tts/voices/{voice}/… (piper)
CACHE_DIR = TTS_DIR / "cache"          # data/tts/cache/{provider}/{hash}.{fmt}
KOKORO_DIR = TTS_DIR / "kokoro"        # kokoro: tts.onnx + tokens.txt + voices.json

# Русские голоса Piper (в релизе k2-fsa/sherpa-onnx tts-models есть все 4)
PIPER_RU_VOICES = {
    "ru_RU-ruslan-medium": "Руслан (муж., рекомендован)",
    "ru_RU-irina-medium": "Ирина (жен.)",
    "ru_RU-dmitri-medium": "Дмитрий (муж.)",
    "ru_RU-denis-medium": "Денис (муж.)",
}

# Голоса Edge (проверены на сервисе Microsoft: русские + англ. для en-миров)
EDGE_VOICES = {
    "ru-RU-SvetlanaNeural": "Светлана (рус., жен.)",
    "ru-RU-DmitryNeural": "Дмитрий (рус., муж., стандарт)",
    "en-US-JennyNeural": "Jenny (англ., жен.)",
    "en-US-GuyNeural": "Guy (англ., муж.)",
    "en-US-AriaNeural": "Aria (англ., жен.)",
    "en-US-ChristopherNeural": "Christopher (англ., муж.)",
    "en-US-EricNeural": "Eric (англ., муж.)",
    "en-US-MichelleNeural": "Michelle (англ., жен.)",
    "en-US-AnaNeural": "Ana (англ., жен.)",
    "en-GB-SoniaNeural": "Sonia (англ. UK, жен.)",
    "en-GB-RyanNeural": "Ryan (англ. UK, муж.)",
}

# Голоса Kokoro (speaker ids — по порядку voices.json 153-голосового списка v1.0; нет русского)
KOKORO_VOICES = {
    "af_heart": "Heart (англ., жен.)",
    "af_bella": "Bella (англ., жен.)",
    "am_michael": "Michael (англ., муж.)",
    "am_fenrir": "Fenrir (англ., муж.)",
}

KOKORO_SIDS = {"af_bella": 0, "am_michael": 4, "af_heart": 22, "am_fenrir": 32}

DEFAULT_VOICES = {
    "piper": "ru_RU-ruslan-medium",
    "edge": "ru-RU-DmitryNeural",
    "kokoro": "af_heart",
}

# Эмодзи и декоративные символы, которые нельзя читать вслух (TTS)
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # основные блоки эмодзи (смайлы, жесты, предметы…)
    "\U00002600-\U000027BF"   # прочие символы / дингбаты (☀ ★ ✀ ➿ …)
    "\U00002B00-\U00002BFF"   # стрелки и символы
    "\U0000FE0F"              # variation selector (текстовые эмодзи)
    "]+", re.UNICODE)

# Количество/множитель в скобках: «(x1)», «(×3)», «(х2)» — не читать как умножение
_PAREN_QTY_RE = re.compile(r"\(\s*[xх×]\s*\d{1,3}\s*\)", re.UNICODE)
# «x2» / «×10» / «х5» между словами (не внутри слова): «урон x2» → «урон 2», «экспорт» не трогаем
_MULT_RE = re.compile(r"(?<![0-9A-Za-zА-Яа-яЁё])[xх×]\s*(\d{1,3})(?![0-9])", re.UNICODE)

# Kokoro-82M для sherpa-onnx: model.onnx + tokens.txt + voices.bin (~350 МБ)
KOKORO_BASE_URL = "https://huggingface.co/csukuangfj/sherpa-onnx-kokoro/resolve/main"
KOKORO_FILES = {"tts.onnx": "model.onnx", "tokens.txt": "tokens.txt", "voices.bin": "voices.bin"}

PIPER_RELEASE_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
                     "vits-piper-{voice}-medium.tar.bz2")

# ═══════════ Маленькие помощники ═══════════

_piper_lock = threading.Lock()          # sherpa-onnx не потокобезопасен — сериализуем синтез
_piper_engines: dict[str, object] = {}  # voice → OfflineTts (кэш, экономия init ~0.6 c)


def _default_voice(provider: str) -> str:
    return DEFAULT_VOICES.get(provider, "")


def _pip_extra():
    """Пути импортов (sherpa_onnx / edge_tts) — импортируем лениво. Возвращает кортеж."""
    import importlib.util
    return {
        "sherpa_onnx": importlib.util.find_spec("sherpa_onnx") is not None,
        "edge_tts": importlib.util.find_spec("edge_tts") is not None,
    }


def engine_status() -> dict:
    """Что доступно для каждого движка (библиотеки + скачанные модели)."""
    specs = _pip_extra()
    voices: dict[str, list[str]] = {"piper": [], "kokoro": [], "edge": []}
    for d in sorted(VOICES_DIR.iterdir()) if VOICES_DIR.is_dir() else []:
        if d.is_dir() and any(d.glob("*.onnx")):
            voices["piper"].append(d.name)
    if (KOKORO_DIR / "tts.onnx").exists():
        voices["kokoro"].append("kokoro-82M")
    voices["edge"] = list(EDGE_VOICES)
    return {
        "libs": {
            "sherpa_onnx": bool(specs["sherpa_onnx"]),
            "edge_tts": bool(specs["edge_tts"]),
        },
        "voices": voices,
        "default_voices": DEFAULT_VOICES,
        "voice_labels": {
            "piper": PIPER_RU_VOICES,
            "edge": EDGE_VOICES,
            "kokoro": KOKORO_VOICES,
        },
        "dirs": {"models": str(TTS_DIR), "cache": str(CACHE_DIR)},
        "kokoro_ready": kokoro_ready(),
    }


def _clean_tts_text(text: str) -> str:
    """Подготовка текста для озвучки: убираем markdown-звёздочки, эмодзи и «xN»-количества,
    чтобы синтезатор не читал «*пыль*», смайлики и «x2» как «умножить на два»."""
    text = re.sub(r"\*+", "", text)            # **жирный** / *курсив*
    text = re.sub(r"`+", "", text)             # code-разметка
    text = _EMOJI_RE.sub(" ", text)             # эмодзи/дингбаты → пробел
    text = _PAREN_QTY_RE.sub("", text)          # «(x1)» → «»
    text = _MULT_RE.sub(r"\1", text)           # «урон x2» → «урон 2»
    text = re.sub(r"\s+\s+", " ", text)
    return text.strip()


def _split_chunks(text: str, limit: int = 1200) -> list[str]:
    """Делит текст на куски ≤ limit символов по границам предложений."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    chunks: list[str] = []
    buf = ""
    for s in sentences:
        if not s:
            continue
        if buf and len(buf) + len(s) + 1 > limit:
            chunks.append(buf)
            buf = s
        elif len(s) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            # длинное предложение: режим по pieces
            while len(s) > limit:
                chunks.append(s[:limit])
                s = s[limit:]
            buf = s
        else:
            buf = (buf + " " + s).strip()
    if buf:
        chunks.append(buf)
    return chunks


def _max_chars() -> int:
    return max(200, min(6000, get_config().tts_max_chars))


def _hash(text: str, provider: str, voice: str, rate: str) -> str:
    return hashlib.sha1(f"{provider}|{voice}|{rate}|{text}".encode("utf-8")).hexdigest()[:16]


# ═══════════ Движок: Piper (локальный, sherpa-onnx) ═══════════

def _voice_dir(voice: str) -> Path:
    return VOICES_DIR / voice


def piper_voice_ready(voice: str) -> bool:
    d = _voice_dir(voice)
    return (d / f"{voice}.onnx").exists() and (d / "tokens.txt").exists()


def _get_piper_engine(voice: str):
    """Создаёт/берёт из кэша sherpa_onnx.OfflineTts для голоса (сериализуется lock'ом снаружи)."""
    if voice in _piper_engines:
        return _piper_engines[voice]
    import sherpa_onnx
    d = _voice_dir(voice)
    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=str(d / f"{voice}.onnx"),
                tokens=str(d / "tokens.txt"),
                lexicon="",
                dict_dir="",
                data_dir=str(d / "espeak-ng-data"),
            ),
            provider="cpu",
            num_threads=4,
            debug=False,
        ),
        rule_fsts="",
        rule_fars="",
        max_num_sentences=2,
    )
    tts = sherpa_onnx.OfflineTts(cfg)
    _piper_engines[voice] = tts
    return tts


def _wav_bytes(samples, sample_rate: int) -> bytes:
    """sherpa-onnx отдаёт float (-1..1) → int16 WAV в памяти."""
    import io
    raw = array("h", (int(max(-1.0, min(1.0, s)) * 32767) for s in samples))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(raw.tobytes())
    return buf.getvalue()



def _chain_samples(groups):
    """Склеивает несколько наборов PCM-сэмплов в единый последовательный поток.
    Для многочастевого синтеза нужен ОДИН WAV с полным аудио, а не склейка отдельных
    WAV-файлов (каждый несёт свой заголовок — плеер сыграл бы лишь первую часть
    и «оборвался на середине текста»)."""
    for g in groups:
        for v in g:
            yield v


def _synth_piper(text: str, voice: str, speed: float) -> bytes:
    """Синхронный синтез Piper → bytes WAV. Вызывается из executor'а (не блокирует loop)."""
    if not piper_voice_ready(voice):
        raise RuntimeError(f"Голос Piper «{voice}» не скачан (скачай в настройках озвучки)")
    with _piper_lock:
        tts = _get_piper_engine(voice)
        sets = []
        sr = 22050
        for chunk in _split_chunks(text, 1200):
            res = tts.generate(chunk, sid=0, speed=float(speed))
            sets.append(res.samples)
            sr = res.sample_rate
        return _wav_bytes(_chain_samples(sets), sr)


# ═══════════ Движок: Kokoro (локальный, sherpa-onnx) ═══════════

def kokoro_ready() -> bool:
    return (KOKORO_DIR / "tts.onnx").exists() and (KOKORO_DIR / "tokens.txt").exists()


_kokoro_engine = None


def _get_kokoro_engine():
    global _kokoro_engine
    if _kokoro_engine is not None:
        return _kokoro_engine
    import sherpa_onnx
    voices_file = str(KOKORO_DIR / "voices.bin") if (KOKORO_DIR / "voices.bin").exists() else \
        (str(KOKORO_DIR / "voices.json") if (KOKORO_DIR / "voices.json").exists() else "")
    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(KOKORO_DIR / "tts.onnx"),
                voices=voices_file,
                tokens=str(KOKORO_DIR / "tokens.txt"),
                data_dir="",
            ),
            provider="cpu",
            num_threads=4,
            debug=False,
        ),
        rule_fsts="",
        rule_fars="",
        max_num_sentences=2,
    )
    _kokoro_engine = sherpa_onnx.OfflineTts(cfg)
    return _kokoro_engine


def _synth_kokoro(text: str, voice: str, speed: float) -> bytes:
    if not kokoro_ready():
        raise RuntimeError("Модель Kokoro не установлена (скачай в настройках озвучки)")
    with _piper_lock:
        tts = _get_kokoro_engine()
        sid = KOKORO_SIDS.get(voice, 0)
        sets = []
        sr = 22050
        for chunk in _split_chunks(text, 1200):
            res = tts.generate(chunk, sid=sid, speed=float(speed))
            sets.append(res.samples)
            sr = res.sample_rate
        return _wav_bytes(_chain_samples(sets), sr)


# ═══════════ Движок: Edge (облако, edge-tts) ═══════════

async def _synth_edge_once(text: str, voice: str, rate: str) -> bytes:
    import edge_tts
    # Сервис Microsoft требует явный знак в rate («+0%», а не «0%») — иначе «Invalid rate '0%'»
    comm = edge_tts.Communicate(text, voice, rate=_norm_rate(rate))
    out = b""
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            out += chunk["data"]
    if not out:
        raise RuntimeError("Edge TTS вернул пустой аудио-поток")
    return out


def _edge_transient(err: BaseException) -> bool:
    """Edge-TTS бросает СВОИ исключения (edge_tts.exceptions.*): «нет аудио», неожиданный
    ответ веб-сокета, обрыв — всё это лечится повтором (сессия 36, п.28), но в общих
    текстовых признаках is_transient не опознаётся. Дополняем, а не подменяем."""
    from .retry import is_transient
    try:
        import edge_tts.exceptions as _ex
        names = tuple(getattr(_ex, n) for n in
                      ("NoAudioReceived", "UnexpectedResponse", "WebSocketError",
                       "UnknownResponse", "SkewAdjustmentError")
                      if isinstance(getattr(_ex, n, None), type))
        if names and isinstance(err, names):
            return True
    except Exception:
        pass
    return is_transient(err)


async def _synth_edge(text: str, voice: str, rate: str) -> bytes:
    """Облачный Edge-синтез с повторами (сессия 36, п.28).

    Microsoft-сервис периодически отдаёт 429/5xx/обрыв соединения — раньше это превращалось
    в `tts_status=-1` и кнопку «⚠» у игрока, хотя обычный ретрай решаает проблему.
    Локальные движки (Piper/Kokoro) не повторяем: у них нет сети.
    """
    from .retry import with_retries
    return await with_retries(lambda: _synth_edge_once(text, voice, rate),
                              what=f"Edge TTS ({voice})",
                              transient_fn=_edge_transient)


# ═══════════ Общий синтез ═══════════

async def synthesize(text: str, provider: str, voice: str, rate: str) -> tuple[bytes, str, int]:
    """Синтез текста → (audio bytes, fmt, sample_rate_hint). provider: piper|kokoro|edge.
    Текст предварительно чистится (markdown, эмодзи, xN-количества) — см. _clean_tts_text."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Пустой текст для озвучки")
    text = _clean_tts_text(text)[:_max_chars()]
    provider = (provider or "none").lower()
    if provider == "piper":
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _synth_piper, text, voice, _speed_for_piper(rate))
        return data, "wav", 22050
    if provider == "kokoro":
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _synth_kokoro, text, voice, _speed_for_piper(rate))
        return data, "wav", 22050
    if provider == "edge":
        data = await _synth_edge(text, voice, rate)
        return data, "mp3", 0
    raise RuntimeError(f"Неизвестный TTS-провайдер: {provider}")


def _speed_for_piper(rate: str) -> float:
    """rate вида '+20%' / '-10%' → множитель скорости (1 = норма)."""
    try:
        pct = int(str(rate).replace("%", "").strip() or "0")
    except (TypeError, ValueError):
        pct = 0
    return max(0.5, min(2.0, 1.0 + pct / 100.0))


def _norm_rate(rate: str | None) -> str:
    r = str(rate or "").strip()
    if not r:
        return "+0%"
    try:
        pct = int(r.replace("%", "").strip())
    except (TypeError, ValueError):
        return "+0%"
    return f"{pct:+d}%"


# ═══════════ Настройки (глобальные + per-world) ═══════════

def _world_tts_overrides(world: dict) -> dict:
    try:
        return json.loads(world.get("tts_settings") or "{}")
    except Exception:
        return {}


def tts_effective(world: dict) -> dict:
    """Эффективные настройки TTS мира: глобальные (.env/админка) + per-world поверх."""
    cfg = get_config()
    ov = _world_tts_overrides(world)
    provider = (ov.get("provider") or "").strip() or cfg.tts_provider or "none"
    voice = (ov.get("voice") or "").strip() or cfg.tts_voice or _default_voice(provider)
    enabled = ov.get("enabled") if "enabled" in ov else cfg.tts_enabled
    auto_play = ov.get("auto_play") if "auto_play" in ov else cfg.tts_auto_play
    rate = _norm_rate(ov.get("rate") or cfg.tts_rate)
    return {
        "enabled": bool(enabled) and provider != "none",
        "provider": provider,
        "voice": voice,
        "rate": rate,
        "auto_play": bool(auto_play),
        "max_chars": _max_chars(),
        "cache_enabled": bool(cfg.tts_cache_enabled),
    }


# ═══════════ Фоновый синтез для события ═══════════

def _cache_row(text_hash: str) -> dict | None:
    rows = db.find_tts_cache(text_hash)
    return rows[0] if rows else None


async def ensure_event_audio(world_id: int, event_id: int, text: str, opts: dict | None = None,
                             provider: str | None = None, voice: str | None = None,
                             rate: str | None = None) -> dict:
    """Фоновая задача: синтез текста события (если включено) → кэш → привязка к событию.
    Никогда не бросает исключение наружу (статус -1 = ошибка в UI)."""
    try:
        world = db.get_world(world_id)
        if not world:
            return {"status": -1, "error": "мир не найден"}
        opts = opts or tts_effective(world)
        if not opts.get("enabled"):
            return {"status": 0}
        prov = provider or opts["provider"]
        if prov not in ("piper", "kokoro", "edge"):
            return {"status": 0}
        voice = voice or opts.get("voice") or _default_voice(prov)
        rate = _norm_rate(rate or opts.get("rate"))
        # чистим текст ДО хеша — кэш и синтез работают с одним и тем же (чистым) текстом
        text = _clean_tts_text(text or "")
        if not text:
            return {"status": -1, "error": "текст пуст после очистки"}

        h = _hash(text, prov, voice, rate)
        cached = _cache_row(h)
        if cached:
            rel = cached["rel_path"]
            db.set_event_tts(event_id, 2, rel)
            return {"status": 2, "file": rel, "cached": True}

        audio, fmt, _sr = await synthesize(text, prov, voice, rate)
        if not audio:
            raise RuntimeError("Синтез вернул пустое аудио")
        rel_dir = f"tts/cache/{prov}/{h[:2]}"
        abs_dir = ROOT / "data" / rel_dir
        abs_dir.mkdir(parents=True, exist_ok=True)
        file_name = f"{h}.{fmt}"
        (abs_dir / file_name).write_bytes(audio)
        rel = f"{rel_dir}/{file_name}"
        db.add_tts_cache(h, prov, voice, rate, fmt, rel)
        db.set_event_tts(event_id, 2, rel)
        return {"status": 2, "file": rel}
    except Exception as e:
        try:
            db.set_event_tts(event_id, -1, "")
        except Exception as e2:
            # сам синтез уже упал с понятной причиной; это — вторая беда (статус не проставлен)
            log.warning("TTS: не удалось пометить ошибку синтеза для события %s: %s",
                        event_id, e2)
        return {"status": -1, "error": str(e)}


# ═══════════ Скачивание моделей ═══════════

async def _http_download(url: str, dest: Path) -> None:
    import httpx
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0)) as client:
        async with client.stream("GET", url, follow_redirects=True) as r:
            r.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as f:
                async for chunk in r.aiter_bytes(262144):
                    f.write(chunk)
            tmp.replace(dest)


async def download_piper_voice(voice: str) -> dict:
    """Скачивает и распаковывает голос Piper из релиза k2-fsa (tts-models)."""
    if voice not in PIPER_RU_VOICES:
        raise ValueError(f"Неизвестный голос Piper: {voice}")
    if piper_voice_ready(voice):
        return {"ok": True, "already": True, "voice": voice, "size_mb": 0}
    dest_dir = _voice_dir(voice)
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = PIPER_RELEASE_URL.format(voice=voice)
    archive = dest_dir / f"{voice}.tar.bz2"
    await _http_download(url, archive)
    size_mb = round(archive.stat().st_size / 1048576, 1)
    # распаковка
    import tarfile
    def _extract():
        with tarfile.open(archive, "r:bz2") as t:
            t.extractall(dest_dir, filter="data")
    await asyncio.get_event_loop().run_in_executor(None, _extract)
    # нормализация layout: inner dir "vits-piper-{voice}" → файлы прямо в dest_dir
    inner = dest_dir / f"vits-piper-{voice}"
    if inner.is_dir():
        for f in os.listdir(inner):
            shutil.move(str(inner / f), str(dest_dir / f))
        inner.rmdir()
    archive.unlink(missing_ok=True)
    ok = piper_voice_ready(voice)
    return {"ok": ok, "voice": voice, "size_mb": size_mb,
            "ready": ok, "error": None if ok else "не удалось распаковать модель"}


async def download_kokoro_model() -> dict:
    """Скачивает Kokoro-82M для sherpa-onnx (~350 МБ) с Hugging Face в data/tts/kokoro/."""
    if kokoro_ready():
        return {"ok": True, "already": True}
    KOKORO_DIR.mkdir(parents=True, exist_ok=True)
    res: dict = {"ok": True, "files": [], "size_mb": 0}
    try:
        for src_name, local_name in KOKORO_FILES.items():
            dest = KOKORO_DIR / local_name
            if dest.exists() and dest.stat().st_size > 0:
                continue
            url = KOKORO_BASE_URL + "/" + src_name
            await _http_download(url, dest)
            res["size_mb"] = round((res.get("size_mb") or 0) + dest.stat().st_size / 1048576, 1)
            res["files"].append(local_name)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    res["ok"] = kokoro_ready()
    return res


# ═══════════ Статус для UI ═══════════

def status_for_ui() -> dict:
    st = engine_status()
    global_status = {
        "enabled": bool(get_config().tts_enabled),
        "provider": get_config().tts_provider or "none",
        "voice": get_config().tts_voice or "",
        "rate": get_config().tts_rate or "+0%",
        "auto_play": bool(get_config().tts_auto_play),
        "cache_enabled": bool(get_config().tts_cache_enabled),
    }
    return {**st, "global": global_status, "kokoro_ready": kokoro_ready()}


# ═══════════ Предзагрузка голосов (сессия 30) ═══════════
# Локальные движки (Piper/Kokoro) инициализируют sherpa-onnx при ПЕРВОМ синтезе — это
# занимает ~0.5–1 с и держит executor. Предзагрузка при старте сервера прогревает движки
# заранее, чтобы первый ответ игрока озвучился сразу (без задержки на инициализацию).

def preload_voice(provider: str | None = None, voice: str = "") -> dict:
    """Прогревает движок озвучки (Piper/Kokoro) в фоне, не блокируя ответ.
    Возвращает краткий статус. Edge предзагружать не нужно (облако, без локальных моделей)."""
    cfg = get_config()
    if not cfg.tts_enabled:
        return {"preloaded": False, "reason": "tts disabled"}
    prov = (provider or cfg.tts_provider or "none").lower()
    if prov not in ("piper", "kokoro"):
        return {"preloaded": False, "reason": f"{prov} не требует предзагрузки (облако)"}
    try:
        if prov == "piper":
            v = voice or cfg.tts_voice or _default_voice("piper")
            if not piper_voice_ready(v):
                return {"preloaded": False, "reason": f"голос {v} не скачан"}
            with _piper_lock:
                _get_piper_engine(v)  # инициализация OfflineTts (одноразовая, кэшируется)
            return {"preloaded": True, "provider": "piper", "voice": v}
        if prov == "kokoro":
            if not kokoro_ready():
                return {"preloaded": False, "reason": "модель Kokoro не установлена"}
            with _piper_lock:
                _get_kokoro_engine()
            return {"preloaded": True, "provider": "kokoro", "voice": "kokoro-82M"}
    except Exception as e:
        log.warning("предзагрузка голоса TTS (%s): %s", prov, e)
        return {"preloaded": False, "reason": str(e)}
    return {"preloaded": False, "reason": f"неизвестный провайдер {prov}"}


def preload_configured_voices() -> None:
    """Прогревает голоса по глобальной конфигурации (при старте сервера, best-effort)."""
    try:
        cfg = get_config()
        if not cfg.tts_enabled:
            return
        prov = (cfg.tts_provider or "none").lower()
        if prov in ("piper", "kokoro"):
            preload_voice(prov)
    except Exception as e:
        log.warning("предзагрузка голосов TTS при старте не удалась (озвучка прогреется "
                    "при первом синтезе): %s", e)


__all__ = ["ensure_event_audio", "synthesize", "tts_effective", "status_for_ui",
           "download_piper_voice", "download_kokoro_model", "piper_voice_ready",
           "kokoro_ready", "engine_status", "PIPER_RU_VOICES", "EDGE_VOICES",
           "KOKORO_VOICES", "DEFAULT_VOICES", "preload_voice", "preload_configured_voices"]