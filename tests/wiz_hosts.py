"""Fake Wiz endpoints that every test shares.

The hosts are placeholders. They name no tenant and no real region, and they do
not resolve. One endpoint is on each Wiz offering, so the region helper sees both
domains. tests/test_privacy.py allows these two hosts and refuses every other
region-style Wiz host in a tracked file.
"""

# Wiz commercial (wiz.io).
WIZ_IO_ENDPOINT = "https://api.example-region.app.wiz.io/graphql"
# Wiz for Government (wiz.us).
WIZ_US_ENDPOINT = "https://api.example-region.app.wiz.us/graphql"
