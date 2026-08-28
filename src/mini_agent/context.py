"""Context assembly boundary between agent state and LLM messages."""

from mini_agent.state import AgentState


Message = dict[str, object]


class ContextManager:
    """Prepare the messages sent to the LLM.

    ``AgentState`` is runtime state and remains separate from the message
    history. In v0.11, system instructions, the task, conversation history,
    and tool results are all still carried by ``history``; later versions can
    give each layer dedicated context handling here.
    """

    def __init__(self, state: AgentState, history: list[Message]) -> None:
        """Keep the state and message history supplied by the caller."""
        self.state: AgentState = state
        self.history: list[Message] = history

    def prepare_messages(self) -> list[Message]:
        """Return the complete history unchanged before an LLM call.

        This v0.11 boundary intentionally performs no state rendering,
        token budgeting, trimming, or summarization.
        """
        return self.history
