#These functions handle directory mapping, adding tracks to the Track class, and a third function that does both.



from Tracks import Tracks
import tkinter as tk
from tkinter import filedialog
import os



#This function reads in directory paths from the user with tkinter.
def wavdir_flacdir():
    root = tk.Tk()

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


#wavdir_tracklist() takes in the wavdir, album name, and artist name to combine in to the Track class.
#wavdir_tracklist() does not need actual wavs, this could easily be use to rename existing flacs.
def wavdir_tracklist(wavdir, track_album, track_artist):
    #We keep track of them in a list!
    track_data = []
    wavTracklist = os.listdir(wavdir)
    track_num = 1
    print("Press Enter to Leave name as is")
    for track in wavTracklist:
        track_name = input(f"Enter the track name for {track}: ")

        if len(str(track_name)) == 0:
            if str(track)[-5:] == ".flac":
                track_name = str(track)[:-5]

        else:
            pass
        #os.path.join is way easier than concat
        track_wav_loc = os.path.join(wavdir, track)
        track = Tracks(track_num, track_name, track_album, track_artist, track_wav_loc )
        track_data.append(track)
        track_num += 1
    return track_data

#Simplifies the whole process
def get_loc_save_tracks():
    wavdir, flacdir = wavdir_flacdir()
    track_album = input("Enter the album name: ")
    track_artist = input("Enter the artist name: ")
    track_data = wavdir_tracklist(wavdir,track_album,track_artist)
    #We return track_data, flacdir for use in wav_to_flac
    return track_data, flacdir


def get_flacs():
    root = tk.Tk()
    while True:
        print("Select Album Directory:")
        album_dir = filedialog.askdirectory()
        print(f"Using files at {album_dir}")
        continue_input = input("Continue with operations? Y/N:  ")

        if continue_input == "Y" or continue_input == "y":
            return album_dir
        else:
            pass

def get_flac_meta():
    flacdir = get_flacs()
    track_album = input("Enter the album name: ")
    track_artist = input("Enter the artist name: ")
    track_data = wavdir_tracklist(flacdir, track_album, track_artist)
    return track_data, flacdir


