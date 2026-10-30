"""The EventMate assistant: a grounded, permissioned, traced agent (ADR-023).

The model is untrusted. It can ask for tools; code decides whether the asker may use them. Anything that changes
state is only ever a proposal that a human approves.
"""
