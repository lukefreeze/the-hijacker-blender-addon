# booster_runner.py - retired.
#
# The Booster used to run as a separate Python process that wrote its result
# with the standard `wave` module, which cannot write floating-point WAV files
# (the audio came out as loud distorted noise). The gain + limiter now run
# inside Blender in core/booster_dsp.py, so this file is no longer used.
