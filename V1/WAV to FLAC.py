#Importing the right libs cause its python.


#Mutagen Help:
#https://mutagen.readthedocs.io/en/latest/
from mutagen.flac import FLAC
import os

#Not linking tkinter help
#Only using this for the open file dialog
import tkinter as tk
from tkinter import filedialog

#Pydub Help:
#https://pydub.com/
from pydub import AudioSegment

root = tk.Tk()
root.withdraw()
welcome_text = r"""
==============================================================================================================
    ____ ____ ____ ____ ____ ___    ____ ____ ____ ____ ____ ___    ____ ____ ____ _    ___  ____ ____ ____ ____ 
    | . \|___\| . \| . \| __\|  \   | . \| __\| __\|   || . \|  \   |  _\|   || . \|\/\ |  \ |_ _\|_ _\| __\| . \
    |  <_| /  | __/| __/|  ]_| . \  |  <_|  ]_| \__| . ||  <_| . \  | _\ | . ||  <_|   \| . \  ||   || |  ]_|  <_
    |/\_/|/   |/   |/   |___/|___/  |/\_/|___/|___/|___/|/\_/|___/  |/   |___/|/\_/|/v\/|/\_/  |/   |/ |___/|/\_/
    
===============================================================================================================   
   
"""


print(welcome_text)
print()
print("Welcome to Ripped Record Formatter. This converts albums of WAV files in to flac files adding relevant metadata along the way.")
print()
input("Press enter to select the WAV files directory and save location directory.")
#bella said name this Diane, just holds the loop var for input paramaters.
DianeLoop = "n"
while DianeLoop != ("y" or "Y"):

    #Gathering file path and Arist name
    print("Select Album Directory")
    AlbumDir = filedialog.askdirectory()


    print("Select Write directory.")
    FinishedDir = filedialog.askdirectory()

    ArtistName = input("Enter Artist Name:")
    AlbumName = input("Enter Album Name:")

    print("Files will be sourced from " + AlbumDir)
    print("The artist name is " + ArtistName)
    print("The Album name is " + AlbumName)
    print("Files will be saved at " + FinishedDir)
    DianeLoop = input("Perform operations? Y/N:    ")

 #Some Shell UI to break up the operations.   
print()
print("==========================================================")
print()
#OG Filenams to New File Names
SongNames = {}
#Creating a the list of file names
Album= os.listdir(AlbumDir)
for Unnamed in Album:
    Renamed = input('Enter the name for track ' + Unnamed)
    SongNames.update({Unnamed: Renamed})

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
    cattrack = "[" + TrackNumber + "]" + " " + Title + ".flac"
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