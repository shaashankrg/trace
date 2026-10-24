"""The investigator: a LangGraph agent that reads an alert, uses the MCP tools, and returns a diagnosis.

It sees only what the tool server returns. It never imports from `eval/` or `injector/`.
"""
