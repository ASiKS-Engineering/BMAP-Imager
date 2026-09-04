# Bmap Imager

Bmap Imager ist ein Windows-Flashtool mit CustomTkinter-GUI. Es schreibt
Disk-Images direkt auf physische Laufwerke und unterstützt `.bmap`-Dateien.
Bei einer BMAP-Datei werden nur die gemappten Bereiche geschrieben. Dadurch
bleiben Sparse-Lücken unberührt und große Images lassen sich effizient auf
SD-Karten oder USB-Datenträger schreiben.

## Funktionen

- moderne CustomTkinter-Oberfläche
- Auswahl von `.img`, `.raw` und `.bin` Images
- separate Auswahl einer optionalen `.bmap`-Datei
- automatische Erkennung einer passenden Datei mit dem Namen `image.img.bmap`
- BMAP-Sparse-Write oder vollständiger Raw-Write ohne BMAP
- optionale SHA-Prüfsummenprüfung aus der BMAP-Datei
- Fortschrittsanzeige und Abbruch während des Schreibens
- Erkennung von Modell, Größe, Bus, Laufwerksbuchstaben und Wechseldatenträgern
- Systemlaufwerke im Speicherdialog standardmäßig ausblendbar
- Sperren und Aushängen der Windows-Volumes vor dem Raw-Write
- native CLI-Engine für Diagnose und Kommandozeilenbetrieb

## Voraussetzungen für die GUI

- Windows 10 oder Windows 11, 64 Bit
- Python 3.10 oder neuer
- Administratorrechte zum Schreiben auf `\\.\PhysicalDriveN`
- Python-Pakete aus `gui/requirements.txt`

### Python installieren

Python kann von python.org installiert werden. Im Installer sollte **Add
Python to PATH** aktiviert werden. Danach prüfen:

```powershell
py --version
```

Falls mehrere Python-Versionen installiert sind, kann die Version explizit
angegeben werden, zum Beispiel `py -3.10`.

### GUI-Abhängigkeiten installieren

Aus dem Projektverzeichnis:

```powershell
cd O:\Tools\bmapflash
py -3.10 -m pip install -r gui\requirements.txt
```

## GUI starten

PowerShell oder CMD als Administrator öffnen:

```powershell
cd O:\Tools\bmapflash\gui
py -3.10 app.py
```

Ablauf:

1. Unter **Image** die `.img`, `.raw` oder `.bin` Datei auswählen.
2. Unter **.bmap-Datei** die zugehörige BMAP-Datei auswählen oder leer lassen.
3. Unter **Ziel** das physische Laufwerk auswählen.
4. Optional die Prüfsummenprüfung aktivieren.
5. **SCHREIBEN** auswählen und die Sicherheitsabfrage exakt mit `YES` bestätigen.

Wenn die BMAP-Datei direkt neben dem Image liegt und `image.img.bmap` heißt,
wird sie automatisch vorausgewählt. Sie kann in der GUI jederzeit geändert oder
entfernt werden.

## GUI als EXE bauen

Für eine eigenständige Windows-EXE wird PyInstaller verwendet:

```powershell
cd O:\Tools\bmapflash
py -3.10 -m pip install -r gui\requirements.txt
py -3.10 -m pip install pyinstaller
py -3.10 -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name BmapImager `
  --paths gui `
  gui\app.py
```

Die fertige Datei liegt danach hier:

```text
dist\BmapImager.exe
```

Die EXE muss für das tatsächliche Flashen ebenfalls als Administrator gestartet
werden. Auf Windows kann das per Rechtsklick **Als Administrator ausführen**
geschehen. Für eine feste UAC-Anforderung kann beim Build zusätzlich eine
Manifest-Datei verwendet werden:

```powershell
py -3.10 -m PyInstaller --noconfirm --clean --onefile --windowed `
  --uac-admin `
  --name BmapImager `
  --paths gui `
  gui\app.py
```

## Native C++-CLI bauen

Die native CLI bleibt für Diagnose und Skripte verfügbar. Empfohlen wird
MSYS2 mit MinGW-w64.

### MSYS2 installieren

In einer MSYS2-UCRT64-Shell:

```bash
pacman -Syu
pacman -S --needed mingw-w64-ucrt-x86_64-gcc mingw-w64-ucrt-x86_64-cmake
```

### Build-Skript

In PowerShell mit `g++.exe` und `cmake.exe` im `PATH`:

```powershell
cd O:\Tools\bmapflash
.\build-mingw.ps1
```

Oder:

```powershell
.\build-mingw.bat
```

Die Ausgabe liegt je nach Skript unter:

```text
bin\bmapflash.exe
```

### CMake-Build

```powershell
cmake -S . -B build -G "MinGW Makefiles" -DCMAKE_BUILD_TYPE=Release
cmake --build build
```

Die Ausgabe liegt dann unter:

```text
build\bmapflash.exe
```

Nicht den Visual-Studio-Generator verwenden. Entscheidend ist der Generator
`MinGW Makefiles`.

Falls MinGW nicht gefunden wird, liegt der Compiler bei einer Standard-MSYS2-
Installation meistens hier:

```powershell
$env:Path = 'C:\msys64\ucrt64\bin;C:\msys64\usr\bin;' + $env:Path
```

## CLI verwenden

PowerShell oder CMD als Administrator öffnen:

```powershell
.\bin\bmapflash.exe list
```

Image und BMAP prüfen:

```powershell
.\bin\bmapflash.exe info `
  'image.img' `
  'image.img.bmap'
```

Gemappte BMAP-Bereiche schreiben:

```powershell
.\bin\bmapflash.exe flash `
  'image.img' `
  'image.img.bmap' `
  PhysicalDrive5
```

Die Laufwerksnummer ist nur ein Beispiel. Vor jedem Schreibvorgang erneut mit
`list` prüfen, welches Laufwerk tatsächlich das gewünschte Ziel ist.

## Sicherheit

Ein Raw-Write auf `\\.\PhysicalDriveN` zerstört vorhandene Daten auf dem
gewählten Ziellaufwerk. Vor dem Schreiben immer Modell, Größe und
`PhysicalDrive`-Nummer kontrollieren.

- Keine Systemlaufwerke als Ziel verwenden.
- Die GUI blendet Systemlaufwerke standardmäßig aus.
- Die Filteroption kann Systemlaufwerke nur bewusst wieder sichtbar machen;
  die Auswahl erfordert eine zusätzliche Warnbestätigung.
- Explorer, Datenträgerverwaltung und andere Programme sollten das Ziel nicht
  verwenden.
- Für das Schreiben sind Administratorrechte erforderlich.
- Ein Abbruch während des Schreibens kann ein unvollständiges Medium ergeben.

## Projektstruktur

```text
gui/
  app.py                 CustomTkinter-GUI
  requirements.txt       Python-Abhängigkeiten
  core/
    bmap.py              BMAP-XML-Parser
    diskutil.py          Windows-Disk- und Volume-Verwaltung
    flasher.py           BMAP- und Raw-Writer
src/
  main.cpp               native C++-CLI
CMakeLists.txt            C++-Build-Konfiguration
build-mingw.ps1           MinGW-Build-Skript
build-mingw.bat           Batch-Wrapper für den Build
```

## Technische Hinweise

Die GUI verwendet `pywin32` für WMI, Windows-IOCTLs und den Zugriff auf
physische Laufwerke. Vor dem Schreiben werden bekannte Volumes des Zielgeräts
versucht zu sperren und auszuhängen. Die BMAP-Datei wird als XML geparst; ihre
`image-size`, `block-size` und `Range`-Einträge werden vor dem Schreiben
validiert.
