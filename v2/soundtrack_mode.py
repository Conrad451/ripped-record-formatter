from import_tracks import wavdir_flacdir
from Tracks import Tracks
import os

# Soundtrack mode functions
def get_loc_save_tracks_soundtrack():
    wavdir, flacdir = wavdir_flacdir()
    track_album = input("Enter the movie name: ")
    track_data = wavdir_tracklist_soundtrack(wavdir, track_album)
    # We return track_data, flacdir for use in wav_to_flac
    return track_data, flacdir


def wavdir_tracklist_soundtrack(wavdir, track_album):
    # We keep track of them in a list!
    track_data = []
    wavTracklist = os.listdir(wavdir)
    track_num = 1
    print("Press Enter to Leave name as is")
    for track in wavTracklist:
        track_name = input(f"Enter the track name for {track}: ")
        track_artist = input(f"Enter the artist name for {track_name}: ")

        if len(str(track_name)) == 0:
            if str(track)[-5:] == ".flac":
                track_name = str(track)[:-5]

        else:
            pass
        # os.path.join is way easier than concat
        track_wav_loc = os.path.join(wavdir, track)
        track = Tracks(track_num, track_name, track_album, track_artist, track_wav_loc)
        track_data.append(track)
        track_num += 1
    return track_data