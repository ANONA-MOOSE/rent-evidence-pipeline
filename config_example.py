# Copy this file to config_local.py and fill in your own flat. config_local.py is git-ignored,
# so your address, rent and evidence never leave your computer. Anything set there overrides the
# defaults at the top of openrent_comps.py.

HOME_LAT, HOME_LNG = 51.4613, -0.0108          # your postcode's centroid: https://api.postcodes.io/postcodes/<postcode>
SUBJECT_POSTCODE = "SE13 7EZ"

# OpenRent area searches; each returns listings within ~3 km of the place named.
SEARCH_TERMS = ["Lewisham, London", "Catford, London"]

SUBJECT = {                       # your flat: used for similarity and adjustments
    "property_type": "Flat",
    "bathrooms": 1,
    "garden": False,
    "parking": False,             # True only if you have your own/allocated space
    "ground_floor": False,
    "building_type": "period_conversion",   # or "modern_block", "older_purpose_built"
}
RENT_HISTORY = [("2025-01", 1800.00)]       # (month the rent started, rent per month)
PROPOSED_RENT_RANGE = None                  # e.g. (1900, 2000) if you expect a proposal
SUBJECT_FLOOR_M2 = None                     # from your EPC (find-energy-certificate.service.gov.uk)
SUBJECT_EPC = "D"

# Deductions for your flat's own unresolved defects, by folder name in evidence/defects/<issue>/.
DEFECT_ADJUSTMENTS = {}                     # e.g. {"Kitchen floor": -4.0}

DASHBOARD_TITLE = "Two-bed rent comparables"

# Public showcase (public_site.py)
PUBLIC_DESCRIPTION = "an example two-bedroom flat"
PUBLIC_SITE_URL = ""
PUBLIC_REPO_URL = ""
PUBLIC_BLOCKLIST = []   # words that must never appear on the public page: your name, house number, postcode...
