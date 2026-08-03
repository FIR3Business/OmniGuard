# Memory Assist integration notes

The uploaded ReMindly source was a React/Vite implementation. OmniGuard is a plain HTML, CSS, JavaScript and FastAPI app, so the recognition system was ported rather than embedded as React.

## Source-to-OmniGuard mapping

| Uploaded source | OmniGuard implementation |
|---|---|
| `src/lib/faceEngine.js` | Engine section at the top of `static/face-memory.js` |
| `loadFaceEngine()` | Local model probe, face-api.js CDN fallback and model timeouts |
| `preprocess()` | Centre-weighted brightness and contrast correction plus 180° camera orientation |
| `detectFaces()` | TinyFaceDetector with adaptive input size and 128-number descriptors |
| `matchWithMargin()` | Preserved in `static/face-memory.js` with the same 0.42 / 0.52 / 0.08 thresholds |
| `FaceMemoryLayer.ingest()` | Persistent tracks, identity voting and presence updates in `static/face-memory.js` |
| `estimatePose()` / `maybeCaptureAngle()` | Progressive left/right/up/down descriptor capture |
| `peopleStore.js` | `aegisone_face_people_v1` localStorage record store |
| `PatientView.jsx` | Memory Assist → Patient view |
| `FamilyFaces.jsx` | Rebuilt as Memory Assist → Family & faces because that file was not present in the uploaded ZIP |

## Important judge demo

Open `static/face-memory.js` and search for `matchWithMargin`. This is the actual identity decision. It finds the closest stored person, compares the runner-up, then returns:

- `confident`: distance below 0.42 and margin at least 0.08
- `probable`: distance below 0.52 and margin at least 0.04
- `unknown`: otherwise

Only a confident match displays and speaks the relationship. A probable match says “might be” and withholds the relationship.

## Privacy changes from the source project

The source records included face snapshots and angle images. OmniGuard intentionally stores only:

- 128-number descriptors
- additional angle descriptors
- consented names, relationships and memory notes
- first/last seen timestamps and meeting counts

No camera photograph is persisted. Descriptors are still biometric data, remain local to the browser, and can be deleted from Family & faces.

## v17: Lip-to-speech sync monitor

The Dementia Care patient view now includes an explicitly consented microphone monitor.

- `mouthOpenness()` uses the 68-point landmark mouth region.
- `updateTrackMouth()` measures short-window mouth-shape variation rather than treating a permanently open mouth as speech.
- `startLipRecording()` begins a temporary microphone segment only after visible mouth motion.
- `calculateLipSync()` compares timestamped mouth-motion samples with local microphone-energy samples across small timing offsets.
- `processLipSpeech()` sends the temporary audio blob to the existing Groq transcription endpoint, displays the transcript, and discards the raw recording.

The score is a timing-consistency indicator only. It is not speaker verification, lip-reading, truth detection, or proof that the recognized identity produced the audio.
