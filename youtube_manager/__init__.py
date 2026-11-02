# -------------------------------------------------------
# YouTube Manager package
#
# The web app, the download worker, the organizer and the modules they share. Imports none of
# them: `python -m youtube_manager.worker` would otherwise find the worker already imported and
# run a second copy of it.
# -------------------------------------------------------

import os

# The release the image was built from, read from YTM_VERSION; "dev" in a checkout.
VERSION = os.environ.get('YTM_VERSION', 'dev')
