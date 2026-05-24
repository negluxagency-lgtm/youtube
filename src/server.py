#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║       YUTU RENDER SERVER v2.0 — API REST para n8n                  ║
║  Motor: FastAPI + FFmpeg 8.x + Ken Burns para imágenes             ║
╚══════════════════════════════════════════════════════════════════════╝

ENDPOINTS:
  POST /render           → Body: array directo de media items
  GET  /status/{job_id}  → Estado del job
  GET  /download/{job_id}→ Descarga el MP4 final
  GET  /jobs             → Lista todos los jobs
  GET  /health           → Healthcheck

TIPOS DE MEDIA SOPORTADOS:
  - Vídeo  (.mp4, .mov, .webm) → trim + normalización
  - Imagen (.jpg, .jpeg, .png) → Ken Burns animado (zoom/pan) → vídeo
  - "SIN_IMAGENES_DISPONIBLES" → Frame negro silencioso

BODY n8n (array JSON directo):
  [
    { "url": "https://cdn.pixabay.com/video/..._large.mp4", "posicion": 1, "score": 602970, "estimated_duration": 16 },
    { "url": "https://pixabay.com/get/ga...jpg",           "posicion": 2, "score": 143867, "estimated_duration": 18 },
    { "url": "SIN_IMAGENES_DISPONIBLES",                   "posicion": 3, "score": 0,      "estimated_duration": 11 },
    ...
  ]
"""

import json
import os
import shutil
import subprocess
import time
import uuid
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ─── CONFIGURACIÓN ────────────────────────────────────────────────────────────

BASE_DIR      = Path(__file__).parent.parent
ARTIFACTS_DIR = BASE_DIR / "artifacts"
JOBS_DIR      = ARTIFACTS_DIR / "jobs"
LOGS_DIR      = ARTIFACTS_DIR / "logs"

HOST          = "0.0.0.0"
PORT          = 8000

TARGET_W      = 1920
TARGET_H      = 1080
TARGET_FPS    = 30
CRF           = 23
PRESET        = "veryfast"
CROSSFADE_DUR = 0.5
DL_TIMEOUT    = 60
MAX_RETRIES   = 3
MIN_FILE_SIZE = 5_000

IMAGE_EXTS    = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTS    = {".mp4", ".mov", ".webm", ".avi", ".mkv"}

# Estilos Ken Burns — se alternan por posición
# Cada estilo es una expresión de FFmpeg zoompan
KEN_BURNS_STYLES = [
    # 0: Zoom in desde el centro
    "zoom_in_center",
    # 1: Zoom out desde el centro
    "zoom_out_center",
    # 2: Pan de izquierda a derecha con zoom suave
    "pan_left_right",
    # 3: Pan de derecha a izquierda con zoom suave
    "pan_right_left",
]

# ─── LOGGING ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("yutu-v2")

# ─── MODELOS PYDANTIC ─────────────────────────────────────────────────────────

class MediaItem(BaseModel):
    url: str
    posicion: int
    score: int = 0
    estimated_duration: int

# ─── STORE DE JOBS ───────────────────────────────────────────────────────────

jobs: dict = {}

def save_job_state(job_id: str):
    path = JOBS_DIR / job_id / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(jobs[job_id], f, indent=2, default=str)

def load_jobs_from_disk():
    if not JOBS_DIR.exists():
        return
    for job_dir in JOBS_DIR.iterdir():
        sf = job_dir / "state.json"
        if sf.exists():
            with open(sf) as f:
                data = json.load(f)
            jobs[data["job_id"]] = data

# ─── DETECCIÓN DE TIPO DE MEDIA ───────────────────────────────────────────────

def detect_media_type(url: str) -> str:
    """Devuelve 'video', 'image' o 'invalid'."""
    if not url or url.strip().upper() in ("SIN_IMAGENES_DISPONIBLES", "NULL", "NONE", ""):
        return "invalid"
    url_lower = url.lower().split("?")[0]  # ignorar query params
    ext = Path(url_lower).suffix
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    # Sin extensión clara → intentar por contenido de la URL
    if any(k in url_lower for k in ["pixabay.com/video", "cdn.pixabay.com/video", ".mp4"]):
        return "video"
    if any(k in url_lower for k in [".jpg", ".jpeg", ".png", "pixabay.com/get/"]):
        return "image"
    return "video"  # default: asumir vídeo

# ─── FFMPEG HELPERS ───────────────────────────────────────────────────────────

def get_duration(path: Path) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    try:
        return float(subprocess.run(cmd, capture_output=True, text=True).stdout.strip())
    except Exception:
        return 0.0

def run_ffmpeg(args: list, step: str) -> float:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = round(time.time() - t0, 2)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg [{step}]: {result.stderr[-800:]}")
    return elapsed

def download_file(url: str, dest: Path, idx: int) -> str:
    """Descarga cualquier URL (imagen o vídeo) con caché y reintentos."""
    if dest.exists() and dest.stat().st_size > MIN_FILE_SIZE:
        return "cached"
    for intento in range(1, MAX_RETRIES + 1):
        try:
            headers = {"User-Agent": "Mozilla/5.0 (compatible; YutuBot/2.0)"}
            resp = requests.get(url, stream=True, timeout=DL_TIMEOUT, headers=headers)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=262144):
                    f.write(chunk)
            return "downloaded"
        except Exception as e:
            if intento < MAX_RETRIES:
                time.sleep(2 ** intento)
            else:
                raise RuntimeError(f"Descarga fallida [{idx}]: {e}")


def process_image_kenburns(img_path: Path, norm_path: Path, dur: int, idx: int, style_idx: int = 0):
    """
    Convierte una imagen estática en un clip de vídeo con efecto Ken Burns.
    Alterna entre 4 estilos: zoom in, zoom out, pan L→R, pan R→L.
    """
    if norm_path.exists() and norm_path.stat().st_size > MIN_FILE_SIZE:
        return "cached"

    d_frames = dur * TARGET_FPS
    style = KEN_BURNS_STYLES[style_idx % 4]

    # Escala la imagen a 3840x2160 para tener margen de zoom/pan sin pixelado
    pre_scale = f"scale=3840:2160:force_original_aspect_ratio=increase,crop=3840:2160"

    if style == "zoom_in_center":
        zoompan = (
            f"zoompan=z='min(zoom+0.0012,1.4)':"
            f"d={d_frames}:"
            f"x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':"
            f"s={TARGET_W}x{TARGET_H}"
        )
    elif style == "zoom_out_center":
        zoompan = (
            f"zoompan=z='if(lte(zoom,1.0),1.35,max(1.001,zoom-0.001))':"
            f"d={d_frames}:"
            f"x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':"
            f"s={TARGET_W}x{TARGET_H}"
        )
    elif style == "pan_left_right":
        zoompan = (
            f"zoompan=z=1.25:"
            f"d={d_frames}:"
            f"x='if(lte(on,1),0,x+0.8)':"
            f"y='ih/2-(ih/zoom/2)':"
            f"s={TARGET_W}x{TARGET_H}"
        )
    else:  # pan_right_left
        zoompan = (
            f"zoompan=z=1.25:"
            f"d={d_frames}:"
            f"x='if(lte(on,1),iw-(iw/zoom),x-0.8)':"
            f"y='ih/2-(ih/zoom/2)':"
            f"s={TARGET_W}x{TARGET_H}"
        )

    vf = f"{pre_scale},{zoompan},fps={TARGET_FPS},format=yuv420p"

    args = [
        "-loop", "1",
        "-i", str(img_path),
        "-f", "lavfi", "-i", f"aevalsrc=0:s=44100:c=stereo:d={dur}",
        "-vf", vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-crf", str(CRF), "-preset", PRESET,
        "-c:a", "aac", "-b:a", "192k",
        "-t", str(dur),
        "-map", "0:v", "-map", "1:a",
        str(norm_path)
    ]
    run_ffmpeg(args, f"kenburns_{idx:02d}_{style}")
    return "kenburns"


def process_video_clip(raw_path: Path, norm_path: Path, dur: int, idx: int):
    """Normaliza un clip de vídeo a 1920x1080@30fps con trim exacto."""
    if norm_path.exists() and norm_path.stat().st_size > MIN_FILE_SIZE:
        return "cached"

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type",
         "-of", "default=noprint_wrappers=1:nokey=1", str(raw_path)],
        capture_output=True, text=True
    )
    has_audio = "audio" in probe.stdout.strip()

    vf = (
        f"trim=start=0:end={dur},setpts=PTS-STARTPTS,"
        f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease,"
        f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={TARGET_FPS},format=yuv420p"
    )

    if has_audio:
        af = f"atrim=start=0:end={dur},asetpts=PTS-STARTPTS,aresample=44100,pan=stereo|c0=c0|c1=c0,apad,atrim=0:{dur}"
        args = [
            "-stream_loop", "-1",
            "-i", str(raw_path),
            "-vf", vf, "-af", af,
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-crf", str(CRF), "-preset", PRESET,
            "-c:a", "aac", "-b:a", "192k", "-t", str(dur),
            str(norm_path)
        ]
    else:
        args = [
            "-stream_loop", "-1",
            "-i", str(raw_path),
            "-f", "lavfi", "-i", f"aevalsrc=0:s=44100:c=stereo:d={dur}",
            "-vf", vf,
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-crf", str(CRF), "-preset", PRESET,
            "-c:a", "aac", "-b:a", "192k", "-t", str(dur),
            "-map", "0:v", "-map", "1:a",
            str(norm_path)
        ]
    run_ffmpeg(args, f"norm_video_{idx:02d}")
    return "normalized"


def create_black_frame(norm_path: Path, dur: int, idx: int):
    """Genera un clip negro silencioso para entradas inválidas."""
    if norm_path.exists() and norm_path.stat().st_size > MIN_FILE_SIZE:
        return "cached"
    args = [
        "-f", "lavfi", "-i",
        f"color=c=black:size={TARGET_W}x{TARGET_H}:rate={TARGET_FPS}:d={dur}",
        "-f", "lavfi", "-i", f"aevalsrc=0:s=44100:c=stereo:d={dur}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-crf", str(CRF), "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k", "-t", str(dur),
        str(norm_path)
    ]
    run_ffmpeg(args, f"black_{idx:02d}")
    return "black_frame"


def build_xfade_filtergraph(items: list) -> tuple[str, list]:
    n = len(items)
    if n == 1:
        return "", ["0:v", "0:a"]

    parts_v = []
    parts_a = []
    offset_acum = 0.0

    for i in range(n - 1):
        dur_i = float(items[i]["estimated_duration"])
        in_v = "[0:v]" if i == 0 else f"[vx{i}]"
        in_a = "[0:a]" if i == 0 else f"[ax{i}]"
        next_v = f"[{i+1}:v]"
        next_a = f"[{i+1}:a]"

        offset_acum += dur_i - CROSSFADE_DUR

        out_v = f"[vx{i+1}]" if i < n - 2 else "[vfinal]"
        out_a = f"[ax{i+1}]" if i < n - 2 else "[afinal]"

        parts_v.append(
            f"{in_v}{next_v}xfade=transition=fade:duration={CROSSFADE_DUR}:offset={offset_acum:.4f}{out_v}"
        )
        parts_a.append(
            f"{in_a}{next_a}acrossfade=d={CROSSFADE_DUR}:c1=tri:c2=tri{out_a}"
        )

    filtergraph = "; ".join(parts_v + parts_a)
    return filtergraph, ["[vfinal]", "[afinal]"]


def assemble_single_xfade(job_id: str, items: list, norm_paths: list, output_path: Path):
    """Ensambla todos los clips en una sola pasada usando un único filter_complex."""
    filtergraph, [out_v, out_a] = build_xfade_filtergraph(items)

    inputs = []
    for p in norm_paths:
        inputs += ["-i", str(p)]

    if filtergraph:
        args = (
            inputs
            + ["-filter_complex", filtergraph]
            + ["-map", out_v, "-map", out_a]
            + ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
            + ["-crf", str(CRF), "-preset", PRESET]
            + ["-c:a", "aac", "-b:a", "192k"]
            + [str(output_path)]
        )
    else:
        args = (
            inputs
            + ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
            + ["-crf", str(CRF), "-preset", PRESET]
            + ["-c:a", "aac", "-b:a", "192k"]
            + [str(output_path)]
        )

    run_ffmpeg(args, "ensamblaje_final")

# ─── WORKER DEL JOB ──────────────────────────────────────────────────────────

def process_job(job_id: str, items: list):
    """Pipeline completo en thread background."""
    job = jobs[job_id]
    job_dir  = JOBS_DIR / job_id
    clips_dir = job_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    output_path = job_dir / "documental_final.mp4"

    def update(status: str, msg: str, pct: int = None):
        job["status"]  = status
        job["message"] = msg
        if pct is not None:
            job["progress_pct"] = pct
        job["updated_at"] = datetime.now().isoformat()
        save_job_state(job_id)
        log.info(f"  [{job_id[:8]}] {status.upper()} ({pct or '?'}%): {msg}")

    try:
        total = len(items)
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def process_item(i, item):
            idx  = item["posicion"]
            url  = item["url"]
            dur  = item["estimated_duration"]
            mtype = detect_media_type(url)

            if mtype == "invalid":
                log.info(f"  [{job_id[:8]}] Escena {idx}: URL inválida → frame negro")
                norm = clips_dir / f"norm_{idx:04d}.mp4"
                create_black_frame(norm, dur, idx)
                return i, norm

            url_clean = url.split("?")[0]
            ext = Path(url_clean).suffix or (".jpg" if mtype == "image" else ".mp4")
            raw = clips_dir / f"raw_{idx:04d}{ext}"

            download_file(url, raw, idx)

            norm = clips_dir / f"norm_{idx:04d}.mp4"
            if mtype == "image":
                process_image_kenburns(raw, norm, dur, idx, style_idx=i)
            else:
                process_video_clip(raw, norm, dur, idx)

            return i, norm

        update("processing", f"Descargando y normalizando {total} clips (paralelo)...", 30)
        norm_paths_dict = {}

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(process_item, i, item) for i, item in enumerate(items)]
            for future in as_completed(futures):
                i, norm = future.result()
                norm_paths_dict[i] = norm
                update("processing", f"Clip {items[i]['posicion']} listo.", 30 + int((len(norm_paths_dict)/total)*60))

        norm_paths = [norm_paths_dict[i] for i in range(total)]

        # ── Ensamblaje final de una sola pasada ─────────────────────────────────────
        update("processing", f"Ensamblando {len(norm_paths)} clips (una sola pasada O(N))...", 92)
        assemble_single_xfade(job_id, items, norm_paths, output_path)

        size_mb  = round(output_path.stat().st_size / 1_048_576, 2)
        dur_net  = sum(it["estimated_duration"] for it in items) - CROSSFADE_DUR * (total - 1)

        job.update({
            "status":       "done",
            "progress_pct": 100,
            "message":      "Renderizado completado.",
            "output_file":  str(output_path),
            "size_mb":      size_mb,
            "duration_s":   round(dur_net, 1),
            "download_url": f"/download/{job_id}",
            "completed_at": datetime.now().isoformat()
        })
        save_job_state(job_id)
        log.info(f"  [{job_id[:8]}] ✅ DONE — {size_mb}MB, {dur_net:.0f}s")

    except Exception as e:
        job.update({
            "status":     "error",
            "message":    str(e),
            "updated_at": datetime.now().isoformat()
        })
        save_job_state(job_id)
        log.error(f"  [{job_id[:8]}] ✗ ERROR: {e}")

# ─── FASTAPI APP ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Yutu Render Server",
    description=(
        "API REST para ensamblaje cinematográfico de vídeos documentales. "
        "Soporta vídeos MP4 e imágenes JPG/PNG con efecto Ken Burns. "
        "Compatible con n8n."
    ),
    version="2.0.0"
)

@app.on_event("startup")
def on_startup():
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    load_jobs_from_disk()
    log.info("🚀 Yutu Render Server v2.0 arrancado en http://0.0.0.0:8000")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "server": "Yutu Render Server v2.0",
        "jobs_procesando": len([j for j in jobs.values() if j["status"] == "processing"]),
        "jobs_total": len(jobs)
    }


@app.post("/render", status_code=200)
def render(items: List[MediaItem], job_name: Optional[str] = None):
    """
    Renderiza el vídeo de forma SÍNCRONA.
    El request se quedará cargando hasta que el vídeo final esté listo.
    """
    if not items:
        raise HTTPException(status_code=400, detail="El array de media items no puede estar vacío.")

    job_id     = str(uuid.uuid4())
    items_data = [it.dict() for it in items]

    media_stats = {"video": 0, "image": 0, "invalid": 0}
    for it in items_data:
        media_stats[detect_media_type(it["url"])] += 1

    jobs[job_id] = {
        "job_id":       job_id,
        "job_name":     job_name or f"job_{job_id[:8]}",
        "status":       "processing",
        "progress_pct": 0,
        "message":      "Iniciando renderizado...",
        "total_items":  len(items_data),
        "media_stats":  media_stats,
        "created_at":   datetime.now().isoformat(),
        "updated_at":   datetime.now().isoformat(),
        "output_file":  None,
        "size_mb":      None,
        "duration_s":   None,
        "download_url": f"/download/{job_id}"
    }
    save_job_state(job_id)

    log.info(f"  Job {job_id[:8]} síncrono iniciado: {len(items_data)} items")

    # Ejecutamos el pipeline de forma bloqueante (n8n se queda esperando)
    process_job(job_id, items_data)

    job_result = jobs[job_id]

    if job_result["status"] == "error":
        raise HTTPException(status_code=500, detail=f"Error en renderizado: {job_result['message']}")

    # Retornar el resultado final
    return {
        "job_id":       job_id,
        "status":       job_result["status"],
        "total_items":  len(items_data),
        "media_stats":  media_stats,
        "size_mb":      job_result["size_mb"],
        "duration_s":   job_result["duration_s"],
        "download_url": job_result["download_url"],
        "message":      "Renderizado completado con éxito."
    }


@app.get("/status/{job_id}")
def status(job_id: str):
    """Polling del estado del job. Úsalo en n8n en un loop cada 30-60s."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' no encontrado.")
    j = jobs[job_id]
    return {
        "job_id":       j["job_id"],
        "job_name":     j.get("job_name"),
        "status":       j["status"],
        "progress_pct": j.get("progress_pct", 0),
        "message":      j.get("message"),
        "total_items":  j.get("total_items"),
        "media_stats":  j.get("media_stats"),
        "created_at":   j.get("created_at"),
        "updated_at":   j.get("updated_at"),
        "completed_at": j.get("completed_at"),
        "download_url": j.get("download_url") if j["status"] == "done" else None,
        "size_mb":      j.get("size_mb"),
        "duration_s":   j.get("duration_s"),
    }


@app.get("/download/{job_id}")
def download(job_id: str):
    """Descarga el MP4 final. Solo disponible cuando status == 'done'."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' no encontrado.")
    j = jobs[job_id]
    if j["status"] != "done":
        raise HTTPException(status_code=425, detail=f"Job en estado '{j['status']}'. Espera a que termine.")
    output = Path(j["output_file"])
    if not output.exists():
        raise HTTPException(status_code=500, detail="Archivo de salida no encontrado en disco.")
    return FileResponse(
        path=str(output),
        media_type="video/mp4",
        filename=f"{j.get('job_name', job_id)}.mp4"
    )


@app.get("/jobs")
def list_jobs():
    return {
        "total": len(jobs),
        "jobs": [
            {
                "job_id":       j["job_id"],
                "job_name":     j.get("job_name"),
                "status":       j["status"],
                "progress_pct": j.get("progress_pct", 0),
                "total_items":  j.get("total_items"),
                "created_at":   j.get("created_at"),
            }
            for j in sorted(jobs.values(), key=lambda x: x.get("created_at", ""), reverse=True)
        ]
    }


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job no encontrado.")
    j = jobs.pop(job_id)
    job_dir = JOBS_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir)
    return {"deleted": job_id, "job_name": j.get("job_name")}


if __name__ == "__main__":
    uvicorn.run("server:app", host=HOST, port=PORT, reload=False, workers=1)
