# Dialogue transcription for Mac (Apple Silicon)

Turns a video or audio recording into text where every line is labelled with the speaker:

```
[SPEAKER_00]: let's start with the second item on the agenda
[SPEAKER_01]: sure, I'll walk you through last month's numbers
```

Everything runs locally on your Mac — nothing is uploaded anywhere.

Russian is the default language ([configuration](#configuration) explains how to change it).

Русская версия: [README.ru.md](README.ru.md)

## What you need

- A Mac with an Apple Silicon chip (M1 or newer).
- About 10 GB of free disk space (the speech models are large).
- A free Hugging Face account — needed once, to download the speaker-detection model.

## Setup (once)

**1. Install Homebrew** (skip it if you already have it). Open the Terminal app and paste:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

**2. Get a Hugging Face token:**

- Create an account at https://huggingface.co/join
- Open https://huggingface.co/pyannote/speaker-diarization-community-1 and accept the model terms (a form appears the first time).
- Open https://huggingface.co/settings/tokens → **Create new token** → type **Read** → copy the token (a long string starting with `hf_`).

**3. Run the installer:** double-click `install.command` in this folder.

A Terminal window opens and installs everything. At the end it asks for the token — paste it and press Enter. Nothing shows up while you paste; that's on purpose.

The whole step takes a few minutes. If macOS refuses to start the file ("unidentified developer"), right-click it → **Open** → **Open**.

Prefer the Terminal? `./install.sh` does the same thing.

## Everyday use

1. Copy your video or audio files into the `input` folder (mp4, mov, mp3, m4a, wav — anything ffmpeg reads).
2. Double-click `run.command` (or run `./run.sh` in the Terminal).
3. Wait. The first run also downloads about 5 GB of models, so it is much slower than the following ones.

Results appear in `output/<file name>/`:

| File   | What it is                                       |
|--------|--------------------------------------------------|
| `.txt` | plain transcript with speaker labels — start here |
| `.srt` | subtitles, playable next to the video            |
| `.vtt` | subtitles for web players                        |
| `.tsv` | table with timestamps, opens in Excel/Numbers    |
| `.json`| all details, for further processing              |

To transcribe one specific file instead of the whole `input` folder:

```bash
./run.sh ~/Desktop/interview.mp4
```

## Configuration

The settings live at the top of `run.sh` — open it in any text editor and change the values after `:-`:

| Setting      | Default     | Meaning                                                        |
|--------------|-------------|----------------------------------------------------------------|
| `LANGUAGE`   | `ru`        | spoken language: `en`, `de`, `fi`, … (`en` for English)         |
| `SPEAKERS`   | `2`         | how many people talk in the recording                          |
| `MODEL`      | `large-v3`  | `medium` or `small` are faster but less accurate               |
| `FORMAT`     | `all`       | set to `txt` to only get the plain transcript                  |
| `OUTPUT_DIR` | `output`    | where results are written                                      |

You can also set them for a single run without editing anything:

```bash
SPEAKERS=3 LANGUAGE=en ./run.sh
```

## If something goes wrong

- **Messages about `libtorchcodec`, `Lightning automatically upgraded…`, or `No --hf_token provided`** — harmless, the transcription still runs. Only worry if the run stops early.
- **"unidentified developer"** when double-clicking — right-click the file → **Open** → **Open**.
- **`Nothing to do: the input/ folder is empty`** — the files were copied somewhere else; they must sit directly in `input`.
- **The speaker model fails to download** — the token is missing or the model terms were not accepted. Redo setup step 2, then delete the `.env` file and run `install.command` again to enter a fresh token.
- **Everybody is labelled `SPEAKER_00`** — the recording is mono-ish or the voices overlap heavily; try setting `SPEAKERS` to the real number of participants.
- **Out of disk space** — the `models` folder and `~/.cache/huggingface` hold several GB; deleting them only means they get downloaded again.

## Files in this folder

| Name               | Purpose                                                     |
|--------------------|-------------------------------------------------------------|
| `install.command`  | one-time setup, double-clickable                             |
| `run.command`      | transcribe everything in `input`, double-clickable           |
| `install.sh`, `run.sh` | the same two scripts for Terminal use                   |
| `requirements.txt` | exact versions of the Python packages (a known-working set)  |
| `.env`             | your Hugging Face token — private, never share or commit it  |
| `input/`, `output/`, `models/` | your recordings, the transcripts, the downloaded models |

Under the hood: [whispermlx](https://pypi.org/project/whispermlx/) (Whisper on Apple's MLX) for speech
recognition and [pyannote](https://huggingface.co/pyannote/speaker-diarization-community-1) for telling
speakers apart.
