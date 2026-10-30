"""`vmc-harness mcp` — 브레인 MCP stdio 서버(search_cards·get_card). 에이전트 설정이 띄운다."""


def register(sub):
    p = sub.add_parser("mcp", help="브레인 MCP stdio 서버")
    p.set_defaults(func=run)


def run(args):
    from vmc_harness.brain import mcp_server
    return mcp_server.serve()
