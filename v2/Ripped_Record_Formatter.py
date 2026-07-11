"""Interactive terminal UI for Ripped Record Formatter.

This is the *UI layer only*. All audio/metadata logic lives in the top-level
``core`` package; this file just gathers input and renders a progress bar. It is
kept working during the GUI transition so existing behaviour is unchanged.
"""

import sys
from pathlib import Path

# Running ``python v2/Ripped_Record_Formatter.py`` puts ``v2/`` on sys.path but
# not the repo root, so make the top-level ``core`` package importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alive_progress import alive_bar

from core import converter
from core.ffmpeg_locator import configure_pydub
from import_tracks import get_flac_meta, get_loc_save_tracks
from soundtrack_mode import get_loc_save_tracks_soundtrack

welcome_text = r"""
==============================================================================================================
    ____ ____ ____ ____ ____ ___    ____ ____ ____ ____ ____ ___    ____ ____ ____ _    ___  ____ ____ ____ ____
    | . \|___\| . \| . \| __\|  \   | . \| __\| __\|   || . \|  \   |  _\|   || . \|\/\ |  \ |_ _\|_ _\| __\| . \
    |  <_| /  | __/| __/|  ]_| . \  |  <_|  ]_| \__| . ||  <_| . \  | _\ | . ||  <_|   \| . \  ||   || |  ]_|  <_
    |/\_/|/   |/   |/   |___/|___/  |/\_/|___/|___/|___/|/\_/|___/  |/   |___/|/\_/|/v\/|/\_/  |/   |/ |___/|/\_/

===============================================================================================================   v3.1

"""


def _ensure_ffmpeg():
    """Resolve ffmpeg once (downloading on first ever run) and point pydub at it."""
    print("Preparing ffmpeg (first run may download it)...")
    configure_pydub()


def _run_with_bar(operation, track_data, flacdir):
    """Drive a core batch operation, ticking an alive_bar after each track."""
    track_data = list(track_data)
    with alive_bar(len(track_data)) as bar:
        def on_progress(current, total, track_name):
            bar()

        return operation(track_data, flacdir, on_progress=on_progress, configure=False)


def _report(result):
    print(result.summary())
    for warning in result.warnings:
        print(f"  ! {warning}")


def conversion():
    print("Converting WAVs to FLACs")
    track_data, flacdir = get_loc_save_tracks()
    _ensure_ffmpeg()
    print("Processing conversion...")
    result = _run_with_bar(converter.convert_wavs_to_flacs, track_data, flacdir)
    _report(result)
    return result


def add_meta():
    print("Adding meta data to existing FLACs")
    track_data, flacdir = get_flac_meta()
    _ensure_ffmpeg()
    print("Processing meta data...")
    result = _run_with_bar(converter.retag_flacs, track_data, flacdir)
    _report(result)
    return result


def soundtrack_mode():
    print("Processing WAVs from a Soundtrack")
    track_data, flacdir = get_loc_save_tracks_soundtrack()
    _ensure_ffmpeg()
    print("Processing conversion...")
    result = _run_with_bar(converter.convert_wavs_to_flacs, track_data, flacdir)
    _report(result)
    return result


def main():
    print(welcome_text)
    print()

    while True:
        print("Choose an Option Below")
        print("-----------------------")
        print("1. Convert WAVs into FLACs, adding Metadata.")
        print("2. Add or change metadata on existing FLACs.")
        print("3. Soundtrack Mode - Specify artist for each track.")
        print("4. Exit Program.")
        print("-----------------------")
        user_choice = input("Choose a number to make a selection: ")
        if user_choice == "1":
            conversion()
        elif user_choice == "2":
            add_meta()
        elif user_choice == "3":
            soundtrack_mode()
        elif user_choice == "4":
            print("Ending Program.")
            break


if __name__ == '__main__':
    main()
