# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Video briefing pipeline: data → script → Polly audio + speech marks → slide PNGs → ffmpeg → S3."""

import json
import logging
import os
import subprocess  # nosec B404 - subprocess used for known internal commands
import tempfile
import uuid

import boto3
from PIL import Image, ImageDraw, ImageFont

from .model_catalog import env_orchestrator_model

logger = logging.getLogger(__name__)

_REGION = "us-east-1"
_BEDROCK_MODEL = env_orchestrator_model(_REGION)
# No hardcoded fallback: a predictable literal bucket name is squattable. The CDK
# app always injects VIDEO_BUCKET (the account/region-unique bucket; see
# adt_compute_stack.py). Uploads fail fast if it is unset (see _upload_to_s3).
_VIDEO_BUCKET = os.environ.get("VIDEO_BUCKET", "")

_SCRIPT_SYSTEM = (
    "You are a video briefing producer for a manufacturing intelligence platform. "
    "Given raw data about plant operations, create a briefing script. "
    "Return JSON with this exact structure:\n"
    '{"narration": "Full narration text as one continuous paragraph per slide, separated by newlines", '
    '"slides": [{"title": "Slide Title", "bullets": ["bullet 1", "bullet 2"], '
    '"kpi_value": "94%", "kpi_label": "Production Rate"}]}'
)

# Theme colors
_BG = (13, 17, 23)
_TEXT = (240, 246, 252)
_TEXT2 = (230, 237, 243)
_MUTED = (125, 133, 144)
_ACCENT = (0, 216, 255)

W, H = 1280, 720


def generate_briefing(prompt_id: str, prompt_text: str, execute_fn) -> str:
    """Orchestrate the full pipeline. Returns S3 presigned URL of the final MP4."""
    work_dir = tempfile.mkdtemp(prefix="briefing_")
    try:
        raw_data = execute_fn(prompt_text)
        script = _generate_script(raw_data)
        audio_path, speech_marks = _synthesize_audio(script["narration"])
        slide_paths = _render_slides(script["slides"])
        mp4_path = _compose_video(slide_paths, audio_path, speech_marks, work_dir)
        url = _upload_to_s3(mp4_path)
        return url
    finally:
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)


def _generate_script(raw_data: str) -> dict:
    """Call Bedrock to generate narration + slides JSON."""
    from .aws_session import bedrock_runtime_client
    from .db import converse_text
    client = bedrock_runtime_client(_REGION)
    text = converse_text(client, _BEDROCK_MODEL, raw_data, system=_SCRIPT_SYSTEM,
                         max_tokens=4096)
    # Extract JSON — handle potential markdown fences
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def _synthesize_audio(narration: str) -> tuple:
    """Synthesize MP3 + sentence speech marks via Polly Long-Form. Returns (mp3_path, marks)."""
    polly = boto3.client("polly", region_name=_REGION)
    common = dict(Text=narration, VoiceId="Ruth", Engine="long-form", TextType="text")

    # Audio
    audio_resp = polly.synthesize_speech(**common, OutputFormat="mp3")
    tmp_mp3 = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp_mp3.write(audio_resp["AudioStream"].read())
    tmp_mp3.flush()
    tmp_mp3.close()

    # Speech marks
    marks_resp = polly.synthesize_speech(**common, OutputFormat="json", SpeechMarkTypes=["sentence"])
    marks = [json.loads(line) for line in marks_resp["AudioStream"].read().decode().splitlines() if line.strip()]

    return tmp_mp3.name, marks


def _load_font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _render_slides(slides: list) -> list:
    """Render each slide as a 1280x720 PNG. Returns list of file paths."""
    paths = []
    for i, slide in enumerate(slides):
        img = Image.new("RGB", (W, H), color=_BG)
        draw = ImageDraw.Draw(img)

        font_title = _load_font(40)
        font_bullet = _load_font(24)
        font_kpi = _load_font(72)
        font_kpi_label = _load_font(20)
        font_watermark = _load_font(14)

        # Title
        title = slide.get("title", "")
        draw.text((W // 2, 48), title, font=font_title, fill=_TEXT, anchor="mm")

        # Accent line below title
        draw.rectangle([(80, 80), (W - 80, 82)], fill=_ACCENT)

        y = 110
        kpi_value = slide.get("kpi_value")
        kpi_label = slide.get("kpi_label")

        if kpi_value:
            # KPI centered in upper portion, bullets below
            draw.text((W // 2, y + 60), kpi_value, font=font_kpi, fill=_ACCENT, anchor="mm")
            if kpi_label:
                draw.text((W // 2, y + 110), kpi_label, font=font_kpi_label, fill=_MUTED, anchor="mm")
            y += 150

        for bullet in slide.get("bullets", []):
            draw.text((100, y), f"• {bullet}", font=font_bullet, fill=_TEXT2)
            y += 38

        # Watermark
        draw.text((W - 16, H - 16), "Agentic Digital Thread", font=font_watermark, fill=_MUTED, anchor="rb")

        tmp = tempfile.NamedTemporaryFile(suffix=f"_slide_{i}.png", delete=False)
        img.save(tmp.name, "PNG")
        tmp.close()
        paths.append(tmp.name)

    return paths


def _get_audio_duration(audio_path: str) -> float:
    """Use ffprobe to get audio duration in seconds."""
    result = subprocess.run(  # nosec B603 B607 - known internal command, no untrusted input; known command, internal tool
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def _compose_video(slide_paths: list, audio_path: str, speech_marks: list, work_dir: str) -> str:
    """Compose MP4 from slides + audio using ffmpeg concat demuxer."""
    total_duration = _get_audio_duration(audio_path)
    n = len(slide_paths)

    # Distribute sentences across slides proportionally
    sentence_times = [m["time"] / 1000.0 for m in speech_marks if m.get("type") == "sentence"]

    if sentence_times and n > 1:
        # Split sentences into n roughly equal groups; each slide starts at its first sentence
        chunk = max(1, len(sentence_times) // n)
        slide_starts = [sentence_times[min(i * chunk, len(sentence_times) - 1)] for i in range(n)]
        durations = [slide_starts[i + 1] - slide_starts[i] for i in range(n - 1)]
        durations.append(max(1.0, total_duration - slide_starts[-1]))
    else:
        # Fallback: equal duration
        per = total_duration / n
        durations = [per] * n

    # Write concat file
    concat_path = os.path.join(work_dir, "concat.txt")
    with open(concat_path, "w", encoding="utf-8") as f:
        for path, dur in zip(slide_paths, durations):
            f.write(f"file '{path}'\nduration {dur:.3f}\n")
        # ffmpeg concat demuxer needs last file repeated without duration
        f.write(f"file '{slide_paths[-1]}'\n")

    output_path = os.path.join(work_dir, "briefing.mp4")
    subprocess.run(  # nosec B603 B607 - known internal command, no untrusted input; known command, internal tool
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_path,
         "-i", audio_path, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "24",
         "-c:a", "aac", "-shortest", output_path],
        check=True,
    )
    return output_path


def _upload_to_s3(mp4_path: str) -> str:
    """Upload MP4 to S3 and return a 7-day presigned URL."""
    if not _VIDEO_BUCKET:
        raise RuntimeError(
            "VIDEO_BUCKET environment variable is not set. The CDK app injects it "
            "automatically (see adt_compute_stack.py); set it explicitly for local runs."
        )
    s3 = boto3.client("s3", region_name=_REGION)
    key = f"briefings/{uuid.uuid4()}.mp4"
    s3.upload_file(mp4_path, _VIDEO_BUCKET, key, ExtraArgs={"ContentType": "video/mp4"})
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": _VIDEO_BUCKET, "Key": key},
        ExpiresIn=7 * 24 * 3600,
    )
