from langgraph.graph import END, StateGraph

from app.agent.nodes import (
    AgentState,
    generate_node,
    no_result_node,
    quality_check_node,
    retry_node,
    retrieve_node,
)

graph = StateGraph(AgentState)

graph.add_node("retrieve", retrieve_node)
graph.add_node("retry", retry_node)
graph.add_node("generate", generate_node)
graph.add_node("no_result_handler", no_result_node)

graph.set_entry_point("retrieve")

graph.add_conditional_edges(
    "retrieve",
    quality_check_node,
    {
        "generate": "generate",
        "retry": "retry",
        "no_result": "no_result_handler",
    },
)

graph.add_conditional_edges(
    "retry",
    quality_check_node,
    {
        "generate": "generate",
        "no_result": "no_result_handler",
    },
)

graph.add_edge("generate", END)
graph.add_edge("no_result_handler", END)

rag_graph = graph.compile()
