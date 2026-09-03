from __future__ import annotations

import base64
import io
import json
import lzma
import os
import re
import shutil
import subprocess
import tempfile
import struct
import sys
import threading
import uuid
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable

from Cryptodome.Cipher import AES
from Cryptodome.Util.strxor import strxor
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, USLT
from mutagen.mp3 import MP3
from mutagen.oggvorbis import OggVorbis


CHUNK_SIZE = 1024 * 1024
SUPPORTED_SUFFIXES = {
    ".ncm", ".kgm", ".kgma", ".vpr", ".mflac", ".mflac0", ".mflach",
    ".mgg", ".mgg0", ".mgg1", ".mggl", ".qmc0", ".qmc2", ".qmc3",
    ".qmc4", ".qmc5", ".qmc6", ".qmc7", ".qmc8", ".qmcflac", ".qmcogg",
    ".tkm", ".kwm", ".kgg", ".x2m", ".x3m", ".mg3d", ".xm",
}
EXPERIMENTAL_SUFFIXES = SUPPORTED_SUFFIXES - {".ncm", ".kgm", ".kgma", ".vpr", ".mflac", ".mgg"}
NCM_CORE_KEY = bytes.fromhex("687a4852416d736f356b496e62617857")
NCM_META_KEY = bytes.fromhex("2331346c6a6b5f215c5d2630553c2728")
NCM_MAGIC = b"CTENFDAM"
KGM_MAGIC = bytes(
    [
        0x7C, 0xD5, 0x32, 0xEB, 0x86, 0x02, 0x7F, 0x4B, 0xA8, 0xAF, 0xA6, 0x8E,
        0x0F, 0xFF, 0x99, 0x14, 0x00, 0x04, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00,
        0x01, 0x00, 0x00, 0x00,
    ]
)
KGM_MEND = bytes(
    [
        0xB8, 0xD5, 0x3D, 0xB2, 0xE9, 0xAF, 0x78, 0x8C, 0x83, 0x33, 0x71, 0x51, 0x76, 0xA0, 0xCD, 0x37,
        0x2F, 0x3E, 0x35, 0x8D, 0xA9, 0xBE, 0x98, 0xB7, 0xE7, 0x8C, 0x22, 0xCE, 0x5A, 0x61, 0xDF, 0x68,
        0x69, 0x89, 0xFE, 0xA5, 0xB6, 0xDE, 0xA9, 0x77, 0xFC, 0xC8, 0xBD, 0xBD, 0xE5, 0x6D, 0x3E, 0x5A,
        0x36, 0xEF, 0x69, 0x4E, 0xBE, 0xE1, 0xE9, 0x66, 0x1C, 0xF3, 0xD9, 0x02, 0xB6, 0xF2, 0x12, 0x9B,
        0x44, 0xD0, 0x6F, 0xB9, 0x35, 0x89, 0xB6, 0x46, 0x6D, 0x73, 0x82, 0x06, 0x69, 0xC1, 0xED, 0xD7,
        0x85, 0xC2, 0x30, 0xDF, 0xA2, 0x62, 0xBE, 0x79, 0x2D, 0x62, 0x62, 0x3D, 0x0D, 0x7E, 0xBE, 0x48,
        0x89, 0x23, 0x02, 0xA0, 0xE4, 0xD5, 0x75, 0x51, 0x32, 0x02, 0x53, 0xFD, 0x16, 0x3A, 0x21, 0x3B,
        0x16, 0x0F, 0xC3, 0xB2, 0xBB, 0xB3, 0xE2, 0xBA, 0x3A, 0x3D, 0x13, 0xEC, 0xF6, 0x01, 0x45, 0x84,
        0xA5, 0x70, 0x0F, 0x93, 0x49, 0x0C, 0x64, 0xCD, 0x31, 0xD5, 0xCC, 0x4C, 0x07, 0x01, 0x9E, 0x00,
        0x1A, 0x23, 0x90, 0xBF, 0x88, 0x1E, 0x3B, 0xAB, 0xA6, 0x3E, 0xC4, 0x73, 0x47, 0x10, 0x7E, 0x3B,
        0x5E, 0xBC, 0xE3, 0x00, 0x84, 0xFF, 0x09, 0xD4, 0xE0, 0x89, 0x0F, 0x5B, 0x58, 0x70, 0x4F, 0xFB,
        0x65, 0xD8, 0x5C, 0x53, 0x1B, 0xD3, 0xC8, 0xC6, 0xBF, 0xEF, 0x98, 0xB0, 0x50, 0x4F, 0x0F, 0xEA,
        0xE5, 0x83, 0x58, 0x8C, 0x28, 0x2C, 0x84, 0x67, 0xCD, 0xD0, 0x9E, 0x47, 0xDB, 0x27, 0x50, 0xCA,
        0xF4, 0x63, 0x63, 0xE8, 0x97, 0x7F, 0x1B, 0x4B, 0x0C, 0xC2, 0xC1, 0x21, 0x4C, 0xCC, 0x58, 0xF5,
        0x94, 0x52, 0xA3, 0xF3, 0xD3, 0xE0, 0x68, 0xF4, 0x00, 0x23, 0xF3, 0x5E, 0x0A, 0x7B, 0x93, 0xDD,
        0xAB, 0x12, 0xB2, 0x13, 0xE8, 0x84, 0xD7, 0xA7, 0x9F, 0x0F, 0x32, 0x4C, 0x55, 0x1D, 0x04, 0x36,
        0x52, 0xDC, 0x03, 0xF3, 0xF9, 0x4E, 0x42, 0xE9, 0x3D, 0x61, 0xEF, 0x7C, 0xB6, 0xB3, 0x93, 0x50,
    ]
)
VPR_MASK_DIFF = bytes([0x25, 0xDF, 0xE8, 0xA7, 0x5E, 0x2C, 0x0D, 0x59, 0x77, 0x1F, 0xF8, 0xC1, 0x22, 0xA6, 0xD8, 0x6D, 0x7D])

Progress = Callable[[int, int], None]
_output_lock = threading.Lock()
_qq_lock = threading.Lock()
_disk_write_lock = threading.Lock()
_nibble_table = bytes(value ^ ((value & 0x0F) << 4) for value in range(256))


class DecodeError(RuntimeError):
    pass


class Cancelled(DecodeError):
    pass


@dataclass(frozen=True)
class DecodeResult:
    source: Path
    target: Path | None
    service: str
    audio_format: str | None
    status: str
    message: str
    lyrics_written: bool = False
    cover_written: bool = False
    output_format: str | None = None


@dataclass(frozen=True)
class AudioPayload:
    audio_format: str
    metadata: dict | None = None
    cover: bytes = b""


@dataclass(frozen=True)
class FormatInfo:
    suffix: str
    service: str
    version: str
    status: str
    note: str = ""


def _suffix(path: Path) -> str:
    name = path.name.lower()
    for value in sorted(SUPPORTED_SUFFIXES, key=len, reverse=True):
        if name.endswith(value):
            return value
    return path.suffix.lower()


def parse_qmc_tail(data: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    for marker in (b"QTag", b"STag", b"MusicEx"):
        if marker in data:
            result[marker.decode("ascii")] = "present"
    match = re.search(rb"media[_-]?mid[^A-Za-z0-9]{0,8}([A-Za-z0-9_-]{8,64})", data, re.I)
    if match:
        result["media_mid"] = match.group(1).decode("ascii", "ignore")
    return result


def detect_format(path: Path) -> FormatInfo:
    suffix = _suffix(path)
    if suffix == ".ncm":
        return FormatInfo(suffix, "网易云音乐", "NCM", "supported")
    if suffix in {".kgm", ".kgma", ".vpr"}:
        return FormatInfo(suffix, "酷狗音乐", "KGM", "supported")
    if suffix in {".mflac", ".mgg"}:
        return FormatInfo(suffix, "QQ音乐", "MusicEx/客户端接口", "supported", "需要已登录 QQ 音乐客户端")
    if suffix in {".mflac0", ".mflach", ".mgg0", ".mgg1", ".mggl"}:
        return FormatInfo(suffix, "QQ音乐", "QMC2", "experimental", "将解析 QTag/STag/MusicEx；当前无可验证样本，未启用伪解码")
    if suffix in {".qmc0", ".qmc2", ".qmc3", ".qmc4", ".qmc5", ".qmc6", ".qmc7", ".qmc8", ".qmcflac", ".qmcogg", ".tkm"}:
        return FormatInfo(suffix, "QQ音乐", "QMC1", "experimental", "已识别老 QMC；尚未加入未经样本验证的密钥算法")
    if suffix == ".kwm":
        return FormatInfo(suffix, "酷我音乐", "KWM", "experimental", "格式已登记，等待可靠离线实现与样本验证")
    if suffix == ".kgg":
        return FormatInfo(suffix, "酷狗音乐", "KGG/KGM v5", "experimental", "可能需要客户端 Key，当前不伪造解码")
    if suffix in {".x2m", ".x3m"}:
        return FormatInfo(suffix, "喜马拉雅", "X2M/X3M", "experimental")
    if suffix == ".mg3d":
        return FormatInfo(suffix, "咪咕音乐", "MG3D", "experimental")
    if suffix == ".xm":
        return FormatInfo(suffix, "虾米音乐", "XM", "experimental")
    return FormatInfo(suffix, "未知", "未知", "unsupported")


class KeyCache:
    def __init__(self, path: Path | None = None):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "YinChe"
        self.path = path or base / "key-cache.json"
        self._lock = threading.Lock()

    def get(self, *identifiers: str) -> str | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        for identifier in identifiers:
            if identifier and data.get(identifier):
                return str(data[identifier])
        return None

    def put(self, ekey: str, *identifiers: str) -> None:
        if not ekey or not identifiers:
            return
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                try: data = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, ValueError): data = {}
                for identifier in identifiers:
                    if identifier: data[str(identifier)] = ekey
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temporary, self.path)
        except OSError:
            return


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def detect_audio_format(header: bytes) -> str | None:
    if header.startswith(b"fLaC"):
        return "flac"
    if header.startswith(b"OggS"):
        return "ogg"
    if header.startswith(b"ID3"):
        return "mp3"
    if len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0:
        return "mp3"
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "wav"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "m4a"
    return None


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def transcode_audio(data: bytes, source_format: str, target_format: str, bitrate: str = "auto", bit_depth: str = "auto") -> bytes:
    executable = ffmpeg_path()
    if not executable:
        raise DecodeError("选择 MP3/FLAC 转码需要 FFmpeg，请将 ffmpeg.exe 加入 PATH 后重试")
    source_suffix = "." + source_format
    target_suffix = "." + target_format
    with tempfile.TemporaryDirectory(prefix="yinche-convert-") as folder:
        folder_path = Path(folder)
        source_path = folder_path / ("input" + source_suffix)
        target_path = folder_path / ("output" + target_suffix)
        source_path.write_bytes(data)
        command = [executable, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source_path)]
        if target_format == "mp3":
            command += ["-codec:a", "libmp3lame"]
            if bitrate != "auto": command += ["-b:a", bitrate]
        elif target_format == "flac":
            command += ["-codec:a", "flac"]
            if bit_depth != "auto": command += ["-sample_fmt", {"16": "s16", "24": "s32", "32": "s32"}[bit_depth]]
        command.append(str(target_path))
        run_kwargs = {"capture_output": True, "timeout": 300}
        if os.name == "nt":
            # FFmpeg is a background worker; do not flash a console window per file.
            run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        completed = subprocess.run(command, **run_kwargs)
        if completed.returncode != 0 or not target_path.exists():
            detail = completed.stderr.decode("utf-8", "replace").strip()
            raise DecodeError(f"FFmpeg 转码失败{(': ' + detail) if detail else ''}")
        return target_path.read_bytes()


def service_for(path: Path) -> str:
    suffix = _suffix(path)
    if suffix == ".ncm":
        return "网易云音乐"
    if suffix in {".kgm", ".kgma", ".vpr"}:
        return "酷狗音乐"
    if suffix in {".mflac", ".mgg"}:
        return "QQ音乐"
    if suffix in {".qmc0", ".qmc2", ".qmc3", ".qmc4", ".qmc5", ".qmc6", ".qmc7", ".qmc8", ".qmcflac", ".qmcogg", ".tkm"}:
        return "QQ音乐"
    if suffix in {".mflac0", ".mflach", ".mgg0", ".mgg1", ".mggl"}:
        return "QQ音乐"
    if suffix in {".kwm"}:
        return "酷我音乐"
    if suffix in {".kgg"}:
        return "酷狗音乐"
    if suffix in {".x2m", ".x3m"}:
        return "喜马拉雅"
    if suffix == ".mg3d":
        return "咪咕音乐"
    if suffix == ".xm":
        return "虾米音乐"
    return "未知"


def _read_exact(stream, size: int) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise DecodeError("文件意外结束")
    return data


def _read_u32(stream) -> int:
    return struct.unpack("<I", _read_exact(stream, 4))[0]


def _aes_decrypt(key: bytes, data: bytes) -> bytes:
    if not data or len(data) % AES.block_size:
        raise DecodeError("NCM AES 数据长度无效")
    decoded = AES.new(key, AES.MODE_ECB).decrypt(data)
    padding = decoded[-1]
    return decoded[:-padding] if padding <= AES.block_size else decoded


def _ncm_keystream(key: bytes) -> bytes:
    box = list(range(256))
    last = offset = 0
    for index in range(256):
        swap = box[index]
        target = (swap + last + key[offset]) & 0xFF
        offset = (offset + 1) % len(key)
        box[index], box[target], last = box[target], swap, target
    cycle = bytearray(256)
    for index, position in enumerate((*range(1, 256), 0)):
        value = box[position]
        cycle[index] = box[(box[(value + position) & 0xFF] + value) & 0xFF]
    return bytes(cycle)


def _read_ncm_header(stream) -> tuple[bytes, dict | None, bytes]:
    if _read_exact(stream, 8) != NCM_MAGIC:
        raise DecodeError("不是有效的 NCM 文件")
    _read_exact(stream, 2)
    encrypted_key = bytes(value ^ 0x64 for value in _read_exact(stream, _read_u32(stream)))
    key = _aes_decrypt(NCM_CORE_KEY, encrypted_key)
    if not key.startswith(b"neteasecloudmusic"):
        raise DecodeError("NCM 音频密钥无效")
    metadata = None
    metadata_length = _read_u32(stream)
    if metadata_length:
        encrypted_meta = bytes(value ^ 0x63 for value in _read_exact(stream, metadata_length))
        try:
            metadata_raw = base64.b64decode(encrypted_meta[22:], validate=True)
            metadata = json.loads(_aes_decrypt(NCM_META_KEY, metadata_raw)[6:].decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            metadata = None
    _read_exact(stream, 5)
    cover_frame_length = _read_u32(stream)
    image_length = _read_u32(stream)
    if image_length > cover_frame_length:
        raise DecodeError("NCM 封面长度无效")
    cover = _read_exact(stream, image_length) if image_length else b""
    stream.seek(cover_frame_length - image_length, os.SEEK_CUR)
    return _ncm_keystream(key[17:]), metadata, cover


def _decode_to_memory(source: Path, cancel: Event, transform, offset: int = 0, progress: Progress | None = None) -> tuple[bytes, str]:
    total = max(1, source.stat().st_size - offset)
    processed = 0
    header = bytearray()
    output = io.BytesIO()
    with source.open("rb") as src:
        src.seek(offset)
        while chunk := src.read(CHUNK_SIZE):
            if cancel.is_set():
                raise Cancelled("任务已取消")
            decoded = transform(chunk, processed)
            if len(header) < 16:
                header.extend(decoded[: 16 - len(header)])
            output.write(decoded)
            processed += len(chunk)
            if progress:
                progress(processed, total)
    audio_format = detect_audio_format(bytes(header))
    if not audio_format:
        raise DecodeError("无法识别解密后的音频格式")
    return output.getvalue(), audio_format


def decode_ncm(source: Path, cancel: Event, progress: Progress | None = None) -> tuple[bytes, AudioPayload]:
    with source.open("rb") as stream:
        key, metadata, cover = _read_ncm_header(stream)
        offset = stream.tell()
    def transform(chunk: bytes, position: int) -> bytes:
        start = position & 0xFF
        cycles, remainder = divmod(start + len(chunk), len(key))
        mask = (key[start:] + key * max(0, cycles - 1) + key[:remainder])[:len(chunk)]
        return strxor(chunk, mask)
    audio_data, audio_format = _decode_to_memory(source, cancel, transform, offset, progress)
    expected = str((metadata or {}).get("format", "")).lower()
    if expected in {"mp3", "flac"} and expected != audio_format:
        raise DecodeError("NCM 元数据与真实音频格式不一致")
    return audio_data, AudioPayload(audio_format, metadata, cover)


_kgm_key: bytes | None = None


def _load_kgm_key() -> bytes:
    global _kgm_key
    if _kgm_key is None:
        try:
            _kgm_key = lzma.decompress(resource_path("assets/kugou_key.xz").read_bytes())
        except (OSError, lzma.LZMAError) as error:
            raise DecodeError(f"无法载入酷狗密钥：{error}") from error
    return _kgm_key


@lru_cache(maxsize=256)
def _kgm_mask(position: int, size: int, is_vpr: bool) -> bytes:
    public_key = _load_kgm_key()
    output = bytearray(size)
    for local in range(size):
        index = position + local
        public = KGM_MEND[index % len(KGM_MEND)] ^ public_key[index // 16]
        mask = _nibble_table[public]
        # The private key is file-specific and is applied separately.
        output[local] = mask ^ (VPR_MASK_DIFF[index % 17] if is_vpr else 0)
    return bytes(output)


def _repeat_key(key: bytes, position: int, size: int) -> bytes:
    offset = position % len(key)
    return (key[offset:] + key * ((size + offset) // len(key)) + key[:offset])[:size]


def decode_kgm(source: Path, cancel: Event, progress: Progress | None = None) -> tuple[bytes, AudioPayload]:
    with source.open("rb") as stream:
        header = _read_exact(stream, 1024)
    if not header.startswith(KGM_MAGIC):
        raise DecodeError("不是有效的 KGM 文件")
    own_key = header[0x1C:0x2C] + b"\x00"
    public_key = _load_kgm_key()
    is_vpr = source.suffix.lower() == ".vpr"
    def transform(chunk: bytes, position: int) -> bytes:
        transformed = chunk.translate(_nibble_table)
        private_mask = _repeat_key(bytes(_nibble_table[value] for value in own_key), position, len(chunk))
        public_mask = _kgm_mask(position, len(chunk), is_vpr)
        return strxor(strxor(transformed, private_mask), public_mask)
    audio_data, audio_format = _decode_to_memory(source, cancel, transform, 1024, progress)
    return audio_data, AudioPayload(audio_format)


def decode_qq(source: Path, cancel: Event, progress: Progress | None = None) -> tuple[bytes, AudioPayload]:
    if cancel.is_set():
        raise Cancelled("任务已取消")
    try:
        import frida
    except ImportError as error:
        raise DecodeError("QQ 解码组件未安装，请重新运行完整安装包") from error
    with _qq_lock:
        try:
            session = frida.attach("QQMusic.exe")
        except Exception as error:
            raise DecodeError("无法连接 QQ 音乐，请先启动已登录且有下载权限的客户端") from error
        try:
            script = session.create_script(resource_path("hook_qq_music.js").read_text(encoding="utf-8"))
            script.load()
            audio_data = bytes(script.exports_sync.decrypt(str(source.resolve())))
        except Exception as error:
            raise DecodeError(f"QQ 音乐解密失败，客户端版本可能不兼容：{error}") from error
        finally:
            session.detach()
    if progress:
        progress(1, 1)
    audio_format = detect_audio_format(audio_data[:16])
    if not audio_format:
        raise DecodeError("无法识别 QQ 音乐解密结果")
    return audio_data, AudioPayload(audio_format)


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _sidecar_text(source: Path) -> str:
    path = source.with_suffix(".lrc")
    if not path.exists():
        return ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding).strip()
        except UnicodeDecodeError:
            continue
    return ""


def _sidecar_cover(source: Path) -> bytes:
    candidates = []
    for suffix in (".jpg", ".jpeg", ".png", ".webp"):
        candidates.append(source.with_suffix(suffix))
    for name in ("cover.jpg", "cover.png", "folder.jpg", "folder.png"):
        candidates.append(source.parent / name)
    for path in candidates:
        if path.is_file():
            try:
                return path.read_bytes()
            except OSError:
                pass
    return b""


def _metadata_fields(source: Path, metadata: dict | None) -> tuple[str, list[str], str, str]:
    data = metadata or {}
    title = str(data.get("musicName") or data.get("title") or "").strip()
    raw_artists = data.get("artist") or data.get("artists") or []
    artists = []
    if isinstance(raw_artists, str):
        artists = [raw_artists.strip()]
    elif isinstance(raw_artists, (list, tuple)):
        for artist in raw_artists:
            value = artist[0] if isinstance(artist, (list, tuple)) and artist else artist
            if str(value).strip():
                artists.append(str(value).strip())
    album = str(data.get("album") or "").strip()
    lyrics = str(data.get("lyrics") or data.get("lyric") or "").strip() or _sidecar_text(source)
    return title, artists, album, lyrics


def write_audio_tags(path: Path, source: Path, payload: AudioPayload) -> tuple[bool, bool]:
    title, artists, album, lyrics = _metadata_fields(source, payload.metadata)
    cover = payload.cover or _sidecar_cover(source)
    artist_text = "; ".join(artists)
    if payload.audio_format == "flac":
        audio = FLAC(path)
        if title:
            audio["title"] = [title]
        if artists:
            audio["artist"] = artists
        if album:
            audio["album"] = [album]
        if lyrics:
            audio["lyrics"] = [lyrics]
        if cover:
            picture = Picture()
            picture.type = 3
            picture.mime = _image_mime(cover)
            picture.desc = "Cover"
            picture.data = cover
            audio.clear_pictures()
            audio.add_picture(picture)
        audio.save()
    elif payload.audio_format == "mp3":
        audio = MP3(path)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags or ID3()
        if title:
            tags.delall("TIT2"); tags.add(TIT2(encoding=3, text=title))
        if artist_text:
            tags.delall("TPE1"); tags.add(TPE1(encoding=3, text=artist_text))
        if album:
            tags.delall("TALB"); tags.add(TALB(encoding=3, text=album))
        if lyrics:
            tags.delall("USLT"); tags.add(USLT(encoding=3, lang="zho", desc="", text=lyrics))
        if cover:
            tags.delall("APIC"); tags.add(APIC(encoding=3, mime=_image_mime(cover), type=3, desc="Cover", data=cover))
        audio.tags = tags
        audio.save(v2_version=3)
    elif payload.audio_format == "ogg":
        audio = OggVorbis(path)
        if title:
            audio["title"] = [title]
        if artists:
            audio["artist"] = artists
        if album:
            audio["album"] = [album]
        if lyrics:
            audio["lyrics"] = [lyrics]
        if cover:
            picture = Picture()
            picture.type = 3
            picture.mime = _image_mime(cover)
            picture.desc = "Cover"
            picture.data = cover
            audio["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
        audio.save()
    return bool(lyrics), bool(cover)


def unique_target(target: Path) -> Path:
    if not target.exists():
        return target
    index = 2
    while True:
        candidate = target.with_name(f"{target.stem} ({index}){target.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def decode_file(
    source: Path,
    output_dir: Path,
    mode: str,
    overwrite: str,
    cancel: Event,
    progress: Progress | None = None,
    bitrate: str = "auto",
    bit_depth: str = "auto",
) -> DecodeResult:
    info = detect_format(source)
    service = info.service
    temporary = output_dir / f".{source.name}.{os.getpid()}.{uuid.uuid4().hex}.part"
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        if info.status == "experimental":
            raise DecodeError(f"已识别 {info.version}，但当前版本尚无经过样本验证的可靠解码实现。{info.note}")
        def decode_progress(done: int, total: int) -> None:
            if progress:
                progress(done * 70, total * 100)
        if source.suffix.lower() == ".ncm":
            audio_data, payload = decode_ncm(source, cancel, decode_progress)
        elif source.suffix.lower() in {".kgm", ".kgma", ".vpr"}:
            audio_data, payload = decode_kgm(source, cancel, decode_progress)
        elif source.suffix.lower() in {".mflac", ".mgg"}:
            audio_data, payload = decode_qq(source, cancel, decode_progress)
        else:
            raise DecodeError("不支持此文件类型")
        audio_format = payload.audio_format
        source_audio_format = audio_format
        if mode in {"mp3", "flac"} and audio_format != mode:
            audio_data = transcode_audio(audio_data, audio_format, mode, bitrate, bit_depth)
            payload = AudioPayload(mode, payload.metadata, payload.cover)
            audio_format = mode
        with _disk_write_lock:
            target = output_dir / f"{source.stem}.{audio_format}"
            with _output_lock:
                if target.exists():
                    if overwrite == "skip":
                        return DecodeResult(source, target, service, audio_format, "skipped", "目标文件已存在")
                    if overwrite == "rename":
                        target = unique_target(target)
            total = max(1, len(audio_data))
            written = 0
            with temporary.open("wb") as output:
                view = memoryview(audio_data)
                for offset in range(0, len(view), CHUNK_SIZE):
                    chunk = view[offset:offset + CHUNK_SIZE]
                    output.write(chunk)
                    written += len(chunk)
                    if progress:
                        progress(70 * total + 30 * written, 100 * total)
                output.flush()
                os.fsync(output.fileno())
            try:
                lyrics_written, cover_written = write_audio_tags(temporary, source, payload)
            except Exception as error:
                os.replace(temporary, target)
                return DecodeResult(source, target, service, audio_format, "done", f"解码完成，标签写入失败：{error}")
            os.replace(temporary, target)
        additions = []
        if lyrics_written:
            additions.append("歌词")
        if cover_written:
            additions.append("封面")
        message = "解码完成" + ("，已写入" + "和".join(additions) if additions else "")
        return DecodeResult(source, target, service, source_audio_format, "done", message, lyrics_written, cover_written, audio_format)
    except Cancelled:
        temporary.unlink(missing_ok=True)
        raise
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
