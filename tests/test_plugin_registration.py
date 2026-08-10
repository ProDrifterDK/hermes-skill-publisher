from hermes_skill_publisher import plugin


class Context:
    def __init__(self, middleware=True):
        self.cli = []
        self.hooks = []
        if middleware:
            self.middleware = []
        else:
            self.register_middleware = None

    def register_cli_command(self, **kwargs):
        self.cli.append(kwargs)

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def register_middleware(self, name, callback):
        self.middleware.append((name, callback))


def test_registers_cli_three_hooks_and_middleware():
    context = Context()
    plugin.register(context)
    assert [item["name"] for item in context.cli] == ["skill-publisher"]
    assert [name for name, _ in context.hooks] == ["post_tool_call", "on_session_start", "on_session_end", "pre_llm_call"]
    assert [name for name, _ in context.middleware] == ["tool_execution"]


def test_missing_middleware_keeps_diagnostics_and_hooks():
    context = Context(middleware=False)
    plugin.register(context)
    assert context.cli and len(context.hooks) == 4
    assert not plugin.middleware_available()
