#!/usr/bin/env python3
"""
overlay_map.py
===============
Bäddar in en bild (t.ex. en kartbild) i en video vid valfri position,
med automatisk bitdjups-detektering (8-bit/10-bit) för att undvika
onödig kvalitetsförlust. Avkodning sker via NVDEC (GPU) och encoding
via NVENC (GPU) - bara själva overlay-blandningen sker på CPU, eftersom
ffmpegs overlay_cuda-filter saknar 10-bit-stöd. Avkodning är deterministisk
(samma pixeldata oavsett hårdvara), så detta påverkar inte bildkvaliteten -
det flyttar bara det tyngsta beräkningssteget bort från CPU:n.

Fungerar oavsett videons upplösning, bildhastighet eller bitdjup -
skriptet läser dessa automatiskt ur filen via ffprobe och anpassar
ffmpeg-kommandot därefter. ffmpegs egen statusrad (frame=... fps=...
time=... speed=...) skrivs ut direkt, som när du kör ffmpeg manuellt.

Användning:
  python overlay_map.py <video> <bild> <x> <y> [--output OUTPUT] [--cq 15]

Exempel:
  python overlay_map.py GS010109.mp4 map.png 7080 1620
  python overlay_map.py GS010110.mp4 map.png 5000 1200 --output GS010110_karta.mp4
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def hitta_verktyg(namn: str) -> str:
    """Hittar sökväg till ffmpeg/ffprobe i PATH, eller avbryter med tydligt felmeddelande."""
    path = shutil.which(namn)
    if path is None:
        print(f"[FEL] Hittar inte '{namn}' i PATH.")
        print("      Installera ffmpeg och öppna en NY PowerShell (se README).")
        sys.exit(1)
    return path


def probe_json(ffprobe: str, filepath: Path) -> dict:
    """Kör ffprobe med JSON-output och returnerar resultatet som dict."""
    cmd = [
        ffprobe, "-v", "error",
        "-print_format", "json",
        "-show_entries", "stream=index,codec_type,pix_fmt,width,height,r_frame_rate,duration,"
                         "color_range,color_space,color_transfer,color_primaries",
        "-show_entries", "format=duration",
        str(filepath),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[FEL] ffprobe misslyckades för {filepath}:\n{result.stderr}")
        sys.exit(1)
    return json.loads(result.stdout)


def hitta_videostream(data: dict, filepath: Path) -> dict:
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    print(f"[FEL] Ingen videoström hittades i: {filepath}")
    sys.exit(1)


def ar_10bit(pix_fmt: str) -> bool:
    """10-bit-pixelformat i ffmpeg innehåller '10le'/'10be' i namnet (t.ex. yuv420p10le)."""
    return "10le" in pix_fmt or "10be" in pix_fmt


def fargtaggar(video_stream: dict) -> dict:
    """Videons färgegenskaper (matris, överföring, primärfärger, range).

    Utdata ska ALLTID få källvideons färgegenskaper - aldrig bildens. En PNG/JPG
    är RGB (eller full range), och lämnas valet åt ffmpeg kan bildens egenskaper
    följa med ut i filen: då märks en vanlig YUV-video som RGB ("gbr"), vilket
    senare steg (t.ex. xfade_concat.py) inte kan ta emot. Saknas en uppgift i
    källan används bt709 / tv, som gäller för HD-video och uppåt.
    """
    def hamta(nyckel: str, standard: str) -> str:
        varde = video_stream.get(nyckel)
        return standard if varde in (None, "", "unknown", "unspecified") else varde

    matris = hamta("color_space", "bt709")
    if matris in ("gbr", "rgb"):
        # YUV-video märkt som RGB är alltid fel (troligen en tidigare overlay).
        print("[VARNING] Videon är märkt som RGB (gbr) fast den är YUV - använder bt709.")
        matris = "bt709"
    return {
        "matris": matris,
        "trc": hamta("color_transfer", "bt709"),
        "primarer": hamta("color_primaries", "bt709"),
        "range": hamta("color_range", "tv"),
    }


def hamta_varaktighet(data: dict, video_stream: dict) -> float:
    dur = data.get("format", {}).get("duration") or video_stream.get("duration")
    return float(dur) if dur not in (None, "N/A") else 0.0


def kor_ffmpeg(cmd: list) -> None:
    """Kör ffmpeg och låter dess egen statusrad (frame=... time=... speed=...)
    skrivas ut direkt i konsolen, precis som vid manuell körning."""
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\n[FEL] ffmpeg avslutades med felkod {result.returncode}.")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bäddar in en bild i en video vid valfri position, med bevarat bitdjup."
    )
    parser.add_argument("video", type=Path, help="Sökväg till videofilen (t.ex. GS010109.mp4)")
    parser.add_argument("bild", type=Path, help="Sökväg till bilden som ska läggas ovanpå")
    parser.add_argument("x", type=int, help="X-position för bildens övre vänstra hörn")
    parser.add_argument("y", type=int, help="Y-position för bildens övre vänstra hörn")
    parser.add_argument("--output", type=Path, default=None,
                         help="Output-filnamn (default: <video>_overlay.mp4)")
    parser.add_argument("--cq", type=int, default=15,
                         help="NVENC-kvalitet, lägre = bättre (default: 15)")
    parser.add_argument("--preset", default="p7",
                         help="NVENC-preset p1 (snabbast) - p7 (bäst kvalitet, default)")
    parser.add_argument("--cpu-decode", action="store_true",
                         help="Avkoda med CPU istället för NVDEC (GPU). Långsammare, "
                              "men kan vara bra att felsöka med om NVDEC strular.")
    parser.add_argument("--opacity", type=float, default=100.0,
                         help="Bildens genomskinlighet i procent, 0-100 "
                              "(default: 100 = helt synlig).")
    args = parser.parse_args()

    if not 0.0 <= args.opacity <= 100.0:
        print(f"[FEL] --opacity måste vara mellan 0 och 100 (fick {args.opacity}).")
        sys.exit(1)
    alfa = args.opacity / 100.0

    if not args.video.exists():
        print(f"[FEL] Hittar inte videofilen: {args.video}")
        sys.exit(1)
    if not args.bild.exists():
        print(f"[FEL] Hittar inte bildfilen: {args.bild}")
        sys.exit(1)

    output = args.output or args.video.with_name(f"{args.video.stem}_overlay.mp4")
    if output.exists():
        svar = input(f"'{output}' finns redan. Skriv över? [y/N] ")
        if svar.strip().lower() != "y":
            print("Avbryter.")
            sys.exit(0)

    ffmpeg = hitta_verktyg("ffmpeg")
    ffprobe = hitta_verktyg("ffprobe")

    print(f"[INFO] Läser videoinfo: {args.video}")
    video_info = probe_json(ffprobe, args.video)
    v = hitta_videostream(video_info, args.video)
    bredd, hojd = v["width"], v["height"]
    pix_fmt = v.get("pix_fmt", "okänt")
    tio_bit = ar_10bit(pix_fmt)
    varaktighet = hamta_varaktighet(video_info, v)
    farg = fargtaggar(v)

    print(f"[INFO] Läser bildinfo: {args.bild}")
    bild_info = probe_json(ffprobe, args.bild)
    b = hitta_videostream(bild_info, args.bild)
    b_bredd, b_hojd = b["width"], b["height"]

    print(f"\n  Video:  {bredd}x{hojd}, pixelformat={pix_fmt} "
          f"({'10-bit' if tio_bit else '8-bit'}), {varaktighet:.1f}s")
    print(f"  Färg:   {farg['matris']}/{farg['trc']}/{farg['primarer']}, range={farg['range']}")
    print(f"  Bild:   {b_bredd}x{b_hojd} vid position ({args.x}, {args.y})\n")

    if args.x + b_bredd > bredd or args.y + b_hojd > hojd or args.x < 0 or args.y < 0:
        print(f"[VARNING] Bilden ({b_bredd}x{b_hojd}) vid ({args.x},{args.y}) hamnar helt eller")
        print(f"          delvis utanför videoramen ({bredd}x{hojd}).")
        svar = input("Fortsätt ändå? [y/N] ")
        if svar.strip().lower() != "y":
            sys.exit(0)

    # Bygg filterkedjan så att källans bitdjup bevaras genom overlay-steget.
    # Videon avkodas via NVDEC (GPU) om inte --cpu-decode angetts - "hwdownload"
    # hämtar sedan ner frames till system-RAM i EXAKT samma pixelformat som
    # CPU-overlay-filtret redan tog emot innan, så resten av kedjan är oförändrad.
    # 10-bit källa: tvinga yuv420p10le genom kedjan + main10-profil på encodern.
    # 8-bit källa: enkel overlay, inget att bevara utöver källans egen kvalitet.
    anvand_nvdec = not args.cpu_decode

    # Färg: bilden konverteras till YUV med VIDEONS matris och range (annars
    # väljer ffmpeg själv, och kartans färger kan förskjutas), och resultatet
    # märks uttryckligen med videons färgegenskaper både i filtret (setparams)
    # och till kodaren - så att bildens RGB-egenskaper aldrig följer med ut.
    bild_konv = f"scale=out_color_matrix={farg['matris']}:out_range={farg['range']}"
    satt_farg = (f"setparams=colorspace={farg['matris']}:color_trc={farg['trc']}"
                 f":color_primaries={farg['primarer']}:range={farg['range']}")
    farg_args = ["-colorspace", farg["matris"], "-color_trc", farg["trc"],
                 "-color_primaries", farg["primarer"], "-color_range", farg["range"]]

    if tio_bit:
        print("[INFO] 10-bit källa upptäckt - bevarar bitdjup genom overlay-steget.")
        # NVDEC lagrar 10-bit internt som p010le - hwdownload måste hämta i det
        # formatet, sedan konverterar vi separat till yuv420p10le (ren layout-
        # omstrukturering, ingen precisionsförlust).
        bas_filter = "hwdownload,format=p010le,format=yuv420p10le" if anvand_nvdec else "format=yuv420p10le"
        filter_complex = (
            f"[0:v]{bas_filter},{satt_farg}[base];"
            f"[1:v]{bild_konv},format=yuva420p10le,colorchannelmixer=aa={alfa}[ovl];"
            f"[base][ovl]overlay={args.x}:{args.y}:format=yuv420p10,{satt_farg}[out]"
        )
        extra_output_args = ["-profile:v", "main10", "-pix_fmt", "p010le"]
    else:
        print("[INFO] 8-bit källa upptäckt - standard overlay.")
        # NVDEC lagrar 8-bit internt som nv12 - samma logik som ovan.
        bas_filter = "hwdownload,format=nv12,format=yuv420p" if anvand_nvdec else "null"
        filter_complex = (
            f"[0:v]{bas_filter},{satt_farg}[base];"
            f"[1:v]{bild_konv},format=yuva420p,colorchannelmixer=aa={alfa}[ovl];"
            f"[base][ovl]overlay={args.x}:{args.y},{satt_farg}[out]"
        )
        extra_output_args = []

    if args.opacity != 100.0:
        print(f"[INFO] Genomskinlighet: {args.opacity:.0f}% synlig.")

    ingang_video = (
        ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", str(args.video)]
        if anvand_nvdec else
        ["-i", str(args.video)]
    )
    if anvand_nvdec:
        print("[INFO] Avkodar med NVDEC (GPU) - avlastar CPU:n. Ingen kvalitetspåverkan "
              "(avkodning är deterministisk).")
    else:
        print("[INFO] Avkodar med CPU (--cpu-decode angiven).")

    cmd = [
        ffmpeg, "-hide_banner", "-y",
        *ingang_video,
        "-i", str(args.bild),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-map", "0:a?",
        "-c:v", "hevc_nvenc",
        "-preset", args.preset,
        "-tune", "hq",
        "-rc", "vbr",
        "-cq", str(args.cq),
        "-b:v", "0",
        *extra_output_args,
        *farg_args,
        "-c:a", "copy",
        str(output),
    ]

    print(f"[INFO] Kör ffmpeg -> {output}\n")
    kor_ffmpeg(cmd)
    print(f"\n\u2705 Klar: {output}")


if __name__ == "__main__":
    main()
