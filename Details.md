# Scrunch Intelligence preview

This preview runs on your Mac. It is an early test build, not a signed installer.

1. Unzip the folder into a location you can write to, such as Documents.
2. Python 3.12 is needed. If your Mac does not have it, setup downloads a private copy into this folder automatically (about 25 MB, checksum-verified; nothing is installed system-wide). On an Intel Mac, install Python 3.12 from https://www.python.org/downloads/macos/ first.
3. Double-click **1 Set up Scrunch (once)** once. It installs, into this folder only: the pinned analysis libraries (and, if needed, Python), the Audio-to-MIDI and Hum-to-MIDI runtime (about 1 GB), the beat tracker (80 MB) and the MuScriptor large model (5.5 GB, from Hugging Face; its makers require a free account, agreeing to their CC BY-NC 4.0 non-commercial licence and a Read token, which setup asks for once and does not keep). It also installs the Scrunch Bridge and Live Ears plug-in for Ableton Live. Run it again any time to finish a skipped or interrupted step.
4. Click **Start Scrunch** (in your Dock after setup, or in this folder). It starts Scrunch, the humming page and the Scrunch HUD over Live, and opens Scrunch in your browser; click it again any time to bring the page back. The first time it opens Settings, where you add your engineer and voice provider. Scrunch keeps running quietly until you log out or uninstall.

**Scrunch HUD.** The translucent panel that sits over Ableton Live (it appears when Live is in front). macOS asks once for Accessibility (so it can follow the Live window) and Microphone (hold the space bar in Live to talk to the engineer). Its Mixing view and Dashboard use this installed Scrunch.

**Audio-to-MIDI.** In the engineer chat, ask it to turn an audio clip into MIDI (for example "make MIDI from the lead on track 3"). The clip must be warped. The MIDI goes on a new track directly below, on Grand Piano at -12 dB; the original audio is not changed. It takes about 20-40 seconds.

**Hum-to-MIDI.** Use **Hum a melody** on the Mixing page: record with your Mac's microphone, play the result back, download the MIDI, or send it to Scrunch to place in Live.

Choose a bounce, select master/pre-master where appropriate, and analyse it. Explore timestamps, frequency balance and stereo/mono findings. Analyse a second bounce to compare it with the first. Section times are elapsed seconds in the bounce; they are not automatically aligned to an Ableton Set. Measurements describe the signal, not whether the music is good.

Choose a point on the timeline or frequency map, then use **Hear this moment** for up to eight seconds of local playback. Stop audio ends it immediately. Listening uses the original file level; it is not a loudness-matched A/B. When opening an existing report, choose its original bounce file: Scrunch checks its SHA-256 hash and enables listening only for an exact match, without reanalysing or calling a provider. At most two chosen listening files are referenced in browser memory. Clear measurements or closing the page releases them. No copied audio file is created.

Audio is decoded and measured in memory. Up to ten reports remain in memory until you clear them or quit. Your original audio files are never removed. Functional settings and explicitly saved conversations live in `~/Library/Application Support/Scrunch/Mixing`; provider secrets use macOS Keychain. This package contains none of John's settings, music or historical trial data.

Bounce analysis and comparison work without Ableton or an AI provider. Optional engineer conversation and cloud voice need your own provider configuration and may incur that provider's charges. Opening the preview does not call a model. The settings link opens Mixing's provider and voice settings. The standalone preview does not bundle the local speech helper; choose cloud transcription explicitly.

Live capture and corrections need the Scrunch Bridge (setup installs it): restart Live, then in Live > Settings > Link, Tempo & MIDI choose ScrunchBridge as a Control Surface (Input/Output None). Live 12.4.5 compatibility, M4 hardware operation, real spoken conversation and musical improvement remain to be tested. Historical or simulated results are not proof on your Mac.

Opening or closing an idle preview does not stop or change Ableton playback. If you request a Live correction, compare it and choose Keep/Revert before quitting; shutdown attempts to revert an undecided trial and reports restoration failure. Never restart an interrupted correction blindly.

If macOS prevents a launcher opening, use its explicit Open option in Finder. Do not disable macOS security globally. A wrong-version existing `.venv` is reported rather than deleted; use a fresh preview folder. Setup and the preview need only local folder access; audio is not uploaded by the local analyzer.

**Removing it:** double-click **Uninstall Scrunch** and type yes. It removes this folder (with the models), the Start Scrunch Dock icon, the Live bridge, the capture plug-in, the HUD and Mixing settings and saved chats, and any AI keys Scrunch stored in Keychain. Your music, bounces and Ableton Sets are never touched.
