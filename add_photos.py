#!/usr/bin/env python3
"""Добавляет фотографии в альбом: уменьшает, шифрует паролем и публикует.

Использование:
    ./add_photos.sh ~/Desktop/фотки            # папка или отдельные файлы
    ./add_photos.sh --no-push фото1.jpg фото2.heic
    ./add_photos.sh --set-password             # первая настройка / смена пароля

Оригиналы копируются в originals/ (в git не попадают). На сайт уходят только
зашифрованные копии: без пароля их невозможно открыть, даже скачав из GitHub.
"""
import argparse
import base64
import getpass
import hashlib
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parent
ORIGINALS = ROOT / "originals"
DATA = ROOT / "data"
CONFIG = DATA / "config.json"
MANIFEST = DATA / "manifest.bin"
PASSWORD_FILE = ROOT / ".password"  # локально, в .gitignore

ITERATIONS = 250_000
FULL_SIZE = 2400
THUMB_SIZE = 700
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff"}


def derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS, 32)


def encrypt(key: bytes, data: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, data, None)


def decrypt(key: bytes, blob: bytes) -> bytes:
    return AESGCM(key).decrypt(blob[:12], blob[12:], None)


def load_password() -> str:
    if PASSWORD_FILE.exists():
        return PASSWORD_FILE.read_text(encoding="utf-8").strip()
    return getpass.getpass("Пароль альбома: ")


def load_manifest(key: bytes) -> list:
    if not MANIFEST.exists():
        return []
    return json.loads(decrypt(key, MANIFEST.read_bytes()))


def save_manifest(key: bytes, items: list) -> None:
    items.sort(key=lambda p: (p.get("taken") or "", p["id"]))
    MANIFEST.write_bytes(encrypt(key, json.dumps(items, ensure_ascii=False).encode("utf-8")))


def sips_props(path: Path) -> dict:
    out = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", "-g", "creation", str(path)],
        capture_output=True, text=True,
    ).stdout
    props = {}
    for line in out.splitlines()[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            props[k.strip()] = v.strip()
    return props


def render_jpeg(src: Path, dst: Path, max_side: int, quality: int) -> tuple:
    subprocess.run(
        ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(quality),
         "-Z", str(max_side), str(src), "--out", str(dst)],
        check=True, capture_output=True,
    )
    p = sips_props(dst)
    return int(p["pixelWidth"]), int(p["pixelHeight"])


def taken_at(src: Path, fallback_date: str = None) -> str:
    creation = sips_props(src).get("creation")  # "2026:10:17 14:03:22"
    if creation and creation != "<nil>":
        try:
            return datetime.strptime(creation, "%Y:%m:%d %H:%M:%S").isoformat()
        except ValueError:
            pass
    mtime = datetime.fromtimestamp(src.stat().st_mtime).replace(microsecond=0)
    # дата в имени файла: 17.10.26, 17.10.2026 или 2026-10-17
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})(?!\d)", src.stem)
    if m:
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
        y = y + 2000 if y < 100 else y
        return f"{y:04d}-{mo:02d}-{d:02d}T{mtime.time().isoformat()}"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", src.stem)
    if m:
        return f"{m[1]}-{m[2]}-{m[3]}T{mtime.time().isoformat()}"
    if fallback_date:
        return f"{fallback_date}T{mtime.time().isoformat()}"
    return mtime.isoformat()


def collect(paths: list) -> list:
    files = []
    for p in map(Path, paths):
        p = p.expanduser()
        if p.is_dir():
            files += sorted(f for f in p.rglob("*") if f.suffix.lower() in IMAGE_EXT)
        elif p.suffix.lower() in IMAGE_EXT:
            files.append(p)
        else:
            print(f"  пропускаю {p.name}: не фото")
    return files


def set_password() -> None:
    old_key = None
    items = []
    if CONFIG.exists() and MANIFEST.exists():
        cfg = json.loads(CONFIG.read_text())
        old = load_password()
        old_key = derive_key(old, base64.b64decode(cfg["salt"]))
        items = load_manifest(old_key)
    new = os.environ.get("ALBUM_NEW_PASSWORD")
    if not new:
        new = getpass.getpass("Новый пароль: ")
        if new != getpass.getpass("Ещё раз: "):
            sys.exit("Пароли не совпали.")
    salt = os.urandom(16)
    key = derive_key(new, salt)
    for item in items:  # перешифровываем уже опубликованное
        for kind in ("full", "thumb"):
            f = DATA / kind / f"{item['id']}.bin"
            f.write_bytes(encrypt(key, decrypt(old_key, f.read_bytes())))
    DATA.mkdir(exist_ok=True)
    CONFIG.write_text(json.dumps({"salt": base64.b64encode(salt).decode(), "iterations": ITERATIONS}))
    save_manifest(key, items)
    PASSWORD_FILE.write_text(new, encoding="utf-8")
    print("Пароль установлен.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--no-push", action="store_true", help="не публиковать на сайт")
    ap.add_argument("--set-password", action="store_true")
    ap.add_argument("--date", help="дата для фото без даты съёмки, например 2026-10-17")
    args = ap.parse_args()

    if args.set_password:
        set_password()
        if not args.paths:
            return
    if not CONFIG.exists():
        sys.exit("Сначала задай пароль: ./add_photos.sh --set-password")
    if not args.paths:
        ap.print_help()
        return

    cfg = json.loads(CONFIG.read_text())
    key = derive_key(load_password(), base64.b64decode(cfg["salt"]))
    try:
        items = load_manifest(key)
    except Exception:
        sys.exit("Неверный пароль.")

    known = {i["sha"] for i in items}
    (DATA / "full").mkdir(parents=True, exist_ok=True)
    (DATA / "thumb").mkdir(parents=True, exist_ok=True)
    ORIGINALS.mkdir(exist_ok=True)

    added = 0
    with tempfile.TemporaryDirectory() as tmp:
        for src in collect(args.paths):
            sha = hashlib.sha256(src.read_bytes()).hexdigest()
            if sha in known:
                print(f"  уже есть: {src.name}")
                continue
            pid = sha[:16]
            full, thumb = Path(tmp) / "full.jpg", Path(tmp) / "thumb.jpg"
            w, h = render_jpeg(src, full, FULL_SIZE, 85)
            render_jpeg(src, thumb, THUMB_SIZE, 75)
            (DATA / "full" / f"{pid}.bin").write_bytes(encrypt(key, full.read_bytes()))
            (DATA / "thumb" / f"{pid}.bin").write_bytes(encrypt(key, thumb.read_bytes()))
            shutil.copy2(src, ORIGINALS / f"{pid}{src.suffix.lower()}")
            items.append({"id": pid, "sha": sha, "w": w, "h": h, "taken": taken_at(src, args.date), "name": src.name})
            known.add(sha)
            added += 1
            print(f"  + {src.name}")

    if not added:
        print("Новых фото нет.")
        return
    save_manifest(key, items)
    print(f"Добавлено фото: {added}, всего в альбоме: {len(items)}")

    if not args.no_push:
        subprocess.run(["git", "add", "data"], cwd=ROOT, check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"Добавлено фото: {added}"], cwd=ROOT, check=True)
        subprocess.run(["git", "push", "-q"], cwd=ROOT, check=True)
        print("Опубликовано. Сайт обновится через минуту-две.")


if __name__ == "__main__":
    main()
