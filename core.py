import io
import os
import re
import sys
import json
import time
import shutil
import logging
import argparse
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

import yt_dlp
from ytmusicapi import YTMusic, OAuthCredentials
from PIL import Image, ImageOps

# =========================================================
# НАСТРОЙКИ
# =========================================================
YADISK_API_BASE = "https://cloud-api.yandex.net/v1/disk"
YADISK_ROOT_DIR = "disk:/NikridRadio"
YADISK_TOKEN_PATH = Path("yadisk_token.txt")

PLAYLIST_JSON_PATH = Path("playlist.json")
COVERS_DIR = Path("covers")
TEMP_AUDIO_DIR = Path("temp_audio")
FAILED_LOG_PATH = Path("failed_tracks.log")
UNAVAILABLE_IDS_PATH = Path("unavailable_ids.json")
SYNC_CONFIG_PATH = Path("sync_config.json")  # {"token": "ghp_...", "gistId": "..."}

BROWSER_JSON_PATH = Path("browser.json")
CREDENTIALS_JSON_PATH = Path("credentials.json")
CLIENT_SECRET_PATH = Path("client_secret.json")

WAVE_TARGET_COUNT = 200
COVER_SIZE = (512, 512)
COVER_WEBP_QUALITY = 84

# ANSI-цвета для консоли (с автоматическим включением VT100 в Windows 10+)
_ANSI_RESET = "\033[0m"
_ANSI_DIM = "\033[90m"
_ANSI_GREEN = "\033[92m"
_ANSI_YELLOW = "\033[93m"
_ANSI_RED = "\033[91m"
_ANSI_CYAN = "\033[96m"
_ANSI_BOLD = "\033[1m"


def _enable_windows_ansi() -> bool:
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h_out = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(h_out, ctypes.byref(mode)):
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                kernel32.SetConsoleMode(h_out, mode.value | 0x0004)
                return True
        except Exception:
            return False
        return False
    return True


_USE_ANSI = _enable_windows_ansi()
_LAST_INLINE_LEN = 0


def _c(text: str, code: str) -> str:
    if not _USE_ANSI:
        return text
    return f"{code}{text}{_ANSI_RESET}"


def _write_file_log(level: str, msg: str, step: str = ""):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    step_tag = f" [{step}]" if step else ""
    line = f"{ts} [{level}]{step_tag} {msg}\n"
    try:
        with open(FAILED_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def ui_clear_inline():
    global _LAST_INLINE_LEN
    if _LAST_INLINE_LEN > 0:
        if _USE_ANSI:
            sys.stdout.write(f"\r{' ' * _LAST_INLINE_LEN}\r")
        else:
            sys.stdout.write("\n")
        sys.stdout.flush()
        _LAST_INLINE_LEN = 0


def ui_status(icon: str, text: str, color: str = "", step: str = "", log_level: str | None = None):
    global _LAST_INLINE_LEN
    clean_msg = f"{icon} {text}".strip()
    colored_msg = _c(clean_msg, color) if color else clean_msg
    if _USE_ANSI and sys.stdout.isatty():
        pad = max(0, _LAST_INLINE_LEN - len(clean_msg))
        sys.stdout.write(f"\r{colored_msg}{' ' * pad}")
        sys.stdout.flush()
        _LAST_INLINE_LEN = len(clean_msg)
    else:
        print(clean_msg, flush=True)
        _LAST_INLINE_LEN = 0
    if log_level:
        _write_file_log(log_level, text, step=step)


def ui_line(icon: str, text: str, color: str = "", step: str = "", log_level: str | None = None):
    ui_clear_inline()
    msg = f"{icon} {text}".strip()
    print(_c(msg, color) if color else msg, flush=True)
    if log_level:
        _write_file_log(log_level, text, step=step)


def ui_section(title: str):
    ui_clear_inline()
    print(f"\n{_c(f'── {title} ', _ANSI_CYAN + _ANSI_BOLD)}{_c('─' * max(10, 56 - len(title)), _ANSI_DIM)}", flush=True)


def ui_progress(step_label: str, current: int, total: int, detail: str = ""):
    global _LAST_INLINE_LEN
    total = max(total, 1)
    pct = min(100, int((current / total) * 100))
    bar_len = 20
    filled = min(bar_len, int((pct / 100) * bar_len))
    bar_plain = f"[{'#' * filled}{'.' * (bar_len - filled)}]"
    plain = f"  {step_label} {bar_plain} {pct:>3}% ({current}/{total})"
    if detail:
        plain += f" • {detail}"

    if _USE_ANSI and sys.stdout.isatty():
        colored = (
            f"  {_c(step_label, _ANSI_BOLD)} "
            f"{_c('[' + '#' * filled, _ANSI_GREEN)}{_c('.' * (bar_len - filled), _ANSI_DIM)} "
            f"{_c(f'{pct:>3}%', _ANSI_CYAN)} ({current}/{total})"
        )
        if detail:
            colored += f" {_c('• ' + detail, _ANSI_DIM)}"
        pad = max(0, _LAST_INLINE_LEN - len(plain))
        sys.stdout.write(f"\r{colored}{' ' * pad}")
        sys.stdout.flush()
        _LAST_INLINE_LEN = len(plain)
    else:
        if current == total or current == 1 or current % 10 == 0:
            print(plain, flush=True)
            _LAST_INLINE_LEN = 0


def _compact_err(err: Exception | str, limit: int = 160) -> str:
    s = re.sub(r"\s+", " ", str(err)).strip()
    return s if len(s) <= limit else s[: limit - 3] + "..."


logging.basicConfig(level=logging.ERROR, handlers=[logging.NullHandler()])


# =========================================================
# YANDEX DISK REST API
# =========================================================
def load_yadisk_token() -> str:
    if not YADISK_TOKEN_PATH.exists():
        raise RuntimeError(
            f"Не найден файл {YADISK_TOKEN_PATH}. Создайте его и вставьте туда OAuth-токен Яндекс Диска."
        )
    token = YADISK_TOKEN_PATH.read_text(encoding="utf-8-sig").strip()
    if not token:
        raise RuntimeError(f"Файл {YADISK_TOKEN_PATH} пуст. Вставьте OAuth-токен Яндекс Диска.")
    return token


def yadisk_request(
    method: str,
    endpoint: str,
    token: str,
    params: dict | None = None,
    data: bytes | None = None,
    headers: dict | None = None,
    timeout: int = 60,
    absolute_url: str | None = None,
) -> tuple[int, dict]:
    if absolute_url:
        url = absolute_url
    else:
        url = f"{YADISK_API_BASE}{endpoint}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

    req_headers = {"User-Agent": "Nikrid-Radio-Sync/2.0"}
    if not absolute_url:
        req_headers["Authorization"] = f"OAuth {token}"
        req_headers["Accept"] = "application/json"
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return resp.status, {}
            try:
                return resp.status, json.loads(raw.decode("utf-8"))
            except Exception:
                return resp.status, {}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        try:
            parsed = json.loads(body) if body else {}
        except Exception:
            parsed = {"raw": body}
        return e.code, parsed


def ensure_yadisk_folder(token: str):
    status, data = yadisk_request(
        "PUT", "/resources", token, params={"path": YADISK_ROOT_DIR}
    )
    if status in (201, 409):
        return
    raise RuntimeError(f"Не удалось создать папку {YADISK_ROOT_DIR} на Яндекс Диске: HTTP {status} {data}")


def fetch_existing_yadisk_files(token: str) -> dict[str, dict]:
    result = {}
    offset = 0
    limit = 200
    while True:
        status, data = yadisk_request(
            "GET",
            "/resources",
            token,
            params={
                "path": YADISK_ROOT_DIR,
                "limit": limit,
                "offset": offset,
                "fields": "_embedded.items.name,_embedded.items.path,_embedded.items.public_url,_embedded.total",
            },
        )
        if status != 200:
            ui_line("!", f"Не удалось получить список файлов Яндекс Диска: HTTP {status}", _ANSI_YELLOW, step="yadisk_list", log_level="WARN")
            break
        embedded = data.get("_embedded") or {}
        items = embedded.get("items") or []
        for item in items:
            name = item.get("name", "")
            stem = Path(name).stem
            if stem:
                result[stem] = {
                    "path": item.get("path", f"{YADISK_ROOT_DIR}/{name}"),
                    "public_url": item.get("public_url", ""),
                }
        total = embedded.get("total", 0)
        offset += len(items)
        if not items or offset >= total:
            break
    return result


def publish_yadisk_resource(token: str, remote_path: str) -> str:
    status, data = yadisk_request(
        "PUT", "/resources/publish", token, params={"path": remote_path}
    )
    if status not in (200, 201, 202):
        raise RuntimeError(f"Ошибка публикации {remote_path}: HTTP {status} {data}")

    for _ in range(5):
        g_status, g_data = yadisk_request(
            "GET",
            "/resources",
            token,
            params={"path": remote_path, "fields": "public_url,path"},
        )
        if g_status == 200 and g_data.get("public_url"):
            return g_data["public_url"]
        time.sleep(0.5)

    raise RuntimeError(f"Не удалось получить public_url после публикации {remote_path}")


def upload_and_publish_yadisk(token: str, local_path: Path, video_id: str) -> tuple[str, str]:
    ext = local_path.suffix.lower().lstrip(".") or "webm"
    remote_path = f"{YADISK_ROOT_DIR}/{video_id}.{ext}"

    status, data = yadisk_request(
        "GET",
        "/resources/upload",
        token,
        params={"path": remote_path, "overwrite": "true"},
    )
    if status != 200 or not data.get("href"):
        raise RuntimeError(f"Не получен upload href для {remote_path}: HTTP {status} {data}")

    file_bytes = local_path.read_bytes()
    put_status, put_data = yadisk_request(
        "PUT",
        "",
        token,
        data=file_bytes,
        headers={"Content-Type": "application/octet-stream"},
        timeout=180,
        absolute_url=data["href"],
    )
    if put_status not in (200, 201, 202):
        raise RuntimeError(f"Ошибка PUT загрузки {remote_path}: HTTP {put_status} {put_data}")

    public_url = publish_yadisk_resource(token, remote_path)
    time.sleep(0.15)
    return remote_path, public_url


def delete_from_yadisk(token: str, remote_path: str):
    if not remote_path:
        return
    status, data = yadisk_request(
        "DELETE",
        "/resources",
        token,
        params={"path": remote_path, "permanently": "true"},
    )
    if status in (202, 204):
        _write_file_log("INFO", f"Удалён с Яндекс Диска: {remote_path}", step="cleanup")
    elif status == 404:
        _write_file_log("INFO", f"Файл уже отсутствует на Яндекс Диске: {remote_path}", step="cleanup")
    else:
        ui_line("!", f"Не удалось удалить {remote_path}: HTTP {status}", _ANSI_YELLOW, step="cleanup", log_level="WARN")


# =========================================================
# ЛОКАЛЬНЫЕ ОБЛОЖКИ (COVERS/<videoId>.webp, 512x512)
# =========================================================
def upgrade_cover_url(url: str) -> str:
    if not url or not url.startswith("http"):
        return ""
    u = url.strip()
    if "googleusercontent.com" in u or "ggpht.com" in u:
        u = re.sub(r"=w\d+-h\d+[^&?]*", "=w600-h600-l90-rj", u)
        u = re.sub(r"=s\d+[^&?]*", "=s600", u)
    return u


def ensure_local_cover(video_id: str, remote_url: str = "") -> str:
    if not video_id:
        return ""
    COVERS_DIR.mkdir(exist_ok=True)
    out_path = COVERS_DIR / f"{video_id}.webp"
    rel_path = f"covers/{video_id}.webp"

    if out_path.exists() and out_path.stat().st_size > 500:
        return rel_path

    candidates = []
    if remote_url and remote_url.startswith("http"):
        candidates.append(upgrade_cover_url(remote_url))
    candidates.append(f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg")
    candidates.append(f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg")

    seen = set()
    last_err = ""
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            if len(raw) < 512:
                last_err = f"Слишком маленький ответ ({len(raw)} B) с {url}"
                continue

            with Image.open(io.BytesIO(raw)) as img:
                img = img.convert("RGB")
                w, h = img.size
                if "ytimg.com" in url and "hqdefault" in url and abs((w / max(h, 1)) - (4 / 3)) < 0.05:
                    crop_y = int(h * 0.125)
                    if h - 2 * crop_y >= 100:
                        img = img.crop((0, crop_y, w, h - crop_y))

                img = ImageOps.fit(img, COVER_SIZE, method=Image.Resampling.LANCZOS)
                img.save(out_path, format="WEBP", quality=COVER_WEBP_QUALITY, method=6)
                return rel_path
        except Exception as e:
            last_err = f"{url} -> {_compact_err(e, 100)}"

    _write_file_log("WARN", f"[{video_id}] Обложка не скачана: {last_err}", step="covers")
    return ""


def delete_local_cover(video_id: str):
    if not video_id:
        return
    p = COVERS_DIR / f"{video_id}.webp"
    try:
        if p.exists():
            p.unlink()
    except Exception as e:
        _write_file_log("WARN", f"Не удалось удалить обложку {p}: {_compact_err(e)}", step="cleanup")


# =========================================================
# АВТОРИЗАЦИЯ YOUTUBE MUSIC
# =========================================================
def _extract_client_creds(path: Path) -> tuple[str, str]:
    if not path.exists():
        return "", ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "installed" in data:
            data = data["installed"]
        elif "web" in data:
            data = data["web"]
        return data.get("client_id", "").strip(), data.get("client_secret", "").strip()
    except Exception:
        return "", ""


def get_ytmusic_client() -> YTMusic:
    auth_data = {}
    if BROWSER_JSON_PATH.exists():
        try:
            auth_data = json.loads(BROWSER_JSON_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    if isinstance(auth_data, dict) and ("access_token" in auth_data or "refresh_token" in auth_data):
        client_id, client_secret = _extract_client_creds(CREDENTIALS_JSON_PATH)
        if not client_id or not client_secret:
            client_id, client_secret = _extract_client_creds(CLIENT_SECRET_PATH)

        if client_id and client_secret:
            return YTMusic(
                str(BROWSER_JSON_PATH),
                oauth_credentials=OAuthCredentials(
                    client_id=client_id,
                    client_secret=client_secret
                )
            )
        raise RuntimeError(
            "В browser.json лежат OAuth-токены, но client_id/client_secret не найдены в credentials.json или client_secret.json."
        )

    return YTMusic(str(BROWSER_JSON_PATH))


# =========================================================
# ОБЛАЧНАЯ СИНХРОНИЗАЦИЯ БУФЕРА (GITHUB GIST)
# =========================================================
def load_cloud_sync_state() -> dict:
    token = ""
    gist_id = ""
    if SYNC_CONFIG_PATH.exists():
        try:
            cfg = json.loads(SYNC_CONFIG_PATH.read_text(encoding="utf-8"))
            token = cfg.get("token", "").strip()
            gist_id = cfg.get("gistId", "").strip()
        except Exception:
            pass

    empty = {"likeIds": [], "dislikeIds": [], "waveListenedIds": [], "dislikeTitles": []}
    if not token:
        ui_line("!", f"Токен Gist не найден в {SYNC_CONFIG_PATH} — буфер пропущен", _ANSI_YELLOW, step="gist_sync", log_level="WARN")
        return empty

    try:
        if not gist_id:
            list_req = urllib.request.Request(
                "https://api.github.com/gists?per_page=30",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "Nikrid-Radio-Sync"
                }
            )
            with urllib.request.urlopen(list_req, timeout=15) as resp:
                gists = json.loads(resp.read().decode("utf-8"))
                for g in gists:
                    if "nikrid_sync.json" in g.get("files", {}):
                        gist_id = g.get("id", "")
                        SYNC_CONFIG_PATH.write_text(
                            json.dumps({"token": token, "gistId": gist_id}, indent=2),
                            encoding="utf-8"
                        )
                        break

        if not gist_id:
            return empty

        req = urllib.request.Request(
            f"https://api.github.com/gists/{gist_id}",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "Nikrid-Radio-Sync"
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data.get("files", {}).get("nikrid_sync.json", {}).get("content", "{}")
            return json.loads(content)
    except Exception as e:
        ui_line("!", f"Gist не прочитан ({_compact_err(e, 90)}) — продолжаю с локальными данными", _ANSI_YELLOW, step="gist_sync", log_level="WARN")
        return empty


# =========================================================
# НАДЁЖНОЕ СКАЧИВАНИЕ АУДИО (YT-DLP)
# =========================================================
def load_unavailable_ids() -> set[str]:
    if not UNAVAILABLE_IDS_PATH.exists():
        return set()
    try:
        data = json.loads(UNAVAILABLE_IDS_PATH.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def save_unavailable_ids(ids: set[str]):
    try:
        UNAVAILABLE_IDS_PATH.write_text(
            json.dumps(sorted(ids), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        _write_file_log("WARN", f"Не удалось сохранить {UNAVAILABLE_IDS_PATH}: {_compact_err(e)}", step="yt_dlp")


def download_audio_robust(video_id: str, unavailable_ids: set[str] | None = None, track_label: str = "") -> Path | None:
    if unavailable_ids is not None and video_id in unavailable_ids:
        _write_file_log("INFO", f"[{video_id}] ({track_label}) пропущен по {UNAVAILABLE_IDS_PATH}", step="yt_dlp")
        return None

    TEMP_AUDIO_DIR.mkdir(exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"

    profiles = [
        {"name": "native_default", "clients": None, "use_cookies": False, "fmt": "251/140/bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best"},
        {"name": "visionos_vr", "clients": ["visionos", "android_vr"], "use_cookies": False, "fmt": "bestaudio/best"},
        {"name": "cookies_fallback", "clients": None, "use_cookies": True, "fmt": "bestaudio/best"},
    ]

    permanent_unavailable = False
    last_err = ""

    for attempt, prof in enumerate(profiles, start=1):
        prof_name = prof["name"]
        if prof["use_cookies"] and not Path("cookies.txt").exists():
            continue

        if attempt > 1:
            ui_status("↻", f"{track_label} — повтор ({prof_name})...", _ANSI_YELLOW, step="yt_dlp")

        out_tmpl = str(TEMP_AUDIO_DIR / f"{video_id}.%(ext)s")
        ydl_opts = {
            "format": prof["fmt"],
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": False,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "nocheckcertificate": True,
            "geo_bypass": True,
        }

        if prof["clients"]:
            ydl_opts["extractor_args"] = {"youtube": {"player_client": prof["clients"]}}

        if prof["use_cookies"]:
            ydl_opts["cookiefile"] = "cookies.txt"

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            for f in TEMP_AUDIO_DIR.glob(f"{video_id}.*"):
                if f.is_file() and f.stat().st_size > 10_000 and not f.name.endswith(".part"):
                    return f
            last_err = "Файл не найден или пуст после yt-dlp"
        except Exception as e:
            err_clean = _compact_err(e, 180)
            last_err = f"[{prof_name}] {err_clean}"
            _write_file_log("WARN", f"[{video_id}] ({track_label}) попытка {attempt} {last_err}", step="yt_dlp")
            lower_err = err_clean.lower()
            if (
                "video unavailable" in lower_err
                or "this video is not available" in lower_err
                or "private video" in lower_err
                or "removed by the uploader" in lower_err
                or "age-restricted" in lower_err
                or "sign in to confirm your age" in lower_err
            ):
                permanent_unavailable = True
            time.sleep(1.0 * attempt)

    if permanent_unavailable and unavailable_ids is not None:
        unavailable_ids.add(video_id)
        save_unavailable_ids(unavailable_ids)
        ui_line("×", f"Пропущен (недоступен на YouTube): {track_label} [{video_id}]", _ANSI_DIM, step="yt_dlp", log_level="ERROR")
    else:
        ui_line("!", f"Ошибка скачивания: {track_label} [{video_id}] — {last_err}", _ANSI_RED, step="yt_dlp", log_level="ERROR")

    return None


def parse_duration_sec(dur_str: str) -> int:
    if not dur_str or ":" not in str(dur_str):
        return 210
    parts = [int(p) for p in str(dur_str).split(":") if p.isdigit()]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 210


def extract_track_meta(raw_item: dict, source: str) -> dict | None:
    vid = raw_item.get("videoId")
    if not vid:
        return None
    title = raw_item.get("title") or "Без названия"
    artists = raw_item.get("artists") or []
    artist_name = ", ".join(a.get("name", "") for a in artists if a.get("name")) or "Неизвестный исполнитель"
    thumbs = raw_item.get("thumbnails") or []
    cover = thumbs[-1].get("url", "") if thumbs else ""
    dur = raw_item.get("duration") or raw_item.get("length") or "3:30"

    return {
        "videoId": vid,
        "title": title,
        "artist": artist_name,
        "duration": dur,
        "seconds": parse_duration_sec(dur),
        "moods": ["Весёлая"],
        "source": source,
        "liked": source == "playlist",
        "image": cover
    }


def fetch_wave_200(yt: YTMusic, liked_seeds: list[str], exclude_vids: set[str]) -> list[dict]:
    wave_items = []
    seen = set(exclude_vids)

    for seed_vid in liked_seeds[:20]:
        if len(wave_items) >= WAVE_TARGET_COUNT:
            break
        try:
            watch = yt.get_watch_playlist(videoId=seed_vid, limit=30)
            for item in watch.get("tracks", []):
                meta = extract_track_meta(item, source="wave")
                if meta and meta["videoId"] not in seen:
                    seen.add(meta["videoId"])
                    wave_items.append(meta)
                    if len(wave_items) >= WAVE_TARGET_COUNT:
                        break
        except Exception as e:
            ui_line("!", f"Волна: сбой подбора от seed {seed_vid} ({_compact_err(e, 90)})", _ANSI_YELLOW, step="wave_fetch", log_level="WARN")

    return wave_items[:WAVE_TARGET_COUNT]


def save_playlist_json(tracks: list[dict]):
    PLAYLIST_JSON_PATH.write_text(
        json.dumps(tracks, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def _format_track_name(track: dict, max_len: int = 44) -> str:
    artist = (track.get("artist") or "").strip()
    title = (track.get("title") or "Без названия").strip()
    label = f"{artist} — {title}" if artist else title
    return label if len(label) <= max_len else label[: max_len - 3] + "..."


def main():
    parser = argparse.ArgumentParser(description="Nikrid Radio Sync (Яндекс Диск + обложки 512x512 WebP)")
    parser.add_argument("--limit", type=int, default=0, help="Ограничить число скачиваемых/мигрируемых аудио за запуск (например --limit 10)")
    parser.add_argument("--covers-only", action="store_true", help="Скачать/сжать только локальные обложки 512x512 в covers/")
    parser.add_argument("--skip-yt-sync", action="store_true", help="Не запрашивать новые треки из YouTube Music")
    args = parser.parse_args()

    start_ts = time.time()
    stats = {
        "migrated_ok": 0,
        "migrated_linked": 0,
        "new_ok": 0,
        "errors": 0,
        "covers_ok": 0,
    }

    ui_section("Nikrid Radio Sync")
    _write_file_log("INFO", f"=== Старт синхронизации (args={vars(args)}) ===", step="init")

    try:
        yadisk_token = load_yadisk_token()
        ensure_yadisk_folder(yadisk_token)
        existing_yadisk_files = fetch_existing_yadisk_files(yadisk_token)
        ui_line("✓", f"Яндекс Диск готов ({YADISK_ROOT_DIR}, файлов: {len(existing_yadisk_files)})", _ANSI_GREEN, step="init")
    except Exception as e:
        ui_line("✕", f"Критическая ошибка Яндекс Диска: {_compact_err(e, 160)}", _ANSI_RED, step="yadisk_auth", log_level="ERROR")
        sys.exit(1)

    # 1. Загружаем текущий playlist.json
    existing_tracks = []
    if PLAYLIST_JSON_PATH.exists():
        try:
            existing_tracks = json.loads(PLAYLIST_JSON_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            ui_line("!", f"Ошибка чтения {PLAYLIST_JSON_PATH} ({_compact_err(e, 100)}) — начат пустой список", _ANSI_YELLOW, step="playlist_load", log_level="WARN")
            existing_tracks = []

    # 2. Подтягиваем облачный буфер лайков/дизлайков/прослушиваний
    cloud = load_cloud_sync_state()
    disliked_ids = set(cloud.get("dislikeIds", []))
    liked_ids = set(cloud.get("likeIds", []))
    wave_listened_ids = set(cloud.get("waveListenedIds", []))

    disliked_vids = set()
    cleaned_tracks = []
    removed_count = 0
    for t in existing_tracks:
        if "source" not in t:
            t["source"] = "playlist"
        if "liked" not in t:
            t["liked"] = t["source"] == "playlist"

        tid = t.get("id")
        vid = t.get("videoId", "")
        is_liked = (tid in liked_ids) or bool(t.get("liked")) or (t.get("source") == "playlist")
        is_listened_wave = (t.get("source") == "wave") and (tid in wave_listened_ids)

        if tid in disliked_ids or (is_listened_wave and not is_liked):
            removed_count += 1
            if vid:
                disliked_vids.add(vid)
                delete_local_cover(vid)
            if t.get("yadiskPath"):
                delete_from_yadisk(yadisk_token, t["yadiskPath"])
            reason = "дизлайк" if tid in disliked_ids else "прослушан в волне"
            _write_file_log("INFO", f"Удалён трек ({reason}): {t.get('title')} [{vid}]", step="cleanup")
            continue

        if tid in liked_ids:
            t["liked"] = True
            if t.get("source") != "wave":
                t["source"] = "playlist"
        cleaned_tracks.append(t)

    existing_tracks = cleaned_tracks
    if removed_count > 0:
        ui_line("•", f"Очищено по буферу/волне: {removed_count} трек(ов)", _ANSI_DIM, step="cleanup")

    by_vid = {t.get("videoId"): t for t in existing_tracks if t.get("videoId")}
    next_numeric_id = max((t.get("id", 0) for t in existing_tracks), default=0) + 1
    unavailable_ids = load_unavailable_ids()

    # 3. Локальные обложки 512x512 WebP в covers/
    ui_section("Обложки (512×512 WebP)")
    total_tracks_for_covers = len(existing_tracks)
    covers_missing = [
        t for t in existing_tracks
        if t.get("videoId") and not (t.get("image") == f"covers/{t['videoId']}.webp" and (COVERS_DIR / f"{t['videoId']}.webp").exists())
    ]

    if not covers_missing:
        ui_line("✓", f"Все обложки на месте ({total_tracks_for_covers} треков)", _ANSI_GREEN, step="covers")
    else:
        for idx, t in enumerate(existing_tracks, start=1):
            vid = t.get("videoId", "")
            if not vid:
                continue
            expected_rel = f"covers/{vid}.webp"
            if t.get("image") == expected_rel and (COVERS_DIR / f"{vid}.webp").exists():
                ui_progress("Обложки", idx, total_tracks_for_covers, _format_track_name(t, 34))
                continue

            ui_progress("Обложки", idx, total_tracks_for_covers, _format_track_name(t, 34))
            local_cover = ensure_local_cover(vid, t.get("image", ""))
            if local_cover:
                t["image"] = local_cover
                stats["covers_ok"] += 1
                if stats["covers_ok"] % 25 == 0:
                    save_playlist_json(existing_tracks)

        ui_clear_inline()
        save_playlist_json(existing_tracks)
        ui_line("✓", f"Обложки готовы: скачано/сжато {stats['covers_ok']} шт.", _ANSI_GREEN, step="covers")

    if args.covers_only:
        elapsed = time.time() - start_ts
        ui_section("Итог")
        ui_line("✓", f"Режим --covers-only завершён за {elapsed:.1f} с", _ANSI_GREEN, step="summary")
        return

    # 4. Миграция существующих треков из playlist.json на Яндекс Диск
    need_migrate = [t for t in existing_tracks if t.get("videoId") and not t.get("yadiskPublicUrl")]
    audio_ops_done = 0
    saved_batch = 0

    if need_migrate:
        ui_section(f"Миграция аудио на Яндекс Диск (осталось: {len(need_migrate)})")
        for idx, t in enumerate(need_migrate, start=1):
            if args.limit > 0 and audio_ops_done >= args.limit:
                ui_line("•", f"Остановлено по лимиту --limit {args.limit}", _ANSI_CYAN, step="migrate")
                break

            vid = t["videoId"]
            short_name = _format_track_name(t, 38)

            if vid in existing_yadisk_files:
                remote_info = existing_yadisk_files[vid]
                remote_path = remote_info["path"]
                pub_url = remote_info["public_url"]
                try:
                    if not pub_url:
                        pub_url = publish_yadisk_resource(yadisk_token, remote_path)
                    t["yadiskPath"] = remote_path
                    t["yadiskPublicUrl"] = pub_url
                    t.pop("driveId", None)
                    t.pop("audioUrl", None)
                    stats["migrated_linked"] += 1
                    saved_batch += 1
                    ui_progress("Миграция", idx, len(need_migrate), f"связан: {short_name}")
                    if saved_batch >= 5:
                        save_playlist_json(existing_tracks)
                        saved_batch = 0
                    continue
                except Exception as e:
                    stats["errors"] += 1
                    ui_line("!", f"Публикация {short_name} [{vid}] не удалась: {_compact_err(e, 110)}", _ANSI_YELLOW, step="migrate", log_level="ERROR")

            ui_status("↓", f"[{idx}/{len(need_migrate)}] Качаю: {short_name}", _ANSI_CYAN, step="migrate")
            local_file = download_audio_robust(vid, unavailable_ids, track_label=f"{short_name}")
            if not local_file:
                stats["errors"] += 1
                continue

            try:
                ui_status("↑", f"[{idx}/{len(need_migrate)}] Заливаю на Диск: {short_name}", _ANSI_CYAN, step="migrate")
                remote_path, pub_url = upload_and_publish_yadisk(yadisk_token, local_file, vid)
                t["yadiskPath"] = remote_path
                t["yadiskPublicUrl"] = pub_url
                t.pop("driveId", None)
                t.pop("audioUrl", None)
                existing_yadisk_files[vid] = {"path": remote_path, "public_url": pub_url}
                audio_ops_done += 1
                stats["migrated_ok"] += 1
                saved_batch += 1
                ui_progress("Миграция", idx, len(need_migrate), f"✓ {short_name}")
                if saved_batch >= 5:
                    save_playlist_json(existing_tracks)
                    saved_batch = 0
            except Exception as e:
                stats["errors"] += 1
                ui_line("!", f"Ошибка Яндекс Диска для {short_name} [{vid}]: {_compact_err(e, 120)}", _ANSI_RED, step="migrate", log_level="ERROR")
            finally:
                try:
                    if local_file.exists():
                        local_file.unlink()
                except Exception:
                    pass

        ui_clear_inline()
        save_playlist_json(existing_tracks)

    # 5. Синхронизация новых треков из YouTube Music
    if not args.skip_yt_sync and (args.limit == 0 or audio_ops_done < args.limit):
        ui_section("Синхронизация с YouTube Music")
        try:
            yt = get_ytmusic_client()
            ui_status("↻", "Запрашиваю лайкнутые треки...", _ANSI_CYAN, step="yt_sync")
            yt_liked_raw = yt.get_liked_songs(limit=1000).get("tracks", [])
            liked_metas = []
            liked_vids = []
            for item in yt_liked_raw:
                meta = extract_track_meta(item, source="playlist")
                if meta and meta["videoId"] not in disliked_vids:
                    liked_metas.append(meta)
                    liked_vids.append(meta["videoId"])

            current_wave_total = [t for t in existing_tracks if t.get("source") == "wave"]
            need_wave = max(0, WAVE_TARGET_COUNT - len(current_wave_total))
            wave_metas = []
            if need_wave > 0:
                ui_status("↻", f"Подбираю «Мою волну» ({need_wave} треков)...", _ANSI_CYAN, step="yt_sync")
                exclude_set = set(liked_vids) | disliked_vids | set(by_vid.keys())
                wave_metas = fetch_wave_200(yt, liked_vids, exclude_set)[:need_wave]

            ui_clear_inline()
            ui_line("✓", f"YTMusic: лайков {len(liked_metas)}, новых в волну {len(wave_metas)}", _ANSI_GREEN, step="yt_sync")

            to_process = liked_metas + wave_metas
            saved_batch = 0

            for idx, meta in enumerate(to_process, start=1):
                if args.limit > 0 and audio_ops_done >= args.limit:
                    ui_line("•", f"Остановлено по лимиту --limit {args.limit}", _ANSI_CYAN, step="yt_sync")
                    break

                vid = meta["videoId"]
                short_name = _format_track_name(meta, 38)

                if vid in by_vid:
                    if meta["source"] == "playlist":
                        by_vid[vid]["source"] = "playlist"
                        by_vid[vid]["liked"] = True
                    by_vid[vid]["moods"] = ["Весёлая"]
                    ui_progress("YTMusic", idx, len(to_process), f"актуален: {short_name}")
                    continue

                ui_status("↓", f"[{idx}/{len(to_process)}] Качаю ({meta['source']}): {short_name}", _ANSI_CYAN, step="yt_sync")
                local_file = download_audio_robust(vid, unavailable_ids, track_label=short_name)
                if not local_file:
                    stats["errors"] += 1
                    continue

                try:
                    ui_status("↑", f"[{idx}/{len(to_process)}] Заливаю на Диск: {short_name}", _ANSI_CYAN, step="yt_sync")
                    remote_path, pub_url = upload_and_publish_yadisk(yadisk_token, local_file, vid)
                    local_cover = ensure_local_cover(vid, meta.get("image", ""))
                    if local_cover:
                        stats["covers_ok"] += 1
                    new_entry = {
                        "id": next_numeric_id,
                        "videoId": vid,
                        "yadiskPath": remote_path,
                        "yadiskPublicUrl": pub_url,
                        "title": meta["title"],
                        "artist": meta["artist"],
                        "duration": meta["duration"],
                        "seconds": meta["seconds"],
                        "moods": ["Весёлая"],
                        "source": meta["source"],
                        "liked": meta["liked"],
                        "image": local_cover
                    }
                    next_numeric_id += 1
                    existing_tracks.append(new_entry)
                    by_vid[vid] = new_entry
                    audio_ops_done += 1
                    stats["new_ok"] += 1
                    saved_batch += 1
                    ui_progress("YTMusic", idx, len(to_process), f"✓ {short_name}")

                    if saved_batch >= 5:
                        save_playlist_json(existing_tracks)
                        saved_batch = 0
                except Exception as e:
                    stats["errors"] += 1
                    ui_line("!", f"Ошибка загрузки {short_name} [{vid}]: {_compact_err(e, 120)}", _ANSI_RED, step="yt_sync", log_level="ERROR")
                finally:
                    try:
                        if local_file.exists():
                            local_file.unlink()
                    except Exception:
                        pass

            ui_clear_inline()
        except Exception as e:
            ui_line("!", f"Сбой авторизации/запроса YouTube Music: {_compact_err(e, 140)}", _ANSI_RED, step="yt_sync", log_level="ERROR")

    save_playlist_json(existing_tracks)
    shutil.rmtree(TEMP_AUDIO_DIR, ignore_errors=True)

    elapsed = time.time() - start_ts
    migrated_count = sum(1 for t in existing_tracks if t.get("yadiskPublicUrl"))

    ui_section("Итог")
    ui_line("✓", f"Время: {elapsed:.1f} с | Всего в playlist.json: {len(existing_tracks)} (на Яндекс Диске: {migrated_count})", _ANSI_GREEN, step="summary")
    ui_line("•", f"Загружено новых: {stats['new_ok']}, мигрировано: {stats['migrated_ok']}, связано готовых: {stats['migrated_linked']}, обложек: {stats['covers_ok']}", _ANSI_DIM, step="summary")
    if stats["errors"] > 0:
        ui_line("!", f"Ошибок/пропусков: {stats['errors']} — подробности в {FAILED_LOG_PATH}", _ANSI_YELLOW, step="summary", log_level="INFO")
    _write_file_log("INFO", f"=== Завершено за {elapsed:.1f} с (total={len(existing_tracks)}, on_disk={migrated_count}, errors={stats['errors']}) ===", step="summary")


if __name__ == "__main__":
    main()
