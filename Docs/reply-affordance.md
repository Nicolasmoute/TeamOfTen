---
schema: teamoften-spec/v1
title: 'Reply Affordance Contract'
status: canonical
spec_group: ui-contract
source_index: truth-index.md
last_reorganized: 2026-05-17
---
# Reply Affordance Contract

The chat UI may expose reply affordances on agent messages when the routing target is unambiguous.

## Message Classes

Agent-to-agent messages are internal coordination messages between slots. In the UI these are visually distinct from normal agent-to-user chat messages, including the existing blue reply affordance behavior.

Agent-to-user messages are visible messages from an agent to the human/operator in that agent's pane or in a shared conversation surface. These are visually distinct from agent-to-agent messages, including the existing white message styling.

## Agent-to-Agent Replies

Existing blue agent-to-agent reply behavior must be preserved. A change that adds replies to agent-to-user messages must not remove, reroute, or visually conflate the existing agent-to-agent reply path.

## Agent-to-User Replies

The UI may add a reply affordance to an agent-to-user message when the target agent slot and conversation context are known. White `.event.text` final agent replies in AgentPane may show the same curved-arrow reply control as coordination messages, but they must route through the existing AgentPane `handleReply` / composer path rather than through a new backend endpoint.

A human reply to an agent-to-user message routes as a human-authored message to the same agent slot and conversation context that produced or owns the original message. For AgentPane `.event.text` rows, the quote sender is `event.agent_id || viewerSlot`; when neither is known, the reply affordance is hidden. The reply should preserve enough context for the receiving agent to understand which prior message is being answered.

If the target slot or conversation context is ambiguous, the UI must not silently guess. It should hide the reply affordance or require a deliberate disambiguation before sending.

## Visual Distinction

Agent-to-user reply affordances must remain visually distinct from the existing blue agent-to-agent reply affordance. The UI should make it clear whether a reply is being sent by the human to an agent, or by one agent/context to another.

## Safety

Adding the agent-to-user reply affordance must not auto-send messages, wake unrelated Players, or change task-stage routing. It is a UI routing affordance only.
