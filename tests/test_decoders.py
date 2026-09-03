from __future__ import annotations

import tempfile
import unittest
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from decoders import (AudioPayload, KeyCache, decode_file, decode_kgm, detect_audio_format,
                      detect_format, parse_qmc_tail, service_for, write_audio_tags)


ROOT = Path(__file__).resolve().parents[1]
KGM_FIXTURE = ROOT / ".references/kugou-kgm-decoder/assets/test_kugou_kgm.dat"
KGM_EXPECTED = ROOT / ".references/kugou-kgm-decoder/assets/test_kugou_kgm_right.dat"


class DecoderTests(unittest.TestCase):
    def test_detect_audio_formats(self) -> None:
        self.assertEqual(detect_audio_format(b"fLaC\x00\x00"), "flac")
        self.assertEqual(detect_audio_format(b"ID3\x04\x00"), "mp3")
        self.assertEqual(detect_audio_format(b"OggS\x00\x02"), "ogg")
        self.assertEqual(detect_audio_format(b"\xff\xfb\x90\x00"), "mp3")
        self.assertIsNone(detect_audio_format(b"unknown"))

    def test_service_detection(self) -> None:
        self.assertEqual(service_for(Path("track.ncm")), "网易云音乐")
        self.assertEqual(service_for(Path("track.kgm")), "酷狗音乐")
        self.assertEqual(service_for(Path("track.mflac")), "QQ音乐")
        self.assertEqual(service_for(Path("track.mflac0")), "QQ音乐")
        self.assertEqual(service_for(Path("track.kwm")), "酷我音乐")

    def test_format_detector_marks_unverified_formats(self) -> None:
        info = detect_format(Path("track.mflac0"))
        self.assertEqual((info.service, info.version, info.status), ("QQ音乐", "QMC2", "experimental"))
        self.assertEqual(detect_format(Path("track.ncm")).status, "supported")

    def test_qmc_tail_markers_and_media_mid(self) -> None:
        parsed = parse_qmc_tail(b"...QTag...media_mid: abcdefghijk123...MusicEx")
        self.assertEqual(parsed["QTag"], "present")
        self.assertEqual(parsed["MusicEx"], "present")
        self.assertEqual(parsed["media_mid"], "abcdefghijk123")

    def test_key_cache_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache = KeyCache(Path(folder) / "keys.json")
            self.assertIsNone(cache.get("mid-1"))
            cache.put("ekey-value", "mid-1", "song-1")
            self.assertEqual(cache.get("missing", "mid-1"), "ekey-value")

    def test_sidecar_lyrics_and_cover_are_embedded_in_flac(self) -> None:
        decoded = Path.home() / "Music/Decoded/七朵组合 - 呵呵【七朵组合】.flac"
        if not decoded.exists():
            self.skipTest("decoded FLAC fixture is not available")
        from mutagen.flac import FLAC
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "sample.ncm"
            target = Path(folder) / "sample.flac"
            shutil.copy2(decoded, target)
            source.with_suffix(".lrc").write_text("[00:01.00]测试歌词", encoding="utf-8")
            source.with_suffix(".jpg").write_bytes(b"\xff\xd8\xff\xe0cover")
            written = write_audio_tags(target, source, AudioPayload("flac", {"musicName": "测试歌曲"}))
            audio = FLAC(target)
            self.assertEqual(written, (True, True))
            self.assertEqual(audio["lyrics"], ["[00:01.00]测试歌词"])
            self.assertEqual(len(audio.pictures), 1)

    @unittest.skipUnless(KGM_FIXTURE.exists(), "reference fixture is not available")
    def test_kgm_matches_reference_output(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            audio_data, payload = decode_kgm(KGM_FIXTURE, Event())
            self.assertEqual(payload.audio_format, "mp3")
            self.assertEqual(audio_data, KGM_EXPECTED.read_bytes())

    @unittest.skipUnless(KGM_FIXTURE.exists(), "reference fixture is not available")
    def test_format_filter_does_not_transcode(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "sample.kgm"
            output = Path(folder) / "output"
            shutil.copy2(KGM_FIXTURE, source)
            result = decode_file(source, output, "flac", "rename", Event())
            self.assertEqual(result.status, "filtered")
            self.assertEqual(result.audio_format, "mp3")
            self.assertEqual(list(output.iterdir()), [])

    @unittest.skipUnless(KGM_FIXTURE.exists(), "reference fixture is not available")
    def test_parallel_outputs_do_not_collide(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "sample.kgm"
            output = Path(folder) / "output"
            shutil.copy2(KGM_FIXTURE, source)
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: decode_file(source, output, "auto", "rename", Event()), range(4)))
            self.assertEqual(len(list(output.glob("*.mp3"))), 4)
            self.assertTrue(all(result.status == "done" for result in results))


if __name__ == "__main__":
    unittest.main()
