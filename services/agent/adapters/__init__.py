# -*- coding: utf-8 -*-
"""Adapters — the ONLY boundary between the agent graph and the rest of
the platform.

Every call the graph makes into an existing service goes through this
package. That is deliberate: it keeps the "do not build a second decision
engine" rule mechanically checkable (a node that reached around the
adapter would have to import a service directly, which the architecture
test forbids) and it gives one place to see exactly which validated
primitives the graph reuses.
"""
