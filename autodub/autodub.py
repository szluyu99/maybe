#!/usr/bin/env python3
"""视频自动中文配音原型。

流程: 视频/URL -> 抽音轨 -> ASR(faster-whisper) -> 翻译(LLM) -> TTS(edge-tts)
     -> 按时间轴变速对齐 + 与原声混音 -> 输出 mp4 + srt

离线可验证: --stub 用假的 ASR/翻译/TTS 替代真实引擎, --dry-run 只打印外部命令,
两者组合可以在没有 ffmpeg / 没有网络的机器上检查编排逻辑。
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DRY = False

TRANSLATE_PROMPT = (
    "你是视频字幕译者。把 JSON 数组里的每条字幕翻译成简体中文口语表达，"
    "条数和顺序必须与输入完全一致，每条的字数尽量贴近原文语音时长，"
    "不要加解释，只输出 JSON 数组。"
)


@dataclass
class Seg:
    """一条字幕/配音片段。"""

    start: float
    end: float
    text: str
    zh: str = ""
    audio: Path | None = None

    @property
    def dur(self) -> float:
        return max(self.end - self.start, 0.2)


def run(cmd: list[str], capture: bool = False) -> str:
    print("$ " + " ".join(shlex.quote(c) for c in cmd), file=sys.stderr)
    if DRY:
        return ""
    r = subprocess.run(cmd, check=True, capture_output=capture, text=capture)
    return r.stdout if capture else ""
def media_dur(path: Path) -> float:
    out = run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture=True,
    )
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def fetch(src: str, work: Path) -> Path:
    """本地路径直接用，http(s) 链接交给 yt-dlp 下载。"""
    if src.startswith(("http://", "https://")):
        out = work / "source.mp4"
        run(["yt-dlp", "-f", "bv*+ba/b", "--merge-output-format", "mp4",
             "-o", str(out), src])
        return out
    p = Path(src).expanduser().resolve()
    if not DRY and not p.exists():
        sys.exit(f"找不到输入文件: {p}")
    return p


def extract_audio(video: Path, work: Path) -> Path:
    wav = work / "audio.wav"
    run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
         str(wav)])
    return wav


def asr(wav: Path, lang: str | None, stub: bool) -> list[Seg]:
    if stub:
        return [
            Seg(0.0, 2.5, "Hello everyone, welcome back to the channel."),
            Seg(3.0, 6.2, "Today we are building an automatic dubbing pipeline."),
            Seg(6.5, 9.0, "Let's get started."),
        ]
    from faster_whisper import WhisperModel

    model = WhisperModel(os.getenv("WHISPER_MODEL", "small"),
                         device="auto", compute_type="int8")
    segments, _ = model.transcribe(str(wav), language=lang, vad_filter=True)
    return [Seg(s.start, s.end, s.text.strip()) for s in segments if s.text.strip()]
def translate(segs: list[Seg], stub: bool) -> list[Seg]:
    """整批送给 LLM，一次请求翻完，保证条数对齐。"""
    if stub:
        for s in segs:
            s.zh = "【中文】" + s.text
        return segs

    base = os.getenv("LLM_API_BASE", "https://api.openai.com/v1").rstrip("/")
    key = os.getenv("LLM_API_KEY")
    if not key:
        sys.exit("需要设置 LLM_API_KEY (或用 --stub 跑通流程)")
    body = json.dumps({
        "model": os.getenv("LLM_MODEL", "gpt-4o-mini"),
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": TRANSLATE_PROMPT},
            {"role": "user",
             "content": json.dumps([s.text for s in segs], ensure_ascii=False)},
        ],
    }).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        content = json.load(resp)["choices"][0]["message"]["content"]

    content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    zh = json.loads(content)
    if len(zh) != len(segs):
        sys.exit(f"翻译条数不匹配: 输入 {len(segs)} 条, 返回 {len(zh)} 条")
    for s, t in zip(segs, zh):
        s.zh = str(t).strip()
    return segs


def tts(segs: list[Seg], work: Path, voice: str, stub: bool) -> list[Seg]:
    for i, s in enumerate(segs):
        out = work / f"seg{i:04d}.mp3"
        if stub:
            out.touch()
        else:
            run(["edge-tts", "--voice", voice, "--text", s.zh,
                 "--write-media", str(out)])
        s.audio = out
    return segs
def ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segs: list[Seg], path: Path) -> Path:
    lines = []
    for i, s in enumerate(segs, 1):
        lines += [str(i), f"{ts(s.start)} --> {ts(s.end)}", s.zh, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def mix(video: Path, segs: list[Seg], out: Path, bg_vol: float,
        srt: Path | None) -> Path:
    """每段配音变速贴回原时间点，再压到被压低的原声上。"""
    inputs: list[str] = ["-i", str(video)]
    for s in segs:
        inputs += ["-i", str(s.audio)]

    chains = [f"[0:a]volume={bg_vol}[bg]"]
    labels = ["[bg]"]
    for i, s in enumerate(segs, 1):
        spoken = media_dur(s.audio) or s.dur
        tempo = min(max(spoken / s.dur, 0.7), 1.6)
        delay = int(s.start * 1000)
        chains.append(
            f"[{i}:a]atempo={tempo:.3f},adelay={delay}|{delay}[a{i}]")
        labels.append(f"[a{i}]")
    chains.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0[mixed]")

    vmap, vcodec = "0:v", ["-c:v", "copy"]
    if srt:
        esc = str(srt).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        chains.append(f"[0:v]subtitles='{esc}'[v]")
        vmap, vcodec = "[v]", ["-c:v", "libx264", "-crf", "20"]

    run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(chains),
         "-map", vmap, "-map", "[mixed]", *vcodec, "-c:a", "aac",
         "-shortest", str(out)])
    return out
def main() -> None:
    global DRY
    ap = argparse.ArgumentParser(description="视频自动中文配音原型")
    ap.add_argument("source", help="本地视频路径或 http(s) 视频链接")
    ap.add_argument("-o", "--out", default="out/dubbed.mp4", help="输出 mp4")
    ap.add_argument("--voice", default="zh-CN-XiaoxiaoNeural", help="edge-tts 音色")
    ap.add_argument("--lang", default=None, help="源语言, 留空自动检测")
    ap.add_argument("--bg-vol", type=float, default=0.15, help="原声保留音量")
    ap.add_argument("--burn-sub", action="store_true", help="把中文字幕烧进画面")
    ap.add_argument("--stub", nargs="?", const="asr,translate,tts", default="",
                    help="用假引擎替代真实引擎, 可指定阶段: --stub translate")
    ap.add_argument("--dry-run", action="store_true", help="只打印外部命令")
    args = ap.parse_args()

    DRY = args.dry_run
    stub = set(filter(None, args.stub.split(",")))
    out = Path(args.out).resolve()
    work = out.parent / "work"
    work.mkdir(parents=True, exist_ok=True)

    video = fetch(args.source, work)
    wav = extract_audio(video, work)

    segs = asr(wav, args.lang, "asr" in stub)
    print(f"[asr] {len(segs)} 段", file=sys.stderr)

    segs = translate(segs, "translate" in stub)
    segs = tts(segs, work, args.voice, "tts" in stub)

    srt = write_srt(segs, work / "zh.srt")
    mix(video, segs, out, args.bg_vol, srt if args.burn_sub else None)

    print(f"[done] {out}\n[srt ] {srt}", file=sys.stderr)


if __name__ == "__main__":
    main()



