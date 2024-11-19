#This function takes in track_data, which is a list of tracks in the Track class
#It uses the filepath saved in Track to convert that WAV to a FLAC, add metadata, and save at flacdir

#provide both track_data and flacdir for this to work.

from pydub import AudioSegment


def wav_to_flac(track_data, flacdir):
    for track in track_data:
        flac = AudioSegment.from_wav(track.track_wav_loc)
        flac.export(flacdir + "\\" + track.__str__(), format="flac", tags = {'artist': track.track_artist, 'album': track.track_album })

    return "Operations Complete"


