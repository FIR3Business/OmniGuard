# OmniGuard — Groq-powered safety MVP

A runnable hackathon website with physical check-ins, digital privacy tools, scam analysis, online-bullying support, and wildfire ignition-risk screening.

## Fastest way to run on Windows

### Before starting

Install **Python 3.10 or newer** if Python is not already installed. During installation, enable **Add Python to PATH**.

### Start the website

1. Extract the downloaded ZIP.
2. Open the extracted `OmniGuard_Sidebar_UI_v24` folder.
3. Double-click **`START_OMNIGUARD.bat`**.
4. The first run automatically installs the required Python packages.
5. The website opens at `http://127.0.0.1:8000`.

To stop it, close the terminal window or press **Ctrl+C** inside that window.

## One-command manual start

Open the project folder in File Explorer. Click the address bar, type `cmd`, and press Enter. This opens Command Prompt **already inside the project folder**.

Run:

```bat
py start.py
```

If `py` is not recognized, run:

```bat
python start.py
```

`start.py` installs missing packages, starts the backend, and opens the website.

## What `cd` means

`cd` means **change directory**. It moves a terminal into a folder before commands are run there.

For example:

```bat
cd Downloads\OmniGuard_Sidebar_UI_v24
```

You can avoid `cd` entirely by opening the folder, clicking the File Explorer address bar, typing `cmd`, and pressing Enter.

## Enter the Groq key on the website

The API key no longer needs to be placed in `.env` or source code.

1. Start OmniGuard.
2. Find **Groq Connection** near the top of the website.
3. Paste the key beginning with `gsk_`.
4. Click **Save for this tab**.
5. Click **Test key**.
6. The top-right status should change to **Groq connected**.

The key is stored only in the browser tab's `sessionStorage`. The browser sends it to the local FastAPI backend in an `X-Groq-API-Key` request header when an AI module is used. The backend forwards it to Groq. The key is not written to `app.py`, `safety.db`, or another project file.

Closing the tab clears the key. The emergency-containment button also clears the current browser session.

> This browser-key design is intended for a local hackathon demonstration. A public production website should keep a shared production key only on a secured server and use authentication, rate limits, and secret management.

## App modules

1. **Physical safety:** browser GPS check-in and map display.
2. **Digital safety:** URL heuristics and browser-side screenshot redaction.
3. **Scam safety:** Groq analysis of fraud and manipulation signals.
4. **Mental safety:** Groq-generated grounding and trusted-human support steps.
5. **Wildfire prevention:** Groq vision inspection combined with activity and supplied weather conditions.

Without a key, Groq modules return built-in demo responses so frontend development and presentation practice can continue.

## Team split

### Akilesh — backend

- Groq prompts and schemas
- Account and trusted-contact permissions
- SMS, email, or push integrations
- OAuth containment integrations
- Database and incident audit trail

### Jun — frontend

- Dashboard and module experience
- Camera and GPS flows
- Risk visualization
- ShareSafe editor
- Mobile responsiveness and presentation demo

## Important limitations

- Third-party token revocation and device logout require an integration with each account provider.
- Trusted-contact delivery requires an SMS, email, or push provider.
- URL checks are structural heuristics, not a malware sandbox or reputation service.
- OCR can miss sensitive details; users must review protected images.
- Wildfire screening is not official authorization or a replacement for local restrictions.
- Mental-safety guidance does not diagnose users or replace trusted adults, professionals, or emergency services.


## Safe Routes module

The Safe Routes tab demonstrates preference-based routing with toggles for lighting, isolated areas, busy streets, reported hazards, accessible sidewalks, and night-safety weighting. The current map layers are simulated for the hackathon demo and are clearly labeled as a prototype.

## New scam-protection tools

### Live Call Listener

Open **Scam → Live call listener**, confirm the microphone notice, and click **Start listening**. The browser records short microphone segments, the local backend sends them to Groq `whisper-large-v3-turbo` for transcription, and the rolling transcript is analyzed for urgency, impersonation, credential requests, payment diversion, threats, secrecy, gift cards, cryptocurrency, and remote-access requests.

The browser microphone hears room or speakerphone audio; it cannot directly tap a cellular phone call. Use it only where listening or recording is permitted and provide notice when required.

### Document Scanner

Open **Scam → Document scanner** and upload a PDF, text file, screenshot, or photograph.

- Images use browser-side Tesseract OCR, then only the extracted text is sent for analysis.
- Text PDFs are extracted on the local backend with `pypdf`.
- The result highlights suspicious excerpts, explains each warning, and provides independent verification steps.
- Image-only PDFs should be uploaded as screenshots or photos for OCR.


## v11 startup fix
The Windows launcher now checks for the PDF scanner dependency (`pypdf`) and installs it automatically. It also starts Uvicorn without reload mode for a simpler Windows launch.


## Where to find Dementia Care

The top navigation order ends with **Mental → Wildfire → Dementia Care**. The dementia face-memory feature is no longer placed between the earlier safety modules.

## Lip-to-speech sync monitor

Under **Dementia Care → Patient view**, enable the consent-based lip-sync monitor after starting the camera. When the local face landmark engine sees sustained mouth movement, the browser records a short microphone segment, compares mouth-motion timing with microphone activity, transcribes the segment through Groq, and then discards the raw audio.

The result is only a timing consistency estimate. It does not prove who spoke, read lips, establish truthfulness, or determine intent.

## v19 voice-recognition fix

- One caregiver consent covers the camera and short microphone segments.
- `Start camera + voice` requests both browser permissions in one action.
- The microphone is automatically connected to the lip-motion monitor.
- A live voice state and transcript appear in the Current Visitor card.
- Static assets use versioned URLs and no-cache headers so an older Dementia Care interface is not reused by the browser.

If voice still shows unavailable, click the lock/site-settings icon beside `127.0.0.1:8000`, set Microphone to Allow, reload, and press Start camera + voice again.


## v22 identity-extraction guardrail

OmniGuard now changes a remembered name only when the transcript contains a clear self-introduction such as `I'm June`, `My name is June`, `This is June`, or `Call me June`. Ordinary phrases such as `I am using gold` cannot become a person's name. A later different name also cannot overwrite an already verified face without caregiver review.

On first load, suspicious auto-generated names from older versions are repaired from prior explicit-introduction notes when possible; otherwise they are changed to `Familiar visitor` for caregiver review.

## v23 navigation layout

The former horizontal module bar is now a fixed left sidebar. The seven pages are:

1. Overview
2. Dementia Care
3. Scam (followed by Digital Safety on the same page)
4. Mental Support
5. Safe Travel (Physical check-in followed by Safe Routes)
6. Wildfire Prevention
7. Settings (Groq API key only)

The main content width and position remain unchanged on wide screens; the sidebar occupies the unused left margin. On smaller screens, it collapses to icons or a bottom navigation bar.


## v27 navigation update
- Removed Dementia Care from the visible sidebar.
- Removed Mental Support from the visible sidebar.
- Renamed Scam to Message Analyzer.
- Updated the visible module count from 7 to 5.
