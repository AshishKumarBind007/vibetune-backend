# VibeTune

VibeTune is a mood-based music recommendation desktop application. Users can choose a mood, genre, and energy level, receive song recommendations from the VibeTune API, and play available tracks through the app's existing Audius playback integration.

## Features

- Desktop interface built with **Python** and **PySide6**.
- Recommendations based on **mood**, **genre**, and **energy level**.
- Cloud recommendation API hosted on Render.
- Existing Audius search and playback integration.
- Audio playback controls and an animated visualizer.
- Packaged Windows build created with PyInstaller.

## Project details

- Dataset: 200,605 songs in the current recommendation dataset.
- Genres exposed by the API: 87.
- Mood categories: 7.
- Energy options: `low`, `medium`, `high`.
- API base URL: `https://vibetune-api.onrender.com`

The app requires an internet connection for cloud recommendations and online track resolution/playback. Track availability depends on the playback provider.

## Requirements

For running from source:

- Windows 10/11
- Python 3.12 (the build was made with Python 3.12.10)
- Internet connection

## Run from source

Open PowerShell in the project directory and activate your virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install runtime dependencies:

```powershell
python -m pip install -r requirements.txt
```

Compile-check and launch:

```powershell
python -m py_compile main.py
python main.py
```

## Build the Windows application

Install PyInstaller:

```powershell
python -m pip install pyinstaller
```

Build the app:

```powershell
python -m PyInstaller --noconfirm --clean --windowed --name VibeTune --collect-all PySide6 main.py
```

The current folder-based build is expected at:

```text
dist/
└── VibeTune/
    ├── VibeTune.exe
    └── supporting files...
```

Keep the entire `dist\VibeTune` folder together when moving or sharing the app. Do not distribute only the executable unless you later create and test a true one-file build.

To create a ZIP of the folder:

```powershell
Compress-Archive -Path ".\dist\VibeTune" -DestinationPath ".\VibeTune-Windows.zip" -Force
```

## API

The desktop application uses the deployed API:

- Base URL: `https://vibetune-api.onrender.com`
- Health check: `/health`
- Recommendation route: `/recommend`

Example recommendation request:

```json
{
  "mood": "happy",
  "genre": "pop",
  "energy": "high",
  "limit": 10
}
```

The API's deployed health endpoint should be checked before a demo. A hosted service on a free or sleep-enabled tier may take time to wake up after inactivity.

## Suggested final test checklist

- [ ] App launches from `python main.py`.
- [ ] Packaged `dist\VibeTune\VibeTune.exe` launches on Windows.
- [ ] Mood, genre, and energy selections return recommendations.
- [ ] Playback starts for an available track.
- [ ] Pause, resume, next track, and volume work as expected.
- [ ] The app remains usable if a request fails or a track is unavailable.
- [ ] The `dist\VibeTune` folder is zipped and tested after extraction.

## Architecture overview

1. **PySide6 desktop UI** collects the user's mood, genre, and energy selections.
2. **VibeTune API** receives the recommendation request and returns matching tracks.
3. **Recommendation engine** uses the existing processed dataset and recommendation logic deployed behind the API.
4. **Audius resolver/player** attempts to locate and stream an available track.
5. **Audio visualizer** presents playback-related animation in the desktop interface.

## Known limitations

- Recommendations require a working internet connection and a reachable API.
- Playback depends on a track being available through the existing Audius integration.
- The packaged app has been built and launched on the development Windows machine; test it on another Windows computer before public distribution.
- The current visualizer fallback uses playback-position-driven animation rather than verified real-time waveform analysis.

## Repository hygiene

Do not commit virtual environments, generated build folders, caches, API secrets, or private dataset files unless intentionally required. Useful items to keep in the project repository include:

- `main.py`
- `requirements.txt`
- `README.md`
- backend source and deployment configuration, if maintained in this repository
- a suitable license and attribution notices for the dataset and dependencies

## Credits and licensing

Add the dataset's actual source, license, and attribution here before public distribution. Also review the applicable terms of the playback provider and the licenses for all dependencies and included assets.
