# ffmpeg-image-embed

Windows/PowerShell-verktyg som bäddar in en bild (t.ex. en kartöverlagring) i en
video vid valfri pixel-position, med automatisk bitdjups-detektering (8-bit/10-bit)
för att undvika onödig kvalitetsförlust. GPU-accelererad HEVC-encoding via NVIDIA
NVENC.

Byggt för GoPro MAX/MAX2 360°-flygfilm som post-processas med
[gopro-max-gpx-pipeline](https://github.com/zekesixniner/gopro-max-gpx-pipeline)
och [OVRLEY](https://github.com/spirokai/OVRLEY), men fungerar på vilken
H.264/H.265-video som helst.

## Vad det gör

- Läser video- och bildinfo automatiskt via `ffprobe` (upplösning, bitdjup) —
  du anger aldrig detta manuellt
- Upptäcker om källan är 8-bit eller 10-bit och bygger rätt ffmpeg-filterkedja
  därefter, så bitdjupet bevaras genom overlay-steget istället för att tystas
  ner till 8-bit i onödan
- Varnar om bildens position skulle hamna helt eller delvis utanför videoramen
- Avkodar via NVDEC (GPU) och kodar via `hevc_nvenc` (GPU), `-cq 15`
  (visuellt lossless) som standard — bara overlay-blandningen sker på CPU
  (`--cpu-decode` finns om du behöver avkoda på CPU istället, t.ex. vid
  felsökning)
- Skriver ut ffmpegs egen statusrad rakt av — ingen egen progress-parsning

## Krav

- Windows 10/11
- Python 3.x (testat på 3.14, installerad via Python Install Manager /
  Microsoft Store)
- ffmpeg med NVENC-stöd
- NVIDIA GPU med NVENC (Turing/RTX 20-serien eller senare rekommenderas)

## Installation

### 1. Hämta koden

Repot utvecklas i WSL men körs på native Windows (behövs för GPU-åtkomst —
NVENC via CUDA fungerar inte genom WSL1, och kräver extra konfiguration i
WSL2). Klona i WSL som vanligt:

```bash
git clone https://github.com/<ditt-användarnamn>/gopro-video-overlay.git ~/dev/gopro-video-overlay
```

Kopiera sedan skriptet till en Windows-katalog via `/mnt/c/`-sökvägen:

```bash
mkdir -p /mnt/c/Users/<dittnamn>/bin
cp ~/dev/gopro-video-overlay/overlay_map.py /mnt/c/Users/<dittnamn>/bin/overlay_map.py
```

(Alternativt: ladda ner `overlay_map.py` direkt i en Windows-webbläsare från
GitHub om du inte vill klona alls.)

### 2. Installera ffmpeg (Windows)

**Om `winget` finns tillgängligt** (saknas t.ex. på Windows IoT Enterprise LTSC
— kontrollera med `winget --version` i PowerShell):

```powershell
winget install "FFmpeg (Essentials Build)"
```

**Manuellt (fungerar alltid, oavsett Windows-utgåva):**

```powershell
Invoke-WebRequest -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile "$env:TEMP\ffmpeg.zip"
Expand-Archive -Path "$env:TEMP\ffmpeg.zip" -DestinationPath "$env:TEMP\ffmpeg_extract" -Force
if (Test-Path "C:\ffmpeg") { Remove-Item "C:\ffmpeg" -Recurse -Force }
$extracted = Get-ChildItem "$env:TEMP\ffmpeg_extract" -Directory | Select-Object -First 1
Move-Item $extracted.FullName "C:\ffmpeg"
$currentUserPath = [Environment]::GetEnvironmentVariable("Path", "User")
[Environment]::SetEnvironmentVariable("Path", "$currentUserPath;C:\ffmpeg\bin", "User")
```

Öppna en **ny** PowerShell efteråt (PATH läses bara in vid ny session),
verifiera:

```powershell
ffmpeg -version
ffprobe -version
```

### 3. Lägg skriptet i PATH

```powershell
$currentUserPath = [Environment]::GetEnvironmentVariable("Path", "User")
[Environment]::SetEnvironmentVariable("Path", "$currentUserPath;C:\Users\<dittnamn>\bin", "User")
```

Ny PowerShell igen, verifiera:

```powershell
python C:\Users\<dittnamn>\bin\overlay_map.py --help
```

## Användning

```powershell
python overlay_map.py <video> <bild> <x> <y> [--output OUTPUT] [--cq 15] [--preset p7]
```

| Argument   | Beskrivning                                                    |
|------------|------------------------------------------------------------------|
| `video`    | Sökväg till videofilen                                          |
| `bild`     | Sökväg till bilden som ska läggas ovanpå                        |
| `x` `y`    | Pixel-position för bildens övre vänstra hörn                    |
| `--output` | Output-filnamn (default: `<video>_overlay.mp4`)                 |
| `--cq`     | NVENC-kvalitet, lägre = bättre (default: 15)                    |
| `--preset` | NVENC-preset p1 (snabbast) – p7 (bäst kvalitet, default)         |
| `--opacity`| Bildens genomskinlighet i procent, 0-100 (default: 100)         |

### Exempel

```powershell
python overlay_map.py GS010096.mp4 map.png 7080 1620
python overlay_map.py GS010110.mp4 map.png 4500 2000 --output karta_klar.mp4 --cq 18
python overlay_map.py GS010096.mp4 map.png 7080 1620 --opacity 60
```

## Hur bitdjupsbevarandet fungerar

GoPro MAX2 spelar in olika bitdjup beroende på upplösning/bildhastighet —
t.ex. 7680×3840@25fps är 10-bit (`yuv420p10le`), medan 5376×2688@50fps och
4096×2048@100fps är 8-bit (`yuv420p`). Skriptet läser källans faktiska
pixelformat via `ffprobe` och väljer filterkedja därefter:

- **10-bit källa:** `format=yuv420p10le` tvingas genom hela filterkedjan
  (bas + overlay-bild), `overlay`-filtret ges explicit `format=yuv420p10`,
  och encodern körs med `-profile:v main10 -pix_fmt p010le`. Utan detta
  faller `hevc_nvenc` tillbaka till 8-bit som standard även om källan är
  10-bit.
- **8-bit källa:** enkel `overlay`-filterkedja utan extra
  pixelformat-tvång — inget att bevara utöver källans egen kvalitet.

### Känd begränsning: GPU-overlay (CUDA) stödjer inte 10-bit

`overlay_cuda`-filtret (full GPU-pipeline: `-hwaccel cuda
-hwaccel_output_format cuda` + `overlay_cuda`) kastar `Unsupported main
input format: p010le` för 10-bit-källor — en känd, olöst begränsning i
ffmpeg sedan minst 2019/2020 (bekräftat mot ffmpeg-devel-mejllistan, samt
empiriskt mot både fristående ffmpeg-nightlies och OVRLEYs bundlade
ffmpeg, augusti 2026). `scale_cuda` (ren skalning) fick en liknande
bugfix för >8-bit-format vid ett tillfälle, men `overlay_cuda` specifikt
har aldrig fått motsvarande stöd.

### Varför NVDEC-avkodning ändå inte påverkar kvaliteten

Skriptet avkodar videon med NVDEC (GPU) istället för mjukvara (CPU), men
utför själva overlay-blandningen på CPU precis som innan — bara
avkodningssteget flyttas. Detta är säkert kvalitetsmässigt eftersom
avkodning är deterministisk: att avkoda en HEVC-bitström innebär bara att
återskapa de pixelvärden som redan är kodade i filen enligt en fastlåst
specifikation (IDCT, motion compensation, deblocking). Det finns inget
kreativt tolkningsutrymme som vid encoding, där olika encoders kan ge
olika resultat vid samma bitrate. NVDEC och ffmpegs mjukvaruavkodare
följer samma spec och producerar samma pixeldata. `hwdownload` hämtar ner
de NVDEC-avkodade frames till system-RAM i exakt samma pixelformat
(`yuv420p10le`/`yuv420p`) som CPU-overlay-filtret redan tog emot innan,
så resten av kedjan är oförändrad — bara CPU-belastningen minskar
kraftigt, vilket är särskilt märkbart på 8K 10-bit-material där
mjukvaruavkodning annars är den tyngsta delen av hela pipelinen.

## Licens

MIT
