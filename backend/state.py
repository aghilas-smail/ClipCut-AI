"""
ClipCut AI — shared in-memory job store
Imported by all API modules and the processor.
"""
# { job_id: { status, progress, message, clips, logs, ... } }
jobs: dict = {}
