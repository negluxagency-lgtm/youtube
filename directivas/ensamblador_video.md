# DIRECTIVA: Ensamblador Cinematográfico de Vídeo Documental
**Versión:** 1.0  
**Estado:** ACTIVA  
**Motor:** FFmpeg 8.x + Python 3.10+  
**Última actualización:** 2026-05-22

---

## 1. OBJETIVO
Tomar un JSON de escenas (url_video_mp4 + duracion_video) y producir un vídeo documental
final con calidad cinematográfica: clips recortados, normalizados, con crossfade suave y
resolución 1080p uniforme.

---

## 2. ARQUITECTURA DE PIPELINE

```
input_escenas.json
       │
       ▼
[FASE 1] Descarga paralela de clips brutos → artifacts/clips/raw_XX.mp4
       │
       ▼
[FASE 2] Normalización FFmpeg por clip:
         - Resolución: 1920x1080 (pad + scale, sin distorsión)
         - FPS: 30
         - Codec video: libx264, CRF 21, preset fast
         - Codec audio: aac 192k (silent si no hay audio)
         - Trim: exactamente duracion_video segundos desde el segundo 0
         - (EJECUCIÓN PARALELIZADA PARA MAYOR VELOCIDAD)
         → artifacts/clips/norm_XX.mp4
       │
       ▼
[FASE 3] Concatenación con xfade (crossfade 0.5s entre escenas)
         - Construye filter_complex dinámico según N clips
         → artifacts/documental_final.mp4
       │
       ▼
[FASE 4] Log de materialización → artifacts/logs/run_TIMESTAMP.json
```

---

## 3. PARÁMETROS CRÍTICOS

| Parámetro | Valor | Razón |
|---|---|---|
| Resolución salida | 1920x1080 | Estándar YouTube 1080p |
| FPS | 30 | Fluido y compatible |
| CRF video | 21 | Equilibrio entre calidad y velocidad para YouTube |
| Preset encode | fast | Aceleración del renderizado |
| Crossfade duración | 0.5s | Suave pero sin ralentizar |
| Crossfade tipo | fade | Neutro, cinematográfico |
| Audio silencio | aevalsrc=0 | Si el clip no tiene audio |
| Timeout descarga | 60s por clip | Evita bloqueos |
| Reintentos descarga | 3 | Resiliencia de red |

---

## 4. RESTRICCIONES

- ⛔ NO usar `-c copy` en ningún paso (rompe normalización y filtros)
- ⛔ NO mezclar clips con distintos FPS/resolución antes de normalizar
- ⛔ El offset de xfade debe calcularse acumulativamente restando la duración de crossfade
- ⛔ Con N clips se necesitan N-1 xfades encadenados
- ✅ La FASE 3 (Normalización) debe ejecutarse en paralelo (ThreadPoolExecutor)
- ✅ Usar `-ss` ANTES de `-i` para seek rápido (keyframe), luego trim con filtro
- ✅ Siempre generar audio aunque el clip original sea mudo (aevalsrc=0:s=44100:c=stereo)
- ✅ Los clips descargados deben cachearse: si el archivo existe y pesa > 10KB, no re-descargar

---

## 5. BITÁCORA DE ANOMALÍAS

| Fecha | Error | Causa | Solución aplicada |
|---|---|---|---|
| 2026-05-23 | Renderizado lento (40min para 13 clips) | Uso de preset slow de forma secuencial | Se cambia a preset fast, CRF 21 y se paraleliza la Fase 3 de normalización |

---

## 6. OUTPUTS ESPERADOS

- `artifacts/clips/raw_XX.mp4` — clips brutos descargados
- `artifacts/clips/norm_XX.mp4` — clips normalizados por escena
- `artifacts/documental_final.mp4` — vídeo final ensamblado
- `artifacts/logs/run_TIMESTAMP.json` — telemetría completa de la ejecución
