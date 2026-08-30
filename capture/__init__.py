"""M2 capture pipeline: extract -> pii -> evaluate -> embed -> dedup -> write.

Each module in this package is one LangGraph node plus the pure helpers that
node is built from. The helpers are deliberately separable from the node
wrappers so they can be unit-tested without a graph, a database, or a provider.
"""
