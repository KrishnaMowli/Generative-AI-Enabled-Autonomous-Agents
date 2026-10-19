"""GenAI-enabled task decomposition and dynamic allocation for heterogeneous agents.

The language model only *proposes* a structured task graph. Everything that touches
execution (validation, allocation, scheduling, safety, simulation, replanning) is
deterministic Python.
"""

__version__ = "1.0.0"
