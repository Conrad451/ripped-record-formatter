#Importing the right libs cause its python.


#Mutagen Help:
#https://mutagen.readthedocs.io/en/latest/
#This used to use mutagen, but adding tags is supporetd via pydub.


#os lets us list the files in DIR
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
while DianeLoop not in ("y", "Y", "Yes", "yes"):

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

#This block adds the Track name values to the SongNames Dict, and the track numbers to TrackNums dict while the songs are still in alphabetical order.
#OG Filenams to New File Names
SongNames = {}
#Creating a the list of file names
Album = os.listdir(AlbumDir)
for Unnamed in Album:
    Renamed = input('Enter the name for track ' + Unnamed + ":  ")
    SongNames.update({Unnamed: Renamed})

TrackNums = {}
TrackNumber = 1
for Track in Album:
    TrackNums.update({Track: TrackNumber})
    TrackNumber += 1


#Performing the operations!
for song in Album:
    SongDir = AlbumDir + "\\" + song
    Flac = AudioSegment.from_wav(SongDir)
    cattrack = "[" + str(TrackNums.get(song)) + "]" + " " + str(SongNames.get(song)) 
    Flac.export(FinishedDir + "\\" + cattrack + ".flac", format = "flac", tags={'artist': ArtistName, 'album': AlbumName, 'track': TrackNums.get(song), 'Title': str(SongNames.get(song))})

input('Operations Complete!')


