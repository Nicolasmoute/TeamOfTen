"""Shared limits for protected-file review flows.

These caps are deliberately above the soft target for human-reviewable
truth sections. They are emergency headroom for existing monolithic
files, not an invitation to grow protected truth blobs forever.
"""

COORD_READ_FILE_MAX_CHARS = 512_000
FILE_WRITE_PROPOSAL_MAX_CHARS = 512_000
