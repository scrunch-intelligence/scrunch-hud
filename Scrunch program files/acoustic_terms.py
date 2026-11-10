"""The audio measurements a model may name as the expected outcome of a change, so it can be checked."""

MEASURES={
 'brightnessHz':'the energy-weighted centre of the spectrum, in Hz',
 'highShare':'how much of the energy sits above 2 kHz, as a share of the whole',
 'lowEnergyDb':'the level of the sub and bass bands, in dB',
 'sustainDb':'how much of a note is still there just after it starts, against its own peak',
 'attackMs':'how long a note takes to reach its peak',
 'decayMs':'how long a note takes to fall to a third of its peak',
 'widthShare':'how much of the energy is in the stereo sides rather than the middle',
 'loudnessLUFS':'programme loudness (EBU R128)',
}
