class Tracks:
    def __init__(self, track_num, track_name, track_album, track_artist):
        self.track_num = track_num
        self.track_name = track_name
        self.track_album = track_album
        self.track_artist = track_artist

    def __str__(self):
        if int(self.track_num) < 10:
            return f"[0{self.track_num}] - {self.track_name}"
        else:
            return f"[{self.track_num}] - {self.track_name}"

    def track_album(self):
        return self.track_album

    def track_artist(self):
        return self.track_artist