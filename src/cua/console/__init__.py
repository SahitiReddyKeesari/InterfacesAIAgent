"""An optional operator console.

The CLI is the documented path and remains so. This exists because two things in this
system are genuinely awkward to drive from a terminal: browsing saved capabilities the
way a calling agent would see them, and taking control of a paused session during an
escalation - which is a task for a person under time pressure, not for someone
remembering flag names.

It deliberately needs no Node, no npm and no build step. React is vendored, the markup
is rendered with htm's tagged templates rather than JSX, and the server is the standard
library. Adding a toolchain to a repo whose only prerequisites are Python and a JDK
would cost the reviewer more than this UI is worth.
"""
