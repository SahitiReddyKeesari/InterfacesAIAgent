"""Deterministic replay: the production execution path. No model, ever.

Given an artifact and typed inputs, execute the recorded plan, verify the checkpoint,
and return a structured result that separates business outcomes from recoverable
conditions from hard failures.
"""
