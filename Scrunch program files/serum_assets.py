"""Locally validated, immutable Rack recipes. No vendor preset loading claim."""
from pathlib import Path
ROOT=Path.home()/'Music/Ableton/User Library/Presets/Instruments/Instrument Rack'
ASSETS={
 'current-original':{'name':'Scrunch Serum - Verified Controls','sha256':'a21921515717a4d9b252f4bd9d0e409301f528c89bc86310982510a6523a0d1e','description':'Configured Init-based sound; five verified controls.'}}
ASSETS['diva-original']={'name':'Scrunch Diva - Verified Controls','profile':'diva','sha256':'57b89d2d72ece00d66120b6b46cb2d0182a73a7bb298826651c5319d526312c6','description':'Configured current Diva sound, five verified controls. Listening acceptance pending.'}
ASSETS['jup-original']={'name':'Scrunch JUP - Verified Controls','profile':'jup','sha256':'df3a98f5d7ac9ec9db0898738d2b6addfdac009a87b7e6cc3b9e3c85e2cae85e','description':'Configured Hardcore Supersaw-based test sound; five verified controls. Listening acceptance pending.'}
def asset(recipe):
 if recipe not in ASSETS:raise ValueError('Unknown verified Serum sound.')
 return {**ASSETS[recipe],'path':ROOT/(ASSETS[recipe]['name']+'.adg')}
