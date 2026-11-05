"""Settlement stations for Kalshi daily temperature markets.

Kalshi settles each market on the NWS Daily Climate Report (CLI) for one
station. The CLI code appears in each series' settlement-source URL
(...product=CLI&issuedby=XXX). We map that code to the station's ICAO id,
coordinates and UTC offset in local *standard* time (the CLI day runs
midnight-to-midnight local standard time, even during daylight time).
"""

# cli_code: (icao, lat, lon, std_utc_offset_hours, display name)
STATIONS = {
    "NYC": ("KNYC", 40.7790, -73.9693, -5, "New York (Central Park)"),
    "MIA": ("KMIA", 25.7881, -80.3169, -5, "Miami (MIA)"),
    "LAX": ("KLAX", 33.9382, -118.3866, -8, "Los Angeles (LAX)"),
    "AUS": ("KAUS", 30.1831, -97.6799, -6, "Austin (Bergstrom)"),
    "BOS": ("KBOS", 42.3606, -71.0097, -5, "Boston (Logan)"),
    "DFW": ("KDFW", 32.8968, -97.0380, -6, "Dallas (DFW)"),
    "OKC": ("KOKC", 35.3889, -97.6006, -6, "Oklahoma City"),
    "MSY": ("KMSY", 29.9934, -90.2580, -6, "New Orleans (MSY)"),
    "LAS": ("KLAS", 36.0719, -115.1634, -8, "Las Vegas"),
    "ATL": ("KATL", 33.6301, -84.4418, -5, "Atlanta"),
    "PHX": ("KPHX", 33.4277, -112.0038, -7, "Phoenix"),
    "SFO": ("KSFO", 37.6197, -122.3647, -8, "San Francisco (SFO)"),
    "DEN": ("KDEN", 39.8466, -104.6562, -7, "Denver (DEN)"),
    "SAT": ("KSAT", 29.5443, -98.4839, -6, "San Antonio"),
    "HOU": ("KHOU", 29.6375, -95.2825, -6, "Houston (Hobby)"),
    "IAH": ("KIAH", 29.9844, -95.3414, -6, "Houston (IAH)"),
    "DCA": ("KDCA", 38.8483, -77.0341, -5, "Washington (National)"),
    "SEA": ("KSEA", 47.4447, -122.3144, -8, "Seattle (Sea-Tac)"),
    "EWR": ("KEWR", 40.6827, -74.1693, -5, "Newark"),
    "MDW": ("KMDW", 41.7860, -87.7524, -6, "Chicago (Midway)"),
    "ORD": ("KORD", 41.9602, -87.9316, -6, "Chicago (O'Hare)"),
    "TTN": ("KTTN", 40.2767, -74.8156, -5, "Trenton"),
    "SAN": ("KSAN", 32.7336, -117.1831, -8, "San Diego"),
    "MSP": ("KMSP", 44.8831, -93.2289, -6, "Minneapolis"),
    "PHL": ("KPHL", 39.8733, -75.2268, -5, "Philadelphia"),
    "DTW": ("KDTW", 42.2124, -83.3534, -5, "Detroit"),
    "CLT": ("KCLT", 35.2140, -80.9431, -5, "Charlotte"),
    "BNA": ("KBNA", 36.1190, -86.6892, -6, "Nashville"),
    "SLC": ("KSLC", 40.7884, -111.9778, -7, "Salt Lake City"),
    "PDX": ("KPDX", 45.5958, -122.6093, -8, "Portland"),
    "TPA": ("KTPA", 27.9618, -82.5404, -5, "Tampa"),
    "MCO": ("KMCO", 28.4180, -81.3241, -5, "Orlando"),
    "JAX": ("KJAX", 30.4941, -81.6879, -5, "Jacksonville"),
}

# Fallback when a series has no parseable settlement URL: match a fragment of
# the series ticker (after KXHIGH / KXLOW and an optional T) to a CLI code.
TICKER_HINTS = {
    "NY": "NYC", "NYC": "NYC", "MIA": "MIA", "LAX": "LAX", "AUS": "AUS",
    "BOS": "BOS", "DAL": "DFW", "OKC": "OKC", "NOLA": "MSY", "LV": "LAS",
    "ATL": "ATL", "PHX": "PHX", "SFO": "SFO", "DEN": "DEN", "SATX": "SAT",
    "HOU": "HOU", "DC": "DCA", "SEA": "SEA", "EWR": "EWR", "CHI": "MDW",
    "TTN": "TTN", "SAN": "SAN", "MIN": "MSP", "MSP": "MSP", "PHIL": "PHL",
    "PHL": "PHL", "DET": "DTW", "CLT": "CLT", "NASH": "BNA", "SLC": "SLC",
    "PDX": "PDX", "TPA": "TPA", "ORL": "MCO", "JAX": "JAX",
}
