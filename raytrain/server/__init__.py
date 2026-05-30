"""raytrain.server: lightweight FastAPI submission server.

Hosts the HTTP API used to submit and inspect training jobs. Starts as a
minimal app exposing liveness/readiness probes; later phases add auth, a
Ray client, and job endpoints.
"""
