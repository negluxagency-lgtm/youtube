#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║       YUTU RENDER SERVER — API REST para n8n / automatización       ║
║  Puerto: 8000  |  Motor: FastAPI + FFmpeg                           ║
╚══════════════════════════════════════════════════════════════════════╝

ENDPOINTS:
  POST /render          → Encola un job de renderizado (body = array de escenas)
  GET  /status/{job_id} → Estado del job (pending / processing / done / error)
  GET  /download/{job_id}→ Descarga el MP4 final cuando esté listo
  GET  /jobs            → Lista todos los jobs activos
  GET  /health          → Healthcheck

FLUJO n8n:
  1. HTTP Request (POST /render, body JSON) → recibe { job_id, status }
  2. Loop/Wait → HTTP Request (GET /status/{job_id}) hasta status == "done"
  3. HTTP Request (GET /download/{job_id}) → descarga el MP4
"""

import json
import os
import subprocess
import time
import uuid
import logging
import threading
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# ─── CONFIGURACIÓN ────────────────────────────────────────────────────────────

BASE_DIR      = Path(__file__).parent.parent
ARTIFACTS_DIR = BASE_DIR / "artifacts"
JOBS_DIR      = ARTIFACTS_DIR / "jobs"
LOGS_DIR      = ARTIFACTS_DIR / "logs"

HOST          = "0.0.0.0"
PORT          = 8000

# Parámetros FFmpeg cinematográficos
TARGET_W      = 1920
TARGET_H      = 1080
TARGET_FPS    = 30
CRF           = 18
PRESET        = "slow"
CROSSFADE_DUR = 0.5
DL_TIMEOUT    = 60
MAX_RETRIES   = 3
MIN_FILE_SIZE = 10_000

# ─── LOGGING ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("yutu-server")

# ─── MODELOS PYDANTIC ─────────────────────────────────────────────────────────

class Escena(BaseModel):
    id_frase: int
    duracion_video: int
    url_video_mp4: str
    origen_video: Optional[str] = "Unknown"

class RenderRequest(BaseModel):
    escenas: List[Escena]
    job_name: Optional[str] = None   # nombre descriptivo opcional

# ─── STORE DE JOBS (in-memory + disco) ───────────────────────────────────────

jobs: dict = {}  # job_id → metadata dict

def save_job_state(job_id: str):
    """Persiste el estado del job en disco para sobrevivir reinicios."""
    path = JOBS_DIR / job_id / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(jobs[job_id], f, indent=2, default=str)

def load_jobs_from_disk():
    """Carga jobs persistidos al arrancar el servidor."""
    if not JOBS_DIR.exists():
        return
    for job_dir in JOBS_DIR.iterdir():
        state_file = job_dir / "state.json"
        if state_file.exists():
            with open(state_file) as f:
                data = json.load(f)
            jobs[data["job_id"]] = data
            log.info(f"  Job recuperado: {data['job_id']} [{data['status']}]")

# ─── PIPELINE FUNCTIONS ───────────────────────────────────────────────────────

def run_ffmpeg(args: list, step: str) -> float:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = round(time.time() - t0, 2)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg falló [{step}]: {result.stderr[-600:]}")
    return elapsed

def download_clip(url: str, dest: Path, idx: int) -> str:
    if dest.exists() and dest.stat().st_size > MIN_FILE_SIZE:
        return "cached"
    for intento in range(1, MAX_RETRIES + 1):
        try:
            headers = {"User-Agent": "Mozilla/5.0 (compatible; YutuBot/1.0)"}
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
                raise RuntimeError(f"Descarga fallida escena {idx}: {e}")

def normalize_clip(raw: Path, norm: Path, dur: int, idx: int):
    if norm.exists() and norm.stat().st_size > MIN_FILE_SIZE:
        return "cached"

    # Detectar audio
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type",
         "-of", "default=noprint_wrappers=1:nokey=1", str(raw)],
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
        af = f"atrim=start=0:end={dur},asetpts=PTS-STARTPTS,aresample=44100,pan=stereo|c0=c0|c1=c0"
        args = [
            "-i", str(raw),
            "-vf", vf, "-af", af,
            "-c:v", "libx264", "-crf", str(CRF), "-preset", PRESET,
            "-c:a", "aac", "-b:a", "192k", "-t", str(dur),
            str(norm)
        ]
    else:
        args = [
            "-i", str(raw),
            "-f", "lavfi", "-i", f"aevalsrc=0:s=44100:c=stereo:d={dur}",
            "-vf", vf,
            "-c:v", "libx264", "-crf", str(CRF), "-preset", PRESET,
            "-c:a", "aac", "-b:a", "192k", "-t", str(dur),
            "-map", "0:v", "-map", "1:a",
            str(norm)
        ]
    run_ffmpeg(args, f"norm_{idx:02d}")
    return "normalized"

def build_xfade_filtergraph(escenas: list):
    n = len(escenas)
    if n == 1:
        return "", ["0:v", "0:a"]
    parts_v, parts_a = [], []
    offset = 0.0
    for i in range(n - 1):
        in_v  = "[0:v]" if i == 0 else f"[vx{i}]"
        in_a  = "[0:a]" if i == 0 else f"[ax{i}]"
        out_v = "[vfinal]" if i == n - 2 else f"[vx{i+1}]"
        out_a = "[afinal]" if i == n - 2 else f"[ax{i+1}]"
        offset += escenas[i]["duracion_video"] - CROSSFADE_DUR
        parts_v.append(f"{in_v}[{i+1}:v]xfade=transition=fade:duration={CROSSFADE_DUR}:offset={offset:.4f}{out_v}")
        parts_a.append(f"{in_a}[{i+1}:a]acrossfade=d={CROSSFADE_DUR}:c1=tri:c2=tri{out_a}")
    return "; ".join(parts_v + parts_a), ["[vfinal]", "[afinal]"]

# ─── WORKER DEL JOB ──────────────────────────────────────────────────────────

def process_job(job_id: str, escenas_data: list):
    """Ejecuta el pipeline completo en un thread background."""
    job = jobs[job_id]
    job_dir = JOBS_DIR / job_id
    clips_dir = job_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    output_path = job_dir / "documental_final.mp4"

    def update(status: str, msg: str, progress: int = None):
        job["status"] = status
        job["message"] = msg
        if progress is not None:
            job["progress_pct"] = progress
        job["updated_at"] = datetime.now().isoformat()
        save_job_state(job_id)
        log.info(f"  [{job_id[:8]}] {status.upper()}: {msg}")

    try:
        total = len(escenas_data)

        # ── FASE 1: Descargas ──────────────────────────────────────────────
        update("processing", f"Descargando {total} clips...", 5)
        norm_paths = []
        for i, escena in enumerate(escenas_data):
            idx  = escena["id_frase"]
            url  = escena["url_video_mp4"]
            dur  = escena["duracion_video"]
            raw  = clips_dir / f"raw_{idx:02d}.mp4"
            norm = clips_dir / f"norm_{idx:02d}.mp4"

            pct_dl = 5 + int((i / total) * 35)
            update("processing", f"Descargando escena {idx}/{total}...", pct_dl)
            download_clip(url, raw, idx)

            pct_norm = 40 + int((i / total) * 50)
            update("processing", f"Normalizando escena {idx}/{total}...", pct_norm)
            normalize_clip(raw, norm, dur, idx)
            norm_paths.append(norm)

        # ── FASE 2: Ensamblaje ─────────────────────────────────────────────
        update("processing", "Ensamblando vídeo final con crossfade...", 92)
        filtergraph, [out_v, out_a] = build_xfade_filtergraph(escenas_data)
        inputs = []
        for p in norm_paths:
            inputs += ["-i", str(p)]

        if filtergraph:
            ffmpeg_args = (
                inputs
                + ["-filter_complex", filtergraph]
                + ["-map", out_v, "-map", out_a]
                + ["-c:v", "libx264", "-crf", str(CRF), "-preset", PRESET]
                + ["-c:a", "aac", "-b:a", "192k"]
                + [str(output_path)]
            )
        else:
            ffmpeg_args = (
                inputs
                + ["-c:v", "libx264", "-crf", str(CRF), "-preset", PRESET]
                + ["-c:a", "aac", "-b:a", "192k"]
                + [str(output_path)]
            )

        run_ffmpeg(ffmpeg_args, "ensamblaje_final")

        size_mb = round(output_path.stat().st_size / 1_048_576, 2)
        dur_total = sum(e["duracion_video"] for e in escenas_data)
        dur_net   = dur_total - CROSSFADE_DUR * (total - 1)

        job["status"]       = "done"
        job["progress_pct"] = 100
        job["message"]      = "Renderizado completado."
        job["output_file"]  = str(output_path)
        job["size_mb"]      = size_mb
        job["duration_s"]   = round(dur_net, 1)
        job["download_url"] = f"/download/{job_id}"
        job["completed_at"] = datetime.now().isoformat()
        save_job_state(job_id)
        log.info(f"  [{job_id[:8]}] ✅ DONE — {output_path.name} ({size_mb} MB, {dur_net:.0f}s)")

    except Exception as e:
        job["status"]  = "error"
        job["message"] = str(e)
        job["updated_at"] = datetime.now().isoformat()
        save_job_state(job_id)
        log.error(f"  [{job_id[:8]}] ✗ ERROR: {e}")

# ─── FASTAPI APP ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Yutu Render Server",
    description="API REST para ensamblaje cinematográfico de vídeos documentales. Compatible con n8n.",
    version="1.0.0"
)

@app.on_event("startup")
def on_startup():
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    load_jobs_from_disk()
    log.info("🚀 Yutu Render Server arrancado en http://0.0.0.0:8000")


@app.get("/health")
def health():
    """Healthcheck — n8n puede usarlo para verificar que el servidor está vivo."""
    return {
        "status": "ok",
        "server": "Yutu Render Server v1.0",
        "jobs_activos": len([j for j in jobs.values() if j["status"] == "processing"]),
        "jobs_total": len(jobs)
    }


@app.post("/render", status_code=202)
def render(req: RenderRequest, background_tasks: BackgroundTasks):
    """
    Encola un job de renderizado.

    Body (JSON):
    {
      "escenas": [
        { "id_frase": 1, "duracion_video": 16, "url_video_mp4": "https://...", "origen_video": "..." },
        ...
      ],
      "job_name": "documental_enero_ep1"   ← opcional
    }

    Respuesta inmediata (202 Accepted):
    {
      "job_id": "abc123...",
      "status": "pending",
      "status_url": "/status/abc123...",
      "download_url": "/download/abc123..."
    }
    """
    if not req.escenas:
        raise HTTPException(status_code=400, detail="El array 'escenas' no puede estar vacío.")

    job_id = str(uuid.uuid4())
    escenas_data = [e.dict() for e in req.escenas]

    jobs[job_id] = {
        "job_id":      job_id,
        "job_name":    req.job_name or f"job_{job_id[:8]}",
        "status":      "pending",
        "progress_pct": 0,
        "message":     "En cola. Iniciando en breve.",
        "escenas":     len(escenas_data),
        "created_at":  datetime.now().isoformat(),
        "updated_at":  datetime.now().isoformat(),
        "output_file": None,
        "size_mb":     None,
        "duration_s":  None,
        "download_url": f"/download/{job_id}"
    }
    save_job_state(job_id)

    # Lanzar pipeline en thread background (no bloquea la respuesta)
    background_tasks.add_task(process_job, job_id, escenas_data)

    log.info(f"  Job encolado: {job_id} ({len(escenas_data)} escenas)")
    return {
        "job_id":       job_id,
        "status":       "pending",
        "escenas":      len(escenas_data),
        "status_url":   f"/status/{job_id}",
        "download_url": f"/download/{job_id}",
        "message":      "Job aceptado. Usa status_url para monitorizar el progreso."
    }


@app.get("/status/{job_id}")
def status(job_id: str):
    """
    Estado del job. Úsalo en n8n en un loop (cada 30-60s) hasta que status == 'done'.

    Respuesta:
    {
      "job_id": "...",
      "status": "pending | processing | done | error",
      "progress_pct": 0-100,
      "message": "...",
      "download_url": "/download/...",   ← disponible cuando status == done
      "size_mb": 123.4,
      "duration_s": 457.0
    }
    """
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' no encontrado.")
    j = jobs[job_id]
    return {
        "job_id":       j["job_id"],
        "job_name":     j.get("job_name"),
        "status":       j["status"],
        "progress_pct": j.get("progress_pct", 0),
        "message":      j.get("message"),
        "escenas":      j.get("escenas"),
        "created_at":   j.get("created_at"),
        "updated_at":   j.get("updated_at"),
        "completed_at": j.get("completed_at"),
        "download_url": j.get("download_url") if j["status"] == "done" else None,
        "size_mb":      j.get("size_mb"),
        "duration_s":   j.get("duration_s"),
    }


@app.get("/download/{job_id}")
def download(job_id: str):
    """
    Descarga el MP4 final cuando el job esté en estado 'done'.
    Devuelve el archivo directamente como response binario.
    """
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' no encontrado.")
    j = jobs[job_id]
    if j["status"] != "done":
        raise HTTPException(status_code=425, detail=f"Job aún en estado '{j['status']}'. Espera a que termine.")
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
    """Lista todos los jobs con su estado actual."""
    return {
        "total": len(jobs),
        "jobs": [
            {
                "job_id":       j["job_id"],
                "job_name":     j.get("job_name"),
                "status":       j["status"],
                "progress_pct": j.get("progress_pct", 0),
                "escenas":      j.get("escenas"),
                "created_at":   j.get("created_at"),
            }
            for j in sorted(jobs.values(), key=lambda x: x.get("created_at", ""), reverse=True)
        ]
    }


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    """Elimina un job y sus archivos (limpieza)."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job no encontrado.")
    j = jobs.pop(job_id)
    job_dir = JOBS_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir)
    return {"deleted": job_id, "job_name": j.get("job_name")}


# ─── ARRANQUE ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("server:app", host=HOST, port=PORT, reload=False, workers=1)
