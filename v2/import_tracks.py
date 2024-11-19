from Tracks import Tracks
import tkinter as tk
from tkinter import filedialog
import os



def wavdir_flacdir():
    root = tk.Tk()
    root.withdraw()

    while True:

        print("Select Album Directory")
        wavdir = filedialog.askdirectory()
        print(f"Sourcing files from {wavdir}")

        print("Select Write Directory")
        flacdir = filedialog.askdirectory()
        print(f"Saving files to {flacdir}")

        continue_input = input("Continue with operations? Y/N:  ")

        if continue_input == "Y" or continue_input == "y":
            return wavdir, flacdir
        else:
            pass

def wavdir_tracklist(wavdir, track_album, track_artist):
    track_data = []
    wavTracklist = os.listdir(wavdir)
    track_num = 1
    for track in wavTracklist:
        track_name = input(f"Enter the track name for {track}: ")
        track_wav_loc = os.path.join(wavdir, track)
        track = Tracks(track_num, track_name, track_album, track_artist, track_wav_loc )
        track_data.append(track)
        track_num += 1
    return track_data

def get_loc_save_tracks():
    wavdir, flacdir = wavdir_flacdir()
    track_album = input("Enter the album name: ")
    track_artist = input("Enter the artist name: ")
    track_data = wavdir_tracklist(wavdir,track_album,track_artist)

    return track_data, flacdir


