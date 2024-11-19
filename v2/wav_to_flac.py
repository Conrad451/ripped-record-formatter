from import_tracks import *
from pydub import AudioSegment


def wav_to_flac(track_data, flacdir):
    for track in track_data:
        flac = AudioSegment.from_wav(track.track_wav_loc)
        flac.export(flacdir + "\\" + track.__str__(), format="flac", tags = {'artist': track.artist, 'album': track.album })

track_data, flacdir = get_loc_save_tracks()

wav_to_flac(track_data, flacdir)
