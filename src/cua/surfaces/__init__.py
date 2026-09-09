"""The perception/action seam.

Everything above this package speaks only Observation and Action. Nothing above it
knows about Playwright, a DOM, or a screen. That boundary is what lets a recorded
flow extend from a modern web app to a legacy frameset app or a native desktop app
by adding a Surface implementation rather than changing the artifact or the replayer.
"""
