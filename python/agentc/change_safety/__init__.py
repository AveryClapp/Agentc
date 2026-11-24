"""Change safety: measure what a change to an agent's model calls does.

A change is a deterministic transform of an eligible request (a prompt
rewrite, a model swap, provider context editing, ...). The runtime sends the
changed request next to the real one as an off-path fork, never returns the
fork's answer to the application, and compares the two responses with cheap
label-free signals. Online detectors turn those signals into a disable
decision with a stated false-alarm rate.
"""
