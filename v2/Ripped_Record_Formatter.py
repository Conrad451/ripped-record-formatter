from import_tracks import *
from wav_to_flac import *

welcome_text = r"""
==============================================================================================================
    ____ ____ ____ ____ ____ ___    ____ ____ ____ ____ ____ ___    ____ ____ ____ _    ___  ____ ____ ____ ____ 
    | . \|___\| . \| . \| __\|  \   | . \| __\| __\|   || . \|  \   |  _\|   || . \|\/\ |  \ |_ _\|_ _\| __\| . \
    |  <_| /  | __/| __/|  ]_| . \  |  <_|  ]_| \__| . ||  <_| . \  | _\ | . ||  <_|   \| . \  ||   || |  ]_|  <_
    |/\_/|/   |/   |/   |___/|___/  |/\_/|___/|___/|___/|/\_/|___/  |/   |___/|/\_/|/v\/|/\_/  |/   |/ |___/|/\_/

===============================================================================================================   

"""


def conversion():
    print("Converting WAVs to FLACs")
    track_data, flacdir = get_loc_save_tracks()
    print("Processing conversion...")
    message = wav_to_flac(track_data, flacdir)
    print(message)
    return message

def add_meta():
    print("Adding meta data to existing FLACs")
    track_data, flacdir = get_flac_meta()
    print("Processing meta data...")
    message = add_flac_meta(track_data, flacdir)
    print(message)
    return message



def main():
    print(welcome_text)
    print()

    while True:
        print("Choose an Option Below")
        print("-----------------------")
        print("1. Convert WAVs into FLACs, adding Metadata.")
        print("2. Add or change metadata on existing FLACs.")
        print("3. Exit Program.")
        print("-----------------------")
        user_choice = input("Choose a number to make a selection: ")
        if user_choice == "1":
            conversion()
        elif user_choice == "2":
            add_meta()
        elif user_choice == "3":
            print("Ending Program.")
            break

if __name__ == '__main__':
    main()