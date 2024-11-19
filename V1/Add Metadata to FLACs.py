#Importing the right libs cause its python.
from mutagen.flac import FLAC
import os

import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()


#Gathering file path and Arist name
print("Select Album Directory")
AlbumDir = filedialog.askdirectory()
print("Using Album at " + AlbumDir)
ArtistName = input("Enter Artist Name:")
AlbumName = input("Enter Album Name:")
#Creating a the list of file names
Album= os.listdir(AlbumDir)
#Creating the empty Dict.
#If we don't save the values to a dict, it just loops over itself forever :(
RenamedDict = {}

for song in Album:
    #Anytime you see this Dir variable pop up, we need to concat the filepath, otherwise we could only run the script in each Directory, which is lame.
    SongDir = AlbumDir + "\\" + song
    #Why do I make a copy of the variable? Fuck if I know.
    Title = song
    TrackDetails = FLAC(SongDir)
    TrackNumberList = (TrackDetails['tracknumber'])
    TrackDetails['Artist'] = ArtistName
    TrackDetails['Album'] = AlbumName
    TrackDetails.save()
    #This for loop creates the str typing we need to concat.
    for num in TrackNumberList:
        TrackNumber = num
    #Concat the new name
    cattrack = "[" + TrackNumber + "]" + " " + Title
    #Dict key is OG title, value is new Concat value.
    RenamedDict[Title] = cattrack
#We can now rename the files since we are not actively looping over them
for OG, Renamed in RenamedDict.items():
    OGSongDir = AlbumDir + "\\" + OG
    #We want the files to stay in the same place, for some reason renaming them actually just copies and deletes the originals. Maybe thats just how it works?
    #Either way, RenamedSongDir saves the newly renamed files in the same place we got them from.
    RenamedSongDir = AlbumDir + "\\" + Renamed
    os.rename(OGSongDir, RenamedSongDir)
    
input('Operation Complete. Press Enter to Exit')    

    