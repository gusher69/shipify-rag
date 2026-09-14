# -*- coding: utf-8 -*-
"""Graph nodes.

Each node is a pure `AgentState -> partial AgentState` function. Nodes
consume the STRUCTURED fields that `resolve_current_turn` produced; a
node that needs the raw text for genuine ambiguity recovery must record
why in `notes`, so the "parse the turn once" invariant stays auditable.
"""
