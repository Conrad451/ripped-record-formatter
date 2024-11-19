class Tracks:
    def __init__(self, track_num, track_name, track_album, track_artist, track_wav_loc):
        self.track_num = track_num
        self.track_name = track_name
        self.track_album = track_album
        self.track_artist = track_artist
        self.track_wav_loc = track_wav_loc

    def __str__(self):
        if int(self.track_num) < 10:
            return f"[0{self.track_num}] - {self.track_name}.flac"
        else:
            return f"[{self.track_num}] - {self.track_name}.flac"

    def track_album(self):
        return self.track_album

    def track_artist(self):
        return self.track_artist

    def track_wav_name(self):
        return self.track_wav_loc