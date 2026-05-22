#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║          ENSAMBLADOR CINEMATOGRÁFICO — YUTU PIPELINE v1.0           ║
║  Directiva: directivas/ensamblador_video.md                         ║
║  Motor: FFmpeg 8.x + Python 3.10+                                   ║
╚══════════════════════════════════════════════════════════════════════╝

PIPELINE:
  1. Descarga clips brutos desde URLs (con caché)
  2. Normaliza cada clip: 1920x1080, 30fps, trim exacto, audio forzado
  3. Ensambla con crossfade cinematográfico (xfade 0.5s)
  4. Materializa artefactos y log de telemetría
"""

import json
import os
import subprocess
import sys
import time
import logging
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ─── CONFIGURACIÓN ────────────────────────────────────────────────────────────

BASE_DIR       = Path(__file__).parent.parent
SRC_DIR        = BASE_DIR / "src"
ARTIFACTS_DIR  = BASE_DIR / "artifacts"
CLIPS_DIR      = ARTIFACTS_DIR / "clips"
LOGS_DIR       = ARTIFACTS_DIR / "logs"
INPUT_JSON     = SRC_DIR / "input_escenas.json"
OUTPUT_FINAL   = ARTIFACTS_DIR / "documental_final.mp4"

# Parámetros cinematográficos (ver directiva)
TARGET_W       = 1920
TARGET_H       = 1080
TARGET_FPS     = 30
CRF            = 18
PRESET         = "slow"
CROSSFADE_DUR  = 0.5        # segundos de overlap entre escenas
DOWNLOAD_TIMEOUT = 60       # segundos por clip
MAX_RETRIES    = 3
MIN_FILE_SIZE  = 10_000     # bytes mínimos para considerar cache válida

# ─── LOGGING ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("yutu")

# ─── UTILIDADES ───────────────────────────────────────────────────────────────

def run_ffmpeg(args: list, step: str) -> dict:
    """Ejecuta FFmpeg y devuelve telemetría del proceso."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args
    log.info(f"  FFmpeg [{step}] → {' '.join(cmd[-6:])}")
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = round(time.time() - t0, 2)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg falló en [{step}]:\n{result.stderr[-800:]}")
    return {"step": step, "elapsed_s": elapsed}


def download_clip(escena: dict, dest: Path) -> dict:
    """Descarga un clip con reintentos y caché."""
    url = escena["url_video_mp4"]
    idx = escena["id_frase"]

    # Caché: si existe y pesa suficiente, reutilizar
    if dest.exists() and dest.stat().st_size > MIN_FILE_SIZE:
        log.info(f"  [Escena {idx:02d}] CACHE HIT → {dest.name}")
        return {"id_frase": idx, "status": "cached", "path": str(dest)}

    for intento in range(1, MAX_RETRIES + 1):
        try:
            log.info(f"  [Escena {idx:02d}] Descargando (intento {intento}/{MAX_RETRIES}): {url[-50:]}")
            headers = {"User-Agent": "Mozilla/5.0 (compatible; YutuBot/1.0)"}
            resp = requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT, headers=headers)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    f.write(chunk)
            size_mb = dest.stat().st_size / 1_048_576
            log.info(f"  [Escena {idx:02d}] ✓ Descargado ({size_mb:.1f} MB)")
            return {"id_frase": idx, "status": "downloaded", "path": str(dest), "size_mb": round(size_mb, 2)}
        except Exception as e:
            log.warning(f"  [Escena {idx:02d}] Intento {intento} fallido: {e}")
            if intento < MAX_RETRIES:
                time.sleep(2 ** intento)
            else:
                raise RuntimeError(f"Descarga fallida para escena {idx} tras {MAX_RETRIES} intentos: {e}")


def normalize_clip(raw_path: Path, norm_path: Path, duracion: int, idx: int) -> dict:
    """
    Normaliza un clip a 1920x1080@30fps, audio stereo, recortado exactamente.
    
    Filtro de video:
      - scale + pad para respetar aspecto sin distorsión (letterbox)
      - fps fijo a 30
      - trim exacto con setpts
    Filtro de audio:
      - Si existe audio → anull + atrim
      - Si no hay audio  → aevalsrc silencio stereo
    """
    # Detectar si el clip tiene pista de audio
    probe_cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=codec_type",
        "-of", "default=noprint_wrappers=1:nokey=1", str(raw_path)
    ]
    probe = subprocess.run(probe_cmd, capture_output=True, text=True)
    has_audio = "audio" in probe.stdout.strip()

    # Filtro de video: escala con letterbox + pad negro + fps + trim
    vf = (
        f"trim=start=0:end={duracion},setpts=PTS-STARTPTS,"
        f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease,"
        f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={TARGET_FPS},format=yuv420p"
    )

    if has_audio:
        af = f"atrim=start=0:end={duracion},asetpts=PTS-STARTPTS,aresample=44100,pan=stereo|c0=c0|c1=c0"
        ffmpeg_args = [
            "-i", str(raw_path),
            "-vf", vf,
            "-af", af,
            "-c:v", "libx264", "-crf", str(CRF), "-preset", PRESET,
            "-c:a", "aac", "-b:a", "192k",
            "-t", str(duracion),
            str(norm_path)
        ]
    else:
        # Generar audio silencioso de la misma duración
        ffmpeg_args = [
            "-i", str(raw_path),
            "-f", "lavfi", "-i", f"aevalsrc=0:s=44100:c=stereo:d={duracion}",
            "-vf", vf,
            "-c:v", "libx264", "-crf", str(CRF), "-preset", PRESET,
            "-c:a", "aac", "-b:a", "192k",
            "-t", str(duracion),
            "-map", "0:v", "-map", "1:a",
            str(norm_path)
        ]

    tel = run_ffmpeg(ffmpeg_args, f"normalize_escena_{idx:02d}")
    return {"id_frase": idx, "has_audio": has_audio, **tel}


def build_xfade_filtergraph(escenas: list) -> tuple[str, list]:
    """
    Construye el filter_complex para encadenar N clips con xfade.
    
    Retorna (filter_complex_string, [output_video_label, output_audio_label])
    
    Para N clips genera N-1 xfades encadenados acumulando el offset:
      offset_i = sum(duraciones[0..i-1]) - CROSSFADE_DUR * i
    """
    n = len(escenas)
    if n == 1:
        return "", ["0:v", "0:a"]

    parts_v = []
    parts_a = []
    offset_acum = 0.0

    for i in range(n - 1):
        dur_i = escenas[i]["duracion_video"]

        if i == 0:
            in_v = "[0:v]"
            in_a = "[0:a]"
        else:
            in_v = f"[vx{i}]"
            in_a = f"[ax{i}]"

        next_v = f"[{i+1}:v]"
        next_a = f"[{i+1}:a]"

        offset_acum += dur_i - CROSSFADE_DUR

        if i < n - 2:
            out_v = f"[vx{i+1}]"
            out_a = f"[ax{i+1}]"
        else:
            out_v = "[vfinal]"
            out_a = "[afinal]"

        parts_v.append(
            f"{in_v}{next_v}xfade=transition=fade:duration={CROSSFADE_DUR}:offset={offset_acum:.4f}{out_v}"
        )
        parts_a.append(
            f"{in_a}{next_a}acrossfade=d={CROSSFADE_DUR}:c1=tri:c2=tri{out_a}"
        )

    filtergraph = "; ".join(parts_v + parts_a)
    return filtergraph, ["[vfinal]", "[afinal]"]


def assemble_final(escenas: list, norm_paths: list) -> dict:
    """Ensambla todos los clips normalizados en el vídeo final con xfades."""
    log.info("  Construyendo filter_complex para xfade cinematográfico...")

    filtergraph, [out_v, out_a] = build_xfade_filtergraph(escenas)

    # Inputs: todos los clips normalizados
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
            + [str(OUTPUT_FINAL)]
        )
    else:
        # Solo 1 clip
        ffmpeg_args = (
            inputs
            + ["-c:v", "libx264", "-crf", str(CRF), "-preset", PRESET]
            + ["-c:a", "aac", "-b:a", "192k"]
            + [str(OUTPUT_FINAL)]
        )

    tel = run_ffmpeg(ffmpeg_args, "ensamblaje_final")
    size_mb = OUTPUT_FINAL.stat().st_size / 1_048_576
    log.info(f"  ✓ Vídeo final: {OUTPUT_FINAL.name} ({size_mb:.1f} MB)")
    return {**tel, "output": str(OUTPUT_FINAL), "size_mb": round(size_mb, 2)}


# ─── MAIN PIPELINE ────────────────────────────────────────────────────────────

def main():
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    telemetry = {
        "run_id": run_id,
        "inicio": datetime.now().isoformat(),
        "fases": {}
    }

    log.info("=" * 68)
    log.info("  YUTU PIPELINE — Ensamblador Cinematográfico v1.0")
    log.info("=" * 68)

    # Crear directorios
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # ── FASE 1: Carga del JSON de escenas ──────────────────────────────────
    log.info("\n[FASE 1] Cargando escenas desde input_escenas.json...")
    with open(INPUT_JSON, encoding="utf-8") as f:
        escenas = json.load(f)
    log.info(f"  → {len(escenas)} escenas cargadas.")
    telemetry["fases"]["carga"] = {"escenas": len(escenas)}

    # ── FASE 2: Descarga paralela de clips ─────────────────────────────────
    log.info(f"\n[FASE 2] Descargando {len(escenas)} clips (paralelo, max 4 workers)...")
    raw_paths = {}
    dl_results = []

    def _download(escena):
        dest = CLIPS_DIR / f"raw_{escena['id_frase']:02d}.mp4"
        return download_clip(escena, dest)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_download, e): e for e in escenas}
        for future in as_completed(futures):
            result = future.result()
            raw_paths[result["id_frase"]] = Path(result["path"])
            dl_results.append(result)

    dl_results.sort(key=lambda x: x["id_frase"])
    telemetry["fases"]["descarga"] = dl_results
    log.info(f"  → Descarga completa. {len(raw_paths)} clips disponibles.")

    # ── FASE 3: Normalización de clips ─────────────────────────────────────
    log.info(f"\n[FASE 3] Normalizando {len(escenas)} clips → 1920x1080@30fps...")
    norm_paths = []
    norm_results = []

    for escena in escenas:
        idx = escena["id_frase"]
        dur = escena["duracion_video"]
        raw = raw_paths[idx]
        norm = CLIPS_DIR / f"norm_{idx:02d}.mp4"

        # Caché de normalización
        if norm.exists() and norm.stat().st_size > MIN_FILE_SIZE:
            log.info(f"  [Escena {idx:02d}] NORM CACHE HIT → {norm.name}")
            norm_paths.append(norm)
            norm_results.append({"id_frase": idx, "status": "cached_norm"})
            continue

        log.info(f"  [Escena {idx:02d}] Normalizando ({dur}s)...")
        try:
            result = normalize_clip(raw, norm, dur, idx)
            norm_paths.append(norm)
            norm_results.append(result)
        except Exception as e:
            log.error(f"  [Escena {idx:02d}] ✗ Error de normalización: {e}")
            # Intentar con parámetros más simples como fallback
            log.warning(f"  [Escena {idx:02d}] Reintentando con modo compatibilidad...")
            try:
                compat_args = [
                    "-i", str(raw),
                    "-f", "lavfi", "-i", f"aevalsrc=0:s=44100:c=stereo:d={dur}",
                    "-vf", (
                        f"trim=start=0:end={dur},setpts=PTS-STARTPTS,"
                        f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease,"
                        f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2:black,"
                        f"fps={TARGET_FPS},format=yuv420p"
                    ),
                    "-c:v", "libx264", "-crf", "23", "-preset", "fast",
                    "-c:a", "aac", "-b:a", "128k",
                    "-t", str(dur),
                    "-map", "0:v", "-map", "1:a",
                    str(norm)
                ]
                run_ffmpeg(compat_args, f"compat_escena_{idx:02d}")
                norm_paths.append(norm)
                norm_results.append({"id_frase": idx, "status": "compat_mode"})
            except Exception as e2:
                log.error(f"  [Escena {idx:02d}] ✗ FALLO TOTAL: {e2}")
                raise

    telemetry["fases"]["normalizacion"] = norm_results
    log.info(f"  → Normalización completa. {len(norm_paths)} clips listos.")

    # ── FASE 4: Ensamblaje final con xfade ─────────────────────────────────
    log.info(f"\n[FASE 4] Ensamblando vídeo final con crossfade cinematográfico...")
    total_dur_raw = sum(e["duracion_video"] for e in escenas)
    total_dur_net = total_dur_raw - CROSSFADE_DUR * (len(escenas) - 1)
    log.info(f"  Duración bruta: {total_dur_raw}s → Duración neta (con overlaps): ~{total_dur_net:.0f}s ({total_dur_net/60:.1f} min)")

    asm_result = assemble_final(escenas, norm_paths)
    telemetry["fases"]["ensamblaje"] = asm_result

    # ── FASE 5: Materialización del log ────────────────────────────────────
    telemetry["fin"] = datetime.now().isoformat()
    log_path = LOGS_DIR / f"run_{run_id}.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(telemetry, f, indent=2, ensure_ascii=False)

    log.info("\n" + "=" * 68)
    log.info("  ✅ ATERRIZAJE EXITOSO. Artefactos materializados:")
    log.info(f"     🎬 Vídeo final : {OUTPUT_FINAL}")
    log.info(f"     📊 Log         : {log_path}")
    log.info(f"     ⏱  Duración   : ~{total_dur_net/60:.1f} minutos")
    log.info(f"     💾 Tamaño     : {asm_result['size_mb']} MB")
    log.info("=" * 68)


if __name__ == "__main__":
    main()
