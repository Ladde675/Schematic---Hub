# Schematic Hub online stellen (Render, mit GitHub)

Du brauchst: ein GitHub-Konto (gratis) und ein Render-Konto (der dauerhafte Speicher kostet dort etwas).

## 1. Dateien auf GitHub
1. github.com -> Konto anlegen oder einloggen -> oben rechts "+" -> "New repository".
2. Name: schematic-hub, "Private" waehlen, "Create repository".
3. Auf der leeren Repo-Seite: "uploading an existing file" anklicken.
4. ALLE Dateien und Ordner aus diesem Ordner hineinziehen (Dockerfile, server.py, own_models.py, mcassets.py, index.html, static, downloads, .dockerignore, .gitignore). Unten "Commit changes".

## 2. Auf Render starten
1. render.com -> "Get Started" -> mit GitHub einloggen.
2. "New +" -> "Web Service" -> dein Repo schematic-hub waehlen -> "Connect".
3. Language: Docker (wird erkannt). Region: Frankfurt. Instance Type: "Starter" (der kleinste bezahlte, ein Gratis-Typ hat keinen dauerhaften Speicher, dann waeren Konten nach jedem Neustart weg).
4. Weiter unten bei "Environment Variables" hinzufuegen:  HUB_INVITE = ein Code deiner Wahl (zum Beispiel bau-geheim-123). Den gibst du nur Freunden.
5. Bei "Disks" -> "Add Disk": Name data, Mount Path /data, Size 1 GB.
6. Health Check Path: /api/health
7. "Create Web Service". Nach ein paar Minuten steht oben dein Link, z.B. https://schematic-hub-xyz.onrender.com

## 3. Danach
1. Link oeffnen und SOFORT als Erster ein Konto erstellen (mit deinem Einladungscode). Das erste Konto ist Admin.
2. Deine Schematics neu hochladen (die Konten und Dateien von deinem Mac werden nicht automatisch mitgenommen).
3. Link und Einladungscode an Freunde schicken.

## Aktualisieren
Neue Dateien in dasselbe GitHub-Repo hochladen (ersetzen). Render baut automatisch neu, die Daten auf /data bleiben.

## Hinweise
- Preise und Menues aendern sich, schau die aktuellen Preise bei Render an, bevor du bestaetigst.
- Die Seite zeigt dort nur selbst gezeichnete Texturen (keine Mojang-Dateien).
- Backup: Im Render-Dashboard kannst du Disk-Snapshots anlegen.
